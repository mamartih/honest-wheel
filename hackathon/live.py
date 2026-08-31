"""hackathon/live.py -- the composition root: wires agent+executor+alpaca_cli.

There is no new logic here. `agent.cycle` decides and expects five injectables
(positions, chain, gates, send, record); `executor.Executor` knows how to
apply the six gates but is a class with `.execute()`, not a list of
callables; `alpaca_cli.AlpacaCLI` talks to the hackathon's paper account via
the official binary. This file just connects them.

Where each injectable comes from:
  positions   AlpacaCLI.positions() (the real paper account), enriched with
              underlying/expiry parsed from the OCC symbol when the CLI does
              not already carry them as separate fields.
  chain       hackathon.chain.select(): CLI-only (PUT 30-45 DTE, delta
              ~-0.25, spread<3%). Replaces, as of 29/08, an earlier
              alpaca-py selector that took three calls, because the
              hackathon rules literally
              require the official CLI or MCP, not a hand-rolled SDK client.
              `alpaca data option chain` answers greeks + quote + daily
              volume in ONE call; open_interest comes from
              `alpaca option get <symbol>`, and is only requested for the
              contract ALREADY chosen, not the whole chain.
  gates       TWO gates, in order. First, one that wraps
              Executor.execute(..., dry_run=True) (always dry-run for the
              check pass, regardless of the global --no-dry-run) and
              translates a rejection into text with its reason_code -- that
              text is the `motivo` that cycle leaves in the record. Second,
              as of 29/08, the LLM veto from hackathon.advisor.review()
              (fail-open, veto-only -- see that module's docstring): it is
              deliberately LAST so a candidate the deterministic gate already
              rejects never spends a network call.
  send        Executor.execute(..., dry_run=dry_run) with the real dry_run
              that comes down from main()/run_cycle(). Here, and only here,
              a real order can go out if --no-dry-run is passed.
  record      one JSON line per cycle in logs/hackathon_cycles.jsonl.

The Executor's INTERNAL record (the one that sees every gate rejection) is
left as a no-op: if it wrote to the same file, the gate (which calls
execute()) and the send (which calls it again) would write two or three
lines per cycle, and the criterion asks for ONE.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from hackathon.advisor import review as advisor_review
from hackathon.agent import cycle
from hackathon.alpaca_cli import AlpacaCLI, _environment
from hackathon.executor import Executor, Limits

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "hackathon_cycles.jsonl"

# The same universe our own options observer has been logging since 27/07 --
# it is the source of the selection, not one invented for the contest.
_DEFAULT_UNDERLYINGS = ["SPY", "QQQ", "GLD", "IWM"]

_OCC = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})[CP]\d{8}$")


def _underlying_and_expiry(pos: dict) -> tuple[Optional[str], Optional[str]]:
    """Derive underlying/expiry from a raw CLI position if they aren't there.

    The official Alpaca CLI returns the OCC symbol (e.g.
    'SPY260930P00600000'); cycle() and Executor compare on separate
    `underlying` and `expiry` fields. Only parsed when the position doesn't
    already carry them -- in case a future CLI version adds them.
    """
    if pos.get("underlying") and pos.get("expiry"):
        return pos["underlying"], pos["expiry"]
    symbol = str(pos.get("symbol", ""))
    m = _OCC.match(symbol)
    if not m:
        return pos.get("underlying"), pos.get("expiry")
    root, yy, mm, dd = m.groups()
    return root, f"20{yy}-{mm}-{dd}"


def _build_positions(cli: AlpacaCLI) -> Callable[[], list[dict]]:
    def positions() -> list[dict]:
        raw_positions = cli.positions()
        enriched = []
        for pos in raw_positions:
            underlying, expiry = _underlying_and_expiry(pos)
            enriched.append({**pos, "underlying": underlying, "expiry": expiry})
        return enriched
    return positions


def _default_signal_filter(stock_data: Any, *, days: int = 420,
                            params: Optional[dict] = None
                            ) -> Callable[[str], Optional[str]]:
    """Return None if the underlying PASSES, or the reason it is blocked.

    TWO LEGS, and they play different roles -- they do not compete:

      REGIME    close above the 200-day average. It is the one piece of this
                design with external evidence behind it: Yang (SSRN) measures
                22.6 years of put-write conditioned on a 50/200 crossover and
                reports an improvement over passive put-write.
                **And it has to be said that 7 days is NOT evaluable**: that
                paper sees 21 signal changes in 22.6 years -- one every
                thirteen months. In one week this is effectively a constant:
                it either lets the whole week through or blocks the whole
                week.

      SIGNAL    `strategies.turn_of_month`, params
                {"days_before": 2, "days_after": 3, "window_bars": 30} --
                the same ones already running in production on two slots.

    **TRIGGER CHANGED, 29/08/2026.** The signal leg USED TO BE
    `strategies.mean_reversion` (`entry_z: 3.0`). It was tested against the
    16 liquid ETFs below with real data and the result was ZERO out of
    sixteen:

        out on REGIME       TLT GLD SLV XLU HYG
        no SIGNAL           SPY QQQ IWM DIA EFA EEM XLF XLE XLK XLV USO
        PASS                none

    `entry_z: 3.0` is a three-sigma event: it almost never fires. With five
    contest sessions left that is zero trades, and the jury's criterion #1
    is P&L.

    **What was NOT done, and it has to be said plainly: `entry_z` was NOT
    lowered.** Loosening the entry just to get trades out is exactly what
    this house's method forbids, and it would also destroy the one thing
    this deliverable has going for it, which is the provenance of the rule.

    Instead the signal leg was replaced with `turn_of_month`: a CALENDAR
    rule (a two-market-day window before month-end close and three after),
    the most auditable rule there is, impossible to overfit to price data --
    and one that is ALREADY in production on two slots, not a new rule
    invented for the contest.

    **And one thing that is not softened**: this rule is chosen IN PART
    because it fires this week (its window -- 27/08 to 03/09 -- covers
    almost the whole contest). That is a demonstrability decision, not a
    performance claim. Choosing among rules ALREADY PRE-REGISTERED by which
    one fires inside the window does not overfit the rule itself, but it DOES
    overfit the choice -- and the difference only holds if it is stated,
    which is what this paragraph does.

    **The backtest for both legs is for the STOCK leg, not the options
    leg.** Spread, theta, gamma and assignment change the whole payoff.
    Here the signal only chooses WHEN to look, it makes no promise about the
    result.

    Applied PER UNDERLYING and never globally: if SPY is out of regime, QQQ
    or GLD can still go. A global filter is a guaranteed zero the week the
    market corrects.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from strategies import turn_of_month

    params = params or {"days_before": 2, "days_after": 3, "window_bars": 30}

    def signal_filter(underlying: str) -> Optional[str]:
        try:
            bars = stock_data.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=underlying, timeframe=TimeFrame.Day,
                start=(datetime.now(timezone.utc) - timedelta(days=days)).date()))
            df = bars.df
            if hasattr(df, "index") and getattr(df.index, "nlevels", 1) > 1:
                df = df.xs(underlying, level=0)
        except Exception as exc:  # noqa: BLE001
            return f"SIN_DATOS: {exc}"
        # 420 CALENDAR days, not 260. With 260 only 178 market bars came out
        # and the filter returned SIN_DATOS for all four underlyings: it
        # would have blocked the whole week silently. No test with doubles
        # catches this -- it only showed up running against real data.
        if df is None or len(df) < 200:
            return f"SIN_DATOS: {0 if df is None else len(df)} barras, hacen falta 200"

        close = df["close"]
        sma_200 = float(close.tail(200).mean())
        last_close = float(close.iloc[-1])
        if last_close <= sma_200:
            return (f"REGIMEN: {last_close:.2f} por debajo de la media de 200 "
                    f"({sma_200:.2f})")

        columns = {c.lower(): c for c in df.columns}
        frame = df.rename(columns={v: k for k, v in columns.items()})
        signal = turn_of_month.generate_signal(frame, None, params)
        if signal.get("action") != "enter_long":
            return f"SENAL: turn_of_month dice {signal.get('action') or 'nada'}"
        return None

    return signal_filter


