"""C231 -- the wire between agent.cycle, executor.Executor and alpaca_cli.

None of this touches the network: everything runs with doubles of the CLI
and the chain. The real-network test lives separately
(hackathon/test_alpaca_cli_network.py) and is not touched here.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hackathon.agent import cycle
from hackathon.executor import Limits
from hackathon.live import build, run_cycle

MARKET_OPEN_NOW = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)  # 11:00 NY, Monday

GOOD_CONTRACT = {
    "symbol": "SPY260930P00600000", "underlying": "SPY", "expiry": "2026-09-30",
    "strike": 600.0, "delta": -0.25, "bid": 5.90, "ask": 6.00,
    "open_interest": 4200, "volume": 310,
}


class CLIDouble:
    """Stands in for AlpacaCLI: returns a good account with plenty of capital,
    and records what gets sent to submit_option to check nothing reaches it
    in dry-run."""

    def __init__(self, *, buying_power=1_000_000.0, positions=None):
        self._buying_power = buying_power
        self._positions = positions if positions is not None else []
        self.sent: list[tuple[dict, bool]] = []

    def account(self) -> dict:
        # `options_buying_power` goes here because Alpaca returns it and the
        # CAPITAL gate reads it. A double that omitted it let the 31/08 bug
        # through a green suite: the code asked for a field the fixture had
        # decided did not exist.
        return {"id": "acc-test", "status": "ACTIVE",
                "buying_power": self._buying_power,
                "options_buying_power": self._buying_power}

    def positions(self) -> list[dict]:
        return list(self._positions)

    def submit_option(self, order: dict, *, dry_run: bool = True) -> dict:
        self.sent.append((order, dry_run))
        return {"id": "ord-test", "dry_run": dry_run, **order}


def _build(tmp_path: Path, **overrides):
    base = dict(
        cli=CLIDouble(),
        chain=lambda: [GOOD_CONTRACT],
        now_fn=lambda: MARKET_OPEN_NOW,
        log_path=tmp_path / "hackathon_cycles.jsonl",
        limits=Limits(),  # default limits: GOOD_CONTRACT passes all of them
    )
    base.update(overrides)
    return build(**base)


# --- (a) the five keys, and cycle() accepts them without a TypeError --------

def test_build_returns_the_five_keys_and_cycle_accepts_them(tmp_path):
    injectables = _build(tmp_path)
    assert set(injectables) == {"positions", "chain", "gates", "send", "record"}

    result = cycle(MARKET_OPEN_NOW, **injectables)  # must not raise TypeError
    assert "decision" in result


# --- (b) positive control: opens, and nothing is sent in dry-run ------------

def test_a_full_cycle_opens_without_sending_anything_in_dry_run(tmp_path):
    cli = CLIDouble()
    injectables = _build(tmp_path, cli=cli)

    result = cycle(MARKET_OPEN_NOW, **injectables)

    assert result["decision"] == "abrir", result["motivo"]
    assert result["orden"] is not None
    assert result["orden"]["symbol"] == GOOD_CONTRACT["symbol"]
    # dry-run by default: the gateway saw the order but ALWAYS with dry_run=True
    assert all(dry_run is True for _order, dry_run in cli.sent)
    assert len(cli.sent) >= 1, "el envio no llego ni siquiera en dry-run"


# --- (c) an Executor gate rejects and its reason_code reaches the reason ----

def test_an_executor_gate_rejects_and_the_reason_code_reaches_the_reason(tmp_path):
    expensive_contract = {**GOOD_CONTRACT, "strike": 999_999.0}  # triggers CAPITAL
    cli = CLIDouble(buying_power=100.0)
    injectables = _build(tmp_path, cli=cli, chain=lambda: [expensive_contract])

    result = cycle(MARKET_OPEN_NOW, **injectables)

    assert result["decision"] == "nada"
    assert "CAPITAL" in result["puerta_que_rechazo"]
    assert "CAPITAL" in result["motivo"]
    assert cli.sent == [], "la puerta rechazo y aun asi se llamo a enviar"


# --- (d) dry_run=False reaches the gateway as dry_run=False -----------------

def test_dry_run_false_reaches_the_gateway_as_dry_run_false(tmp_path):
    cli = CLIDouble()
    injectables = _build(tmp_path, cli=cli, dry_run=False)

    result = cycle(MARKET_OPEN_NOW, **injectables)

    assert result["decision"] == "abrir"
    # the gate ALWAYS tries in dry_run=True; the final send is the one that
    # carries the real dry_run. There has to be at least one False call.
    dry_runs = [dr for _order, dr in cli.sent]
    assert False in dry_runs, f"ninguna llamada llego con dry_run=False: {dry_runs}"


# --- (e) logs/hackathon_cycles.jsonl: ONE line per cycle, even with no trade

def test_one_line_per_cycle_even_when_nothing_happens(tmp_path):
    log_path = tmp_path / "hackathon_cycles.jsonl"

    injectables_open = build(
        cli=CLIDouble(), chain=lambda: [GOOD_CONTRACT],
        now_fn=lambda: MARKET_OPEN_NOW, log_path=log_path)
    cycle(MARKET_OPEN_NOW, **injectables_open)

    injectables_nothing = build(
        cli=CLIDouble(), chain=lambda: [],
        now_fn=lambda: MARKET_OPEN_NOW, log_path=log_path)
    cycle(MARKET_OPEN_NOW, **injectables_nothing)

    lines = log_path.read_text(encoding="utf-8").strip("\n").split("\n")
    assert len(lines) == 2, f"se esperaban 2 lineas (una por ciclo), hay {len(lines)}"
    rows = [json.loads(l) for l in lines]
    assert rows[0]["decision"] == "abrir"
    assert rows[1]["decision"] == "nada"


# --- build()/run_cycle() from outside, with the same double -----------------

def test_run_cycle_does_build_plus_cycle(tmp_path):
    cli = CLIDouble()
    result = run_cycle(
        now=MARKET_OPEN_NOW, cli=cli,
        chain=lambda: [GOOD_CONTRACT],
        log_path=tmp_path / "hackathon_cycles.jsonl")

    assert result["decision"] == "abrir"


def test_market_closed_is_rejected_by_the_HORARIO_gate(tmp_path):
    """Negative control on the wiring: outside market hours, HORARIO has to
    show up in the reason -- confirms the gate really reaches Executor and
    isn't a stub that always lets things through."""
    sunday = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)
    cli = CLIDouble()
    injectables = _build(tmp_path, cli=cli, now_fn=lambda: sunday)

    result = cycle(sunday, **injectables)

    assert result["decision"] == "nada"
    assert "HORARIO" in result["puerta_que_rechazo"]


