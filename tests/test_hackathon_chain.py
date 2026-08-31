"""tests for hackathon/chain.py -- contract selection via the official CLI only.

Doubles only, no network. `run_cli` is injected as a callable
`(list[str]) -> str` that mimics the raw stdout of `alpaca ...` commands, so
these tests never touch subprocess or the real CLI binary.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from hackathon.chain import select

TODAY = date.today()
EXPIRY_INSIDE = TODAY + timedelta(days=35)   # inside the 30-45 DTE window
EXPIRY_OUTSIDE = TODAY + timedelta(days=10)  # outside the window


def _symbol(underlying: str, expiry: date, option_type: str, strike: float) -> str:
    """Build an OCC option symbol, e.g. SPY260930P00600000."""
    yy = expiry.strftime("%y")
    mm = expiry.strftime("%m")
    dd = expiry.strftime("%d")
    strike_raw = f"{round(strike * 1000):08d}"
    return f"{underlying}{yy}{mm}{dd}{option_type}{strike_raw}"


def _snapshot(delta: float, bid: float, ask: float, daily_volume: int,
              last_trade_volume: int = 1) -> dict:
    return {
        "dailyBar": {"v": daily_volume},
        "greeks": {"delta": delta},
        "latestQuote": {"bp": bid, "ap": ask},
        "latestTrade": {"s": last_trade_volume},
    }


class RunCLIDouble:
    """Fake `run_cli(arguments) -> str` that answers chain/get calls from
    canned data instead of touching subprocess or the network."""

    def __init__(self, chains: dict[str, dict] | None = None,
                 oi: dict[str, str] | None = None,
                 failures: frozenset[str] = frozenset(),
                 pages: dict[str, list[dict]] | None = None):
        self.chains = chains or {}
        self.oi = oi or {}
        self.failures = failures
        # underlying -> list of successive page payloads (for pagination test)
        self.pages = pages or {}
        self.calls: list[list[str]] = []
        self._current_page: dict[str, int] = {}

    def __call__(self, arguments: list[str]) -> str:
        self.calls.append(list(arguments))
        if "chain" in arguments:
            underlying = arguments[arguments.index("--underlying-symbol") + 1]
            if underlying in self.failures:
                raise RuntimeError(f"simulated CLI failure for {underlying}")
            if underlying in self.pages:
                idx = self._current_page.get(underlying, 0)
                page = self.pages[underlying][idx]
                self._current_page[underlying] = idx + 1
                return json.dumps(page)
            snapshots = self.chains.get(underlying, {})
            return json.dumps({"next_page_token": "", "snapshots": snapshots})
        if "get" in arguments:
            symbol = arguments[arguments.index("--symbol-or-id") + 1]
            return json.dumps({"symbol": symbol,
                               "open_interest": self.oi.get(symbol, "0")})
        raise AssertionError(f"unexpected CLI call: {arguments}")


# --- (a) closest to -0.25 delta among those inside the DTE window -----------

def test_picks_the_delta_closest_to_target_inside_the_window():
    far_symbol = _symbol("SPY", EXPIRY_INSIDE, "P", 550)    # delta -0.10, far
    near_symbol = _symbol("SPY", EXPIRY_INSIDE, "P", 600)   # delta -0.25, exact
    chains = {"SPY": {
        far_symbol: _snapshot(-0.10, 4.90, 5.00, 300),
        near_symbol: _snapshot(-0.25, 5.90, 6.00, 300),
    }}
    double = RunCLIDouble(chains=chains, oi={near_symbol: "4200", far_symbol: "4200"})

    result = select(["SPY"], run_cli=double)

    assert len(result) == 1
    assert result[0]["symbol"] == near_symbol
    assert result[0]["delta"] == pytest.approx(-0.25)


# --- (b) spread > 3% is discarded even with the best delta ------------------

def test_discards_by_spread_even_when_the_delta_is_the_best(capsys):
    best_delta_bad_spread = _symbol("SPY", EXPIRY_INSIDE, "P", 600)   # -0.245
    worse_delta_good_spread = _symbol("SPY", EXPIRY_INSIDE, "P", 590)  # -0.30
    chains = {"SPY": {
        best_delta_bad_spread: _snapshot(-0.245, 1.00, 1.10, 300),    # 9.5% spread
        worse_delta_good_spread: _snapshot(-0.30, 1.00, 1.02, 300),   # 1.98% spread
    }}
    double = RunCLIDouble(chains=chains, oi={
        best_delta_bad_spread: "4200", worse_delta_good_spread: "4200"})

    result = select(["SPY"], run_cli=double)

    assert len(result) == 1
    assert result[0]["symbol"] == worse_delta_good_spread
    output = capsys.readouterr().err
    assert "SPREAD" in output, "el motivo del descarte por spread no se puede consultar"
    assert best_delta_bad_spread in output


# --- (c) volume comes from dailyBar.v, NOT latestTrade.s --------------------

def test_volume_comes_from_dailybar_v_not_latesttrade_s():
    symbol = _symbol("SPY", EXPIRY_INSIDE, "P", 600)
    chains = {"SPY": {
        symbol: _snapshot(-0.25, 5.90, 6.00, daily_volume=800,
                          last_trade_volume=1),
    }}
    double = RunCLIDouble(chains=chains, oi={symbol: "4200"})

    result = select(["SPY"], run_cli=double)

    assert len(result) == 1
    assert result[0]["volume"] == 800, (
        "volume tiene que salir de dailyBar.v, no de latestTrade.s -- "
        "este es el error exacto que tumbo los ciclos del 28/08")


# --- (d) a contract outside the DTE window is never chosen ------------------

def test_a_contract_outside_the_dte_window_is_never_chosen():
    outside_perfect = _symbol("SPY", EXPIRY_OUTSIDE, "P", 600)  # delta -0.25, DTE=10
    inside_ok = _symbol("SPY", EXPIRY_INSIDE, "P", 580)         # delta -0.20, DTE=35
    chains = {"SPY": {
        outside_perfect: _snapshot(-0.25, 5.90, 6.00, 300),
        inside_ok: _snapshot(-0.20, 4.90, 5.00, 300),
    }}
    double = RunCLIDouble(chains=chains, oi={outside_perfect: "4200", inside_ok: "4200"})

    result = select(["SPY"], run_cli=double)

    assert len(result) == 1
    assert result[0]["symbol"] == inside_ok


# --- (e) a CALL is never chosen ----------------------------------------------

def test_a_call_is_never_chosen():
    # delta forced to -0.25 on purpose -- if the filter depended only on delta
    # (and not on the type parsed from the OCC symbol), this call would pass.
    call_with_perfect_delta = _symbol("SPY", EXPIRY_INSIDE, "C", 600)
    acceptable_put = _symbol("SPY", EXPIRY_INSIDE, "P", 580)  # delta -0.20
    chains = {"SPY": {
        call_with_perfect_delta: _snapshot(-0.25, 5.90, 6.00, 300),
        acceptable_put: _snapshot(-0.20, 4.90, 5.00, 300),
    }}
    double = RunCLIDouble(chains=chains, oi={
        call_with_perfect_delta: "4200", acceptable_put: "4200"})

    result = select(["SPY"], run_cli=double)

    assert len(result) == 1
    assert result[0]["symbol"] == acceptable_put


# --- (f) one underlying failing doesn't take down the rest ------------------

def test_a_failing_underlying_does_not_take_down_the_sweep(capsys):
    ok_symbol = _symbol("QQQ", EXPIRY_INSIDE, "P", 400)
    chains = {"QQQ": {ok_symbol: _snapshot(-0.25, 3.90, 4.00, 300)}}
    double = RunCLIDouble(chains=chains, oi={ok_symbol: "1000"},
                          failures=frozenset({"SPY"}))

    result = select(["SPY", "QQQ"], run_cli=double)

    assert len(result) == 1
    assert result[0]["symbol"] == ok_symbol
    output = capsys.readouterr().err
    assert "SPY" in output, "el fallo de SPY tiene que quedar registrado, no tragado en silencio"


# --- shape: exactly the keys executor.py / agent.py already consume ---------

def test_the_candidate_shape_has_exactly_the_expected_keys():
    symbol = _symbol("SPY", EXPIRY_INSIDE, "P", 600)
    chains = {"SPY": {symbol: _snapshot(-0.25, 5.90, 6.00, 300)}}
    double = RunCLIDouble(chains=chains, oi={symbol: "4200"})

    result = select(["SPY"], run_cli=double)

    assert len(result) == 1
    assert set(result[0]) == {
        "symbol", "underlying", "expiry", "strike", "delta",
        "bid", "ask", "open_interest", "volume"}
    assert result[0]["open_interest"] == 4200
    assert result[0]["underlying"] == "SPY"
    assert result[0]["strike"] == 600.0


# --- bonus: pagination is followed until next_page_token is empty -----------

def test_it_pages_until_next_page_token_is_empty():
    page_1 = _symbol("SPY", EXPIRY_INSIDE, "P", 550)  # far delta, page 1
    page_2 = _symbol("SPY", EXPIRY_INSIDE, "P", 600)  # exact delta, page 2
    pages = {"SPY": [
        {"next_page_token": "abc123", "snapshots": {
            page_1: _snapshot(-0.10, 4.90, 5.00, 300)}},
        {"next_page_token": "", "snapshots": {
            page_2: _snapshot(-0.25, 5.90, 6.00, 300)}},
    ]}
    double = RunCLIDouble(oi={page_1: "4200", page_2: "4200"}, pages=pages)

    result = select(["SPY"], run_cli=double)

    assert len(result) == 1
    assert result[0]["symbol"] == page_2, "no siguio a la segunda pagina"
    chain_calls = [c for c in double.calls if "chain" in c]
    assert len(chain_calls) == 2
    assert "--page-token" in chain_calls[1]


# --- the credentials guard, 29/08 --------------------------------------------

def test_it_does_not_fall_back_to_generic_credentials():
    """On the VPS the production credentials live alongside these. If the
    agent inherited ALPACA_API_KEY it would send options into the real bot's
    account."""
    import pytest
    from hackathon.alpaca_cli import AlpacaCLIError, _environment

    generic_only = {"ALPACA_API_KEY": "de-produccion",
                    "ALPACA_SECRET_KEY": "de-produccion"}
    with pytest.raises(AlpacaCLIError) as err:
        _environment(generic_only)
    assert "HACKATON" in str(err.value)


def test_with_dedicated_credentials_it_resolves_and_drops_live_trade():
    from hackathon.alpaca_cli import _environment

    env = _environment({"ALPACA_HACKATON_API_KEY": "k",
                        "ALPACA_HACKATON_SECRET_KEY": "s",
                        "ALPACA_LIVE_TRADE": "1"})
    assert env["ALPACA_API_KEY"] == "k"
    assert "ALPACA_LIVE_TRADE" not in env
