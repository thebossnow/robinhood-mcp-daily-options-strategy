"""Managed credit-structure backtest over EOD marks at weekly checkpoints.

Unlike engine.py (hold-to-expiry settlement of debit spreads), this engine
carries a book of open positions and applies the mechanical management rules
that define the strategy: profit-take at a fraction of entry credit, exit on
short-strike breach, hard time exit at a DTE floor, settlement at intrinsic
at expiry.

Cadence is WEEKLY, both for entries and management, driven by data-source
economics: the DoltHub SQL API serves one full (symbol, day) chain fetch in
~10-30s, so a multi-year daily-management backtest is not feasible against
it. One fetch per symbol-week prices entries and every open position's
marks. This is an expectancy study, not a portfolio simulation: one contract
per position, no capital interaction; overlapping positions on the same
underlying are correlated, so treat trade counts as optimistic for
statistical independence.

Honest limitations:
- WEEKLY exit checks: a profit target or breach that occurred midweek is
  acted on at the next checkpoint's marks. Profit-takes fill late (slightly
  understates results); breaches also fill late (overstates losses — the
  conservative direction for a short-premium strategy).
- The DoltHub dataset quotes only a rotating subset of expirations each day
  (~2/4/6-week DTE buckets), so a held expiration is often unquoted at a
  checkpoint. Exits still evaluate: unquoted legs are marked with
  Black-Scholes at the leg's ENTRY implied vol (sticky-strike). Each closed
  trade records `mark_source` ('marks' | 'model' | 'mixed' | 'settlement')
  and the summary reports the model-marked share — read expectancy
  accordingly. Sticky entry IV under-prices exits after vol spikes
  (optimistic on breach losses) and over-prices after vol crush
  (pessimistic on profit-takes).
- No volume/open interest in the DoltHub dataset: liquidity is assumed.
- Fills pay slippage: a configurable fraction of each leg's half-spread,
  entry and exit, at that day's quoted spreads (entry-day spreads proxy
  for model-marked legs).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as date_cls

import pandas as pd

from ..signals.credit import (
    CreditPosition, CreditVariantConfig, bs_price, build_position,
)

logger = logging.getLogger(__name__)


@dataclass
class ClosedTrade:
    underlying: str
    variant: str
    entry_date: str
    exit_date: str
    expiration: str
    dte_at_entry: int
    days_held: int
    credit: float            # per share, after entry slippage
    credit_frac: float
    exit_cost: float         # per share, after exit slippage
    pnl: float               # per contract, dollars (x100)
    max_loss: float          # per contract, dollars
    exit_reason: str         # profit_target | breach | time_exit | expired
    mark_source: str         # marks | model | mixed | settlement
    short_put_strike: float | None
    short_call_strike: float | None
    spot_at_entry: float
    spot_at_exit: float

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


@dataclass
class ManagedBacktestResult:
    trades: list[ClosedTrade] = field(default_factory=list)
    skipped_no_expiration: int = 0
    skipped_no_position: int = 0    # legs unselectable or credit gate failed
    skipped_no_data: int = 0        # checkpoint had no chains (dataset gap)

    def summary(self, variant: str | None = None,
                underlying: str | None = None) -> dict:
        trades = [t for t in self.trades
                  if (variant is None or t.variant == variant)
                  and (underlying is None or t.underlying == underlying)]
        if not trades:
            return {"trades": 0}
        pnls = [t.pnl for t in trades]
        wins = [p for p in pnls if p > 0]
        gross_win = sum(p for p in pnls if p > 0)
        gross_loss = -sum(p for p in pnls if p < 0)
        running, peak, max_dd = 0.0, 0.0, 0.0
        for t in sorted(trades, key=lambda t: t.entry_date):
            running += t.pnl
            peak = max(peak, running)
            max_dd = min(max_dd, running - peak)
        reasons: dict[str, int] = {}
        sources: dict[str, int] = {}
        for t in trades:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
            sources[t.mark_source] = sources.get(t.mark_source, 0) + 1
        return {
            "trades": len(trades),
            "win_rate": round(len(wins) / len(trades), 4),
            "total_pnl": round(sum(pnls), 2),
            "expectancy_per_trade": round(sum(pnls) / len(trades), 2),
            "avg_credit": round(sum(t.credit for t in trades) / len(trades), 4),
            "avg_days_held": round(sum(t.days_held for t in trades) / len(trades), 1),
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else float("inf"),
            "max_drawdown": round(max_dd, 2),
            "worst_trade": round(min(pnls), 2),
            "best_trade": round(max(pnls), 2),
            "exit_reasons": reasons,
            "mark_sources": sources,
        }


def _dte(day: str, expiration: str) -> int:
    return (date_cls.fromisoformat(expiration) - date_cls.fromisoformat(day)).days


def weekly_entry_days(trading_days: list[str]) -> list[str]:
    """First trading day of each ISO week."""
    out, seen = [], set()
    for day in sorted(trading_days):
        d = date_cls.fromisoformat(day)
        key = d.isocalendar()[:2]
        if key not in seen:
            seen.add(key)
            out.append(day)
    return out


class ManagedBacktestEngine:
    """history must provide day_chains(symbol, day, max_dte) -> DataFrame
    with columns [expiration, type, strike, bid, ask, iv, delta] — see
    data/dolthub.py DoltHubHistory. spot_lookup maps (symbol, day) -> close
    and must extend ~60 days past `end` so late entries can be managed out.
    """

    MAX_DTE_FETCH = 50   # covers entry window (<=45 DTE) and every held exp

    def __init__(self, history, spot_lookup: dict,
                 slippage_half_spread_frac: float = 0.5):
        self.history = history
        self.spots = spot_lookup
        self.slip_frac = slippage_half_spread_frac

    def run(self, symbols: list[str], start: str, end: str,
            variants: list[CreditVariantConfig],
            progress: bool = False) -> ManagedBacktestResult:
        result = ManagedBacktestResult()
        for symbol in symbols:
            self._run_symbol(symbol, start, end, variants, result, progress)
        return result

    def _run_symbol(self, symbol: str, start: str, end: str,
                    variants: list[CreditVariantConfig],
                    result: ManagedBacktestResult, progress: bool) -> None:
        all_days = sorted(d for (s, d) in self.spots if s == symbol)
        checkpoints = weekly_entry_days(all_days)
        entry_grid = {d for d in checkpoints if start <= d <= end}
        book: list[tuple[CreditPosition, CreditVariantConfig]] = []

        for i, day in enumerate(checkpoints):
            if day < start:
                continue
            if day > end and not book:
                break
            chains = self.history.day_chains(symbol, day, self.MAX_DTE_FETCH)
            spot = self.spots.get((symbol, day))
            if chains.empty:
                result.skipped_no_data += 1

            # 1) manage the open book at this checkpoint's marks
            still_open = []
            for pos, cfg in book:
                trade = self._check_exit(pos, cfg, day, spot, chains, all_days)
                if trade is not None:
                    result.trades.append(trade)
                else:
                    still_open.append((pos, cfg))
            book = still_open

            # 2) new entries
            if day in entry_grid and spot and not chains.empty:
                book.extend(self._enter(symbol, day, spot, chains,
                                        variants, result))
            if progress and (i + 1) % 25 == 0:
                logger.info("%s: %d/%d checkpoints, %d trades closed",
                            symbol, i + 1, len(checkpoints), len(result.trades))

        # Anything still open (dataset gaps at the tail): settle at intrinsic.
        for pos, cfg in book:
            trade = self._settle_at_expiry(pos, all_days)
            if trade is not None:
                result.trades.append(trade)

    def _enter(self, symbol: str, day: str, spot: float, chains: pd.DataFrame,
               variants: list[CreditVariantConfig],
               result: ManagedBacktestResult):
        v0 = variants[0]   # shipped variants share the DTE window
        exps = sorted({str(e)[:10] for e in chains["expiration"]})
        exps = [e for e in exps if v0.min_dte <= _dte(day, e) <= v0.max_dte]
        if not exps:
            result.skipped_no_expiration += 1
            return []
        expiration = min(exps, key=lambda e: abs(_dte(day, e) - v0.target_dte))
        chain = chains[chains["expiration"].astype(str).str[:10] == expiration]

        entered = []
        for cfg in variants:
            pos = build_position(chain, spot, symbol, day, expiration,
                                 _dte(day, expiration), cfg, self.slip_frac)
            if pos is None:
                result.skipped_no_position += 1
            else:
                entered.append((pos, cfg))
        return entered

    def _close_quotes(self, pos: CreditPosition, chains: pd.DataFrame,
                      spot: float | None,
                      day: str) -> tuple[float, float, str] | None:
        """(cost_to_close_at_mid, total_half_spread, mark_source) at this
        checkpoint. Legs the dataset quotes today are priced at their mid;
        unquoted legs are model-marked (Black-Scholes at entry IV, today's
        spot/DTE) with the entry half-spread as the slippage proxy. Returns
        None only when a leg can be neither quoted nor modeled."""
        exp_chain = (chains[chains["expiration"].astype(str).str[:10]
                            == pos.expiration]
                     if not chains.empty else chains)
        t_years = max(_dte(day, pos.expiration), 0) / 365.0
        cost, half_spread_sum, modeled = 0.0, 0.0, 0
        for leg in pos.legs:
            row = (exp_chain[(exp_chain["type"] == leg.type)
                             & (exp_chain["strike"] == leg.strike)]
                   if not exp_chain.empty else exp_chain)
            bid = ask = None
            if not row.empty:
                bid, ask = float(row.iloc[0]["bid"]), float(row.iloc[0]["ask"])
                if ask <= 0 or ask < bid:
                    bid = ask = None
            if bid is not None:
                cost += -leg.side * (bid + ask) / 2.0
                half_spread_sum += (ask - bid) / 2.0
            else:
                if not spot or leg.entry_iv <= 0:
                    return None
                cost += -leg.side * bs_price(leg.type, spot, leg.strike,
                                             leg.entry_iv, t_years)
                half_spread_sum += leg.entry_half_spread
                modeled += 1
        source = ("marks" if modeled == 0
                  else "model" if modeled == len(pos.legs) else "mixed")
        return cost, half_spread_sum, source

    def _check_exit(self, pos: CreditPosition, cfg: CreditVariantConfig,
                    day: str, spot: float | None, chains: pd.DataFrame,
                    all_days: list[str]) -> ClosedTrade | None:
        if day >= pos.expiration:
            return self._settle_at_expiry(pos, all_days)

        exit_cost, source = None, "marks"
        quotes = self._close_quotes(pos, chains, spot, day)
        if quotes is not None:
            cost_mid, half_spreads, source = quotes
            exit_cost = cost_mid + self.slip_frac * half_spreads

        reason = None
        if exit_cost is not None and \
                pos.credit - exit_cost >= cfg.profit_take_frac * pos.credit:
            reason = "profit_target"
        if reason is None and cfg.exit_on_breach and spot:
            if (pos.short_put_strike is not None and spot < pos.short_put_strike) or \
               (pos.short_call_strike is not None and spot > pos.short_call_strike):
                reason = "breach"
        if reason is None and _dte(day, pos.expiration) <= cfg.time_exit_dte:
            reason = "time_exit"

        if reason is None:
            return None
        if exit_cost is None:
            # Can't price the exit at this checkpoint (no quotes, no spot,
            # or no entry IV); the position stays open until next week.
            return None
        return self._closed(pos, day, exit_cost, reason, spot, source)

    def _settle_at_expiry(self, pos: CreditPosition,
                          all_days: list[str]) -> ClosedTrade | None:
        on_or_before = [d for d in all_days if d <= pos.expiration]
        if not on_or_before:
            return None
        settle_day = on_or_before[-1]
        spot = self.spots.get((pos.underlying, settle_day))
        if spot is None:
            return None
        exit_cost = pos.intrinsic_close_cost(spot)
        return self._closed(pos, settle_day, exit_cost, "expired", spot,
                            "settlement")

    def _closed(self, pos: CreditPosition, day: str, exit_cost: float,
                reason: str, spot: float | None,
                mark_source: str) -> ClosedTrade:
        # A closed spread can't be worth less than 0 or more than max width.
        max_width = max(pos.widths().values())
        exit_cost = min(max(exit_cost, 0.0), max_width)
        pnl = (pos.credit - exit_cost) * 100.0
        return ClosedTrade(
            underlying=pos.underlying,
            variant=pos.variant,
            entry_date=pos.entry_date,
            exit_date=day,
            expiration=pos.expiration,
            dte_at_entry=pos.dte_at_entry,
            days_held=(date_cls.fromisoformat(day)
                       - date_cls.fromisoformat(pos.entry_date)).days,
            credit=pos.credit,
            credit_frac=pos.credit_frac,
            exit_cost=round(exit_cost, 4),
            pnl=round(pnl, 2),
            max_loss=round(pos.max_loss * 100.0, 2),
            exit_reason=reason,
            mark_source=mark_source,
            short_put_strike=pos.short_put_strike,
            short_call_strike=pos.short_call_strike,
            spot_at_entry=pos.spot_at_entry,
            spot_at_exit=float(spot) if spot else 0.0,
        )