def _require_declared_account(cli: AlpacaCLI) -> None:
    """Refuse to run against any account other than the declared one.

    Second layer, and it exists because the first one can be misconfigured.
    Environment variables can be wrong; an account number cannot. On a host
    that also runs a production bot, "we believe it is isolated" is not good
    enough -- this makes trading the wrong account impossible rather than
    unlikely.

    Set `ALPACA_HACKATON_ACCOUNT_NUMBER` to enable it. Absent, it warns and
    continues: a machine with no production credentials on it does not need
    the guard, and refusing to start there would be theatre.
    """
    expected = os.environ.get("ALPACA_HACKATON_ACCOUNT_NUMBER")
    if not expected:
        print("AVISO: sin ALPACA_HACKATON_ACCOUNT_NUMBER no se puede comprobar "
              "contra que cuenta se opera", file=sys.stderr)
        return
    actual = (cli.account() or {}).get("account_number")
    if actual != expected:
        raise RuntimeError(
            f"cuenta equivocada: el CLI responde {actual!r} y la declarada es "
            f"{expected!r}. El agente NO opera")


def _default_chain(*, executable: Optional[str], underlyings: list[str],
                    cache: dict[str, dict],
                    signal_filter: Optional[Callable[[str], Optional[str]]] = None
                    ) -> Callable[[], list[dict]]:
    """Production chain(): hackathon.chain.select, CLI-only.

    The filter is consulted BEFORE calling the CLI (saves the call for an
    underlying already known to be out). `select` already brings its own
    per-underlying resilience (one downed CLI call doesn't take the rest
    down), so it is not duplicated here. `cache` is filled the same way as
    before -- the gate uses it to enrich `orden` with what agent.cycle does
    not propagate (see _build_gate).
    """
    from hackathon.chain import select

    def chain() -> list[dict]:
        cache.clear()
        active_underlyings = []
        for underlying in underlyings:
            if signal_filter is not None:
                block = signal_filter(underlying)
                if block:
                    # The reason is PRINTED. An underlying that disappears from
                    # the chain without saying why is indistinguishable from
                    # one that failed to download.
                    print(f"cadena: {underlying} fuera -- {block}", file=sys.stderr)
                    continue
            active_underlyings.append(underlying)
        if not active_underlyings:
            return []

        candidates = select(active_underlyings, cli=executable)
        for c in candidates:
            cache[c["symbol"]] = c
        return candidates
    return chain


