from __future__ import annotations

import os
from pathlib import Path

import pytest

from hackathon.alpaca_cli import AlpacaCLI


@pytest.mark.network
def test_cli_oficial_trae_la_cuenta_paper_real():
    executable = os.environ.get("ALPACA_CLI")
    if not executable or not Path(executable).exists():
        pytest.skip("ALPACA_CLI no apunta al binario oficial")
    if not os.environ.get("ALPACA_HACKATON_API_KEY"):
        pytest.skip("credenciales dedicadas del hackathon no cargadas")
    esperado = os.environ.get("ALPACA_HACKATON_ACCOUNT_NUMBER")
    if not esperado:
        pytest.skip("ALPACA_HACKATON_ACCOUNT_NUMBER no configurado -- nada contra que comparar")

    account = AlpacaCLI(executable).account()

    assert account["id"]
    assert account.get("status") == "ACTIVE"

    # Y que sea LA CUENTA DEDICADA, no cualquiera que responda. El wrapper cae a
    # `ALPACA_API_KEY` si no encuentra las `ALPACA_HACKATON_*`, asi que sin esto
    # el test pasaria igual apuntando a otra cuenta -- y el reglamento dice que
    # una cuenta reutilizada NO ES ELEGIBLE para el juicio. La elegibilidad no
    # se comprueba leyendo el codigo: se comprueba aqui, contra el numero real,
    # que se pasa por entorno y nunca queda escrito en el repositorio.
    assert account.get("account_number") == esperado, (
        f"el CLI hablo con {account.get('account_number')!r}, no con la cuenta "
        "esperada. Revisa que ALPACA_HACKATON_* y ALPACA_HACKATON_ACCOUNT_NUMBER "
        "esten en el entorno"
    )
