"""Tests for hackathon/dashboard.py -- the contest's demo page.

The dashboard COMPUTES NOTHING NEW: it only reads logs/hackathon_cycles.jsonl
(one row per cycle, written by hackathon.agent.cycle / hackathon.live) and
whatever the official CLI wrapper (hackathon.alpaca_cli.AlpacaCLI) returns
from account() and positions(). No network here: the CLI is a double.
"""
from __future__ import annotations

import json

import pytest

from hackathon.dashboard import (
    GATE_NAMES,
    MDE_VS_SPY,
    count_gate_rejections,
    read_cycles,
    render_page,
)


class FakeCLI:
    """Stands in for hackathon.alpaca_cli.AlpacaCLI. No subprocess, no network."""

    def __init__(self, *, account=None, positions=None,
                 account_error=None, positions_error=None):
        self._account = account
        self._positions = positions if positions is not None else []
        self._account_error = account_error
        self._positions_error = positions_error

    def account(self) -> dict:
        if self._account_error:
            raise RuntimeError(self._account_error)
        return self._account

    def positions(self) -> list:
        if self._positions_error:
            raise RuntimeError(self._positions_error)
        return self._positions


GOOD_ACCOUNT = {
    "account_number": "PA3ALIBKZ0U6", "status": "ACTIVE",
    "equity": "100430.50", "last_equity": "100200.00", "cash": "99000",
}


