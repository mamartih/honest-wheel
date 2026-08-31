"""Tests for hackathon/advisor.py -- the LLM veto gate. Written BEFORE the
module exists, with doubles, no network.

Design under test (see hackathon/advisor.py docstring for the full contract):
  - the AI can only VETO, never authorize -- the deterministic code already
    decided to open before the advisor is ever consulted.
  - it FAILS OPEN: no API key, a broken `ask`, or an unparsable answer all
    mean the trade proceeds, with the reason recorded.
  - as a gate in hackathon/live.py it must be the LAST one: a cheap,
    deterministic rejection must never spend a network call.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hackathon.advisor import review

CANDIDATE = {
    "symbol": "SPY260930P00600000", "underlying": "SPY", "expiry": "2026-09-30",
    "strike": 600.0, "delta": -0.25, "bid": 5.90, "ask": 6.00,
    "open_interest": 4200, "volume": 310,
}


# --- (a) no API key -> fails open, does not even try to call `ask` ----------

def test_without_api_key_it_fails_open_and_does_not_consult(monkeypatch):
    monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
    calls = []

    def ask(prompt: str) -> str:
        calls.append(prompt)
        return '{"veto": true, "reason": "should never be reached"}'

    result = review(CANDIDATE, ask=ask)

    assert result["veto"] is False
    assert result["consulted"] is False
    assert result["model"] is None
    assert "FEATHERLESS_API_KEY" in result["reason"]
    assert calls == [], "sin clave no se deberia ni llamar a ask()"


# --- (b) a clear veto from the model reaches the result ---------------------

def test_a_clear_veto_from_the_model_reaches_the_result(monkeypatch):
    monkeypatch.setenv("FEATHERLESS_API_KEY", "test-key")

    def ask(prompt: str) -> str:
        assert "SPY" in prompt and "600.0" in prompt  # the contract travels
        return '{"veto": true, "reason": "IV spike ahead of an FOMC print"}'

    result = review(CANDIDATE, ask=ask)

    assert result["veto"] is True
    assert result["consulted"] is True
    assert "FOMC" in result["reason"]


# --- (c) `ask` raises -> fails open, consulted=False -------------------------

def test_ask_raising_fails_open(monkeypatch):
    monkeypatch.setenv("FEATHERLESS_API_KEY", "test-key")

    def ask(prompt: str) -> str:
        raise ConnectionError("featherless no responde")

    result = review(CANDIDATE, ask=ask)

    assert result["veto"] is False
    assert result["consulted"] is False
    assert "featherless no responde" in result["reason"] or "ConnectionError" in result["reason"]


# --- (d) unparsable answer -> fails open, and says it could not be parsed ---

def test_unparsable_answer_fails_open(monkeypatch):
    monkeypatch.setenv("FEATHERLESS_API_KEY", "test-key")

    def ask(prompt: str) -> str:
        return "esto no es JSON ni se le parece"

    result = review(CANDIDATE, ask=ask)

    assert result["veto"] is False
    # consulted IS True: the model answered, we just couldn't use it.
    assert result["consulted"] is True
    assert "pars" in result["reason"].lower()


# --- (e) wired as a gate in live.py: a veto ends the cycle in "nada" with
#         the advisor's reason reaching `motivo` ----------------------------

def test_as_a_gate_in_live_a_veto_ends_the_cycle_in_nada_with_its_reason(tmp_path):
    from hackathon.agent import cycle
    from hackathon.executor import Limits
    from hackathon.live import build

    class CLIDouble:
        """The deterministic gate ALWAYS dry-runs against submit_option to
        validate (see hackathon/executor.py) -- that call is expected and
        must succeed. What must never happen is a REAL send (dry_run=False),
        which only the advisor gate, further down the list, could still be
        blocking at this point."""

        def __init__(self):
            self.sent: list[tuple[dict, bool]] = []

        def account(self):
            return {"id": "acc-test", "buying_power": 1_000_000.0,
                    "options_buying_power": 1_000_000.0}

        def positions(self):
            return []

        def submit_option(self, order, *, dry_run=True):
            self.sent.append((order, dry_run))
            return {"id": "ord-test", "dry_run": dry_run, **order}

    cli = CLIDouble()
    now = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)  # market open, Tuesday

    def advisor_ask(prompt: str) -> str:
        return '{"veto": true, "reason": "opcion sospechosamente barata"}'

    import os
    os.environ["FEATHERLESS_API_KEY"] = "test-key"
    try:
        injectables = build(
            cli=cli, chain=lambda: [CANDIDATE], now_fn=lambda: now,
            log_path=tmp_path / "cycles.jsonl", limits=Limits(),
            advisor_ask=advisor_ask,
        )
        result = cycle(now, **injectables)
    finally:
        os.environ.pop("FEATHERLESS_API_KEY", None)

    assert result["decision"] == "nada"
    assert "opcion sospechosamente barata" in result["motivo"]
    assert all(dry_run is True for _order, dry_run in cli.sent), (
        "una orden real llego al broker tras un veto del asesor")


# --- (f) the advisor is the LAST gate: a deterministic rejection means
#         `ask` is never called, not even once -------------------------------

def test_the_advisor_is_the_last_gate_a_cheap_rejection_skips_it(tmp_path):
    from hackathon.agent import cycle
    from hackathon.executor import Limits
    from hackathon.live import build

    expensive_candidate = {**CANDIDATE, "strike": 999_999.0}  # triggers CAPITAL

    class CLIDouble:
        def account(self):
            return {"id": "acc-test", "buying_power": 100.0,
                    "options_buying_power": 100.0}  # too little capital

        def positions(self):
            return []

        def submit_option(self, order, *, dry_run=True):
            raise AssertionError("no deberia enviarse nada tras un rechazo")

    now = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)

    calls = []

    def advisor_ask(prompt: str) -> str:
        calls.append(prompt)
        return '{"veto": true, "reason": "no deberia llegar a llamarse"}'

    import os
    os.environ["FEATHERLESS_API_KEY"] = "test-key"
    try:
        injectables = build(
            cli=CLIDouble(), chain=lambda: [expensive_candidate], now_fn=lambda: now,
            log_path=tmp_path / "cycles.jsonl", limits=Limits(),
            advisor_ask=advisor_ask,
        )
        result = cycle(now, **injectables)
    finally:
        os.environ.pop("FEATHERLESS_API_KEY", None)

    assert result["decision"] == "nada"
    assert "CAPITAL" in result["puerta_que_rechazo"]
    assert calls == [], "el asesor se llamo aunque una puerta barata ya habia rechazado"
