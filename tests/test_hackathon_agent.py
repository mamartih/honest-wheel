"""N12 -- the hackathon's autonomous loop. Objective test, written BEFORE.

This file's positive control was verified reachable against a disposable
reference implementation before the task was handed off. On 26/08 the lane
was given an impossible test to pass and spent four rounds figuring that out;
the failure was in the spec, not in the model.
"""
from datetime import datetime, timezone

import pytest

pytest.importorskip("hackathon.agent",
                    reason="N12 aun no entregada: hackathon/agent.py no existe")

from hackathon.agent import cycle  # noqa: E402

NOW = datetime(2026, 9, 1, 15, 30, tzinfo=timezone.utc)

CONTRACT = {"symbol": "SPY260930P00600000", "underlying": "SPY",
            "expiry": "2026-09-30", "strike": 600.0, "delta": -0.25,
            "bid": 5.90, "ask": 6.00, "open_interest": 4200, "volume": 310}

OPEN_POSITION = {"underlying": "SPY", "expiry": "2026-09-30", "qty": -1}


def _env(**overrides):
    """An environment that passes every gate, except what gets overridden."""
    base = dict(positions=lambda: [], chain=lambda: [CONTRACT],
                gates=[], send=lambda order: {"id": "ord-1", **order},
                record=lambda row: None)
    base.update(overrides)
    return base


# --- 4. the positive control, and it goes first because it's the one that was verified ---

def test_positive_control_builds_the_order():
    rows = []
    result = cycle(NOW, **_env(record=rows.append))

    assert result["decision"] == "abrir"
    assert result["orden"] is not None
    assert result["orden"]["symbol"] == CONTRACT["symbol"]
    assert result["puerta_que_rechazo"] is None
    assert result["motivo"]
    assert len(rows) == 1


# --- 1. idempotency --------------------------------------------------------

def test_two_cycles_dont_open_the_same_expiry_twice():
    opened = []

    def send(order):
        opened.append(order)
        return {"id": f"ord-{len(opened)}", **order}

    def positions():
        return [{"underlying": o["underlying"], "expiry": o["expiry"],
                 "qty": -1} for o in opened]

    env = _env(positions=positions, send=send)
    first = cycle(NOW, **env)
    second = cycle(NOW, **env)

    assert first["decision"] == "abrir"
    assert second["decision"] == "nada"
    assert len(opened) == 1, "abrio dos veces el mismo subyacente y vencimiento"


def test_the_already_open_position_is_named_in_the_reason():
    result = cycle(NOW, **_env(positions=lambda: [OPEN_POSITION]))

    assert result["decision"] == "nada"
    assert result["orden"] is None
    assert "SPY" in result["motivo"] or "2026-09-30" in result["motivo"]


# --- 2. inaction gets recorded ---------------------------------------------

@pytest.mark.parametrize("override,case", [
    ({"chain": lambda: []}, "cadena vacia"),
    ({"positions": lambda: [OPEN_POSITION]}, "ya hay posicion"),
    ({"gates": [lambda order: "CAPITAL insuficiente"]}, "puerta rechaza"),
])
def test_a_cycle_with_no_trade_leaves_a_row_with_a_reason(override, case):
    rows = []
    result = cycle(NOW, **_env(record=rows.append, **override))

    assert result["decision"] == "nada", case
    assert len(rows) == 1, f"{case}: un ciclo sin operar no dejo rastro"
    assert rows[0]["motivo"], f"{case}: la fila no dice por que"
    assert rows[0]["decision"] == "nada"


def test_an_empty_chain_does_not_crash():
    result = cycle(NOW, **_env(chain=lambda: []))
    assert result["decision"] == "nada"
    assert result["motivo"]


# --- 3. gates rule and their reason travels ---------------------------------

def test_a_rejecting_gate_blocks_the_send_and_its_reason_arrives():
    sent = []
    gate = lambda order: "SPREAD 3.2% supera el 3%"  # noqa: E731
    rows = []

    result = cycle(NOW, **_env(
        gates=[gate], record=rows.append,
        send=lambda order: sent.append(order)))

    assert sent == [], "mando la orden con una puerta cerrada"
    assert result["decision"] == "nada"
    assert result["puerta_que_rechazo"] == "SPREAD 3.2% supera el 3%"
    assert "SPREAD" in rows[0]["motivo"]


