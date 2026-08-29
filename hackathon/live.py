"""hackathon/live.py -- la raiz de composicion: cablea agent+executor+alpaca_cli.

No es logica nueva. `agent.ciclo` decide y espera cinco inyectables
(posiciones, cadena, puertas, enviar, registrar); `executor.Executor` sabe
pasar las seis puertas pero es una clase con `.execute()`, no una lista de
callables; `alpaca_cli.AlpacaCLI` habla con la cuenta paper del hackathon via
el binario oficial. Este fichero los conecta y nada mas.

De donde sale cada inyectable:
  posiciones  AlpacaCLI.positions() (cuenta paper real), enriquecido con
              underlying/expiry parseados del simbolo OCC cuando el CLI no
              los trae sueltos.
  cadena      NO tiene un selector de contratos por defecto en este
              repositorio publico: la seleccion real (PUT 30-45 DTE,
              delta ~-0.25, spread<3%) es propietaria de TradeHub y no forma
              parte de esta entrega. Quien use este modulo debe pasar su
              propio `cadena` -- un callable sin argumentos que devuelva una
              lista de dicts con symbol/underlying/expiry/strike/bid/ask/
              open_interest/volume. Los tests de este repo son exactamente
              ese caso: cablean un doble.
  puertas     UNA puerta que envuelve Executor.execute(..., dry_run=True)
              (siempre en dry-run para la fase de comprobacion, pase lo que
              pase con el --no-dry-run global) y traduce un rechazo a texto
              con su reason_code -- ese texto es el `motivo` que ciclo deja
              en el registro.
  enviar      Executor.execute(..., dry_run=dry_run) con el dry_run real que
              baja de main()/correr(). Aqui, y solo aqui, puede salir una
              orden de verdad si se pasa --no-dry-run.
  registrar   una linea JSON por ciclo en logs/hackathon_cycles.jsonl.

El registrar INTERNO del Executor (el que ve cada rechazo de puerta) se deja
en no-op: si escribiera al mismo fichero, la puerta (que llama a execute())
y el enviar (que lo vuelve a llamar) escribirian dos o tres lineas por
ciclo, y el criterio pide UNA.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from hackathon.agent import ciclo
from hackathon.alpaca_cli import AlpacaCLI
from hackathon.executor import Executor, Limits

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "hackathon_cycles.jsonl"

_OCC = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})[CP]\d{8}$")


def _underlying_y_expiry(pos: dict) -> tuple[Optional[str], Optional[str]]:
    """Deriva underlying/expiry de una posicion cruda del CLI si no vienen ya.

    El CLI oficial de Alpaca devuelve el simbolo OCC (p.ej.
    'SPY260930P00600000'); ciclo() e Executor comparan por `underlying` y
    `expiry` sueltos. Se parsea solo si la posicion no los trae ya -- por si
    una version futura del CLI los añade.
    """
    if pos.get("underlying") and pos.get("expiry"):
        return pos["underlying"], pos["expiry"]
    simbolo = str(pos.get("symbol", ""))
    m = _OCC.match(simbolo)
    if not m:
        return pos.get("underlying"), pos.get("expiry")
    raiz, aa, mm, dd = m.groups()
    return raiz, f"20{aa}-{mm}-{dd}"


def _puertas_posiciones(cli: AlpacaCLI) -> Callable[[], list[dict]]:
    def posiciones() -> list[dict]:
        crudas = cli.positions()
        resultado = []
        for pos in crudas:
            under, expiry = _underlying_y_expiry(pos)
            resultado.append({**pos, "underlying": under, "expiry": expiry})
        return resultado
    return posiciones


def _construir_puerta(executor: Executor, *, cli: AlpacaCLI,
                       ahora_fn: Callable[[], datetime],
                       cache: dict[str, dict]) -> Callable[[dict], Optional[str]]:
    """Envuelve Executor.execute() como UNA puerta. NO duplica las seis
    puertas: las seis las evalua Executor.execute(), esta funcion solo
    traduce su veredicto al contrato que espera `ciclo` (motivo | None)."""

    def puerta(orden: dict) -> Optional[str]:
        candidato = {**cache.get(orden.get("symbol"), {}), **orden}
        cuenta = cli.account()
        buying_power = float(cuenta.get("buying_power") or cuenta.get("cash") or 0)
        posiciones_actuales = cli.positions()
        resultado = executor.execute(
            candidato, buying_power=buying_power, positions=posiciones_actuales,
            now=ahora_fn(), dry_run=True,
        )
        if resultado.get("decision") == "rechazada":
            return f"{resultado['reason_code']}: {resultado['motivo']}"
        if resultado.get("decision") == "error":
            return f"API: {resultado['motivo']}"
        return None
    return puerta


def _construir_enviar(executor: Executor, *, cli: AlpacaCLI,
                       ahora_fn: Callable[[], datetime],
                       cache: dict[str, dict],
                       dry_run: bool) -> Callable[[dict], dict]:
    def enviar(orden: dict) -> dict:
        candidato = {**cache.get(orden.get("symbol"), {}), **orden}
        cuenta = cli.account()
        buying_power = float(cuenta.get("buying_power") or cuenta.get("cash") or 0)
        posiciones_actuales = cli.positions()
        resultado = executor.execute(
            candidato, buying_power=buying_power, positions=posiciones_actuales,
            now=ahora_fn(), dry_run=dry_run,
        )
        if resultado.get("decision") in ("rechazada", "error"):
            # ciclo() ya llamo a la puerta antes de llegar aqui; si esto
            # rechaza es que el estado cambio entre la puerta y el envio
            # (otra posicion abierta, capital movido). Se trata como fallo
            # de envio -- ciclo() lo registra como "nada", no como "abrir".
            raise RuntimeError(
                f"{resultado.get('reason_code', 'ERROR')}: {resultado['motivo']}")
        # Se devuelve LA ORDEN QUE SALIO, no la ficha contable del Executor.
        # Devolviendo `resultado` a secas, el registro del ciclo guardaba
        # {decision, reason_code, motivo, order, response} y no tenia `symbol`
        # arriba: la fila decia que se abrio algo sin decir QUE. Este mismo
        # defecto -- guardar la respuesta del broker en vez de la orden
        # enviada -- ya se habia visto antes en otro modulo; aqui se cierra
        # de raiz.
        return {**resultado.get("order", {}),
                "respuesta_broker": resultado.get("response"),
                "modo": resultado.get("decision")}
    return enviar


def _registrar_jsonl(log_path: Path) -> Callable[[dict], None]:
    def registrar(fila: dict) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(fila, default=str, ensure_ascii=False) + "\n")
    return registrar


def construir(*, executable: Optional[str] = None, dry_run: bool = True,
              limits: Optional[Limits] = None,
              cli: Optional[AlpacaCLI] = None,
              cadena: Optional[Callable[[], list[dict]]] = None,
              ahora_fn: Optional[Callable[[], datetime]] = None,
              log_path: Optional[Path] = None) -> dict:
    """Construye los cinco inyectables de `agent.ciclo`.

    Cada dependencia real (CLI, cadena) admite un doble via parametro --
    asi el test cablea sin tocar la red ni el subproceso del CLI oficial.
    `cadena` no tiene valor por defecto: ver la nota en el docstring del
    modulo sobre por que la seleccion de contratos queda fuera de este
    repositorio.
    """
    if ahora_fn is None:
        ahora_fn = lambda: datetime.now(timezone.utc)  # noqa: E731

    if cli is None:
        ejecutable = executable or os.environ.get("ALPACA_CLI", "alpaca")
        cli = AlpacaCLI(ejecutable)

    if cadena is None:
        raise NotImplementedError(
            "no hay selector de cadena de contratos por defecto en este "
            "repositorio publico -- pasa tu propio `cadena` (ver los tests "
            "para la forma esperada: symbol/underlying/expiry/strike/bid/"
            "ask/open_interest/volume)"
        )

    cache: dict[str, dict] = {}
    cadena_original = cadena

    def cadena_enriquecida() -> list[dict]:
        # La cache se rellena en cada llamada -- la puerta la usa para
        # enriquecer `orden` con lo que agent.ciclo no propaga (ver
        # _construir_puerta).
        candidatos = cadena_original()
        cache.clear()
        for c in candidatos:
            if c.get("symbol"):
                cache[c["symbol"]] = c
        return candidatos

    executor = Executor(cli, limits=limits or Limits(), registrar=lambda _row: None)

    return {
        "posiciones": _puertas_posiciones(cli),
        "cadena": cadena_enriquecida,
        "puertas": [_construir_puerta(executor, cli=cli, ahora_fn=ahora_fn, cache=cache)],
        "enviar": _construir_enviar(executor, cli=cli, ahora_fn=ahora_fn, cache=cache, dry_run=dry_run),
        "registrar": _registrar_jsonl(log_path or LOG_PATH),
    }


def correr(ahora: Optional[datetime] = None, **kwargs) -> dict:
    """construir() + ciclo(): un ciclo completo, de verdad."""
    ahora = ahora if ahora is not None else datetime.now(timezone.utc)
    kwargs.setdefault("ahora_fn", lambda: ahora)
    inyectables = construir(**kwargs)
    return ciclo(ahora, **inyectables)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Ciclo autonomo del hackathon (paper).")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                        help="no envia ninguna orden real (por defecto)")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false",
                        help="envia de verdad a la cuenta paper del hackathon")
    parser.add_argument("--once", action="store_true",
                        help="corre exactamente un ciclo y termina")
    args = parser.parse_args(argv)

    if not args.once:
        print("modo continuo no implementado -- usa --once", file=sys.stderr)
        return 1

    resultado = correr(dry_run=args.dry_run)
    print(json.dumps(resultado, default=str, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