# --- the signal filter, 29/08 ------------------------------------------------
# Regime out, reversal in, and PER UNDERLYING. A global filter is a
# guaranteed zero the week the market corrects.

def _raw_chain(*symbols):
    return [{"symbol": f"{s}260930P00100000", "underlying": s,
             "expiry": "2026-09-30", "strike": 100.0, "delta": -0.25,
             "bid": 1.00, "ask": 1.02, "open_interest": 900, "volume": 400}
            for s in symbols]


def test_the_filter_leaves_out_only_the_blocked_underlying():
    from hackathon import live

    seen = []

    def signal_filter(underlying):
        seen.append(underlying)
        return "REGIMEN: por debajo de la media" if underlying == "SPY" else None

    cache = {}
    chain = live._default_chain(
        executable="alpaca-binario-inexistente", underlyings=["SPY", "QQQ"],
        cache=cache, signal_filter=signal_filter)
    # With an executable that doesn't exist, `select()` will fail to invoke
    # the subprocess for QQQ (the only one the filter lets through) -- but
    # that failure is swallowed by hackathon.chain.select per underlying, it
    # doesn't blow up here. What is checked is that the filter is consulted
    # for BOTH and does not cut the whole loop short at the first block.
    chain()
    assert seen == ["SPY", "QQQ"], (
        "un bloqueo corto el bucle: los demas subyacentes ni se miraron")


def test_with_no_filter_the_chain_behaves_as_before():
    from hackathon import live

    cache = {}
    chain = live._default_chain(
        executable=None, underlyings=[], cache=cache, signal_filter=None)
    assert chain() == []


# --- the loop, 29/08 ---------------------------------------------------------
# It's going to keep running on its own. The only thing that matters is that
# IT DOESN'T DIE.

def test_the_loop_runs_the_requested_cycles():
    from hackathon import live

    done = []
    live.loop(limit=3, sleep_fn=lambda s: None,
              run_cycle_fn=lambda **kw: done.append(kw) or {"decision": "nada",
                                                             "motivo": "cerrado"},
              output=io.StringIO())
    assert len(done) == 3


