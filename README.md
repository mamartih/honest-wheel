# honest-wheel

An autonomous agent that sells cash-secured puts on liquid ETFs through
Alpaca's **official CLI**, on a dedicated paper-trading account.

It is called `honest-wheel` because the interesting part is not the P&L —
seven days is not enough time to tell skill from luck — it's what the agent
refuses to do, and what it admits it hasn't proven.

## The six risk gates

Every candidate order passes through `Executor.execute()`
([`hackathon/executor.py`](hackathon/executor.py)) before it can reach the
broker. The gates run in a fixed order and stop at the first rejection, so a
rejected order always carries exactly one reason:

| # | Gate | Rejects when | Why |
|---|------|---------------|-----|
| 1 | `CAPITAL` | buying power < strike × 100 × qty | a cash-secured put that isn't actually secured isn't the strategy |
| 2 | `CONCENTRACION_SUBYACENTE` | more than 2 contracts on the same underlying | one bad print on one ticker shouldn't wreck the account |
| 3 | `CONCENTRACION_VENCIMIENTO` | more than 3 contracts on the same expiry | assignment risk shouldn't cluster on one date |
| 4 | `SPREAD` | bid/ask spread > 3% of midpoint | a wide spread means the fill price is a guess, not a quote |
| 5 | `LIQUIDEZ_INTERES_ABIERTO` / `LIQUIDEZ_VOLUMEN` | open interest < 10 or **daily** volume < 5 | a contract with real interest can be closed later; one nobody trades can't |
| 6 | `HORARIO` | outside NYSE regular hours, or a weekend | no fills to be had when the exchange is closed |
| — | `IDEMPOTENCIA` | a position already exists on that underlying+expiry | protects against sending the same order twice in one cycle |

`IDEMPOTENCIA` isn't numbered with the other six because it isn't a *risk*
gate in the same sense — it's a safety check against double-submission, not
a bet the agent is choosing not to take. The six that are about risk are the
first six in the table.

Every rejection is written to the log with its `reason_code` and a
human-readable `motivo` (reason). Nothing is silently dropped.

One gate exists as a deliberate contrast: `dry_run` is a **parameter** to
`execute()`, not a hardcoded `True`. An earlier version of this code had
`dry_run=True` fixed in the call — which technically satisfies "supports
dry-run," but also means the agent could never place a real order, in a
contest that scores P&L first. `test_el_dry_run_es_un_parametro_y_no_un_candado`
in [`tests/test_hackathon_executor.py`](tests/test_hackathon_executor.py) is
written specifically to catch that regression if it ever comes back.

## The loop

```
observe -> decide -> gates -> act -> log
```

`agent.ciclo()` ([`hackathon/agent.py`](hackathon/agent.py)) runs one cycle:
it looks at open positions and the current contract chain, and picks one of
three decisions — **open** a new position, **roll** one that's within 7 days
of expiry, or do **nothing**. All three write exactly one line to the log.

**Doing nothing is a decision, and it leaves a row too.** An agent that only
writes to its log when it trades looks idle on a quiet day and looks broken
on a bad one — there's no way to tell "the market didn't offer anything
worth taking" from "the process died six hours ago" unless silence itself is
distinguishable from a logged, reasoned pass. Every cycle — trade or not —
appends one JSON line to `logs/hackathon_cycles.jsonl` with a `motivo` field
explaining what happened.

[`hackathon/live.py`](hackathon/live.py) is the composition root: it wires
`agent.ciclo()`, `executor.Executor`, and `alpaca_cli.AlpacaCLI` together and
nothing else. It does not reimplement any of the three.

## Why the official CLI, not the MCP server or raw HTTP

Alpaca ships both an official **Trading CLI** and an official **MCP server**
for options trading. This project uses the CLI
([`hackathon/alpaca_cli.py`](hackathon/alpaca_cli.py)) as the transport
boundary: it returns account, positions, option chains, quotes, and order
submission as JSON, supports `--dry-run` natively, and is a deterministic
interface that a subprocess test can exercise end-to-end without the agent
implementing any HTTP itself.

