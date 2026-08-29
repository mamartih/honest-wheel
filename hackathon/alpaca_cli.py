"""Ruta oficial del hackathon hacia Alpaca: su CLI, nunca HTTP propio."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping


class AlpacaCLIError(RuntimeError):
    pass


def _environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(source or os.environ)
    key = env.get("ALPACA_HACKATON_API_KEY") or env.get("ALPACA_API_KEY")
    secret = env.get("ALPACA_HACKATON_SECRET_KEY") or env.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise AlpacaCLIError("faltan credenciales de la cuenta paper del hackathon")
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
