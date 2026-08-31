"""hackathon/dashboard.py -- the contest's "Application URL" / demo platform.

The agent is a background loop with nothing to click; the contest submission
form still asks for a URL and a demo. This is the minimal thing that answers
that ask: ONE read-only HTML page, served locally, that PRESENTS what the
agent already produces. It computes nothing new. Its two sources are:

    logs/hackathon_cycles.jsonl   one row per cycle (hackathon.agent.cycle /
                                   hackathon.live write it)
    hackathon.alpaca_cli.AlpacaCLI  the official CLI wrapper: account(),
                                   positions()

Stdlib only (http.server). The page is static and read-only -- no forms, no
state, no capabilities -- so pulling in fastapi/uvicorn would be a dependency
this file does not need.

This module imports nothing beyond the standard library and its sibling
`hackathon.alpaca_cli`. That is deliberate: the dashboard has to be startable
on a bare host with no private configuration available to it.
"""
from __future__ import annotations

import argparse
import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

from hackathon.alpaca_cli import AlpacaCLI

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "hackathon_cycles.jsonl"

# The six risk gates enforced by hackathon.executor.Executor.execute(). Two of
# them reject under more than one reason_code and are shown here as ONE gate,
# matching how the contest brief talks about "risk gates":
#   CONCENTRACION_SUBYACENTE + CONCENTRACION_VENCIMIENTO -> CONCENTRACION
#   LIQUIDEZ_INTERES_ABIERTO + LIQUIDEZ_VOLUMEN          -> LIQUIDEZ
GATE_NAMES = ["CAPITAL", "CONCENTRACION", "SPREAD", "LIQUIDEZ", "HORARIO", "IDEMPOTENCIA"]

# The minimum detectable effect of our own 20-year paper-trading series
# measured against a SPY benchmark: the smallest Sharpe difference a sample
# this size could resolve at all. It is pasted here as a constant, NOT
# recomputed -- this file must stay standalone. Value as of 2026-08.
MDE_VS_SPY = 0.7658

# Alpaca paper accounts open at exactly $100,000 (verified against this
# hackathon account: `alpaca account get` on 2026-08-28 showed
# created_at "2026-08-27..." with equity/cash both "100000"). Used only to
# turn `equity` into a P&L figure for display -- a subtraction against a
# known constant, not a new calculation over our data.
STARTING_EQUITY = 100_000.0

DECISIONS_SHOWN = 30


# ---------------------------------------------------------------------------
# Reading logs/hackathon_cycles.jsonl
# ---------------------------------------------------------------------------

def read_cycles(path: Path) -> tuple[list[dict], int]:
    """Reads the cycle log. A missing or empty file is NOT an error -- it
    means the agent has not completed a cycle yet, which is a fact worth
    showing, not a crash. A line that is not valid JSON (or not a JSON
    object) is skipped, and counted, rather than taking the whole page
    down."""
    if not path.exists():
        return [], 0
    rows: list[dict] = []
    skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(row, dict):
            skipped += 1
            continue
        rows.append(row)
    return rows, skipped


def _gate_family(code: str) -> Optional[str]:
    code = code.strip()
    for name in GATE_NAMES:
        if code == name or code.startswith(name + "_"):
            return name
    return None


def count_gate_rejections(rows: list[dict]) -> dict[str, int]:
    """Counts rejections per gate family. Looks at `puerta_que_rechazo`
    first (the key name hackathon.agent.cycle()'s RETURN value uses) and
    falls back to `puerta` (the key name it actually WRITES to the jsonl
    today, e.g. {"puerta": "LIQUIDEZ_VOLUMEN: 1"}) -- reading both keeps this
    working whichever one a given row happens to carry."""
    counts = {name: 0 for name in GATE_NAMES}
    for row in rows:
        raw = row.get("puerta_que_rechazo") or row.get("puerta")
        if not raw:
            continue
        code = str(raw).split(":", 1)[0]
        family = _gate_family(code)
        if family:
            counts[family] += 1
    return counts


def _row_time(row: dict) -> str:
    for key in ("cuando", "timestamp", "hora"):
        value = row.get(key)
        if value:
            return str(value)
    return "time unknown"


def recent_decisions(rows: list[dict], limit: int = DECISIONS_SHOWN) -> list[dict]:
    """Most recent first. `rows` is in file (= chronological append) order."""
    return list(reversed(rows[-limit:]))


# ---------------------------------------------------------------------------
# Reading the account, through the official CLI wrapper only
# ---------------------------------------------------------------------------

def safe_account(cli: Any) -> tuple[Optional[dict], Optional[str]]:
    try:
        return cli.account(), None
    except Exception as exc:  # noqa: BLE001 -- a display page must not crash
        return None, str(exc)