The MCP server is appropriate for conversational, exploratory use, but its
tools are discovered dynamically at runtime and its interface changed
without backward compatibility between v1 and v2 — not what you want as the
one reproducible gate a paper-trading process depends on every cycle.

## Running it

Everything below assumes the official `alpaca` CLI binary is installed
separately (`go install github.com/alpacahq/cli/cmd/alpaca@latest`, or a
release binary from `alpacahq/cli`) and that `ALPACA_HACKATON_API_KEY` /
`ALPACA_HACKATON_SECRET_KEY` are set to a **dedicated** paper account — the
wrapper in `alpaca_cli.py` falls back to `ALPACA_API_KEY` /
`ALPACA_SECRET_KEY` only if the dedicated pair isn't set.

```powershell
# install the Python side (pytest only -- see requirements.txt for why)
pip install -r requirements.txt

# run the offline test suite (no network, no credentials needed)
pytest -m "not network"

# run everything, including the one test that hits the real paper account
$env:ALPACA_CLI = "$HOME\bin\alpaca.exe"
pytest
```

`hackathon/live.py` is a composition root, not a full CLI product: this
public repository does not ship a default contract-chain selector (see
"What is not tested" below), so `construir()` raises `NotImplementedError`
unless you pass your own `cadena` callable. The tests show the exact shape
it expects — a zero-argument callable returning a list of dicts with
`symbol` / `underlying` / `expiry` / `strike` / `bid` / `ask` /
`open_interest` / `volume`.

## What is not tested — and this is the part that matters

No entry in this list is softened. This project's actual edge isn't the
strategy — cash-secured puts on liquid ETFs are about as plain-vanilla as
options trading gets — it's being explicit about exactly how little a week
of paper trading can prove.

- **There is no backtest of the options leg. None.** No historical options
  chain data exists at the granularity and time depth this would need, on
  the timeline available. The equity/ETF underlyings could be backtested;
  the options themselves could not.
- **Seven days does not separate skill from luck.** A week of paper trading
  on a strategy that sells 30-45 DTE premium is, statistically, close to
  noise. Any P&L shown for the contest window should be read as "the loop
  ran and didn't break," not "the strategy works."
- **The contracts are 30-45 DTE: none of them expire inside the contest
  window.** That means the week's P&L is mark-to-market and theta decay,
  not a closed, realized result. A put sold on day one is still open, at
  whatever the market currently prices it, when the contest ends — it has
  not been assigned, expired, or bought back.
- **The production contract-chain selector is not included here.** The real
  selection logic (screening for ~30-45 DTE puts around a target delta,
  filtered for spread) is proprietary to the private codebase this project
  was extracted from, and stays there. What's public is the part that is
  genuinely reusable on its own: the risk gates, the decide/act/log loop,
  and the official-CLI wrapper — each independently testable with an
  injected double, which is exactly what the test suite does.

## Evidence: the decision log

`evidence/cycles_<fecha>.jsonl` is a **snapshot**, not the live log -- the
real one grows every 15 minutes and does not belong in a public repo's
history. `cycles_2026-08-29.jsonl` was cut at **2026-08-29 (UTC)** and holds
32 cycles, one per row, each with its gate-rejection reason. **31 of those 32
rows have no timestamp**: until 29/08 only error cycles recorded one, a bug
fixed the same day (every row now gets its own `cuando`) but not
backfillable -- the missing hours are gone, not invented. Only the last row
of this snapshot carries a real `cuando`. A later snapshot, once the fixed
recorder has run for a full session, will show distinct timestamps per row.

## Repository layout

```
hackathon/
  agent.py                  the decision loop: open / roll / nothing
  executor.py                the six risk gates
  alpaca_cli.py               the official-CLI transport wrapper
  live.py                     composition root: wires the three together
  test_alpaca_cli_network.py  the one test that hits the real paper account
tests/
  test_hackathon_agent.py
  test_hackathon_executor.py
  test_hackathon_live.py
```

## License

MIT — see [LICENSE](LICENSE).