def test_it_stops_at_the_first_gate_and_names_that_one():
    calls = []

    def gate(name):
        def _gate(order):
            calls.append(name)
            return f"{name} rechaza"
        return _gate

    result = cycle(NOW, **_env(
        gates=[gate("CAPITAL"), gate("LIQUIDEZ")]))

    assert calls == ["CAPITAL"], "siguio evaluando puertas tras el rechazo"
    assert result["puerta_que_rechazo"] == "CAPITAL rechaza"


def test_gates_that_pass_dont_get_in_the_way():
    result = cycle(NOW, **_env(
        gates=[lambda order: None, lambda order: None]))

    assert result["decision"] == "abrir"
    assert result["puerta_que_rechazo"] is None


# --- 5. a network failure leaves no half-done state -------------------------

def test_a_send_failure_leaves_no_half_position():
    rows = []

    def send(order):
        raise ConnectionError("alpaca no responde")

    result = cycle(NOW, **_env(send=send, record=rows.append))

    assert result["decision"] == "nada"
    assert result["orden"] is None, "registro una orden que nunca salio"
    assert "alpaca no responde" in result["motivo"]
    assert len(rows) == 1, "el fallo de red no dejo rastro"


def test_a_send_failure_is_not_swallowed_silently():
    """Tell apart 'there was nothing to do' from 'I tried and it failed'."""
    no_failure = cycle(NOW, **_env(chain=lambda: []))

    def send(order):
        raise ConnectionError("alpaca no responde")

    with_failure = cycle(NOW, **_env(send=send))

    assert no_failure["motivo"] != with_failure["motivo"], (
        "un ciclo tranquilo y uno con la API caida cuentan lo mismo")


# --- the signature's contract ------------------------------------------------

def test_cycle_can_be_called_with_just_now():
    """The injectables are for tests; in production the signature is cycle(now)."""
    import inspect

    signature = inspect.signature(cycle)
    required_params = [p for p in signature.parameters.values()
                       if p.default is inspect.Parameter.empty
                       and p.kind is not p.VAR_KEYWORD]
    assert [p.name for p in required_params] == ["now"]


def test_now_is_not_called_from_inside():
    """`now` comes in as a parameter or the loop can't be tested in the past."""
    import hackathon.agent as agent_module

    source = __import__("inspect").getsource(agent_module)
    assert "datetime.now(" not in source and "utcnow(" not in source


# --- N14 and N15, written on 28/08 after reading the delivery ----------------
# N14: the spec declared three decisions -abrir, rodar, nada- and my 14 cases
# only measured two. The agent doesn't know how to roll because nobody asked
# it to.
# N15: idempotency only looks at contracts[0], and if that one is already
# open it returns "nada" without looking at the rest of the chain. It opens
# SPY on day 1 and never trades again all week.

EXPIRES_SOON = {"underlying": "SPY", "expiry": "2026-09-04", "qty": -1,
                "symbol": "SPY260904P00590000"}
OTHER = {"symbol": "QQQ260930P00480000", "underlying": "QQQ",
         "expiry": "2026-09-30", "strike": 480.0, "delta": -0.24,
         "bid": 4.10, "ask": 4.20, "open_interest": 3100, "volume": 260}


def test_a_position_expiring_soon_gets_ROLLED():
    rows = []
    result = cycle(NOW, **_env(
        positions=lambda: [EXPIRES_SOON], record=rows.append))

    assert result["decision"] == "rodar", (
        f"no rodo: {result['decision']} -- {result['motivo']}")
    assert result["orden"] is not None
    assert len(rows) == 1


def test_the_roll_row_names_the_old_and_the_new():
    """A roll that doesn't say WHAT closes and WHAT opens can't be audited."""
    rows = []
    cycle(NOW, **_env(positions=lambda: [EXPIRES_SOON],
                       record=rows.append))
    text = str(rows[0])
    assert EXPIRES_SOON["expiry"] in text, "no dice que vencimiento cierra"
    assert CONTRACT["symbol"] in text, "no dice que contrato abre"


def test_a_far_out_position_is_not_rolled():
    """The negative control: always rolling would be worse than never rolling."""
    far_out = dict(EXPIRES_SOON, expiry="2026-10-30")
    result = cycle(NOW, **_env(positions=lambda: [far_out]))
    assert result["decision"] != "rodar"


def test_rolling_respects_the_gates():
    result = cycle(NOW, **_env(
        positions=lambda: [EXPIRES_SOON],
        gates=[lambda order: "CAPITAL insuficiente"]))
    assert result["decision"] == "nada"
    assert result["puerta_que_rechazo"] == "CAPITAL insuficiente"