def _options_buying_power(account: dict) -> float:
    """The cash a cash-secured put can actually consume.

    NOT `buying_power`. That field is the MARGINABLE figure for equities and
    on a Reg-T account it is roughly twice the cash; a short put's collateral
    is withheld from the NON-marginable balance instead. Reading the wrong one
    is not a rounding error, it is an order the broker refuses.

    THIS COST A REAL ORDER, 31/08/2026. With one SPY put already open, the
    agent's own CAPITAL gate compared 74,194 against `buying_power` 103,743,
    approved, and Alpaca answered 403: "insufficient options buying power
    (required: 74194.01, available: 25935.98)". The gate was right; it was
    fed the wrong magnitude.

    `buying_power` is deliberately NOT in the fallback chain. Putting it there
    as a last resort would reintroduce the same defect the first day Alpaca
    omits the options field -- silently, and only in production.
    """
    for campo in ("options_buying_power", "non_marginable_buying_power", "cash"):
        valor = account.get(campo)
        if valor not in (None, ""):
            return float(valor)
    return 0.0


def _build_gate(executor: Executor, *, cli: AlpacaCLI,
                 now_fn: Callable[[], datetime],
                 cache: dict[str, dict]) -> Callable[[dict], Optional[str]]:
    """Wraps Executor.execute() as ONE gate. It does NOT duplicate the six
    gates: Executor.execute() evaluates all six, this function only
    translates its verdict into the contract `cycle` expects (motivo |
    None)."""

    def gate(order: dict) -> Optional[str]:
        candidate = {**cache.get(order.get("symbol"), {}), **order}
        account = cli.account()
        buying_power = _options_buying_power(account)
        current_positions = cli.positions()
        result = executor.execute(
            candidate, buying_power=buying_power, positions=current_positions,
            now=now_fn(), dry_run=True,
        )
        if result.get("decision") == "rechazada":
            return f"{result['reason_code']}: {result['motivo']}"
        if result.get("decision") == "error":
            return f"API: {result['motivo']}"
        return None
    return gate


