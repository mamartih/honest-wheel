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
def test_cada_puerta_rechaza_y_registra_el_motivo(change, reason):
    rows = []
    kwargs = {"candidate": CANDIDATE, "buying_power": 100_000,
              "positions": [], "now": NOW}
    kwargs.update(change)

    result = Executor(Gateway(), registrar=rows.append).execute(**kwargs)

    assert result["decision"] == "rechazada"
    assert result["reason_code"] == reason
    assert rows[-1]["reason_code"] == reason


def test_todas_abiertas_construye_orden_correcta_sin_enviarla():
    gateway = Gateway()
    rows = []

    result = Executor(gateway, registrar=rows.append).execute(
        CANDIDATE, buying_power=100_000, positions=[], now=NOW,
    )

    order, dry_run = gateway.calls[0]
    assert dry_run is True
    assert order == {"symbol": CANDIDATE["symbol"], "side": "sell", "qty": 1,
                     "limit_price": 4.95, "time_in_force": "day"}
    assert result["decision"] == "dry_run"
    assert rows[-1]["decision"] == "dry_run"


def test_capital_csp_es_strike_por_cien_por_contratos():
    assert Executor.required_capital(CANDIDATE, qty=2) == 100_000


def test_fallo_api_no_registra_una_posicion_parcial():
    rows = []
    gateway = Gateway(error=RuntimeError("API caída"))

    result = Executor(gateway, registrar=rows.append).execute(
        CANDIDATE, buying_power=100_000, positions=[], now=NOW,
    )

    assert result["decision"] == "error"
    assert "API caída" in result["motivo"]
    assert all(row["decision"] != "abierta" for row in rows)


# --- la concentracion se cuenta en contratos, no en el signo de qty --------
#
# Los casos de concentracion de arriba usan posiciones SIN `qty`, asi que
# `pos.get("qty", 1)` vale 1 y la suma sale positiva. Una put VENDIDA -- que es
# la estrategia entera -- llega de Alpaca con qty NEGATIVO, y entonces la suma
# resta y la puerta no salta nunca.

CORTAS = [{"underlying": "SPY", "expiry": "2026-10-02", "qty": -1},
          {"underlying": "SPY", "expiry": "2026-10-02", "qty": -1}]


def test_la_concentracion_cuenta_contratos_no_signo():
    ex = Executor(Gateway())
    fila = ex.execute(CANDIDATE, buying_power=10_000_000,
                      positions=CORTAS, now=NOW)
    assert fila["decision"] == "rechazada"
    assert fila["reason_code"] in ("CONCENTRACION_SUBYACENTE",
                                  "CONCENTRACION_VENCIMIENTO", "IDEMPOTENCIA")


def test_dos_cortas_en_otro_vencimiento_topan_por_subyacente():
    """Sin idempotencia de por medio: aisla la puerta de concentracion."""
    cortas = [dict(p, expiry="2026-09-18") for p in CORTAS]
    ex = Executor(Gateway())
    fila = ex.execute(CANDIDATE, buying_power=10_000_000,
                      positions=cortas, now=NOW)
    assert fila["reason_code"] == "CONCENTRACION_SUBYACENTE"


def test_el_dry_run_es_un_parametro_y_no_un_candado():
    """Un ejecutor que NUNCA puede enviar no ejecuta. El jurado mide P&L."""
    gw = Gateway(response={"id": "ord-1"})
    fila = Executor(gw).execute(CANDIDATE, buying_power=10_000_000,
                                positions=[], now=NOW, dry_run=False)
    assert gw.calls[0][1] is False, "el dry_run seguia fijo en el codigo"
    assert fila["decision"] == "enviada"


def test_por_defecto_sigue_siendo_dry_run():
    gw = Gateway()
    fila = Executor(gw).execute(CANDIDATE, buying_power=10_000_000,
                                positions=[], now=NOW)
    assert gw.calls[0][1] is True
    assert fila["decision"] == "dry_run"
