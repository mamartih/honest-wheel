"""El cable entre agent.ciclo, executor.Executor y alpaca_cli.

Nada de esto sale a la red: todo va con dobles del CLI y de la cadena. El
test de red real vive aparte (hackathon/test_alpaca_cli_network.py) y no se
toca aqui.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hackathon.agent import ciclo
from hackathon.executor import Limits
from hackathon.live import construir, correr

AHORA_MERCADO_ABIERTO = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)  # 11:00 NY, lunes

CONTRATO_BUENO = {
    "symbol": "SPY260930P00600000", "underlying": "SPY", "expiry": "2026-09-30",
    "strike": 600.0, "delta": -0.25, "bid": 5.90, "ask": 6.00,
    "open_interest": 4200, "volume": 310,
}


class CLIDoble:
    """Sustituye a AlpacaCLI: da cuenta buena y capital de sobra, y registra
    lo que se le manda a submit_option para comprobar que no llega nada en
    dry-run."""

    def __init__(self, *, buying_power=1_000_000.0, posiciones=None):
        self._buying_power = buying_power
        self._posiciones = posiciones if posiciones is not None else []
        self.enviados: list[tuple[dict, bool]] = []

    def account(self) -> dict:
        return {"id": "acc-test", "status": "ACTIVE", "buying_power": self._buying_power}

    def positions(self) -> list[dict]:
        return list(self._posiciones)

    def submit_option(self, order: dict, *, dry_run: bool = True) -> dict:
        self.enviados.append((order, dry_run))
        return {"id": "ord-test", "dry_run": dry_run, **order}


def _construir(tmp_path: Path, **cambios):
    base = dict(
        cli=CLIDoble(),
        cadena=lambda: [CONTRATO_BUENO],
        ahora_fn=lambda: AHORA_MERCADO_ABIERTO,
        log_path=tmp_path / "hackathon_cycles.jsonl",
        limits=Limits(),  # limites por defecto: el CONTRATO_BUENO los pasa todos
    )
    base.update(cambios)
    return construir(**base)


# --- (a) las cinco claves, y ciclo() las acepta sin TypeError ---------------

def test_construir_devuelve_las_cinco_claves_y_ciclo_las_acepta(tmp_path):
    inyectables = _construir(tmp_path)
    assert set(inyectables) == {"posiciones", "cadena", "puertas", "enviar", "registrar"}

    resultado = ciclo(AHORA_MERCADO_ABIERTO, **inyectables)  # no debe lanzar TypeError
    assert "decision" in resultado


# --- construir() exige una cadena: no hay selector propietario incluido ----

def test_construir_sin_cadena_da_notimplementederror():
    with pytest.raises(NotImplementedError):
        construir(cli=CLIDoble(), ahora_fn=lambda: AHORA_MERCADO_ABIERTO)


# --- (b) control positivo: abre, y en dry-run no se envia nada --------------

def test_ciclo_completo_abre_sin_enviar_nada_en_dry_run(tmp_path):
    cli = CLIDoble()
    inyectables = _construir(tmp_path, cli=cli)

    resultado = ciclo(AHORA_MERCADO_ABIERTO, **inyectables)

    assert resultado["decision"] == "abrir", resultado["motivo"]
    assert resultado["orden"] is not None
    assert resultado["orden"]["symbol"] == CONTRATO_BUENO["symbol"]
    # dry-run por defecto: el gateway vio la orden pero SIEMPRE con dry_run=True
    assert all(dry_run is True for _orden, dry_run in cli.enviados)
    assert len(cli.enviados) >= 1, "el envio no llego ni siquiera en dry-run"


# --- (c) una puerta del Executor rechaza y su reason_code llega al motivo ---

def test_una_puerta_del_executor_rechaza_y_el_reason_code_llega_al_motivo(tmp_path):
    contrato_caro = {**CONTRATO_BUENO, "strike": 999_999.0}  # dispara CAPITAL
    cli = CLIDoble(buying_power=100.0)
    inyectables = _construir(tmp_path, cli=cli, cadena=lambda: [contrato_caro])

    resultado = ciclo(AHORA_MERCADO_ABIERTO, **inyectables)

    assert resultado["decision"] == "nada"
    assert "CAPITAL" in resultado["puerta_que_rechazo"]
    assert "CAPITAL" in resultado["motivo"]
    assert cli.enviados == [], "la puerta rechazo y aun asi se llamo a enviar"


# --- (d) dry_run=False llega al gateway con dry_run=False -------------------

def test_dry_run_false_llega_al_gateway_con_dry_run_false(tmp_path):
    cli = CLIDoble()
    inyectables = _construir(tmp_path, cli=cli, dry_run=False)

    resultado = ciclo(AHORA_MERCADO_ABIERTO, **inyectables)

    assert resultado["decision"] == "abrir"
    # la puerta SIEMPRE prueba en dry_run=True; el envio final es el que
    # lleva el dry_run real. Tiene que haber al menos una llamada False.
    dry_runs = [dr for _orden, dr in cli.enviados]
    assert False in dry_runs, f"ninguna llamada llego con dry_run=False: {dry_runs}"


# --- (e) logs/hackathon_cycles.jsonl: UNA linea por ciclo, tambien sin operar

def test_una_linea_por_ciclo_tambien_cuando_no_se_hace_nada(tmp_path):
    log_path = tmp_path / "hackathon_cycles.jsonl"

    inyectables_abre = construir(
        cli=CLIDoble(), cadena=lambda: [CONTRATO_BUENO],
        ahora_fn=lambda: AHORA_MERCADO_ABIERTO, log_path=log_path)
    ciclo(AHORA_MERCADO_ABIERTO, **inyectables_abre)

    inyectables_nada = construir(
        cli=CLIDoble(), cadena=lambda: [],
        ahora_fn=lambda: AHORA_MERCADO_ABIERTO, log_path=log_path)
    ciclo(AHORA_MERCADO_ABIERTO, **inyectables_nada)

    lineas = log_path.read_text(encoding="utf-8").strip("\n").split("\n")
    assert len(lineas) == 2, f"se esperaban 2 lineas (una por ciclo), hay {len(lineas)}"
    filas = [json.loads(l) for l in lineas]
    assert filas[0]["decision"] == "abrir"
    assert filas[1]["decision"] == "nada"


# --- construir()/correr() por fuera, con el mismo doble ---------------------

def test_correr_hace_construir_mas_ciclo(tmp_path):
    cli = CLIDoble()
    resultado = correr(
        ahora=AHORA_MERCADO_ABIERTO, cli=cli,
        cadena=lambda: [CONTRATO_BUENO],
        log_path=tmp_path / "hackathon_cycles.jsonl")

    assert resultado["decision"] == "abrir"


def test_horario_cerrado_rechaza_por_la_puerta_horario(tmp_path):
    """Control negativo del wiring: fuera de mercado, HORARIO tiene que
    aparecer en el motivo -- confirma que la puerta llega de verdad hasta
    Executor y no un stub que siempre deja pasar."""
    domingo = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)
    cli = CLIDoble()
    inyectables = _construir(tmp_path, cli=cli, ahora_fn=lambda: domingo)

    resultado = ciclo(domingo, **inyectables)

    assert resultado["decision"] == "nada"
    assert "HORARIO" in resultado["puerta_que_rechazo"]