def _build_advisor_gate(*, cache: dict[str, dict],
                         ask: Optional[Callable[[str], str]] = None,
                         state: Optional[dict] = None
                         ) -> Callable[[dict], Optional[str]]:
    """The LLM veto, wired as ONE MORE gate -- see hackathon/advisor.py for
    the full design (fail-open, veto-only). This function only adapts
    `advisor.review()`'s dict result to the `Callable[[dict], Optional[str]]`
    contract `cycle()` expects for every gate: None means allow, a string
    means reject and becomes the recorded reason.

    `ask` is forwarded to `review()` as-is (None means "use the real
    Featherless call"); it is the injection point tests use to avoid the
    network, exactly like `cli`/`chain` elsewhere in this module.
    """

    def gate(order: dict) -> Optional[str]:
        candidate = {**cache.get(order.get("symbol"), {}), **order}
        result = advisor_review(candidate, ask=ask)
        # THE OUTCOME IS RECORDED EITHER WAY, veto or not. Returning None on a
        # fail-open left no trace at all, which makes "the advisor approved"
        # indistinguishable from "the advisor never ran" -- and the write-up
        # claims the record tells them apart. A silence is not an approval.
        if state is not None:
            state["advisor"] = {"consulted": result["consulted"],
                                "veto": result["veto"],
                                "reason": result["reason"],
                                "model": result["model"]}
        if result["veto"]:
            return f"ADVISOR: {result['reason']}"
        return None

    return gate


def _build_send(executor: Executor, *, cli: AlpacaCLI,
                 now_fn: Callable[[], datetime],
                 cache: dict[str, dict],
                 dry_run: bool) -> Callable[[dict], dict]:
    def send(order: dict) -> dict:
        candidate = {**cache.get(order.get("symbol"), {}), **order}
        account = cli.account()
        buying_power = _options_buying_power(account)
        current_positions = cli.positions()
        result = executor.execute(
            candidate, buying_power=buying_power, positions=current_positions,
            now=now_fn(), dry_run=dry_run,
        )
        if result.get("decision") in ("rechazada", "error"):
            # cycle() already called the gate before getting here; if this
            # rejects it is because state changed between the gate and the
            # send (another position opened, capital moved). Treated as a
            # send failure -- cycle() records it as "nada", not "abrir".
            raise RuntimeError(
                f"{result.get('reason_code', 'ERROR')}: {result['motivo']}")
        # Returns THE ORDER THAT WENT OUT, not the Executor's accounting
        # record. Returning `result` as-is, the cycle's record stored
        # {decision, reason_code, motivo, order, response} with no `symbol`
        # at the top level: the row said something opened without saying
        # WHAT. It is the same defect already noted in N12 -- storing the
        # broker's response instead of the order that was sent -- closed
        # here.
        return {**result.get("order", {}),
                "respuesta_broker": result.get("response"),
                "modo": result.get("decision")}
    return send


def _jsonl_recorder(log_path: Path,
                     extra: Optional[dict] = None,
                     now_fn: Optional[Callable[[], datetime]] = None
                     ) -> Callable[[dict], None]:
    """One JSON line per cycle. `extra` is merged in and then CLEARED.

    It carries what the gates learned during this cycle -- today, whether the
    LLM advisor was actually consulted, not just whether it vetoed. Returning
    None on a fail-open left no trace at all, which made "the advisor approved"
    indistinguishable from "the advisor never ran".

    It is cleared after writing so a stale verdict from an earlier cycle can
    never be reported as this one's: a record that repeats the last thing it
    knew is worse than one that says nothing.
    """
    def record(row: dict) -> None:
        if extra:
            row = {**row, **extra}
            extra.clear()
        # EVERY row gets a timestamp, not just the error branch. Until 29/08
        # only failures carried `cuando`, so the dashboard rendered almost the
        # whole log as "time unknown" -- and a decision log with no clock
        # cannot be audited: you cannot tell when the agent decided to do
        # nothing, or whether it has been stuck for six hours.
        #
        # It uses the CYCLE's clock, not the writer's, and it never overwrites
        # a timestamp the row already carries: the row knows when its decision
        # was made better than the moment it happened to be flushed to disk.
        if "cuando" not in row:
            stamp = (now_fn or (lambda: datetime.now(timezone.utc)))()
            row = {**row, "cuando": stamp.isoformat(timespec="seconds")}
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
    return record


