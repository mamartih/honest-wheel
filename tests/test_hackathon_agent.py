"""El lazo autonomo del hackathon: agent.ciclo().

Un ciclo decide una de tres cosas -- abrir, rodar o no hacer nada -- y en
las tres deja una fila registrada con su motivo. El control positivo va
primero porque es el que se verifica primero: construye la orden esperada
contra una cadena y unas posiciones conocidas.
"""
from datetime import datetime, timezone

import pytest

pytest.importorskip("hackathon.agent",
                    reason="hackathon/agent.py no existe todavia")

from hackathon.agent import ciclo  # noqa: E402

AHORA = datetime(2026, 9, 1, 15, 30, tzinfo=timezone.utc)

CONTRATO = {"symbol": "SPY260930P00600000", "underlying": "SPY",
            "expiry": "2026-09-30", "strike": 600.0, "delta": -0.25,
            "bid": 5.90, "ask": 6.00, "open_interest": 4200, "volume": 310}

ABIERTA = {"underlying": "SPY", "expiry": "2026-09-30", "qty": -1}


def _entorno(**cambios):
    """Un entorno que pasa todas las puertas, salvo lo que se cambie."""
    base = dict(posiciones=lambda: [], cadena=lambda: [CONTRATO],
                puertas=[], enviar=lambda orden: {"id": "ord-1", **orden},
                registrar=lambda fila: None)
    base.update(cambios)
    return base


# --- 4. el control positivo, y va el primero porque es el que se verifica ---

def test_control_positivo_construye_la_orden():
    filas = []
    resultado = ciclo(AHORA, **_entorno(registrar=filas.append))

    assert resultado["decision"] == "abrir"
    assert resultado["orden"] is not None
    assert resultado["orden"]["symbol"] == CONTRATO["symbol"]
    assert resultado["puerta_que_rechazo"] is None
    assert resultado["motivo"]
    assert len(filas) == 1


# --- 1. idempotencia --------------------------------------------------------

def test_dos_ciclos_no_abren_dos_veces_el_mismo_vencimiento():
    abiertas = []

    def enviar(orden):
        abiertas.append(orden)
        return {"id": f"ord-{len(abiertas)}", **orden}

    def posiciones():
        return [{"underlying": o["underlying"], "expiry": o["expiry"],
                 "qty": -1} for o in abiertas]

    entorno = _entorno(posiciones=posiciones, enviar=enviar)
    primero = ciclo(AHORA, **entorno)
    segundo = ciclo(AHORA, **entorno)

    assert primero["decision"] == "abrir"
    assert segundo["decision"] == "nada"
    assert len(abiertas) == 1, "abrio dos veces el mismo subyacente y vencimiento"


def test_la_posicion_ya_abierta_se_dice_en_el_motivo():
    resultado = ciclo(AHORA, **_entorno(posiciones=lambda: [ABIERTA]))

    assert resultado["decision"] == "nada"
    assert resultado["orden"] is None
    assert "SPY" in resultado["motivo"] or "2026-09-30" in resultado["motivo"]


# --- 2. la inaccion se registra ---------------------------------------------

@pytest.mark.parametrize("cambio,caso", [
    ({"cadena": lambda: []}, "cadena vacia"),
    ({"posiciones": lambda: [ABIERTA]}, "ya hay posicion"),
    ({"puertas": [lambda orden: "CAPITAL insuficiente"]}, "puerta rechaza"),
])
def test_un_ciclo_sin_operacion_deja_fila_con_motivo(cambio, caso):
    filas = []
    resultado = ciclo(AHORA, **_entorno(registrar=filas.append, **cambio))

    assert resultado["decision"] == "nada", caso
    assert len(filas) == 1, f"{caso}: un ciclo sin operar no dejo rastro"
    assert filas[0]["motivo"], f"{caso}: la fila no dice por que"
    assert filas[0]["decision"] == "nada"


def test_la_cadena_vacia_no_revienta():
    resultado = ciclo(AHORA, **_entorno(cadena=lambda: []))
    assert resultado["decision"] == "nada"
    assert resultado["motivo"]


# --- 3. las puertas mandan y su motivo viaja --------------------------------

def test_una_puerta_que_rechaza_impide_el_envio_y_su_motivo_llega():
    enviados = []
    puerta = lambda orden: "SPREAD 3.2% supera el 3%"  # noqa: E731
    filas = []

    resultado = ciclo(AHORA, **_entorno(
        puertas=[puerta], registrar=filas.append,
        enviar=lambda orden: enviados.append(orden)))

    assert enviados == [], "mando la orden con una puerta cerrada"
    assert resultado["decision"] == "nada"
    assert resultado["puerta_que_rechazo"] == "SPREAD 3.2% supera el 3%"
    assert "SPREAD" in filas[0]["motivo"]


def test_se_para_en_la_primera_puerta_y_nombra_esa():
    llamadas = []

    def puerta(nombre):
        def _puerta(orden):
            llamadas.append(nombre)
            return f"{nombre} rechaza"
        return _puerta

    resultado = ciclo(AHORA, **_entorno(
        puertas=[puerta("CAPITAL"), puerta("LIQUIDEZ")]))

    assert llamadas == ["CAPITAL"], "siguio evaluando puertas tras el rechazo"
    assert resultado["puerta_que_rechazo"] == "CAPITAL rechaza"


def test_las_puertas_que_dejan_pasar_no_estorban():
    resultado = ciclo(AHORA, **_entorno(
        puertas=[lambda orden: None, lambda orden: None]))

    assert resultado["decision"] == "abrir"
    assert resultado["puerta_que_rechazo"] is None


