# hackathon/agent.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional


def ciclo(
    ahora: datetime,
    *,
    posiciones: Callable[[], list[dict]] = lambda: [],
    cadena: Callable[[], list[dict]] = lambda: [],
    puertas: list[Callable[[dict], Optional[str]]] = None,
    enviar: Callable[[dict], dict] = lambda o: {"id": "ord-1", **o},
    registrar: Callable[[dict], None] = lambda f: None,
) -> dict:
    """
    Ejecuta un ciclo de decisión del agente.

    Devuelve un dict con claves:
        decision: "abrir" | "rodar" | "nada"
        orden: dict | None
        puerta_que_rechazo: str | None
        motivo: str
    """
    if puertas is None:
        puertas = []

    # 1. Obtener posiciones abiertas y cadena de contratos
    current_positions = posiciones()
    chain = cadena()

    # 2. Buscar posición a rodar (vencimiento <= 7 días)
    roll_candidate = None
    for pos in current_positions:
        try:
            expiry_date = datetime.fromisoformat(pos["expiry"]).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        dias = (expiry_date - ahora).days
        if 0 <= dias <= 7:
            roll_candidate = pos
            break

    # 3. Si hay candidato a rodar, intentamos rodar
    if roll_candidate:
        # Buscar primer contrato en cadena que NO tenga posición abierta
        new_contract = None
        for contrato in chain:
            # Verificar si ya hay posición para este underlying+expiry
            occupied = any(
                p.get("underlying") == contrato.get("underlying")
                and p.get("expiry") == contrato.get("expiry")
                for p in current_positions
            )
            if not occupied:
                new_contract = contrato
                break

        if new_contract is None:
            # Cadena entera ocupada
            ocupados = ", ".join(
                f"{p.get('underlying')}-{p.get('expiry')}" for p in current_positions
            )
            motivo = f"cadena entera ocupada: {ocupados}"
            fila = {"decision": "nada", "motivo": motivo}
            registrar(fila)
            return {
                "decision": "nada",
                "orden": None,
                "puerta_que_rechazo": None,
                "motivo": motivo,
            }

        # Construir orden para el nuevo contrato
        orden = {
            "symbol": new_contract["symbol"],
            "underlying": new_contract["underlying"],
            "expiry": new_contract["expiry"],
            "qty": -1,
            "side": "sell",
            "type": "limit",
            "limit_price": new_contract["ask"],
        }

        # Pasar por puertas
        for puerta in puertas:
            rechazo = puerta(orden)
            if rechazo:
                motivo = f"rodar rechazado por {rechazo}"
                fila = {"decision": "nada", "motivo": motivo, "puerta": rechazo}
                registrar(fila)
                return {
                    "decision": "nada",
                    "orden": None,
                    "puerta_que_rechazo": rechazo,
                    "motivo": motivo,
                }

        # Enviar orden
        try:
            respuesta = enviar(orden)
        except Exception as e:
            motivo = f"fallo de envío al rodar: {e}"
            fila = {"decision": "nada", "motivo": motivo}
            registrar(fila)
            return {
                "decision": "nada",
                "orden": None,
                "puerta_que_rechazo": None,
                "motivo": motivo,
            }

        # Registrar fila de rodar
        motivo = f"rodar: cierra {roll_candidate.get('expiry')} abre {new_contract['symbol']}"
        fila = {
            "decision": "rodar",
            "motivo": motivo,
            "cierra": roll_candidate.get("expiry"),
            "abre": new_contract["symbol"],
        }
        registrar(fila)

        return {
            "decision": "rodar",
            "orden": respuesta,
            "puerta_que_rechazo": None,
            "motivo": motivo,
        }

    # 4. No hay posición a rodar -> intentar abrir nueva
    # Buscar primer contrato en cadena sin posición abierta
    new_contract = None
    for contrato in chain:
        occupied = any(
            p.get("underlying") == contrato.get("underlying")
            and p.get("expiry") == contrato.get("expiry")
            for p in current_positions
        )
        if not occupied:
            new_contract = contrato
            break

    if new_contract is None:
        # Cadena entera ocupada
        ocupados = ", ".join(
            f"{p.get('underlying')}-{p.get('expiry')}" for p in current_positions
        )
        motivo = f"cadena entera ocupada: {ocupados}"
        fila = {"decision": "nada", "motivo": motivo}
        registrar(fila)
        return {
            "decision": "nada",
            "orden": None,
            "puerta_que_rechazo": None,
            "motivo": motivo,
        }

    # Construir orden
    orden = {
        "symbol": new_contract["symbol"],
        "underlying": new_contract["underlying"],
        "expiry": new_contract["expiry"],
        "qty": -1,
        "side": "sell",
        "type": "limit",
        "limit_price": new_contract["ask"],
    }

    # Pasar por puertas
    for puerta in puertas:
        rechazo = puerta(orden)
        if rechazo:
            motivo = f"abrir rechazado por {rechazo}"
            fila = {"decision": "nada", "motivo": motivo, "puerta": rechazo}
            registrar(fila)
            return {
                "decision": "nada",
                "orden": None,
                "puerta_que_rechazo": rechazo,
                "motivo": motivo,
            }

    # Enviar orden
    try:
        respuesta = enviar(orden)
    except Exception as e:
        motivo = f"fallo de envío al abrir: {e}"
        fila = {"decision": "nada", "motivo": motivo}
        registrar(fila)
        return {
            "decision": "nada",
            "orden": None,
            "puerta_que_rechazo": None,
            "motivo": motivo,
        }

    # Registrar fila de abrir
    motivo = f"abre {new_contract['symbol']}"
    fila = {"decision": "abrir", "motivo": motivo, "symbol": new_contract["symbol"]}
    registrar(fila)

    return {
        "decision": "abrir",
        "orden": respuesta,
        "puerta_que_rechazo": None,
        "motivo": motivo,
    }