def build(*, executable: Optional[str] = None, dry_run: bool = True,
          limits: Optional[Limits] = None,
          cli: Optional[AlpacaCLI] = None,
          chain: Optional[Callable[[], list[dict]]] = None,
          underlyings: Optional[list[str]] = None,
          now_fn: Optional[Callable[[], datetime]] = None,
          signal_filter: Optional[Callable[[str], Optional[str]]] = None,
          log_path: Optional[Path] = None,
          advisor_ask: Optional[Callable[[str], str]] = None) -> dict:
    """Builds the five injectables for `agent.cycle`.

    Each real dependency (CLI, chain) accepts a double via parameter -- so
    the test wires everything without touching the network or the official
    CLI's subprocess.
    """
    if now_fn is None:
        now_fn = lambda: datetime.now(timezone.utc)  # noqa: E731

    if cli is None:
        executable_bin = executable or os.environ.get("ALPACA_CLI", "alpaca")
        cli = AlpacaCLI(executable_bin)
        _require_declared_account(cli)

    cache: dict[str, dict] = {}
    # Filled in by the advisor gate on every cycle so the recorded row can
    # say whether the model was consulted at all, not just whether it vetoed.
    advisor_state: dict[str, Any] = {}

    if chain is None:
        # Contract selection (hackathon.chain.select) is CLI-only and never
        # touches alpaca-py. stock_data is STILL needed: the REGIME leg of
        # the filter (200-day average) uses
        # StockHistoricalDataClient.get_stock_bars. Same credentials that
        # alpaca_cli._environment resolves -- the ALPACA_HACKATON_* ->
        # ALPACA_API_KEY fallback is not reimplemented.
        env = _environment()
        from alpaca.data.historical.stock import StockHistoricalDataClient

        stock_data = StockHistoricalDataClient(env["ALPACA_API_KEY"], env["ALPACA_SECRET_KEY"])
        chain = _default_chain(
            executable=cli.executable,
            underlyings=underlyings or _DEFAULT_UNDERLYINGS, cache=cache,
            signal_filter=signal_filter if signal_filter is not None
            else _default_signal_filter(stock_data))
    # If a `chain` is injected (test), the enrichment cache is filled from
    # whatever that `chain()` returns -- each candidate already carries what
    # it needs, there is no SDK to query.
    else:
        chain_original = chain

        def chain() -> list[dict]:  # noqa: F811 -- wraps the injected double
            candidates = chain_original()
            cache.clear()
            for c in candidates:
                if c.get("symbol"):
                    cache[c["symbol"]] = c
            return candidates

    executor = Executor(cli, limits=limits or Limits(), record=lambda _row: None)

    return {
        "positions": _build_positions(cli),
        "chain": chain,
        # ORDER MATTERS: the six deterministic Executor checks go FIRST
        # (cheap, certain, no network) and the LLM advisor goes LAST -- a
        # candidate a deterministic gate already rejects must never spend a
        # Featherless call. `cycle()` stops at the first non-None rejection,
        # so this ordering is what makes that guarantee hold.
        "gates": [_build_gate(executor, cli=cli, now_fn=now_fn, cache=cache),
                  _build_advisor_gate(cache=cache, ask=advisor_ask,
                                      state=advisor_state)],
        "send": _build_send(executor, cli=cli, now_fn=now_fn, cache=cache, dry_run=dry_run),
        "record": _jsonl_recorder(log_path or LOG_PATH, extra=advisor_state,
                                  now_fn=now_fn),
    }