# --- 5. un fallo de red no deja estado a medias -----------------------------

def test_un_fallo_de_envio_no_deja_media_posicion():
    filas = []

    def enviar(orden):
        raise ConnectionError("alpaca no responde")

    resultado = ciclo(AHORA, **_entorno(enviar=enviar, registrar=filas.append))

    assert resultado["decision"] == "nada"
    assert resultado["orden"] is None, "registro una orden que nunca salio"
    assert "alpaca no responde" in resultado["motivo"]
    assert len(filas) == 1, "el fallo de red no dejo rastro"


def test_el_fallo_de_envio_no_se_traga_como_si_nada():
    """Distinguir 'no habia nada que hacer' de 'lo intente y fallo'."""
    sin_fallo = ciclo(AHORA, **_entorno(cadena=lambda: []))

    def enviar(orden):
        raise ConnectionError("alpaca no responde")

    con_fallo = ciclo(AHORA, **_entorno(enviar=enviar))

    assert sin_fallo["motivo"] != con_fallo["motivo"], (
        "un ciclo tranquilo y uno con la API caida cuentan lo mismo")


# --- el contrato de la firma ------------------------------------------------

def test_ciclo_se_puede_llamar_solo_con_ahora():
    """Los inyectables son para el test; en produccion la firma es ciclo(ahora)."""
    import inspect

    firma = inspect.signature(ciclo)
    obligatorios = [p for p in firma.parameters.values()
                    if p.default is inspect.Parameter.empty
                    and p.kind is not p.VAR_KEYWORD]
    assert [p.name for p in obligatorios] == ["ahora"]


def test_no_se_llama_a_now_dentro():
    """`ahora` entra por parametro o el lazo no se puede probar en el pasado."""
    import hackathon.agent as agente

    fuente = __import__("inspect").getsource(agente)
    assert "datetime.now(" not in fuente and "utcnow(" not in fuente


# --- rodar, y el orden de preferencia dentro de la cadena -------------------
#
# Dos comportamientos que el primer corte del agente no cubria: una posicion
# cerca de vencer tiene que RODARSE, no ignorarse: y cuando el primer
# contrato de la cadena ya esta ocupado, el agente tiene que probar el
# siguiente en vez de rendirse.

VENCE_PRONTO = {"underlying": "SPY", "expiry": "2026-09-04", "qty": -1,
                "symbol": "SPY260904P00590000"}
OTRO = {"symbol": "QQQ260930P00480000", "underlying": "QQQ",
        "expiry": "2026-09-30", "strike": 480.0, "delta": -0.24,
        "bid": 4.10, "ask": 4.20, "open_interest": 3100, "volume": 260}


def test_una_posicion_que_vence_pronto_se_rueda():
    filas = []
    resultado = ciclo(AHORA, **_entorno(
        posiciones=lambda: [VENCE_PRONTO], registrar=filas.append))

    assert resultado["decision"] == "rodar", (
        f"no rodo: {resultado['decision']} -- {resultado['motivo']}")
    assert resultado["orden"] is not None
    assert len(filas) == 1


def test_la_fila_de_rodar_nombra_el_viejo_y_el_nuevo():
    """Rodar sin decir QUE se cierra y QUE se abre no se puede auditar."""
    filas = []
    ciclo(AHORA, **_entorno(posiciones=lambda: [VENCE_PRONTO],
                            registrar=filas.append))
    texto = str(filas[0])
    assert VENCE_PRONTO["expiry"] in texto, "no dice que vencimiento cierra"
    assert CONTRATO["symbol"] in texto, "no dice que contrato abre"


def test_una_posicion_lejana_no_se_rueda():
    """El control negativo: rodar siempre seria peor que no rodar nunca."""
    lejana = dict(VENCE_PRONTO, expiry="2026-10-30")
    resultado = ciclo(AHORA, **_entorno(posiciones=lambda: [lejana]))
    assert resultado["decision"] != "rodar"


def test_rodar_respeta_las_puertas():
    resultado = ciclo(AHORA, **_entorno(
        posiciones=lambda: [VENCE_PRONTO],
        puertas=[lambda orden: "CAPITAL insuficiente"]))
    assert resultado["decision"] == "nada"
    assert resultado["puerta_que_rechazo"] == "CAPITAL insuficiente"


def test_si_el_primero_esta_abierto_se_prueba_el_siguiente():
    enviados = []
    resultado = ciclo(AHORA, **_entorno(
        cadena=lambda: [CONTRATO, OTRO],
        posiciones=lambda: [ABIERTA],
        enviar=lambda o: enviados.append(o) or {"id": "ord-1", **o}))

    assert resultado["decision"] == "abrir", (
        f"se bloqueo en el primero: {resultado['motivo']}")
    assert resultado["orden"]["symbol"] == OTRO["symbol"]


def test_con_toda_la_cadena_abierta_es_nada_con_motivo():
    todas = [ABIERTA, {"underlying": "QQQ", "expiry": "2026-09-30", "qty": -1}]
    resultado = ciclo(AHORA, **_entorno(
        cadena=lambda: [CONTRATO, OTRO], posiciones=lambda: todas))
    assert resultado["decision"] == "nada"
    assert resultado["motivo"]


def test_el_primero_libre_se_sigue_prefiriendo():
    """Control que impide 'arreglarlo' saltandose siempre el primero."""
    resultado = ciclo(AHORA, **_entorno(cadena=lambda: [CONTRATO, OTRO]))
    assert resultado["orden"]["symbol"] == CONTRATO["symbol"]
