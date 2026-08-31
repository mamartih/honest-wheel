"""The hackathon's official route to Alpaca: its CLI, never a homegrown HTTP client."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping


class AlpacaCLIError(RuntimeError):
    pass


def _environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Resolve the dedicated account's credentials. NO FALLBACK, on purpose.

    This used to read `ALPACA_HACKATON_API_KEY or ALPACA_API_KEY`. On a laptop
    that fallback is a convenience; on a host that also runs a production bot it
    is a loaded gun: if the dedicated variables were ever missing, the agent
    would inherit the production account's credentials and send option orders
    into it. The convenience is worth nothing and the failure mode is
    unbounded, so the fallback is gone -- missing variables now fail loudly.

    It also refuses the generic variables outright: inheriting them silently is
    the exact accident this is meant to prevent.
    """
    env = dict(source or os.environ)
    key = env.get("ALPACA_HACKATON_API_KEY")
    secret = env.get("ALPACA_HACKATON_SECRET_KEY")
    if not key or not secret:
        raise AlpacaCLIError(
            "faltan ALPACA_HACKATON_API_KEY / ALPACA_HACKATON_SECRET_KEY. "
            "No se cae a las genericas a proposito: en una maquina que tambien "
            "corre el bot de produccion, ese respaldo mandaria ordenes a la "
            "cuenta equivocada")
    env["ALPACA_API_KEY"] = key
    env["ALPACA_SECRET_KEY"] = secret
    env.pop("ALPACA_LIVE_TRADE", None)
    env["ALPACA_OUTPUT"] = "json"
    return env


class AlpacaCLI:
    def __init__(self, executable: str | Path = "alpaca",
                 *, environment: Mapping[str, str] | None = None):
        self.executable = str(executable)
        self.environment = environment

    def run(self, *arguments: str) -> Any:
        completed = subprocess.run(
            [self.executable, *arguments], capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=_environment(self.environment),
            timeout=60,
        )
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()
            raise AlpacaCLIError(f"alpaca CLI terminó con {completed.returncode}: {detail}")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AlpacaCLIError("alpaca CLI no devolvió JSON") from exc

    def account(self) -> dict:
        result = self.run("account", "get")
        if not isinstance(result, dict) or not result.get("id"):
            raise AlpacaCLIError("respuesta de cuenta incompleta")
        return result

    def positions(self) -> list[dict]:
        result = self.run("position", "list")
        return result if isinstance(result, list) else []

    def submit_option(self, order: Mapping[str, Any], *, dry_run: bool = True) -> dict:
        arguments = [
            "order", "submit", "--symbol", str(order["symbol"]),
            "--side", str(order["side"]), "--qty", str(order["qty"]),
            "--type", "limit", "--limit-price", str(order["limit_price"]),
            "--time-in-force", "day",
        ]
        if dry_run:
            arguments.append("--dry-run")
        result = self.run(*arguments)
        return result if isinstance(result, dict) else {"result": result}
