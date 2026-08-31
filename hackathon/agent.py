# hackathon/agent.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional


def cycle(
    now: datetime,
    *,
    positions: Callable[[], list[dict]] = lambda: [],
    chain: Callable[[], list[dict]] = lambda: [],
    gates: list[Callable[[dict], Optional[str]]] = None,
    send: Callable[[dict], dict] = lambda o: {"id": "ord-1", **o},
    record: Callable[[dict], None] = lambda f: None,
) -> dict:
    """
    Runs one decision cycle of the agent.

    Returns a dict with keys:
        decision: "abrir" | "rodar" | "nada"
        orden: dict | None
        puerta_que_rechazo: str | None
        motivo: str
    """
    if gates is None:
        gates = []

    # 1. Get the open positions and the option chain
    current_positions = positions()
    contracts = chain()

    # 2. Look for a position to roll (expiry <= 7 days)
    roll_candidate = None
    for pos in current_positions:
        try:
            expiry_date = datetime.fromisoformat(pos["expiry"]).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        days = (expiry_date - now).days
        if 0 <= days <= 7:
            roll_candidate = pos
            break

    # 3. If there is a roll candidate, try to roll it
    if roll_candidate:
        # Look for the first contract in the chain that has NO open position
        new_contract = None
        for contract in contracts:
            # Check whether a position already exists for this underlying+expiry
            occupied = any(
                p.get("underlying") == contract.get("underlying")
                and p.get("expiry") == contract.get("expiry")
                for p in current_positions
            )
            if not occupied:
                new_contract = contract
                break

        if new_contract is None:
            # Whole chain occupied
            occupied_list = ", ".join(
                f"{p.get('underlying')}-{p.get('expiry')}" for p in current_positions
            )
            reason = f"cadena entera ocupada: {occupied_list}"
            row = {"decision": "nada", "motivo": reason}
            record(row)
            return {
                "decision": "nada",
                "orden": None,
                "puerta_que_rechazo": None,
                "motivo": reason,
            }

        # Build the order for the new contract
        order = {
            "symbol": new_contract["symbol"],
            "underlying": new_contract["underlying"],
            "expiry": new_contract["expiry"],
            "qty": -1,
            "side": "sell",
            "type": "limit",
            "limit_price": new_contract["ask"],
        }

        # Run through the gates
        for gate in gates:
            rejection = gate(order)
            if rejection:
                reason = f"rodar rechazado por {rejection}"
                row = {"decision": "nada", "motivo": reason, "puerta": rejection}
                record(row)
                return {
                    "decision": "nada",
                    "orden": None,
                    "puerta_que_rechazo": rejection,
                    "motivo": reason,
                }

        # CLOSING LEG FIRST. Closing a sold put means BUYING it back; if that
        # fails, the new one does not open -- a half-completed roll would leave
        # the old one open AND a new one stacked on top.
        closing_order = {"symbol": roll_candidate.get("symbol"),
                  "underlying": roll_candidate.get("underlying"),
                  "expiry": roll_candidate.get("expiry"),
                  "qty": 1, "side": "buy", "type": "market"}
        try:
            close_response = send(closing_order)
        except Exception as e:
            reason = f"fallo al cerrar la pata vieja, no se abre la nueva: {e}"
            row = {"decision": "nada", "motivo": reason}
            record(row)
            return {"decision": "nada", "orden": None,
                    "puerta_que_rechazo": None, "motivo": reason}

        # Send the order
        try:
            response = send(order)
        except Exception as e:
            reason = f"fallo de envío al rodar: {e}"
            row = {"decision": "nada", "motivo": reason}
            record(row)
            return {
                "decision": "nada",
                "orden": None,
                "puerta_que_rechazo": None,
                "motivo": reason,
            }

        # Record the roll row
        reason = f"rodar: cierra {roll_candidate.get('expiry')} abre {new_contract['symbol']}"
        row = {
            "decision": "rodar",
            "motivo": reason,
            "cierra": roll_candidate.get("expiry"),
            "abre": new_contract["symbol"],
        }
        record(row)

        return {
            "decision": "rodar",
            "orden": response,
            "puerta_que_rechazo": None,
            "motivo": reason,
        }

    # 4. No position to roll -> try to open a new one
    # Look for the first contract in the chain without an open position
    new_contract = None
    for contract in contracts:
        occupied = any(
            p.get("underlying") == contract.get("underlying")
            and p.get("expiry") == contract.get("expiry")
            for p in current_positions
        )
        if not occupied:
            new_contract = contract
            break

    if new_contract is None:
        # Whole chain occupied
        # "occupied" and "empty" are different things, and stating the wrong one
        # lies in the record. With no open positions, a chain with no
        # candidates is not occupied: it means no underlying passed the
        # signal filter.
        occupied_list = ", ".join(
            f"{p.get('underlying')}-{p.get('expiry')}" for p in current_positions
        )
        reason = (f"cadena entera ocupada: {occupied_list}" if occupied_list
                  else "sin candidatos: ningun subyacente paso el filtro")
        row = {"decision": "nada", "motivo": reason}
        record(row)
        return {
            "decision": "nada",
            "orden": None,
            "puerta_que_rechazo": None,
            "motivo": reason,
        }

    # Build the order
    order = {
        "symbol": new_contract["symbol"],
        "underlying": new_contract["underlying"],
        "expiry": new_contract["expiry"],
        "qty": -1,
        "side": "sell",
        "type": "limit",
        "limit_price": new_contract["ask"],
    }

    # Run through the gates
    for gate in gates:
        rejection = gate(order)
        if rejection:
            reason = f"abrir rechazado por {rejection}"
            row = {"decision": "nada", "motivo": reason, "puerta": rejection}
            record(row)
            return {
                "decision": "nada",
                "orden": None,
                "puerta_que_rechazo": rejection,
                "motivo": reason,
            }

    # Send the order
    try:
        response = send(order)
    except Exception as e:
        reason = f"fallo de envío al abrir: {e}"
        row = {"decision": "nada", "motivo": reason}
        record(row)
        return {
            "decision": "nada",
            "orden": None,
            "puerta_que_rechazo": None,
            "motivo": reason,
        }

    # Record the open row
    reason = f"abre {new_contract['symbol']}"
    row = {"decision": "abrir", "motivo": reason, "symbol": new_contract["symbol"]}
    record(row)

    return {
        "decision": "abrir",
        "orden": response,
        "puerta_que_rechazo": None,
        "motivo": reason,
    }