def safe_positions(cli: Any) -> tuple[list[dict], Optional[str]]:
    try:
        return cli.positions(), None
    except Exception as exc:  # noqa: BLE001 -- a display page must not crash
        return [], str(exc)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_STYLE = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 1.25rem 1rem 3rem;
  background: #0b0f14; color: #e6e9ef;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  line-height: 1.45;
}
main { max-width: 640px; margin: 0 auto; }
h1 { font-size: 1.15rem; font-weight: 600; margin: 0 0 0.25rem; color: #f4f6f9; }
.subtitle { color: #8b93a1; font-size: 0.85rem; margin: 0 0 1.5rem; }
section { margin-bottom: 1.75rem; }
section > h2 {
  font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em;
  color: #8b93a1; margin: 0 0 0.6rem; font-weight: 600;
}
.headline-box {
  background: #131a24; border: 1px solid #24303f; border-radius: 10px;
  padding: 1rem 1.1rem;
}
.headline-figures { display: flex; flex-wrap: wrap; gap: 1.5rem; margin-bottom: 0.6rem; }
.figure .label { display: block; font-size: 0.72rem; color: #8b93a1; }
.figure .value { display: block; font-size: 1.4rem; font-weight: 700; }
.value.positive { color: #4ade80; }
.value.negative { color: #f87171; }
.value.neutral { color: #e6e9ef; }
.headline-note { font-size: 0.82rem; color: #b6bdc9; margin: 0; }
table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
th, td { text-align: left; padding: 0.4rem 0.5rem; border-bottom: 1px solid #1c2531; }
th { color: #8b93a1; font-weight: 600; font-size: 0.72rem; text-transform: uppercase; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.decision-row .time { color: #8b93a1; font-size: 0.78rem; white-space: nowrap; }
.decision-row .tag {
  display: inline-block; padding: 0.05rem 0.4rem; border-radius: 4px;
  font-size: 0.72rem; font-weight: 600; text-transform: uppercase;
}
.tag-nada { background: #1c2531; color: #8b93a1; }
.tag-abrir, .tag-rodar { background: #133a2b; color: #4ade80; }
.tag-error { background: #3a1616; color: #f87171; }
.empty { color: #8b93a1; font-style: italic; font-size: 0.9rem; }
.error-note { color: #f0b429; font-size: 0.85rem; }
.skip-note { color: #8b93a1; font-size: 0.78rem; margin-top: 0.4rem; }
footer { color: #4b5563; font-size: 0.72rem; text-align: center; margin-top: 2rem; }
@media (max-width: 420px) {
  .headline-figures { gap: 1rem; }
  .figure .value { font-size: 1.2rem; }
}
"""


def _fmt_money(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


def _headline_html(account: Optional[dict], account_error: Optional[str]) -> str:
    if account is not None:
        try:
            equity = float(account.get("equity", STARTING_EQUITY))
            pnl = equity - STARTING_EQUITY
            pct = (pnl / STARTING_EQUITY) * 100 if STARTING_EQUITY else 0.0
            css_class = "positive" if pnl > 0 else ("negative" if pnl < 0 else "neutral")
            pnl_value = f"{_fmt_money(pnl)} ({pct:+.2f}%)"
        except (TypeError, ValueError):
            css_class, pnl_value = "neutral", "P&L unavailable (bad account data)"
    else:
        css_class = "neutral"
        reason = html.escape(account_error or "unknown error")
        pnl_value = f"P&L unavailable &mdash; account data unavailable: {reason}"

    return f"""
    <section id="headline">
      <h2>Headline</h2>
      <div class="headline-box">
        <div class="headline-figures">
          <div class="figure">
            <span class="label">Account P&amp;L (paper)</span>
            <span class="value {css_class}">{pnl_value}</span>
          </div>
          <div class="figure">
            <span class="label">MDE vs. SPY</span>
            <span class="value neutral">{MDE_VS_SPY}</span>
          </div>
        </div>
        <p class="headline-note">
          Seven days of paper trading cannot tell skill from luck, so we
          publish both numbers side by side: the P&amp;L this account
          produced, and the minimum detectable effect (MDE) our own
          methodology requires before we would call any result real.
        </p>
      </div>
    </section>
    """


def _gates_html(counts: dict[str, int]) -> str:
    rows = "\n".join(
        f'<tr><td>{html.escape(name)}</td><td class="num">{count}</td></tr>'
        for name, count in counts.items()
    )
    return f"""
    <section id="gates">
      <h2>Risk gates (rejections so far)</h2>
      <table>
        <thead><tr><th>Gate</th><th class="num">Rejections</th></tr></thead>
        <tbody>
        {rows}
        </tbody>
      </table>
    </section>
    """


_DECISION_TAG_CLASS = {
    "nada": "tag-nada", "abrir": "tag-abrir", "rodar": "tag-rodar",
    "error": "tag-error", "rechazada": "tag-error",
}


def _decisions_html(rows: list[dict], skipped: int) -> str:
    decisions = recent_decisions(rows)
    if not rows:
        body = '<p class="empty">No cycles recorded yet.</p>'
    else:
        items = []
        for row in decisions:
            decision = str(row.get("decision", "?"))
            tag_class = _DECISION_TAG_CLASS.get(decision, "tag-nada")
            motivo = html.escape(str(row.get("motivo", "")))
            when = html.escape(_row_time(row))
            items.append(
                '<tr class="decision-row">'
                f'<td class="time">{when}</td>'
                f'<td><span class="tag {tag_class}">{html.escape(decision)}</span></td>'
                f'<td>{motivo}</td>'
                "</tr>"
            )
        body = (
            "<table><thead><tr><th>Time</th><th>Decision</th><th>Reason</th>"
            f"</tr></thead><tbody>{''.join(items)}</tbody></table>"
        )
    skip_note = (
        f'<p class="skip-note">{skipped} malformed line(s) in the log were '
        "skipped.</p>" if skipped else ""
    )
    return f"""
    <section id="decisions">
      <h2>Last {DECISIONS_SHOWN} decisions (including "do nothing")</h2>
      {body}
      {skip_note}
    </section>
    """


def _positions_html(account: Optional[dict], account_error: Optional[str],
                     positions: list[dict], positions_error: Optional[str]) -> str:
    account_number = html.escape(str(account.get("account_number"))) if account else None
    account_line = (
        f'<p>Account: <strong>{account_number}</strong></p>' if account_number
        else f'<p class="error-note">Account unavailable: '
             f'{html.escape(account_error or "unknown error")}</p>'
    )

    if positions_error:
        body = (f'<p class="error-note">Positions unavailable: '
                 f'{html.escape(positions_error)}</p>')
    elif not positions:
        body = '<p class="empty">No open positions.</p>'
    else:
        rows = []
        for pos in positions:
            rows.append(
                "<tr>"
                f'<td>{html.escape(str(pos.get("symbol", "?")))}</td>'
                f'<td class="num">{html.escape(str(pos.get("qty", "?")))}</td>'
                f'<td>{html.escape(str(pos.get("side", "?")))}</td>'
                f'<td class="num">{html.escape(str(pos.get("market_value", "?")))}</td>'
                f'<td class="num">{html.escape(str(pos.get("unrealized_pl", "?")))}</td>'
                "</tr>"
            )
        body = (
            "<table><thead><tr><th>Symbol</th><th class=\"num\">Qty</th>"
            "<th>Side</th><th class=\"num\">Market value</th>"
            "<th class=\"num\">Unrealized P&amp;L</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    return f"""
    <section id="positions">
      <h2>Open positions</h2>
      {account_line}
      {body}
    </section>
    """


_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>TradeHub hackathon agent -- live status</title>
<style>{style}</style>
</head>
<body>
<main>
  <h1>TradeHub hackathon agent</h1>
  <p class="subtitle">
    Autonomous options paper-trading agent (Alpaca). This page only reads
    what the agent already writes -- it computes nothing new.
  </p>
  {headline}
  {gates}
  {decisions}
  {positions}
  <footer>Read-only status page. Refreshes every 60 seconds.</footer>
</main>
</body>
</html>
"""


def render_page(*, log_path: Path = LOG_PATH, cli: Any) -> str:
    """Builds the whole page. `cli` is anything with .account() and
    .positions() -- normally a hackathon.alpaca_cli.AlpacaCLI, a double in
    tests."""
    rows, skipped = read_cycles(log_path)
    gate_counts = count_gate_rejections(rows)
    account, account_error = safe_account(cli)
    positions, positions_error = safe_positions(cli)

    return _PAGE_TEMPLATE.format(
        style=_STYLE,
        headline=_headline_html(account, account_error),
        gates=_gates_html(gate_counts),
        decisions=_decisions_html(rows, skipped),
        positions=_positions_html(account, account_error, positions, positions_error),
    )


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

def build_handler(log_path: Path, cli_factory: Callable[[], Any]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 -- stdlib method name
            if self.path.split("?", 1)[0] not in ("/", "/index.html"):
                self.send_response(404)
                self.end_headers()
                return
            try:
                body = render_page(log_path=log_path, cli=cli_factory()).encode("utf-8")
                status = 200
            except Exception as exc:  # noqa: BLE001 -- last-resort net catch
                body = f"dashboard error: {exc}".encode("utf-8")
                status = 500
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass  # keep stdout quiet; nothing here is worth logging per-request

    return Handler


def serve(port: int, *, host: str = "127.0.0.1", log_path: Path = LOG_PATH,
          alpaca_executable: str = "alpaca") -> None:
    cli_factory = lambda: AlpacaCLI(alpaca_executable)  # noqa: E731
    handler = build_handler(log_path, cli_factory)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"hackathon dashboard: http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only demo page for the hackathon agent (local only).")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address; 127.0.0.1 by default (local only, on purpose)")
    parser.add_argument("--alpaca-executable", default="alpaca")
    args = parser.parse_args(argv)
    serve(args.port, host=args.host, alpaca_executable=args.alpaca_executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