def test_a_cycle_that_CRASHES_does_not_bring_down_the_loop():
    """An autonomous loop that dies on the first network failure isn't autonomous."""
    from hackathon import live

    calls = {"n": 0}

    def run_cycle_fn(**kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ConnectionError("alpaca no responde")
        return {"decision": "nada", "motivo": "cerrado"}

    output = io.StringIO()
    live.loop(limit=4, sleep_fn=lambda s: None, run_cycle_fn=run_cycle_fn,
              record=lambda row: None, output=output)

    assert calls["n"] == 4, "el bucle murio en el ciclo que fallo"
    assert "ERROR" in output.getvalue()


def test_only_state_CHANGES_get_notified():
    """96 identical messages a day is not monitoring: it's noise."""
    from hackathon import live

    notifications = []
    states = [("nada", "HORARIO"), ("nada", "HORARIO"), ("nada", "HORARIO"),
              ("abrir", "CSP en SPY"), ("abrir", "CSP en SPY")]
    it = iter(states)

    def run_cycle_fn(**kw):
        d, m = next(it)
        return {"decision": d, "motivo": m}

    live.loop(limit=5, sleep_fn=lambda s: None, run_cycle_fn=run_cycle_fn,
              notify=lambda t, c: notifications.append(c), output=io.StringIO())

    assert len(notifications) == 2, f"aviso repetido sin cambio de estado: {notifications}"


def test_the_loop_respects_the_interval():
    from hackathon import live

    waits = []
    live.loop(limit=3, interval=900, sleep_fn=waits.append,
              run_cycle_fn=lambda **kw: {"decision": "nada", "motivo": "x"},
              output=io.StringIO())
    assert waits == [900, 900], "no durmio entre ciclos, o durmio de mas"


def test_the_loop_does_not_write_to_the_production_record(tmp_path, monkeypatch):
    """My own tests put two fake rows into logs/hackathon_cycles.jsonl because
    the error branch called _jsonl_recorder(LOG_PATH) directly. The record is
    the contest deliverable: it cannot carry test data."""
    from hackathon import live

    monkeypatch.setattr(live, "LOG_PATH", tmp_path / "no_debe_existir.jsonl")
    rows = []

    def blow_up(**kw):
        raise ConnectionError("fallo de prueba")

    live.loop(limit=1, sleep_fn=lambda s: None, run_cycle_fn=blow_up,
              record=rows.append, output=io.StringIO())

    assert rows and rows[0]["decision"] == "error"
    assert not (tmp_path / "no_debe_existir.jsonl").exists()


def test_it_refuses_to_trade_against_an_account_that_is_not_the_declared_one(monkeypatch):
    """Environment variables can be wrong; the account number can't."""
    import pytest
    from hackathon import live

    monkeypatch.setenv("ALPACA_HACKATON_ACCOUNT_NUMBER", "PA3ALIBKZ0U6")

    class OtherCLI:
        def account(self):
            return {"account_number": "PA0000PRODUCCION", "status": "ACTIVE"}

    with pytest.raises(RuntimeError) as err:
        live._require_declared_account(OtherCLI())
    assert "PA0000PRODUCCION" in str(err.value)


def test_with_the_correct_account_it_does_not_get_in_the_way(monkeypatch):
    from hackathon import live

    monkeypatch.setenv("ALPACA_HACKATON_ACCOUNT_NUMBER", "PA3ALIBKZ0U6")

    class GoodCLI:
        def account(self):
            return {"account_number": "PA3ALIBKZ0U6"}

    live._require_declared_account(GoodCLI())


# --- el asesor deja constancia AUNQUE NO VETE, 29/08 -----------------------
# La puerta devolvia None en el fallo abierto y no dejaba rastro: "el asesor
# aprobo" quedaba indistinguible de "el asesor no llego a correr". El write-up
# del concurso afirma que el registro los distingue, asi que tiene que hacerlo.

def _entorno_con_candidato(tmp_path, ask):
    from hackathon import live

    candidato = {"symbol": "SPY260930P00600000", "underlying": "SPY",
                 "expiry": "2026-09-30", "strike": 600.0, "delta": -0.25,
                 "bid": 5.90, "ask": 6.00, "open_interest": 4200, "volume": 310}

    class CLIDoble:
        def account(self):
            return {"account_number": "PA3ALIBKZ0U6", "buying_power": "1000000",
                    "options_buying_power": "1000000"}

        def positions(self):
            return []

        def submit_option(self, order, *, dry_run=True):
            return {"id": "ord-doble", "dry_run": dry_run}

    return live.build(cli=CLIDoble(), chain=lambda: [candidato],
                      advisor_ask=ask, log_path=tmp_path / "ciclos.jsonl",
                      now_fn=lambda: datetime(2026, 9, 1, 15, 0,
                                              tzinfo=timezone.utc)), \
        tmp_path / "ciclos.jsonl"


def test_el_asesor_sin_clave_QUEDA_REGISTRADO_aunque_no_vete(tmp_path, monkeypatch):
    from hackathon.agent import cycle

    monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
    inyectables, log = _entorno_con_candidato(tmp_path, ask=None)
    cycle(datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc), **inyectables)

    fila = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert "advisor" in fila, (
        "el asesor no dejo rastro: no se puede distinguir 'aprobo' de 'no corrio'")
    assert fila["advisor"]["consulted"] is False
    assert fila["advisor"]["veto"] is False


