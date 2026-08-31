from __future__ import annotations

import os
from pathlib import Path

import pytest

from hackathon.alpaca_cli import AlpacaCLI


@pytest.mark.network
def test_official_cli_returns_the_real_paper_account():
    executable = os.environ.get("ALPACA_CLI")
    if not executable or not Path(executable).exists():
        pytest.skip("ALPACA_CLI no apunta al binario oficial")
    if not os.environ.get("ALPACA_HACKATON_API_KEY"):
        pytest.skip("credenciales dedicadas del hackathon no cargadas")

    account = AlpacaCLI(executable).account()

    assert account["id"]
    assert account.get("status") == "ACTIVE"

    # And that it is THE DEDICATED ACCOUNT, not just anything that answers. The
    # wrapper falls back to `ALPACA_API_KEY` if it can't find the
    # `ALPACA_HACKATON_*` ones, so without this the test would pass just the
    # same while pointing at the bot's account -- and the rules say a reused
    # account IS NOT ELIGIBLE for judging. Eligibility isn't checked by
    # reading the code: it's checked here.
    assert account.get("account_number") == "PA3ALIBKZ0U6", (
        f"el CLI hablo con {account.get('account_number')!r}, no con la cuenta "
        "nueva de M24. Revisa que ALPACA_HACKATON_* esten en el entorno")
