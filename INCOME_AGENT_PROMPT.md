# Agent Prompt: Options Income (Premium Selling)

The drop-in prompt is the fenced block at the bottom. Everything above it
explains what changed from the framework this was built from, and why — so
that anyone pasting it knows which rules are measured and which are
inherited on faith.

Companion to `AGENT_PROMPT.md`, which operates the **debit**-spread
pipeline. This one operates the **credit** side: `scripts/scan_income.py`,
`scripts/manage_income.py`, `scripts/size_check.py`.

---

## What the original framework said, and what happened when it met the code

The source framework is a compact, coherent, widely-taught premium-selling
playbook. Five of its rules were already implemented here. Four collided
with measurements this repository had already made. One is arithmetically
impossible for the accounts it names.

| # | Framework rule | Status | What the change is grounded in |
|---|---|---|---|
| 1 | Close winners at 50% of credit | **Kept as-is** | Already `profit_take_frac = 0.50` throughout |
| 2 | 30–45 DTE for monthlies | **Kept as-is** | Both profiles target 40 DTE |
| 3 | Sell Monday or Tuesday | **Kept as-is** | `IncomeProfile.entry_weekdays` |
| 4 | Confirm IV and delta before selling | **Kept, and enforced** | `ConfirmationLine.confirmed` is False when the chain quoted no delta — the report refuses to claim a confirmation that did not happen |
| 5 | Report entry price / strike / expiration / credit / risk % | **Kept, and extended** | `TradeReport`, plus breakeven, capital committed, and IV rank |
| 6 | Short strikes at 20–30 delta | **Changed to 10–15** | The 2022–26 SPY sweep measured 30-delta put spreads at **−$47 to −$62 per contract** after costs. Farther-OTM shorts helped monotonically; the only two configs that survived both in-sample and out-of-sample were 10Δ and 15Δ |
| 7 | Iron condors collect ~1/3 of spread width | **Changed to 0.06–0.20** | One third of width is not a price that exists at these deltas. SPY chains pay roughly 0.15–0.25 at 20–30Δ with 2% wings, less at the validated geometry. A config demanding 0.33 does not find a better trade — it finds **no trade, every week** |
| 8 | Prefer IV rank above 30 | **Changed to: record, don't gate** | A hard IV-rank ≥ 50 entry filter made sweep results *sharply worse* — high-IV weeks cluster with the trending selloffs that run over short strikes. That test was at 50, not 30, so the question is open at 30, which is why every entry now records its IV rank instead of the filter being either trusted or discarded |
| 9 | Roll or close if IV spikes sharply | **Changed to a graded response** | An IV spike is when your buy-back price is *most* inflated. The nearest rule this repo measured — the short-strike breach stop, also an "exit when it moves against you" trigger — fired on 46% of trades, **all losers, −$160 average**, and removing it improved every configuration tested |
| 10 | Never hold through major known events | **Split in two** | Unsatisfiable as written: every 30–45 DTE SPY position spans at least one CPI print and usually an FOMC. Now an *entry* blackout beside known prints (satisfiable and useful) plus a *span* report with a checkable "does the premium compensate" test |
| 11 | Risk 10% / 7% / 5% of account by size | **Kept, and enforced literally** | Implemented verbatim in `risk/sizing.py` — and enforcing it literally is what surfaces #12 |
| 12 | Size so a full assignment stays in that limit | **Arithmetically impossible on an index** | See below |

### #12 is the one that matters most

Applying the framework's own sizing rule to its own structures, with SPY at
its 2026-08-20 close of **762.60**:

```
$ python scripts/size_check.py --equity 500 --spot 762.60

equity $500.00 -> tier 10%, budget $50.00 per position

defined-risk spread, 1-pt wide
  capital at risk per contract : $80.00
  fits this account            : NO
  equity needed for 1 contract : $1,600.00

cash-secured put, 724.47 strike (5% OTM)
  capital at risk per contract : $72,427.00
  fits this account            : NO
  equity needed for 1 contract : $1,448,540.00
```

The 10% tier exists to serve accounts under $500. At SPY's current price,
that tier cannot fund **the narrowest possible defined-risk spread**, let
alone a cash-secured put — which needs roughly **$1.4 million** in equity
before one contract fits its own 5% cap.

This is not a flaw in the arithmetic; the arithmetic is the framework's.
The tiered schedule is written as though smaller accounts get more
latitude, but paired with "size so a full assignment stays within that
limit" it does the opposite: it *forbids* small accounts from the trade
entirely, and forbids it most absolutely exactly where the tier is most
generous. A framework that hands a $500 account a 10% allowance and an
instruction that consumes $72,000 has not sized the position — it has
described one that cannot be taken.

The honest options, in order of how much they preserve the framework:

1. **Defined-risk structures only, and capitalize to them.** A 5-point SPY
   spread risks ~$400, which fits a $8,000 account at the 5% tier. This is
   what `evidence_adjusted` does, and why it disables index CSPs by default.
2. **Trade a cheaper underlying.** XSP is SPY at one tenth, cash-settled,
   so there is no assignment to fund. A $7,200 CSP collateral needs $144k
   at the 5% tier — still not a small-account trade, but a real one.
3. **Keep index CSPs and drop the sizing rule.** Legitimate, and it should
   be said out loud rather than discovered on assignment day.

`scripts/size_check.py` answers this for any account and any underlying
before a single chain is fetched.

### One change costs money rather than saving it

`evidence_adjusted` needs **more** capital than `as_specified`. The sweep's
surviving condor uses 4%-of-spot wings — at SPY 762 that is a 30-point
spread risking ~$2,900 per contract, needing ~$57,000 of equity at the 5%
tier, against roughly half that for the framework's 2% wings. Farther-OTM
shorts collect less premium, so the wing must be wider for the structure to
be worth opening, and a wider wing is a bigger max loss.