def test_el_veredicto_del_asesor_no_se_arrastra_al_ciclo_siguiente(tmp_path, monkeypatch):
    """Un registro que repite lo ultimo que supo es peor que uno que calla."""
    from hackathon.agent import cycle

    monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
    inyectables, log = _entorno_con_candidato(tmp_path, ask=None)
    ahora = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
    cycle(ahora, **inyectables)

    # segundo ciclo SIN candidato: no se consulta al asesor
    inyectables["chain"] = lambda: []
    cycle(ahora, **inyectables)

    segunda = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert "advisor" not in segunda, (
        "arrastro el veredicto del ciclo anterior a uno que no consulto")


# --- la hora en CADA fila, 29/08 -------------------------------------------
# Solo la rama de error escribia `cuando`. Las filas normales -- que son casi
# todas -- salian sin hora, y el panel las pinta como "time unknown". Un
# registro de decisiones sin hora no es auditable: no se puede decir cuando
# el agente decidio nada, ni si lleva seis horas parado.

def test_toda_fila_del_registro_lleva_la_hora(tmp_path):
    from hackathon import live

    filas = []
    ahora = datetime(2026, 9, 1, 15, 30, tzinfo=timezone.utc)
    grabar = live._jsonl_recorder(tmp_path / "c.jsonl", now_fn=lambda: ahora)
    grabar({"decision": "nada", "motivo": "cerrado"})

    fila = json.loads((tmp_path / "c.jsonl").read_text(encoding="utf-8").strip())
    assert "cuando" in fila, "una fila sin hora no se puede auditar"
    assert fila["cuando"].startswith("2026-09-01T15:30")


def test_la_hora_del_CICLO_manda_sobre_la_del_escritor(tmp_path):
    """Si la fila ya trae su hora -- la rama de error la pone -- no se pisa."""
    from hackathon import live

    grabar = live._jsonl_recorder(
        tmp_path / "c.jsonl",
        now_fn=lambda: datetime(2026, 9, 1, 15, 30, tzinfo=timezone.utc))
    grabar({"decision": "error", "motivo": "x", "cuando": "2026-08-30T09:00:00"})

    fila = json.loads((tmp_path / "c.jsonl").read_text(encoding="utf-8").strip())
    assert fila["cuando"] == "2026-08-30T09:00:00"


class CLIConMargen:
    """Account with stock margin available but almost no options buying power.

    This is the shape Alpaca returns once a cash-secured put is open: the
    collateral is withheld from `options_buying_power` (non-marginable) while
    `buying_power` still shows the marginable figure for equities.
    """

    def __init__(self):
        self.sent: list[tuple[dict, bool]] = []

    def account(self) -> dict:
        return {"id": "acc-test", "status": "ACTIVE",
                "buying_power": "103743.92",
                "options_buying_power": "25935.98",
                "non_marginable_buying_power": "25935.98",
                "cash": "100535.98"}

    def positions(self) -> list[dict]:
        return []

    def submit_option(self, order: dict, *, dry_run: bool = True) -> dict:
        self.sent.append((order, dry_run))
        return {"id": "ord-test", "dry_run": dry_run, **order}


def test_capital_gate_uses_options_buying_power_not_margin():
    """A cash-secured put cannot be margined: the gate must read the
    non-marginable figure.

    REGRESSION, 31/08/2026. The live agent passed its own CAPITAL gate
    (74,194 < 103,743) and Alpaca refused the order with a 403:
    "insufficient options buying power (required: 74194.01, available:
    25935.98)". The gate was correct; it was handed the wrong magnitude.
    """
    from datetime import datetime, timezone

    from hackathon.executor import Executor
    from hackathon.live import _build_gate

    cli = CLIConMargen()
    executor = Executor(cli)
    candidate = {"symbol": "SPY261002P00746000", "underlying": "SPY",
                 "expiry": "2026-10-02", "strike": 746.0,
                 "bid": 5.30, "ask": 5.40, "open_interest": 500, "volume": 50}
    gate = _build_gate(executor, cli=cli, now_fn=lambda: datetime(
        2026, 8, 31, 14, 0, tzinfo=timezone.utc), cache={candidate["symbol"]: candidate})

    motivo = gate({"symbol": candidate["symbol"]})

    assert motivo is not None, (
        "la puerta aprobo una orden que el broker rechaza con 403: esta "
        "midiendo contra el margen de acciones, no contra el efectivo")
    assert "CAPITAL" in motivo, motivo