def test_if_the_first_is_open_the_next_one_is_tried():
    sent = []
    result = cycle(NOW, **_env(
        chain=lambda: [CONTRACT, OTHER],
        positions=lambda: [OPEN_POSITION],
        send=lambda o: sent.append(o) or {"id": "ord-1", **o}))

    assert result["decision"] == "abrir", (
        f"se bloqueo en el primero: {result['motivo']}")
    assert result["orden"]["symbol"] == OTHER["symbol"]


def test_with_the_whole_chain_open_it_is_nada_with_a_reason():
    all_positions = [OPEN_POSITION, {"underlying": "QQQ", "expiry": "2026-09-30", "qty": -1}]
    result = cycle(NOW, **_env(
        chain=lambda: [CONTRACT, OTHER], positions=lambda: all_positions))
    assert result["decision"] == "nada"
    assert result["motivo"]


def test_the_first_free_one_is_still_preferred():
    """Control that prevents 'fixing it' by always skipping the first one."""
    result = cycle(NOW, **_env(chain=lambda: [CONTRACT, OTHER]))
    assert result["orden"]["symbol"] == CONTRACT["symbol"]


# --- N16: "rodar" has to CLOSE, not just say that it closes -----------------
# My N14 criterion asked for the row to NAME the old and the new, and that is
# satisfied just by writing the text. The delivery built ONE order -the
# opening one-, logged it as "rodar", and left the old position open: the
# agent was piling up risk while reporting rolls. A criterion that measures
# the record and not the effect measures nothing. These cases COUNT CALLS TO
# `send`.

def _send_spy():
    sent = []

    def send(order):
        sent.append(order)
        return {"id": f"ord-{len(sent)}", **order}
    return sent, send


def test_rolling_sends_TWO_orders_one_closing_one_opening():
    sent, send = _send_spy()
    result = cycle(NOW, **_env(
        positions=lambda: [EXPIRES_SOON], send=send))

    assert result["decision"] == "rodar"
    assert len(sent) == 2, (
        f"una rodada son DOS patas y solo salieron {len(sent)}: "
        "la posicion vieja se queda abierta")


def test_the_closing_leg_is_on_the_expiring_contract_and_is_a_BUY():
    """Closing a SOLD put means buying it back. If another sell comes out
    instead, nothing was closed: the bet was doubled."""
    sent, send = _send_spy()
    cycle(NOW, **_env(positions=lambda: [EXPIRES_SOON], send=send))

    closing_order = next((o for o in sent
                          if o.get("symbol") == EXPIRES_SOON["symbol"]), None)
    assert closing_order is not None, "ninguna orden toca el contrato que vence"
    assert str(closing_order.get("side", "")).lower() == "buy", (
        f"la pata de cierre salio como {closing_order.get('side')!r}")


def test_if_the_close_FAILS_the_new_one_is_not_opened():
    """A half-roll is worse than no roll: the old one would stay AND a new one."""
    sent = []

    def send(order):
        sent.append(order)
        if order.get("symbol") == EXPIRES_SOON["symbol"]:
            raise ConnectionError("el cierre no salio")
        return {"id": "ord", **order}

    result = cycle(NOW, **_env(
        positions=lambda: [EXPIRES_SOON], send=send))

    assert result["decision"] == "nada"
    openings = [o for o in sent
                if o.get("symbol") != EXPIRES_SOON["symbol"]]
    assert openings == [], "abrio la nueva con el cierre fallido"
    assert "cierre" in result["motivo"].lower() or "cerrar" in result["motivo"].lower()


def test_a_normal_open_still_sends_only_ONE_order():
    """The control that prevents 'fixing it' by always sending two."""
    sent, send = _send_spy()
    result = cycle(NOW, **_env(send=send))

    assert result["decision"] == "abrir"
    assert len(sent) == 1


def test_with_no_positions_an_empty_chain_does_not_say_OCCUPIED():
    """Seen in the first real loop run: 'cadena entera ocupada: ' with an
    empty list. Occupied and empty are different things, and the record said
    the wrong one. My earlier test only required the reason to be non-empty."""
    result = cycle(NOW, **_env(chain=lambda: [], positions=lambda: []))
    assert result["decision"] == "nada"
    assert "ocupada" not in result["motivo"].lower(), result["motivo"]
