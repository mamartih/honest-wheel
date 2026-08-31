# honest-wheel — one-page write-up

An autonomous cash-secured-put agent on Alpaca that **publishes its P&L next to
the smallest effect seven days could detect**.

---

## The claim we are NOT making

Seven trading days cannot tell skill from luck. That is not modesty, it is
arithmetic, and we can put a number on it: measured against SPY on our own
20-year portfolio series, the **minimum detectable effect is 0.7658 Sharpe**.
A week of paper trading resolves nothing close to that.

So this agent reports its P&L **and, beside it, the effect size the week could
not have detected anyway**. If it makes money, that is not evidence it works.
If it loses money, that is not evidence it does not. We say so on the dashboard,
in the log, and here.

Every other honest submission is in the same position. The difference is that
we wrote the number down.

---

## AI logic

**The model can only VETO. It can never authorise.**

The decision to open a position is made by deterministic code from a
pre-registered rule. The model is then handed the candidate contract and asked
one question: *is there a reason not to sell this put?* A "yes" blocks the
trade and the reason is recorded. A "no" changes nothing — the trade was
already going to happen.

It **fails open**. No API key, a timeout, an unparseable answer: the cycle
continues and the record says the advisor did not opine. An advisor that halts
the agent when it breaks is worse than no advisor.

This is the same asymmetry we run in production, and it exists because a
language model's confident wrong answer costs more than its correct one gains.
Giving it the power to say *no* uses its pattern-matching where a false positive
is cheap. Giving it the power to say *yes* would put it where a false positive
is expensive.

Inference runs on **Featherless AI**, serverless open-model inference:
`Qwen/Qwen2.5-7B-Instruct`, one POST per surviving candidate, `temperature: 0`,
capped at 150 tokens. Every cycle records `consulted: true|false` whether or not
a veto followed — otherwise "the advisor approved" and "the advisor never ran"
would look identical in the log, and this page would be unfalsifiable.

---

## The entry rule, and where it comes from

**Turn-of-month on liquid US ETFs.** Two trading days before month end through
three after. When the window is open on an underlying, the agent looks for a
put to sell.

Two things about that rule matter more than the rule itself:

**It is older than this contest.** It runs in our production portfolio on two
slots and it went through a 20-25 year out-of-sample process before it ever
placed an order. We did not fit a strategy to this week.

**And we chose it, in part, because it fires this week.** Its window covers
31 Aug through 3 Sep — four of the five contest sessions. That is a
demonstrability decision, not a performance claim. Picking among rules that
were *already* pre-registered does not overfit the rule, but it does overfit
the choice, and the difference only counts if you say it out loud.

A second, independent condition has to hold: the underlying must be above its
200-day average. That one has external support — Yang (SSRN) measures 22.6
years of regime-conditioned index put-writing and reports improvement over
passive put-writing. **It is also not evaluable in seven days**: that work sees
21 signal changes in 22.6 years, roughly one every thirteen months. Over one
week it is a constant. It is in because it is defensible, not because this week
will say anything about it.

**What the backtest does NOT cover:** the options leg. The out-of-sample record
is for the equity expression of the signal. Spread, theta, gamma and assignment
change the payoff entirely. The rule chooses *when to look*. It promises
nothing about the result.

---

## Risk gates

Six deterministic gates, in this order. Each one **records its reason**, and the
agent stops at the first rejection and names it.

| gate | rejects when |
|---|---|
| `CAPITAL` | cash below strike × 100 × contracts. A cash-secured put that is not cash-secured is a naked put |
| `CONCENTRACION_SUBYACENTE` | too many contracts on one underlying |
| `CONCENTRACION_VENCIMIENTO` | too many contracts on one expiry |
| `SPREAD` | bid-ask above 3% of the midpoint |
| `LIQUIDEZ_*` | open interest or **daily volume** below floor |
| `HORARIO` | outside regular session |
| `IDEMPOTENCIA` | a position already exists for that underlying and expiry |

The advisor is a seventh gate and it goes **last**: cheap, certain checks run
before a network call is spent on a contract that capital or liquidity has
already rejected.

Two of these gates were broken and the tests did not see it. Both are worth
naming because the fix is the interesting part:

- **Concentration summed the sign of `qty`.** A sold put arrives with a
  negative quantity, and the whole strategy is sold puts, so two short SPY
  positions summed to −2 and the gate never tripped. The tests missed it
  because their fixtures had no `qty` at all.
- **Liquidity measured the size of the last trade, not the day's volume.** A
  contract traded 4,000 times and one traded once both reported "1". The first
  live cycle died on that gate with volume 1. It now reads the daily bar.

**Doing nothing is a decision and it is recorded like any other.** An agent
that only writes to its log when it trades looks identical whether it is idle
or broken.

---

## Alpaca infrastructure

Everything goes through the **official Alpaca CLI** — account, positions, option
chain, order submission. No hand-rolled HTTP client and no SDK on the trading
path.

That was not the first design. The chain selection originally used the Python
SDK across three calls. Then we read the CLI's help and found
`alpaca data option chain` returns greeks, quote and daily volume in **one**
call, with server-side filtering by type and expiry window — 661 SPY contracts
in a single page. One call replaced three, and the contest's own requirement is
satisfied by the thing actually doing the work rather than by a wrapper added
to satisfy it.

The account is a brand-new dedicated paper account. The agent **refuses to run
against any other**: it compares the account number the CLI reports against a
declared value and stops if they differ. It also refuses to fall back to
generic credentials — on a host that also runs a production bot, that fallback
would be a loaded gun.

---

## P&L, as of 31 Aug 2026

The page promises a P&L next to its error bar, so here is both.

```
equity      $100,000.00     starting balance, unchanged
positions   0
orders      0
cycles      183, each one logged with its decision and reason
MDE vs SPY  0.7658 Sharpe
```

**No trades yet, and the reason is in the log rather than in a paragraph.**
`turn_of_month` evaluates the last *completed* daily bar. Through 30 Aug that
bar was Friday 28 Aug — one day short of the window, which opens on the second
market day before month end. Every one of the 183 cycles recorded
`sin candidatos: ningun subyacente paso el filtro`, with the failing leg named
per underlying.

This section is rewritten from the live account before submission. If the agent
trades, the P&L goes here beside the 0.7658. **If it never trades, that goes
here unedited too.** An agent that declines when its conditions are absent is
working; an agent that relaxes them to have a number to show is not, and this
whole page would be worthless coming from the second one.

---

## What we would do with another week

Measure the option leg. We have no historical options chains, so nothing here —
including the wheel itself — has a backtest. That is the honest state, and no
amount of a good week changes it.