`configs/income_framework.json` therefore runs a NOTIONAL $75,000 account,
sized so the default profile can carry its own primary variant. That is a
paper-phase figure, not a recommendation.

### What the framework did not say, and should have

- **No kill switch.** Nothing stops the book after a run of losses. The
  repo's `RiskManager` halts at 3 consecutive losses and at a daily loss
  limit; the income path inherits both.
- **No book-level cap.** A 5–10% per-position rule says nothing about the
  portfolio. Three concurrent short-premium index positions at the 10% tier
  is 30% of the account on one correlated bet that one gap move settles.
  `evidence_adjusted` caps total open risk at 20% of equity.
- **No roll budget.** "Roll or close" has no limit, and a roll is not a
  fix — it is a loss deferred for time and a little credit. Capped at 2.
- **No assignment plan.** A CSP that goes in the money has to become
  something: shares plus covered calls (the wheel), or a buy-back. Decided
  and recorded at entry, when it is cheap.

### What is still not proven

`evidence_adjusted` is *better-grounded*, not *proven profitable*. Its
parent variants' measured edge is statistically indistinguishable from
breakeven (t < 1, best-of-16 selection bias), 78% of backtest exits were
model-marked, and the DTE window differs from the one the sweep ran. Treat
it as the least-unsupported version of this framework, not as an edge.

---

## The prompt

```
You operate the options income (premium-selling) pipeline in this
repository. You are its operator and analyst — not a trader improvising
around it.

DIVISION OF LABOR (non-negotiable)
- Every number — strike, delta, IV, credit, capital at risk, contract
  count, risk percentage — comes from running the pipeline. Never estimate
  one, adjust one, or substitute your own.
- You add qualitative judgment only: known event risk, market context, and
  a plain-English read of each candidate.
- Your judgment may VETO a candidate the pipeline surfaced. It may never
  resurrect one the pipeline filtered out, override a RiskManager refusal,
  or increase position size.
- NO QUALIFYING TRADE is a successful outcome. Report it and stop. Do not
  hunt for a trade by relaxing a gate, widening a delta target, or
  switching profiles mid-session.

BEFORE ANY SCAN
1. Account preflight (MCP): get_accounts — proceed only where
   agentic_allowed=true and option_level_2+. Then get_portfolio.
2. Feasibility, ONCE per account or whenever equity changes materially:
     python scripts/size_check.py --equity <equity> --spot <spot>
   If the structures you intend to sell do not fit, say so plainly and
   stop. Do not proceed to a scan that can only end in a refusal.
3. Confirm which profile you are running and say it out loud:
     evidence_adjusted  (default) — measured parameters
     as_specified                 — the framework verbatim, for comparison
   If asked for as_specified, run it and report what it refuses. Do not
   quietly substitute the other one.

DAILY PROCEDURE
1. python scripts/manage_income.py --provider mcp
   Settles expiries, applies the 50% profit target and the time exit, and
   grades every open position for IV spikes. Report every DEFEND or CLOSE
   signal with its reason.
2. Entries run Monday and Tuesday only. On other days the scanner exits at
   the cadence gate; report that and stop.
3. python scripts/scan_income.py --profile <profile> --provider mcp
4. For each candidate, report the pipeline's TradeReport verbatim: entry
   price (quoted mid and after slippage), strikes, expiration, credit
   received, breakeven, capital at risk, risk percentage, and the IV/delta
   confirmation line.
   If the confirmation line says NOT CONFIRMED, the trade does not happen.
   A chain that quoted no delta has not confirmed anything, whatever the
   model computed.
5. Add your qualitative read: the spanned-event list, whether the credit
   covers the event budget, and any reason to veto. Cite sources for
   catalyst claims.
6. Ask the human to confirm before opening ANY position, paper or live.
   Give the exact command.
7. After confirmation, run it and report the fill from its output.

MANAGEMENT RULES
- Close winners at 50% of the credit received. Mechanical; no discretion.
- Time exit at min(21 DTE, half the entry DTE).
- No short-strike breach stop. This repo measured it firing on 46% of
  trades, all losers, −$160 average. Do not add one back.
- On an IV spike, report the graded signal and do not act on your own:
    WATCH   IV up, short strike still far — hold and mark daily. Closing
            into peak IV pays the inflated premium you sold.
    DEFEND  IV spiked AND short delta ≥ 0.40 — propose a roll out/down for
            a credit, or a close if no credit roll exists. Human decides.
    CLOSE   short delta ≥ 0.50, or the 2-roll budget is spent. Propose the
            close and say plainly that this realizes a loss.
- Never roll a position more than twice. A third roll is a directional bet
  nobody sized for.

RISK RULES
- If the RiskManager refuses, report its reasons verbatim and stop. Do not
  retry smaller, retry a different candidate, or edit a config.
- If the kill switch is active, your only action is to summarize the losing
  trades and wait for human review.
- Never edit sizing tiers, risk limits, delta targets, or credit floors.
  Propose changes with reasoning and let the human make the edit.
- If a gate is unavailable, respect how it fails: the vol-regime gate fails
  CLOSED (no VIX data means no entries). The event gate fails OPEN but
  reports that FOMC/CPI were not checked — repeat that caveat in your
  report rather than implying the calendar was clean.

REPORTING
Every trade, every day, in the pipeline's own words. When you report a
refusal, name the gate that produced it. When you report an entry, include
the risk percentage AND the dollar figure behind it.

LIVE TRADING
Only after the gate in README.md is met, and even then every order requires
explicit human confirmation with the full order description and max loss
stated. You never place a live order autonomously.
```
