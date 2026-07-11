# Robinhood MCP Daily Options Strategy - Full Project Handoff Plan

## Executive Summary
This document serves as a complete handoff for the ongoing development of the AI-powered daily options trading strategy using Robinhood's Agentic Trading MCP. The project focuses on defined-risk vertical debit spreads with rigorous data-driven validation, honest backtesting via point-in-time snapshots, and safe automation for data collection.

**Core Principles (non-negotiable):**
- Numbers (filters, EV, sizing, risk) come from deterministic Python code — never from the LLM.
- "NO QUALIFYING TRADE" is a valid and successful outcome.
- Prove the edge with data first: ≥60 days snapshots + positive backtest EV + ≥40 closed paper trades before live.
- Vertical spreads are preferred (defined risk); single-leg is a pragmatic fallback due to current MCP limitations.
- All data collection and backtesting must be point-in-time to avoid bias.

**Current Status (as of 2026-07-09):**
- Repo consolidated to canonical `/home/banderson/robinhood-mcp-daily-options-strategy` (workspace copy removed).
- Main contains:
  - Full vertical spreads architecture (`options_trader/` package with RiskManager, SnapshotStore, BacktestEngine, etc.).
  - MCPDataProvider (hardened with pagination, validation, OAuth fixes from PR #7).
  - Synthesis improvements from PR #3 (ported `signals/math.py`, EM filter with configurable multiplier, premium bands, no-trade logging, realistic agent prompt).
  - `--data-only` mode in `scan.py` for clean frequent collection.
  - `scripts/collect_data.sh` + cron for 45-min interval data gathering.
  - Storage fix: snapshots now use time in filename (`YYYY-MM-DD_HHMMSS_exp...`) to support intra-day multiples.
  - CI workflow (`.github/workflows/ci.yml`).
  - `PHASE2_VALIDATION.md` guide.
- Data: 44+ snapshots (including multi-per-day from 2026-07-08), runs/ logs.
- Cron active: `CRON_TZ=America/New_York` + `*/15 9-16 * * 1-5` (ET-pinned, version-controlled in `crontab.txt`); the script matches ET slots (9:00, 9:45, ..., 16:30) within a ±2 min tolerance and runs `--data-only --save-snapshot --provider mcp`.
- No open PRs with overlaps (clean PR #9 for CI+docs merged/ incorporated; old PRs closed/superseded).
- Tests: 83+ passing.
- Journal empty (pure data collection phase; no live/paper trades yet).
- Botched merge issues (conflict markers, provider regression) resolved in current main.

**Key Wins So Far:**
- Fixed truncation bug in MCP provider (full chains now, 400+ rows vs ~100).
- Enabled frequent data collection without file overwrites or report spam.
- Preserved superior verticals architecture over single-leg rewrite.
- Honest dataset building for backtesting.

## Overall Roadmap
The project follows a gated, data-first approach before any real money exposure.

### Phase 0: Foundations (Completed)
- Architecture: vertical spreads + EV after costs + hard RiskManager + SnapshotStore for point-in-time data.
- MCP integration: `MCPDataProvider` (live chains/quotes) + yfinance fallback.
- Synthesis: Merged best of PRs (verticals primary + PR#3 math/prompt/journal bits).
- Repo hygiene: Consolidated copies, cleaned old PRs/branches (#8 closed, #9 clean docs PR).
- Fixes applied: Pagination in provider, storage for multi-snapshots, --data-only flag, collect script + cron.

**Deliverables:**
- Working `scan.py --save-snapshot`, backtest, paper_trade.
- Data snapshots accumulating.
- Basic automation.

### Phase 1: Ship the Synthesis (Completed)
- Merged PR #7 (synthesis of #2 MCP + curated #3).
- Consolidated workspace/home repos + data.
- Fixed provider truncation (full pagination + validation).
- Ported PR#3 math (expected_move, LiquidityRules, prob_touch, etc.) into `signals/math.py`.
- Added EM filter + premium bands to candidates/config.
- Added no-trade support to journal.
- Updated prompt/README for MCP realities (preflight, regime filters, verticals preferred).
- Created `collect_data.sh` (ET slot matching with ±2 min tolerance for 45-min slots) + cron setup (`crontab.txt`, ET-pinned via `CRON_TZ`).
- Added --data-only to scan.py (skips report/runs/ for pure collection).

**Current Blockers (if any):** None major; data collection running.

### Phase 2: Validation & Hardening (In Progress / Next)
**Goal:** Rigorously validate before any live/paper trading. Prove positive expectancy.

**Prerequisites:**
- Python 3.10+ + `pip install -r requirements.txt pytest`.
- Real MCP token (`.rh_mcp_token.json` or Claude cache) for live tests.
- Run during market hours only (9:30am-4pm ET).

**Key Tasks:**
1. **Full Test Suite (ongoing)**
   - `python -m pytest tests/ -v`
   - Must pass (focus: `test_signals_math.py`, `test_mcp_provider.py`, `test_candidates.py`, `test_risk_and_journal.py`, `test_execution_and_backtest.py`).
   - Current: 83+ passing.

2. **Data Collection (high priority - already automated)**
   - Run `scripts/scan.py --data-only --save-snapshot --provider mcp` frequently.
   - Schedule: Every 45 min (30min pre-open → post-close), Mon-Fri.
     - Slots (ET): 9:00, 9:45, 10:30, 11:15, 12:00, 12:45, 13:30, 14:15, 15:00, 15:45, 16:30.
   - Cron: `CRON_TZ=America/New_York` + `*/15 9-16 * * 1-5` calling `scripts/collect_data.sh` (which matches ET slots within a ±2 min tolerance). Version-controlled in `crontab.txt`.
   - Verify: Full chains (hundreds of strikes straddling spot), no truncation, complete OI/volume/IV.
   - Collect ≥60 unique trading days of snapshots.
   - Post-processing: Compare OI (sum open_interest) close vs pre-open next day for overnight vs day volume.

3. **Scanner & Data Validation**
   - `python scripts/scan.py --save-snapshot --provider mcp`
   - Check: EM filter active, premium bands respected, candidates use new math.
   - Snapshots must be timestamped accurately (`taken_at`).

4. **Real MCP Testing (critical)**
   - Exercise full flow during market hours.
   - Commands:
     ```
     python scripts/scan.py --provider mcp --save-snapshot
     python scripts/paper_trade.py status
     ```
   - Verify: Live quotes, preflight (agentic_allowed + buying power), regime filters (earnings/FOMC/CPI), after-hours zero bids.
   - Test both yfinance fallback and MCP.

5. **Paper Trading Loop (minimum 40+ trades)**
   - Daily:
     1. `python scripts/paper_trade.py settle`
     2. Scan + review candidates.
     3. `python scripts/paper_trade.py open --scan-file runs/xxx.json --index 0`
     4. Manage/close, log no-trades.
   - Weekly: `python scripts/paper_trade.py stats`
   - Gates:
     - Positive expectancy after costs/slippage.
     - Realized win rate ≈ entry p_win.
     - Max drawdown acceptable.
     - ≥40 closed trades before scaling.

6. **Backtesting**
   - `python scripts/backtest.py --per-snapshot-trades 1`
   - Uses collected snapshots + yfinance settlements.
   - Entry: mid + slippage; hold to expiry, settle at intrinsic.
   - Compare to modeled EV/p_win.
   - Goal: Positive expectancy after costs.

7. **CI / Automation**
   - GitHub Actions: `.github/workflows/ci.yml` (pytest on 3.10-3.12).
   - Ensure green on PRs/pushes.
   - Logs: `logs/data_collection.log`.

8. **Small Issues / Polish**
   - Monitor EM filter (idxmin + configurable multiplier).
   - Harden ATM straddle extraction if edge cases appear.
   - Token security (rotation; `.rh_mcp_token.json` is already gitignored).
   - Update docs (e.g., PHASE2 for frequent collection + OI analysis).
   - Add more integration tests (MCP + math).

**Success Criteria for Phase 2:**
- 60+ days snapshots + positive backtest EV after costs.
- 40+ closed paper trades with positive expectancy + acceptable drawdown.
- Full MCP testing passed (no truncation, regime filters work).
- CI green.
- Human confirmation enforced.

**Deliverables:**
- Rich snapshot dataset (multi-time-of-day for OI/intraday analysis).
- Validated positive edge in backtest + paper.
- Automated collection running cleanly.

### Phase 3: Strategy & Code Polish (After Phase 2 Gates)
- Deepen math integration (full config for EM multiplier, premium bands).
- Enhance journal (richer no-trade records, OI delta tracking script).
- Prompt/docs updates (reflect frequent collection, time-of-day insights).
- Add intraday management simulation (if needed; current is hold-to-expiry conservative).
- Security: Token handling review, .env for secrets.
- Performance: Profile frequent scans (MCP calls).

**Gates:** All Phase 2 criteria + polished code/docs.

### Phase 4: Measurement & Gates (Pre-Live)
- Strengthen go-live:
  - Human approval on *every* order (agent proposes only).
  - Weekly journal stats review (flag divergence in realized vs modeled).
  - Max position sizing limits (e.g., 1 contract until 30+ trades).
- Define "positive expectancy" thresholds explicitly.
- Risk: Enforce daily loss cap, kill switch.
- Optional: Time-of-day backtest slicing (morning vs EOD entries).

**Deliverables:** Documented "ready for small live" criteria.

### Phase 5: Longer-Term Direction
- Monitor MCP for spread support → switch primary to verticals (already architected for it).
- Evaluate single-leg path: Keep only if MCP limitations persist; otherwise deprecate.
- Advanced features:
  - Dividend yield in BS probs.
  - Intraday management (profit targets/stops) — requires more granular data.
  - ML for regime detection (optional, after strong baseline).
- Scale: Once gates passed, small real size with strict monitoring.
- Observability: Better logging for why trades filtered/rejected.
- Storage: Prune old snapshots if needed; consider compression.

**Ongoing:**
- Daily/weekly data review.
- Update gates based on real results.
- Never bypass "prove with data" — no profitability claims.

## Key Code Areas & Reuse
- **Data Collection:** `scripts/scan.py` (core + --data-only), `options_trader/data/provider.py` (SnapshotStore with time fix), `scripts/collect_data.sh` (cron wrapper + ET slot matching w/ ±2 min tolerance), `crontab.txt` (version-controlled ET-pinned schedule).
- **Backtesting:** `scripts/backtest.py`, `options_trader/backtest/engine.py` (per-snapshot independent, uses taken_at).
- **Strategy Core:** `options_trader/config.py` (DTE, liquidity, premium, em_filter_multiplier, risk limits), `options_trader/signals/` (candidates.py with EM, math.py ported, probability.py).
- **Risk/Execution:** `options_trader/risk/manager.py`, `options_trader/execution/paper.py`, `options_trader/journal/journal.py` (no-trade support).
- **MCP:** `options_trader/data/mcp_provider.py` (hardened pagination + validation).
- **Tests:** `tests/` (run with pytest).
- **Docs:** `README.md`, `AGENT_PROMPT.md`, `PHASE2_VALIDATION.md`, `HANDOFF_PLAN.md` (this file).

**Patterns to Reuse:**
- Always use `if args.data_only: return 0` for quiet mode.
- Snapshot filenames now include time for uniqueness.
- Backtest replays exactly as collected (no guessing settlements).
- Config-driven everything (add new knobs here first).

## Automation & Cron
- Cron (version-controlled in `crontab.txt`; install with `crontab crontab.txt`):
  ```
  CRON_TZ=America/New_York
  */15 9-16 * * 1-5  → scripts/collect_data.sh
  ```
  `CRON_TZ` pins scheduling to Eastern, so the schedule stays correct regardless of the machine's system timezone.
- Script: Matches ET time against slots within a ±2 min tolerance (a cron tick that fires slightly late from load/skew still counts); runs `scan.py --data-only --save-snapshot --provider mcp`.
- Logs: `logs/data_collection.log` (gitignored).
- Schedule rationale: Captures open/mid/EOD dynamics + overnight OI deltas.

## Risks & Disclaimers
- **High Risk:** Options trading involves substantial loss. This is research/automation only.
- No profitability claims — must prove via data.
- MCP limitations: Currently single-leg easier; verticals manual if needed.
- Data quality: yfinance delayed; use MCP for live.
- Over-collection: Monitor API limits/costs/storage.
- Botched merges: Always resolve conflicts before commit; verify imports/tests.
- Diverging copies: Consolidated; use home repo only.

## Next Immediate Actions (Post-Handoff)
1. ✓ Done: CI + docs on main (PR #9 incorporated); cron hardening merged (PR #10 — ET-pinned `crontab.txt` + ±2 min tolerance).
2. Rebase any feature branches (e.g., mcp-data-provider) onto main if needed.
3. Run full validation per PHASE2_VALIDATION.md (focus data collection + paper loop).
4. Monitor cron logs + snapshot growth.
5. Once 60 days/40 trades: Review stats, decide on live pilot.
6. Push any local commits; clean old branches (e.g., `git push origin --delete mcp-data-provider`).
7. Update this handoff as phases complete.

## Contact / Ownership
- Primary: thebossnow (owner).
- Handoff created: 2026-07-09.
- All prior work (synthesis, fixes, automation) incorporated.
- Questions? Review git history, PHASE2_VALIDATION.md, or this file.

**Remember:** Slow and steady data collection wins. Validate relentlessly before scaling.
