# Phase 2: Validation & Hardening

This document outlines the steps to validate the current implementation before any live trading or scaling.

## Prerequisites
- Python 3.10+
- `pip install -r requirements.txt pytest`
- For real MCP testing: valid Robinhood agentic token (`.rh_mcp_token.json` or Claude cache)
- Run during market hours (9:30 AM - 4:00 PM ET) for meaningful data.

## 1. Full Test Suite
```bash
python -m pytest tests/ -v
```
- All tests must pass (currently ~37+ in this snapshot).
- Focus on:
  - `test_signals_math.py` (PR#3 math port)
  - `test_mcp_provider.py`
  - `test_candidates.py`, `test_risk_and_journal.py`, `test_execution_and_backtest.py`

## 2. Scanner & Data Collection Validation
```bash
python scripts/scan.py --save-snapshot --account-value 10000
```
- Verify it produces candidates using the new math (expected move, premium bands, etc.).
- Check data_snapshots/ for complete chains (should have many more than 100 rows per expiration after pagination fix).
- Confirm EM filter is active (strikes within ~1.5x expected move).

Run daily for data collection.

## 3. Real MCP Testing (Critical)
**Setup token:**
- Use `.rh_mcp_token.json` or let it fall back to `~/.claude/.credentials.json`.

**Commands:**
```bash
python scripts/scan.py --provider mcp --save-snapshot --account-value 5000
python scripts/paper_trade.py status
```

**Verify:**
- Full chains (hundreds of strikes straddling spot).
- Live quotes with proper bid/ask, OI, volume, IV.
- No truncation (strikes well above and below spot).
- After-hours: bids drop to 0 → no qualifying trades (correct behavior).
- Agent prompt preflight and regime filters work when used with MCP tools.

## 4. Paper Trading Loop (Minimum 40+ trades recommended)
Daily workflow:
1. `python scripts/paper_trade.py settle`
2. `python scripts/scan.py --save-snapshot --provider mcp`
3. Review candidates, use `python scripts/paper_trade.py open ...`
4. Manage with `status`, `close`, log no-trade days.
5. Weekly: `python scripts/paper_trade.py stats`

**Success gates (from original design):**
- Positive expectancy after costs.
- Realized win rate reasonably close to entry p_win.
- Max drawdown within tolerance.
- At least 30-40 closed trades before considering scaling.

## 5. Backtesting
```bash
python scripts/backtest.py
```
- Uses collected snapshots.
- Entry at mid + slippage, settle at intrinsic on expiry.
- Compare results to modeled EV.

Note: yfinance settlement data may be limited for very recent/future dates.

## 6. Small Issues / Polish to Address
- ATM straddle extraction in candidates.py (currently improved but monitor for edge cases).
- Make EM multiplier configurable (already partially done via config).
- Add more robust error handling / logging for MCP calls.
- Consider adding pytest to requirements or CI matrix.
- Token security: ensure `.rh_mcp_token.json` is in .gitignore (it is).

## 7. Go-Live Readiness Checklist
- [ ] 60+ days of snapshots collected.
- [ ] Backtest shows positive expectancy after costs.
- [ ] 40+ closed paper trades with positive expectancy and acceptable drawdown.
- [ ] Full MCP testing completed during market hours.
- [ ] Human confirmation step is enforced in workflow.
- [ ] All tests passing.
- [ ] CI green on GitHub.

## Next After Phase 2
- Merge any validation fixes.
- Begin small live paper or very small real size only after gates passed.
- Monitor journal weekly for divergence between modeled and realized.

**Remember:** This project makes no profitability claims. Profitability must be proven with data. Nothing here is financial advice.
