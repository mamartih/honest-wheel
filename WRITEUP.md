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

## P&L, as of 3 Sep 2026

The page promises a P&L next to its error bar, so here is both.

```
equity            $100,202.91      +$202.91   ·   +0.20%
open position     SPY 746 PUT, exp. 2026-10-02, 1 contract short
  premium taken   $536
  current value   $333
  unrealised      +$203
cycles            513, each one logged with its decision and reason
trades            1 opened, 0 closed
MDE vs SPY        0.7658 Sharpe
```

**+0.20% over four sessions. That is inside the noise band and we are not
going to pretend otherwise.** With a minimum detectable effect of 0.7658
Sharpe, a week cannot tell this apart from luck — it would look the same if
the strategy had no edge at all. The number is real; what it is *evidence of*
is nothing yet.

**The agent traded once, and then spent 202 cycles being told it could not
trade again.** A cash-secured put on SPY withholds $74,600 of collateral, which
is three quarters of the account, so nothing else fit. That is not a
malfunction — it is what a fully collateralised put costs — but it does mean
this week's sample is one position, not a strategy.

Two things happened in those four days that are worth more than the P&L:

- **The broker rejected an order our own CAPITAL gate had approved** (403,
  "insufficient options buying power"). The gate was reading the marginable
  equity figure instead of the non-marginable one. Fixed the same day, with the
  regression test written first — and fixing it exposed that three CLI test
  doubles returned an account object missing that field, which is why a green
  suite had never caught it.
- **The LLM advisor ran inside the live loop for the first time**, logged as
  `consulted: true, veto: false` on the cycle that opened the position. It has
  been consulted twice: once per candidate that survived every deterministic
  gate.

Both are in `evidence/` and in the commit history, timestamped.

---

## What we would do with another week

Measure the option leg. We have no historical options chains, so nothing here —
including the wheel itself — has a backtest. That is the honest state, and no
amount of a good week changes it.