def _write_jsonl(path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- (a) the six gate families, correct counts, five of them present -------

SAMPLE_ROWS = [
    {"decision": "nada", "motivo": "abrir rechazado por HORARIO: x", "puerta": "HORARIO: x"},
    {"decision": "nada", "motivo": "abrir rechazado por HORARIO: x", "puerta": "HORARIO: x"},
    {"decision": "nada", "motivo": "abrir rechazado por HORARIO: x", "puerta": "HORARIO: x"},
    {"decision": "nada", "motivo": "r", "puerta": "LIQUIDEZ_VOLUMEN: 1"},
    {"decision": "nada", "motivo": "r", "puerta": "LIQUIDEZ_INTERES_ABIERTO: 2"},
    {"decision": "nada", "motivo": "r", "puerta": "CAPITAL: exige 60000; disponible 100"},
    {"decision": "nada", "motivo": "r", "puerta_que_rechazo": "CONCENTRACION_SUBYACENTE: SPY"},
    {"decision": "nada", "motivo": "r", "puerta_que_rechazo": "CONCENTRACION_VENCIMIENTO: 2026-09-30"},
    {"decision": "nada", "motivo": "r", "puerta": "SPREAD: 5.00% > 3.00%"},
    # not a gate rejection at all -- must not be counted anywhere
    {"decision": "nada", "motivo": "sin candidatos: ningun subyacente paso el filtro"},
    {"decision": "abrir", "motivo": "abre SPY260930P00600000", "symbol": "SPY260930P00600000",
     "cuando": "2026-08-28T10:00:00+00:00"},
]


def test_gates_counted_from_jsonl_five_present(tmp_path):
    jsonl = tmp_path / "cycles.jsonl"
    _write_jsonl(jsonl, [json.dumps(r) for r in SAMPLE_ROWS])

    rows, skipped = read_cycles(jsonl)
    assert skipped == 0
    counts = count_gate_rejections(rows)

    assert set(GATE_NAMES) == {
        "CAPITAL", "CONCENTRACION", "SPREAD", "LIQUIDEZ", "HORARIO", "IDEMPOTENCIA",
    }
    assert counts["HORARIO"] == 3
    assert counts["LIQUIDEZ"] == 2  # VOLUMEN + INTERES_ABIERTO
    assert counts["CAPITAL"] == 1
    assert counts["CONCENTRACION"] == 2  # SUBYACENTE + VENCIMIENTO
    assert counts["SPREAD"] == 1
    assert counts["IDEMPOTENCIA"] == 0  # never appears in this sample -- still shown

    html = render_page(log_path=jsonl, cli=FakeCLI(account=GOOD_ACCOUNT))
    for name in GATE_NAMES:
        assert name in html
    assert "HORARIO" in html and ">3<" in html or "3" in html
    assert "LIQUIDEZ" in html


# --- (b) "nada" decisions are NOT filtered out of the recent list ----------

def test_nada_decisions_appear_in_recent_list(tmp_path):
    jsonl = tmp_path / "cycles.jsonl"
    _write_jsonl(jsonl, [json.dumps(r) for r in SAMPLE_ROWS])

    html = render_page(log_path=jsonl, cli=FakeCLI(account=GOOD_ACCOUNT))

    assert "sin candidatos: ningun subyacente paso el filtro" in html
    assert "abre SPY260930P00600000" in html


# --- (c) P&L and MDE both appear, together in the headline -----------------

def test_pnl_and_mde_appear_together_in_headline(tmp_path):
    jsonl = tmp_path / "cycles.jsonl"
    _write_jsonl(jsonl, [json.dumps(r) for r in SAMPLE_ROWS])

    html = render_page(log_path=jsonl, cli=FakeCLI(account=GOOD_ACCOUNT))

    start = html.index('id="headline"')
    end = html.index("</section>", start)
    headline = html[start:end]

    assert "P&amp;L" in headline
    assert "MDE" in headline
    assert str(MDE_VS_SPY) in headline


# --- (d) missing / empty jsonl does not crash the page ---------------------

def test_missing_jsonl_does_not_crash(tmp_path):
    jsonl = tmp_path / "does_not_exist.jsonl"

    html = render_page(log_path=jsonl, cli=FakeCLI(account=GOOD_ACCOUNT))

    assert "No cycles recorded yet" in html
    for name in GATE_NAMES:
        assert name in html


def test_empty_jsonl_does_not_crash(tmp_path):
    jsonl = tmp_path / "cycles.jsonl"
    jsonl.write_text("", encoding="utf-8")

    html = render_page(log_path=jsonl, cli=FakeCLI(account=GOOD_ACCOUNT))

    assert "No cycles recorded yet" in html


# --- (e) a corrupted row is skipped, not fatal, and the count is shown -----

def test_corrupted_row_is_skipped_and_counted(tmp_path):
    jsonl = tmp_path / "cycles.jsonl"
    lines = [json.dumps(r) for r in SAMPLE_ROWS]
    lines.insert(3, "{not valid json at all")
    _write_jsonl(jsonl, lines)

    rows, skipped = read_cycles(jsonl)
    assert skipped == 1
    assert len(rows) == len(SAMPLE_ROWS)

    html = render_page(log_path=jsonl, cli=FakeCLI(account=GOOD_ACCOUNT))
    assert "1" in html
    assert "skipped" in html.lower()
    # the good rows around the corrupt one are still counted correctly
    counts = count_gate_rejections(rows)
    assert counts["HORARIO"] == 3


# --- account/positions failures degrade, they don't crash the page --------

def test_account_error_does_not_crash_page(tmp_path):
    jsonl = tmp_path / "cycles.jsonl"
    _write_jsonl(jsonl, [json.dumps(r) for r in SAMPLE_ROWS])

    html = render_page(log_path=jsonl, cli=FakeCLI(account_error="alpaca CLI not found"))

    assert "MDE" in html  # still shown even without account data
    assert "unavailable" in html.lower() or "no disponible" in html.lower()


def test_positions_and_account_number_shown(tmp_path):
    jsonl = tmp_path / "cycles.jsonl"
    _write_jsonl(jsonl, [json.dumps(r) for r in SAMPLE_ROWS])

    positions = [{"symbol": "SPY260930P00600000", "qty": "-1", "side": "short",
                  "market_value": "-590.00", "unrealized_pl": "12.30"}]
    html = render_page(log_path=jsonl, cli=FakeCLI(account=GOOD_ACCOUNT, positions=positions))

    assert "PA3ALIBKZ0U6" in html
    assert "SPY260930P00600000" in html


def test_no_open_positions_message(tmp_path):
    jsonl = tmp_path / "cycles.jsonl"
    _write_jsonl(jsonl, [json.dumps(r) for r in SAMPLE_ROWS])

    html = render_page(log_path=jsonl, cli=FakeCLI(account=GOOD_ACCOUNT, positions=[]))

    assert "no open positions" in html.lower()