def run_cycle(now: Optional[datetime] = None, **kwargs) -> dict:
    """build() + cycle(): one full cycle, for real."""
    now = now if now is not None else datetime.now(timezone.utc)
    kwargs.setdefault("now_fn", lambda: now)
    injectables = build(**kwargs)
    return cycle(now, **injectables)


def loop(*, dry_run: bool = True, interval: int = 900,
         limit: Optional[int] = None,
         run_cycle_fn: Optional[Callable[..., dict]] = None,
         sleep_fn: Callable[[float], None] = time.sleep,
         notify: Optional[Callable[[str, str], None]] = None,
         record: Optional[Callable[[dict], None]] = None,
         output=sys.stdout) -> int:
    """One cycle every `interval` seconds. IT DOES NOT DIE ON AN EXCEPTION.

    An autonomous loop that falls over on the first network failure is not
    autonomous: it is a script. Here any exception from a cycle is recorded
    and the loop keeps going -- only Ctrl-C or `limit` stops it.

    **And a cycle that fails still leaves a row.** A record file that only
    has good cycles makes "there was nothing to do" indistinguishable from
    "it has been failing for six hours", the same lesson already in
    `cycle()`.

    With the market closed the HORARIO gate rejects every cycle, and that is
    NOT a malfunction: it is the correct behavior. The loop says so and
    keeps going.
    """
    run_cycle_fn = run_cycle_fn or run_cycle
    # The recorder IS INJECTABLE, and it did not used to be: the error branch
    # called `_jsonl_recorder(LOG_PATH)` directly, so the TESTS were writing
    # into the production record. Two fake "alpaca not responding" rows ended
    # up in the file that is the contest deliverable. A record with test data
    # inside is not a record: it is a mix nobody can audit.
    record = record or _jsonl_recorder(LOG_PATH)
    completed, failures = 0, 0
    # ONLY CHANGES GET NOTIFIED. One cycle every 15 minutes is 96 notices a
    # day, and ninety-six identical messages are not monitoring: they are
    # noise that gets learned to ignore. There has been a `STOP NATIVO
    # AUSENTE` warning repeating every four hours since 22/08 on this very
    # channel, a week now with nobody acting on it. A detector nobody
    # watches is not a detector.
    last_state: Optional[str] = None
    try:
        while limit is None or completed < limit:
            timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                result = run_cycle_fn(dry_run=dry_run)
                state = f"{result.get('decision')} | {result.get('motivo')}"
                print(f"{timestamp}  {state}", file=output, flush=True)
                if notify is not None and state != last_state:
                    notify("Agente del hackathon", f"{timestamp}\n{state}")
                last_state = state
            except Exception as exc:  # noqa: BLE001 -- the loop never dies
                failures += 1
                row = {"decision": "error", "motivo": f"ciclo reventado: {exc}",
                        "orden": None, "puerta_que_rechazo": None,
                        "cuando": timestamp}
                record(row)
                print(f"{timestamp}  ERROR  {exc}", file=output, flush=True)
                state = f"error | {exc}"
                if notify is not None and state != last_state:
                    notify("Agente del hackathon: ERROR", f"{timestamp}\n{exc}")
                last_state = state
            completed += 1
            if limit is None or completed < limit:
                sleep_fn(interval)
    except KeyboardInterrupt:
        print(f"\ndetenido a mano tras {completed} ciclos ({failures} con error)",
              file=output, flush=True)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Ciclo autonomo del hackathon (paper).")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                        help="no envia ninguna orden real (por defecto)")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false",
                        help="envia de verdad a la cuenta paper del hackathon")
    parser.add_argument("--once", action="store_true",
                        help="corre exactamente un ciclo y termina")
    parser.add_argument("--interval", type=int, default=900,
                        help="segundos entre ciclos en modo continuo (900 = 15 min)")
    parser.add_argument("--limit", type=int, default=None,
                        help="para tras N ciclos; por defecto no para")
    args = parser.parse_args(argv)

    if not args.once:
        return loop(dry_run=args.dry_run, interval=args.interval,
                    limit=args.limit)

    result = run_cycle(dry_run=args.dry_run)
    print(json.dumps(result, default=str, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
