from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hackathon.executor import Executor, Limits


NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 8, 28, 11, 0, tzinfo=NY)
CANDIDATE = {
    "symbol": "SPY261002P00500000", "underlying": "SPY",
    "expiry": "2026-10-02", "strike": 500, "bid": 4.90, "ask": 5.00,
    "open_interest": 500, "volume": 100,
}


class Gateway:
    def __init__(self, response=None, error=None):
        self.response = response or {"dry_run": True}
        self.error = error
        self.calls = []

    def submit_option(self, order, *, dry_run=True):
        self.calls.append((order, dry_run))
        if self.error:
            raise self.error
        return self.response


@pytest.mark.parametrize(("change", "reason"), [
    ({"buying_power": 49_999}, "CAPITAL"),
    ({"positions": [{"underlying": "SPY", "expiry": "2026-09-18"}] * 2}, "CONCENTRACION_SUBYACENTE"),
    ({"positions": [{"underlying": "QQQ", "expiry": "2026-10-02"}] * 3}, "CONCENTRACION_VENCIMIENTO"),
    ({"candidate": {**CANDIDATE, "bid": 4.0, "ask": 5.0}}, "SPREAD"),
    ({"candidate": {**CANDIDATE, "open_interest": 9}}, "LIQUIDEZ_INTERES_ABIERTO"),
    ({"candidate": {**CANDIDATE, "volume": 4}}, "LIQUIDEZ_VOLUMEN"),
    ({"now": datetime(2026, 8, 29, 11, 0, tzinfo=NY)}, "HORARIO"),
    ({"positions": [{"underlying": "SPY", "expiry": "2026-10-02"}]}, "IDEMPOTENCIA"),
])
def test_each_gate_rejects_and_records_the_reason(change, reason):
    rows = []
    kwargs = {"candidate": CANDIDATE, "buying_power": 100_000,
              "positions": [], "now": NOW}
    kwargs.update(change)

    result = Executor(Gateway(), record=rows.append).execute(**kwargs)

    assert result["decision"] == "rechazada"
    assert result["reason_code"] == reason
    assert rows[-1]["reason_code"] == reason


def test_all_gates_open_builds_the_correct_order_without_sending_it():
    gateway = Gateway()
    rows = []

    result = Executor(gateway, record=rows.append).execute(
        CANDIDATE, buying_power=100_000, positions=[], now=NOW,
    )

    order, dry_run = gateway.calls[0]
    assert dry_run is True
    assert order == {"symbol": CANDIDATE["symbol"], "side": "sell", "qty": 1,
                     "limit_price": 4.95, "time_in_force": "day"}
    assert result["decision"] == "dry_run"
    assert rows[-1]["decision"] == "dry_run"


def test_csp_capital_is_strike_times_100_times_contracts():
    assert Executor.required_capital(CANDIDATE, qty=2) == 100_000


def test_an_api_failure_does_not_record_a_partial_position():
    rows = []
    gateway = Gateway(error=RuntimeError("API caída"))

    result = Executor(gateway, record=rows.append).execute(
        CANDIDATE, buying_power=100_000, positions=[], now=NOW,
    )

    assert result["decision"] == "error"
    assert "API caída" in result["motivo"]
    assert all(row["decision"] != "abierta" for row in rows)


# --- reviewed at the gate on 28/08 ------------------------------------------
# The concentration cases above use positions WITHOUT `qty`, so
# `pos.get("qty", 1)` is 1 and the sum comes out positive. A SOLD put -- which
# is the whole strategy -- arrives from Alpaca with a NEGATIVE qty, and then
# the sum subtracts and the gate never trips.

SHORTS = [{"underlying": "SPY", "expiry": "2026-10-02", "qty": -1},
          {"underlying": "SPY", "expiry": "2026-10-02", "qty": -1}]


def test_concentration_counts_contracts_not_sign():
    executor = Executor(Gateway())
    row = executor.execute(CANDIDATE, buying_power=10_000_000,
                           positions=SHORTS, now=NOW)
    assert row["decision"] == "rechazada"
    assert row["reason_code"] in ("CONCENTRACION_SUBYACENTE",
                                  "CONCENTRACION_VENCIMIENTO", "IDEMPOTENCIA")


def test_two_shorts_in_another_expiry_cap_out_by_underlying():
    """No idempotency in the way: isolates the concentration gate."""
    shorts = [dict(p, expiry="2026-09-18") for p in SHORTS]
    executor = Executor(Gateway())
    row = executor.execute(CANDIDATE, buying_power=10_000_000,
                           positions=shorts, now=NOW)
    assert row["reason_code"] == "CONCENTRACION_SUBYACENTE"


def test_dry_run_is_a_parameter_not_a_lock():
    """An executor that can NEVER send does not execute. The jury measures P&L."""
    gw = Gateway(response={"id": "ord-1"})
    row = Executor(gw).execute(CANDIDATE, buying_power=10_000_000,
                               positions=[], now=NOW, dry_run=False)
    assert gw.calls[0][1] is False, "el dry_run seguia fijo en el codigo"
    assert row["decision"] == "enviada"


def test_it_still_defaults_to_dry_run():
    gw = Gateway()
    row = Executor(gw).execute(CANDIDATE, buying_power=10_000_000,
                               positions=[], now=NOW)
    assert gw.calls[0][1] is True
    assert row["decision"] == "dry_run"
