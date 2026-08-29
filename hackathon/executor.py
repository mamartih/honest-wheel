"""Ejecutor paper de opciones: las puertas son la interfaz principal."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Limits:
    max_contracts_underlying: int = 2
    max_contracts_expiry: int = 3
    max_spread_ratio: float = 0.03
    min_open_interest: int = 10
    min_volume: int = 5


class Executor:
    def __init__(self, gateway: Any, *, limits: Limits | None = None,
                 registrar: Callable[[dict], None] | None = None):
        self.gateway = gateway
        self.limits = limits or Limits()
        self.registrar = registrar or (lambda _row: None)

    @staticmethod
    def required_capital(candidate: Mapping[str, Any], *, qty: int = 1) -> float:
        return float(candidate["strike"]) * 100 * qty

    def _reject(self, code: str, motivo: str) -> dict:
        row = {"decision": "rechazada", "reason_code": code, "motivo": motivo}
        self.registrar(row)
        return row

    @staticmethod
    def _contratos(pos: Mapping[str, Any]) -> int:
        """Contratos de una posicion, EN VALOR ABSOLUTO.

        Una put vendida llega de Alpaca con `qty` negativo, y toda la
        estrategia son puts vendidas. Sumando el signo, dos cortas sobre SPY
        daban -2, y `-2 + 1 > 2` es falso: la puerta que impide amontonar
        riesgo sobre un subyacente no saltaba nunca. Sus tests no lo veian
        porque usaban posiciones sin `qty`, y el defecto por defecto vale 1.
        """
        return abs(int(float(pos.get("qty", 1) or 1)))

    def execute(self, candidate: Mapping[str, Any], *, buying_power: float,
                positions: Sequence[Mapping[str, Any]], now: datetime,
                qty: int = 1, dry_run: bool = True) -> dict:
        required = self.required_capital(candidate, qty=qty)
        if buying_power < required:
            return self._reject("CAPITAL", f"exige {required:.2f}; disponible {buying_power:.2f}")

        same_underlying = sum(
            self._contratos(pos) for pos in positions
            if pos.get("underlying") == candidate["underlying"])
        if same_underlying + qty > self.limits.max_contracts_underlying:
            return self._reject("CONCENTRACION_SUBYACENTE", candidate["underlying"])

        same_expiry = sum(
            self._contratos(pos) for pos in positions
            if pos.get("expiry") == candidate["expiry"])
        if same_expiry + qty > self.limits.max_contracts_expiry:
            return self._reject("CONCENTRACION_VENCIMIENTO", candidate["expiry"])

        bid, ask = float(candidate["bid"]), float(candidate["ask"])
        midpoint = (bid + ask) / 2
        spread = (ask - bid) / midpoint if midpoint > 0 else float("inf")
        if spread > self.limits.max_spread_ratio:
            return self._reject("SPREAD", f"{spread:.2%} > {self.limits.max_spread_ratio:.2%}")

        if int(candidate.get("open_interest") or 0) < self.limits.min_open_interest:
            return self._reject("LIQUIDEZ_INTERES_ABIERTO", str(candidate.get("open_interest", 0)))
        if int(candidate.get("volume") or 0) < self.limits.min_volume:
            return self._reject("LIQUIDEZ_VOLUMEN", str(candidate.get("volume", 0)))

        local = now.astimezone(ZoneInfo("America/New_York"))
        if local.weekday() >= 5 or not time(9, 30) <= local.time().replace(tzinfo=None) < time(16):
            return self._reject("HORARIO", local.isoformat())

        if any(pos.get("underlying") == candidate["underlying"]
               and pos.get("expiry") == candidate["expiry"] for pos in positions):
            return self._reject("IDEMPOTENCIA", f"{candidate['underlying']} {candidate['expiry']}")

        order = {
            "symbol": candidate["symbol"], "side": "sell", "qty": qty,
            "limit_price": round(midpoint, 2), "time_in_force": "day",
        }
        try:
            response = self.gateway.submit_option(order, dry_run=dry_run)
        except Exception as exc:  # transporte: nunca registrar posición
            row = {"decision": "error", "reason_code": "API",
                   "motivo": str(exc), "order": order}
            self.registrar(row)
            return row
        # El dry_run era un CANDADO en el codigo: `dry_run=True` fijo. Cumplia
        # el criterio -- que pedia dry-run -- y a cambio el agente no podia
        # mandar una orden jamas, con un concurso que puntua P&L el primero.
        # Sigue siendo el defecto por defecto: enviar de verdad se pide.
        row = {"decision": "dry_run" if dry_run else "enviada",
               "reason_code": None,
               "motivo": ("orden validada; no enviada" if dry_run
                          else "orden enviada a la cuenta paper"),
               "order": order, "response": response}
        self.registrar(row)
        return row
