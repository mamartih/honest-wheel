"""Contract selection via the official Alpaca CLI only -- no `alpaca-py`.

Why this exists: our earlier contract selector (PUT 30-45 DTE, delta ~-0.25,
spread<3%) went through the `alpaca-py` SDK in three separate calls
(contracts + chain + open interest). The contest rules require the official
CLI or MCP rather than a hand-rolled SDK client, and the CLI returns greeks,
quote and daily volume in ONE call -- so this replaces three calls with one.

`alpaca data option chain` turns out to answer almost everything in ONE call:
greeks, latest quote (bid/ask) and the day's bar (volume) per contract
symbol. Open interest is the one field that call does not return, so the
selected contract (only the selected one, not the whole chain) gets one extra
`alpaca option get <symbol>` call.

Public knowledge only: PUT selling by delta/DTE/spread is a standard
covered-put screen, nothing proprietary lives here.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from hackathon.alpaca_cli import AlpacaCLIError, _environment

# OCC symbol, e.g. SPY260930P00600000 -> root=SPY, exp=2026-09-30, type=P,
# strike=600.000. Same shape as the regex in hackathon/live.py, duplicated on
# purpose: this module must not import anything that isn't part of hackathon/.
_OCC = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")

# Safety cap on how many chain pages we'll follow per underlying. In practice
# `--type put` + `--expiration-date-gte/-lte` already narrow the window to a
# few hundred contracts and one page (`--limit 1000`) covers it -- verified
# against the real SPY chain on 2026-08-29 (661 contracts, empty
# next_page_token). The cap exists only so a future wide window can't spin
# forever if Alpaca ever ignores the filters.
_MAX_PAGES = 10
_PAGE_LIMIT = "1000"


def _parse_symbol(symbol: str) -> Optional[dict]:
    """Parse an OCC option symbol into its parts, or None if it doesn't match.

    The chain endpoint keys its `snapshots` map by OCC symbol and does not
    repeat underlying/expiry/strike/type as separate fields, so this is the
    only source for them.
    """
    m = _OCC.match(symbol)
    if not m:
        return None
    root, yy, mm, dd, option_type, strike_raw = m.groups()
    try:
        expiry = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None
    return {
        "underlying": root, "expiry": expiry, "type": option_type,
        "strike": int(strike_raw) / 1000,
    }


def _default_run_cli(cli: str | Path | None = None) -> Callable[[list[str]], str]:
    """Build the default `run_cli(arguments) -> stdout` using subprocess.

    `cli` is the path to the `alpaca` executable (defaults to `ALPACA_CLI` env
    var or plain "alpaca", same fallback `hackathon/alpaca_cli.py` uses).
    Credential resolution is NOT reimplemented here: `_environment()` from
    `hackathon.alpaca_cli` is reused as-is, same pattern as `AlpacaCLI.run`.
    """
    executable = str(cli) if cli else os.environ.get("ALPACA_CLI", "alpaca")

    def run_cli(arguments: list[str]) -> str:
        completed = subprocess.run(
            [executable, *arguments], capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=_environment(), timeout=60,
        )
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()
            raise AlpacaCLIError(
                f"alpaca CLI termino con {completed.returncode}: {detail}")
        return completed.stdout

    return run_cli


def _fetch_chain(run_cli: Callable[[list[str]], str], underlying: str,
                  dte: tuple[int, int], today: date) -> dict[str, dict]:
    """Fetch and merge every page of `data option chain` for one underlying.

    Filtering is pushed to the CLI itself (`--type put`,
    `--expiration-date-gte/-lte`) instead of pulling the whole unfiltered
    chain and filtering client-side -- that flag combo is what keeps this to
    one page in practice. Pagination is still followed defensively via
    `next_page_token` / `--page-token`, capped at `_MAX_PAGES`.
    """
    start = today + timedelta(days=dte[0])
    end = today + timedelta(days=dte[1])
    base = [
        "data", "option", "chain",
        "--underlying-symbol", underlying,
        "--type", "put",
        "--expiration-date-gte", start.isoformat(),
        "--expiration-date-lte", end.isoformat(),
        "--limit", _PAGE_LIMIT,
    ]

    snapshots: dict[str, dict] = {}
    page_token: Optional[str] = None
    for _ in range(_MAX_PAGES):
        arguments = list(base)
        if page_token:
            arguments += ["--page-token", page_token]
        output = run_cli(arguments)
        try:
            data = json.loads(output)
        except json.JSONDecodeError as exc:
            raise AlpacaCLIError("alpaca CLI no devolvio JSON") from exc
        snapshots.update(data.get("snapshots") or {})
        page_token = data.get("next_page_token") or None
        if not page_token:
            break
    return snapshots


def _open_interest(run_cli: Callable[[list[str]], str], symbol: str) -> int:
    """Open interest for one contract via `alpaca option get`.

    The chain snapshot never carries open interest (verified against the
    real account on 2026-08-29). Only called for the contract that already
    won the selection, not for every candidate in the chain.
    """
    try:
        output = run_cli(["option", "get", "--symbol-or-id", symbol])
        data = json.loads(output)
        oi = data.get("open_interest")
        return int(float(oi)) if oi else 0
    except Exception:  # noqa: BLE001 -- missing OI is data, not a crash
        return 0


def _best_contract(snapshots: Mapping[str, dict], *, underlying: str,
                    dte: tuple[int, int], target_delta: float,
                    spread_max: float, today: date) -> Optional[dict]:
    """Pick the PUT closest to `target_delta` among those inside the DTE
    window and under `spread_max`, or None if nothing qualifies.

    Every rejection reason is printed to stderr with the symbol -- a
    candidate that silently disappears is indistinguishable from one that
    never downloaded (same lesson as the signal filter in
    `hackathon/live.py`).
    """
    best: Optional[dict] = None
    best_distance = float("inf")

    for symbol, snap in snapshots.items():
        parts = _parse_symbol(symbol)
        if parts is None or parts["underlying"] != underlying:
            continue
        if parts["type"] != "P":
            continue  # calls are never eligible, regardless of delta
        days = (parts["expiry"] - today).days
        if not (dte[0] <= days <= dte[1]):
            print(f"chain: {symbol} fuera de ventana DTE ({days}d)", file=sys.stderr)
            continue

        greeks = snap.get("greeks") or {}
        delta = greeks.get("delta")
        if delta is None:
            print(f"chain: {symbol} sin delta", file=sys.stderr)
            continue

        quote = snap.get("latestQuote") or {}
        bid, ask = quote.get("bp"), quote.get("ap")
        if bid is None or ask is None:
            print(f"chain: {symbol} sin cotizacion", file=sys.stderr)
            continue
        bid, ask = float(bid), float(ask)
        mid = (bid + ask) / 2
        spread = (ask - bid) / mid if mid > 0 else float("inf")
        if spread > spread_max:
            print(f"chain: {symbol} descartado SPREAD {spread:.2%} > "
                  f"{spread_max:.2%}", file=sys.stderr)
            continue

        distance = abs(delta - target_delta)
        if distance < best_distance:
            volume = int((snap.get("dailyBar") or {}).get("v") or 0)
            best_distance = distance
            best = {
                "symbol": symbol, "underlying": underlying,
                "expiry": parts["expiry"].isoformat(), "strike": parts["strike"],
                "delta": float(delta), "bid": bid, "ask": ask, "volume": volume,
            }
    return best


def select(underlyings: list[str], *, cli: str | Path | None = None,
           dte: tuple[int, int] = (30, 45),
           target_delta: float = -0.25, spread_max: float = 0.03,
           run_cli: Optional[Callable[[list[str]], str]] = None
           ) -> list[dict]:
    """Select the best PUT-selling candidate per underlying, CLI-only.

    Returns a list with exactly the keys `executor.py`/`agent.py` already
    consume: symbol underlying expiry strike delta bid ask open_interest
    volume. One entry per underlying at most (the best one); underlyings
    with nothing qualifying are simply absent from the result, and a failure
    on one underlying (CLI error, bad JSON) is caught and logged so the rest
    of the sweep still runs -- one dead underlying must not blank the whole
    chain for a symbol whose data was fine.
    """
    if run_cli is None:
        run_cli = _default_run_cli(cli)

    today = date.today()
    result: list[dict] = []
    for underlying in underlyings:
        try:
            snapshots = _fetch_chain(run_cli, underlying, dte, today)
            best = _best_contract(
                snapshots, underlying=underlying, dte=dte,
                target_delta=target_delta, spread_max=spread_max, today=today)
            if best is None:
                continue
            best["open_interest"] = _open_interest(run_cli, best["symbol"])
            result.append(best)
        except Exception as exc:  # noqa: BLE001 -- one bad underlying, not the sweep
            print(f"chain: {underlying} fallo al seleccionar ({exc})", file=sys.stderr)
            continue
    return result


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Select PUT-selling candidates via the official Alpaca CLI.")
    parser.add_argument("underlyings", nargs="+")
    args = parser.parse_args(argv)

    candidates = select(args.underlyings)
    print(json.dumps(candidates, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
