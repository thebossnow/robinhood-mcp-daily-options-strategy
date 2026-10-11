"""Multi-strategy, multi-ticker portfolio backtest for the small-account
options income spec (options_income_backtest_spec.md section 9).

Nothing before this module runs more than one strategy/ticker slot at a
time: managed.py's ManagedBacktestEngine aggregates trades ACROSS symbols
into one result, but each symbol's book is independent -- no shared
position cap, no satellite cap, no drawdown halt, no combined equity
curve. This module adds that layer on top, reusing (not duplicating) the
per-position primitives already built:
  - S2/S3 (plain credit structures): signals/credit.py + managed.py's
    _check_exit/_settle_at_expiry, called directly since Phase A of the
    wheel is also a CreditPosition and shares the same exit rules.
  - S1 (wheel): signals/wheel.py's entry/settlement primitives, with a new
    Phase-B (covered call) exit check here -- wheel.py's own docstring
    scopes day-to-day management as this module's job, matching how
    credit.py stays entry/settlement-only and leaves _check_exit to
    managed.py.

Simplifications, stated plainly rather than hidden:
  - One open structure per (sleeve, ticker) slot at a time -- no stacking
    multiple weekly entries on the same ticker within a sleeve. The spec's
    "max 2 bull-put positions at once" is satisfied by running two tickers
    in the S2 sleeve, not by stacking one ticker.
  - Equity updates once per trade, at close, by that trade's full realized
    P&L -- not continuous mark-to-market of open positions. This matches
    ManagedBacktestResult.summary()'s own drawdown convention; the
    consequence (stated in that module's docstring too) is that
    intra-trade drawdown is understated between checkpoints.
  - Entries and exits are both checked DAILY (this repo's first daily-
    cadence backtest loop): managed.py stayed weekly because live DoltHub
    queries cost 10-30s each, but DoltHubHistory's on-disk cache removes
    that cost once warmed, so this loop can check every trading day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_cls

import pandas as pd

from ..signals.credit import CreditVariantConfig, bs_price, build_position
from ..signals.wheel import (
    CoveredCallPosition, ShareLot, WheelConfig, build_phase_a, build_phase_b,
    resolve_covered_call_settlement, resolve_put_spread_settlement,
)
from .managed import ManagedBacktestEngine, weekly_entry_days

STOP_OUT_REASONS = {"hard_stop", "breach", "max_loss"}


@dataclass
class PortfolioConfig:
    starting_equity: float = 10_000.0
    max_risk_per_trade_pct: float = 0.02
    max_ticker_alloc_pct: float = 0.25
    max_open_structures: int = 4
    max_satellite_open: int = 1
    drawdown_halt_pct: float = 0.12
    stop_out_sit_out_days: int = 5
    slippage_half_spread_frac: float = 0.5


@dataclass
class Sleeve:
    """One strategy's slice of the book. `kind` picks the entry/exit path:
    'credit' for S2/S3 (plain CreditVariantConfig structures), 'wheel' for
    S1 (the phase A/B state machine)."""
    sleeve_id: str
    tickers: list[str]
    is_satellite: bool
    kind: str                                  # 'credit' | 'wheel'
    credit_cfg: CreditVariantConfig | None = None
    wheel_cfg: WheelConfig | None = None
    entry_cadence: str = "weekly"               # 'weekly' | 'daily'

    def __post_init__(self):
        if self.kind == "credit" and self.credit_cfg is None:
            raise ValueError(f"{self.sleeve_id}: kind='credit' needs credit_cfg")
        if self.kind == "wheel" and self.wheel_cfg is None:
            raise ValueError(f"{self.sleeve_id}: kind='wheel' needs wheel_cfg")


@dataclass
class OpenStructure:
    sleeve_id: str
    underlying: str
    is_satellite: bool
    kind: str                    # 'credit' | 'covered_call'
    position: object             # CreditPosition | CoveredCallPosition
    contracts: int
    cfg: object                  # CreditVariantConfig | WheelConfig
    wheel_phase: str | None = None   # 'A' | 'B', wheel sleeves only


@dataclass
class PortfolioTrade:
    sleeve_id: str
    underlying: str
    kind: str
    entry_date: str
    exit_date: str
    expiration: str
    exit_reason: str
    contracts: int
    pnl: float                   # dollars, contracts already applied
    equity_after: float
    credit_per_share: float | None = None      # credit-structure exits only
    hard_stop_mult: float | None = None        # credit-structure exits only


@dataclass
class PortfolioResult:
    trades: list[PortfolioTrade] = field(default_factory=list)
    equity_curve: list[tuple[str, float]] = field(default_factory=list)


def _dte(day: str, expiration: str) -> int:
    return (date_cls.fromisoformat(expiration) - date_cls.fromisoformat(day)).days


def size_contracts(equity: float, max_loss_per_contract: float,
                   capital_required_per_contract: float,
                   cfg: PortfolioConfig) -> int:
    """Spec section 4's sizing formula: risk-capped, then ticker-allocation
    capped. `capital_required_per_contract` is the collateral a broker would
    hold (== max_loss*100 for a defined-risk spread; irrelevant, pass 0,
    for a covered call sold against shares already owned)."""
    if max_loss_per_contract <= 0:
        return 0
    contracts = int((equity * cfg.max_risk_per_trade_pct) // max_loss_per_contract)
    if capital_required_per_contract > 0:
        alloc_cap = int((equity * cfg.max_ticker_alloc_pct)
                        // capital_required_per_contract)
        contracts = min(contracts, alloc_cap)
    return max(contracts, 0)


def _pick_expiration(chains: pd.DataFrame, day: str, min_dte: int,
                     max_dte: int, target_dte: int) -> str | None:
    exps = sorted({str(e)[:10] for e in chains["expiration"]})
    exps = [e for e in exps if min_dte <= _dte(day, e) <= max_dte]
    if not exps:
        return None
    return min(exps, key=lambda e: abs(_dte(day, e) - target_dte))


def _quote_or_model(chains: pd.DataFrame, expiration: str, opt_type: str,
                    strike: float, spot: float | None, entry_iv: float,
                    entry_half_spread: float,
                    day: str) -> tuple[float, float] | None:
    """Cost to close one short leg (side=-1): (mid_cost, half_spread), or
    None if neither a live quote nor a model mark is available. Mirrors
    managed.py's _close_quotes but for the single short call leg a covered
    call carries -- no wing, so no loop over legs."""
    exp_chain = (chains[chains["expiration"].astype(str).str[:10] == expiration]
                if not chains.empty else chains)
    row = (exp_chain[(exp_chain["type"] == opt_type)
                     & (exp_chain["strike"] == strike)]
          if not exp_chain.empty else exp_chain)
    if not row.empty:
        bid, ask = float(row.iloc[0]["bid"]), float(row.iloc[0]["ask"])
        if ask > 0 and ask >= bid >= 0:
            return (bid + ask) / 2.0, (ask - bid) / 2.0
    if not spot or entry_iv <= 0:
        return None
    t_years = max(_dte(day, expiration), 0) / 365.0
    return bs_price(opt_type, spot, strike, entry_iv, t_years), entry_half_spread


def check_covered_call_exit(pos: CoveredCallPosition, cfg: WheelConfig,
                            day: str, spot: float | None,
                            chains: pd.DataFrame,
                            slip_frac: float) -> tuple[str, float] | None:
    """Returns (exit_reason, exit_cost_per_share) or None if still open.
    No breach analog: a covered call moving ITM is the position working
    as intended, not a stop condition."""
    if day >= pos.expiration:
        return None   # caller settles via resolve_covered_call_settlement
    quote = _quote_or_model(chains, pos.expiration, "call", pos.leg.strike,
                            spot, pos.leg.entry_iv, pos.leg.entry_half_spread,
                            day)
    if quote is None:
        return None
    cost_mid, half_spread = quote
    exit_cost = cost_mid + slip_frac * half_spread
    if pos.credit - exit_cost >= cfg.call_profit_take_frac * pos.credit:
        return "profit_target", exit_cost
    if cfg.hard_stop_credit_mult is not None and \
            exit_cost - pos.credit >= cfg.hard_stop_credit_mult * pos.credit:
        return "hard_stop", exit_cost
    threshold = min(cfg.call_time_exit_dte,
                    int(pos.dte_at_entry * cfg.call_time_exit_frac))
    if _dte(day, pos.expiration) <= threshold:
        return "time_exit", exit_cost
    return None


class PortfolioBacktestEngine:
    """history must provide day_chains(symbol, day, max_dte) -> DataFrame
    (options_trader.data.dolthub.DoltHubHistory). spot_lookup maps
    (symbol, day) -> close and must extend ~60 days past `end`."""

    MAX_DTE_FETCH = 50

    def __init__(self, history, spot_lookup: dict, cfg: PortfolioConfig):
        self.history = history
        self.spots = spot_lookup
        self.cfg = cfg
        self._managed = ManagedBacktestEngine(
            history, spot_lookup, cfg.slippage_half_spread_frac)

    def run(self, sleeves: list[Sleeve], start: str, end: str) -> PortfolioResult:
        result = PortfolioResult()
        all_days = sorted({d for (s, d) in self.spots
                           if any(s == t for sl in sleeves for t in sl.tickers)})
        trading_days = [d for d in all_days if start <= d <= end]
        weekly = set(weekly_entry_days(all_days))

        equity = self.cfg.starting_equity
        peak_equity = equity
        open_structures: dict[tuple[str, str], OpenStructure] = {}
        wheel_lots: dict[tuple[str, str], ShareLot | None] = {}
        last_stop_out: dict[tuple[str, str], str] = {}

        for day in trading_days:
            # 1) manage the open book
            for key, struct in list(open_structures.items()):
                spot = self.spots.get((struct.underlying, day))
                chains = self.history.day_chains(struct.underlying, day,
                                                  self.MAX_DTE_FETCH)
                closed = self._check_and_settle(struct, day, spot, chains,
                                                all_days)
                if closed is None:
                    continue
                (reason, pnl_per_contract, next_phase, next_lot,
                 credit_per_share, hard_stop_mult) = closed
                pnl_dollars = round(pnl_per_contract * struct.contracts, 2)
                equity += pnl_dollars
                peak_equity = max(peak_equity, equity)
                result.trades.append(PortfolioTrade(
                    sleeve_id=struct.sleeve_id, underlying=struct.underlying,
                    kind=struct.kind, entry_date=struct.position.entry_date,
                    exit_date=day, expiration=struct.position.expiration,
                    exit_reason=reason, contracts=struct.contracts,
                    pnl=pnl_dollars, equity_after=round(equity, 2),
                    credit_per_share=credit_per_share, hard_stop_mult=hard_stop_mult,
                ))
                del open_structures[key]
                if reason in STOP_OUT_REASONS:
                    last_stop_out[key] = day
                if struct.wheel_phase is not None:
                    wheel_lots[key] = next_lot
                    # next_phase/next_lot drive re-entry below; nothing
                    # else to track here since a fresh position is built
                    # from scratch on the next qualifying entry day.

            # 2) new entries
            for sleeve in sleeves:
                for ticker in sleeve.tickers:
                    key = (sleeve.sleeve_id, ticker)
                    if key in open_structures:
                        continue
                    stop_day = last_stop_out.get(key)
                    if stop_day is not None:
                        idx_days = [d for d in all_days if stop_day <= d <= day]
                        if len(idx_days) - 1 < self.cfg.stop_out_sit_out_days:
                            continue
                    if len(open_structures) >= self.cfg.max_open_structures:
                        continue
                    if sleeve.is_satellite:
                        n_satellite = sum(1 for s in open_structures.values()
                                         if s.is_satellite)
                        if n_satellite >= self.cfg.max_satellite_open:
                            continue
                        if peak_equity > 0 and \
                                (peak_equity - equity) / peak_equity > \
                                self.cfg.drawdown_halt_pct:
                            continue
                    entry_ok = (day in weekly if sleeve.entry_cadence == "weekly"
                               else True)
                    if not entry_ok:
                        continue
                    spot = self.spots.get((ticker, day))
                    if spot is None:
                        continue
                    chains = self.history.day_chains(ticker, day, self.MAX_DTE_FETCH)
                    if chains.empty:
                        continue
                    struct = self._try_enter(sleeve, ticker, day, spot, chains,
                                             equity, wheel_lots.get(key))
                    if struct is not None:
                        open_structures[key] = struct

            result.equity_curve.append((day, round(equity, 2)))

        return result

    def _try_enter(self, sleeve: Sleeve, ticker: str, day: str, spot: float,
                   chains: pd.DataFrame, equity: float,
                   pending_lot: ShareLot | None) -> OpenStructure | None:
        if sleeve.kind == "credit":
            cfg = sleeve.credit_cfg
            expiration = _pick_expiration(chains, day, cfg.min_dte,
                                          cfg.max_dte, cfg.target_dte)
            if expiration is None:
                return None
            chain = chains[chains["expiration"].astype(str).str[:10] == expiration]
            pos = build_position(chain, spot, ticker, day, expiration,
                                 _dte(day, expiration), cfg,
                                 self.cfg.slippage_half_spread_frac)
            if pos is None:
                return None
            contracts = size_contracts(
                equity, pos.max_loss * 100, pos.max_loss * 100, self.cfg)
            if contracts < 1:
                return None
            return OpenStructure(sleeve_id=sleeve.sleeve_id, underlying=ticker,
                                is_satellite=sleeve.is_satellite, kind="credit",
                                position=pos, contracts=contracts, cfg=cfg)

        # wheel sleeve
        wcfg = sleeve.wheel_cfg
        if pending_lot is not None:
            expiration = _pick_expiration(
                chains, day, wcfg.put_min_dte, wcfg.put_max_dte, wcfg.put_max_dte)
            if expiration is None:
                return None
            chain = chains[chains["expiration"].astype(str).str[:10] == expiration]
            cc = build_phase_b(chain, spot, ticker, day, expiration,
                               _dte(day, expiration), pending_lot, wcfg,
                               slippage_half_spread_frac=self.cfg.slippage_half_spread_frac)
            if cc is None:
                return None
            return OpenStructure(sleeve_id=sleeve.sleeve_id, underlying=ticker,
                                is_satellite=sleeve.is_satellite,
                                kind="covered_call", position=cc,
                                contracts=pending_lot.shares // 100,
                                cfg=wcfg, wheel_phase="B")

        expiration = _pick_expiration(chains, day, wcfg.put_min_dte,
                                      wcfg.put_max_dte, wcfg.put_max_dte)
        if expiration is None:
            return None
        chain = chains[chains["expiration"].astype(str).str[:10] == expiration]
        pos = build_phase_a(chain, spot, ticker, day, expiration,
                            _dte(day, expiration), wcfg,
                            self.cfg.slippage_half_spread_frac)
        if pos is None:
            return None
        contracts = size_contracts(equity, pos.max_loss * 100,
                                   pos.max_loss * 100, self.cfg)
        if contracts < 1:
            return None
        return OpenStructure(sleeve_id=sleeve.sleeve_id, underlying=ticker,
                            is_satellite=sleeve.is_satellite, kind="credit",
                            position=pos, contracts=contracts, cfg=wcfg,
                            wheel_phase="A")

    def _check_and_settle(
        self, struct: OpenStructure, day: str, spot: float | None,
        chains: pd.DataFrame, all_days: list[str],
    ) -> tuple[str, float, str | None, ShareLot | None, float | None, float | None] | None:
        """Returns (exit_reason, pnl_per_contract_dollars, next_wheel_phase,
        next_wheel_lot, credit_per_share, hard_stop_mult) or None if the
        structure is still open. pnl_per_contract_dollars is the P&L for ONE
        contract (100 shares' worth); the caller scales by struct.contracts.
        credit_per_share/hard_stop_mult are populated for both credit-
        structure exits and covered-call exits (both can hit hard_stop --
        see the gap-risk diagnostic in robinhood-mcp-small-account-income-
        backtest memory, which initially missed the covered-call path and
        undercounted); None only for final settlement outcomes (expiry
        assignment/call-away), which don't have a single "credit" concept
        in the same sense."""
        if struct.kind == "credit":
            cfg = (struct.cfg if struct.wheel_phase is None
                  else struct.cfg.phase_a_variant())
            trade = self._managed._check_exit(struct.position, cfg, day, spot,
                                              chains, all_days)
            if trade is None:
                return None
            if struct.wheel_phase != "A":
                return (trade.exit_reason, trade.pnl, None, None,
                       trade.credit, cfg.hard_stop_credit_mult)
            # Wheel Phase A: expiry settlement can assign into shares;
            # any earlier exit (profit/hard_stop/time_exit) just closes
            # the spread for cash and restarts Phase A next cycle.
            if trade.exit_reason == "expired" and day >= struct.position.expiration:
                outcome, cash_pnl_per_share, lot = resolve_put_spread_settlement(
                    struct.position, float(spot) if spot else trade.spot_at_exit,
                    contracts=struct.contracts)
                return (outcome, cash_pnl_per_share * 100.0,
                       ("B" if lot else "A"), lot, None, None)
            return (trade.exit_reason, trade.pnl, "A", None,
                   trade.credit, cfg.hard_stop_credit_mult)

        # covered call (struct.kind == "covered_call")
        wcfg: WheelConfig = struct.cfg
        exit_info = check_covered_call_exit(
            struct.position, wcfg, day, spot, chains,
            self.cfg.slippage_half_spread_frac)
        if exit_info is not None:
            reason, exit_cost = exit_info
            pnl_per_share = struct.position.credit - exit_cost
            # Selling a covered call early never touches the shares --
            # they're carried forward into the next Phase B cycle.
            return (reason, pnl_per_share * 100.0, "B", struct.position.lot,
                   struct.position.credit, wcfg.hard_stop_credit_mult)
        if day >= struct.position.expiration:
            settle_spot = float(spot) if spot else struct.position.spot_at_entry
            outcome, pnl_per_share = resolve_covered_call_settlement(
                struct.position, settle_spot)
            next_phase = "A" if outcome == "called_away" else "B"
            next_lot = None if outcome == "called_away" else struct.position.lot
            return (outcome, pnl_per_share * 100.0, next_phase, next_lot,
                   None, None)
        return None
