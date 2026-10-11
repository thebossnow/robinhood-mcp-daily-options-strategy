"""Independent quote/IV check: TradingView MCP (Yahoo-backed) vs Robinhood MCP.

TradingView MCP's `yahoo_price`, `market_snapshot`, and `stock_options_chain`
tools read Yahoo Finance, not a TradingView options feed. That is still a
useful second tape: NBBO should match Robinhood closely; IV often will not,
because each venue's vol model differs. Credit-path delta falls back to
Black-Scholes on Robinhood IV when the MCP chain has no delta column.

No live orders. This module never calls place_* tools.
"""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

IV_REL_WARN = 0.10          # flag when |RH-TV|/TV exceeds this
IV_PERCENT_CUTOFF = 3.0     # IV > 3.0 is treated as percent (25.3 → 0.253)


@dataclass
class LegDiff:
    side: str
    strike: float
    tv_bid: float | None
    rh_bid: float | None
    tv_ask: float | None
    rh_ask: float | None
    tv_volume: int | None
    rh_volume: int | None
    tv_oi: int | None
    rh_oi: int | None
    tv_iv: float | None
    rh_iv: float | None
    iv_rel_diff: float | None
    iv_warn: bool
    iv_unit_suspect: bool


@dataclass
class SymbolReport:
    symbol: str
    expiry: str | None
    tv_spot: float | None
    rh_spot: float | None
    spot_diff: float | None
    tv_vix: float | None
    rh_vix: float | None
    vix_diff: float | None
    vol_gate_allowed: bool | None
    vol_gate_reason: str | None
    atm_strike: float | None
    legs: list[LegDiff]
    notes: list[str]


def strike_key(value: Any) -> float:
    return round(float(value), 2)


def normalize_iv(raw: Any) -> tuple[float | None, bool]:
    """Return (decimal_iv, unit_suspect). unit_suspect means the source
    looked like a percent and was divided by 100."""
    if raw is None:
        return None, False
    try:
        iv = float(raw)
    except (TypeError, ValueError):
        return None, False
    if iv > IV_PERCENT_CUTOFF:
        return iv / 100.0, True
    return iv, False


def pick_expiry(expirations: list[str], today: date,
                min_dte: int, max_dte: int) -> str | None:
    """First expiration in [min_dte, max_dte], preferring the closest to min_dte."""
    in_window = []
    for raw in expirations:
        try:
            exp = date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        dte = (exp - today).days
        if min_dte <= dte <= max_dte:
            in_window.append((dte, str(raw)[:10]))
    if not in_window:
        return None
    in_window.sort()
    return in_window[0][1]


def nearest_shared_strike(tv_strikes: list[float], rh_strikes: list[float],
                          spot: float) -> float | None:
    shared = {strike_key(s) for s in tv_strikes} & {strike_key(s) for s in rh_strikes}
    if not shared:
        return None
    return min(shared, key=lambda k: abs(k - spot))


def _rel_diff(a: float | None, b: float | None) -> float | None:
    """(b - a) / a when a is a usable denominator."""
    if a is None or b is None or a == 0:
        return None
    return (b - a) / a


def diff_leg(side: str, strike: float, tv: dict, rh: dict,
             iv_rel_warn: float = IV_REL_WARN) -> LegDiff:
    tv_iv, tv_suspect = normalize_iv(tv.get("iv"))
    rh_iv, rh_suspect = normalize_iv(rh.get("iv"))
    rel = _rel_diff(tv_iv, rh_iv)
    return LegDiff(
        side=side,
        strike=strike,
        tv_bid=_opt_float(tv.get("bid")),
        rh_bid=_opt_float(rh.get("bid")),
        tv_ask=_opt_float(tv.get("ask")),
        rh_ask=_opt_float(rh.get("ask")),
        tv_volume=_opt_int(tv.get("volume")),
        rh_volume=_opt_int(rh.get("volume")),
        tv_oi=_opt_int(tv.get("open_interest")),
        rh_oi=_opt_int(rh.get("open_interest")),
        tv_iv=tv_iv,
        rh_iv=rh_iv,
        iv_rel_diff=rel,
        iv_warn=rel is not None and abs(rel) > iv_rel_warn,
        iv_unit_suspect=tv_suspect or rh_suspect,
    )


def tv_rows_by_side(chain: dict) -> dict[str, dict[float, dict]]:
    """Map TradingView/Yahoo chain payload → {call|put: {strike: row}}."""
    out: dict[str, dict[float, dict]] = {"call": {}, "put": {}}
    for side, key in (("call", "calls"), ("put", "puts")):
        for raw in chain.get(key) or []:
            if raw.get("strike") is None:
                continue
            k = strike_key(raw["strike"])
            out[side][k] = {
                "bid": raw.get("bid"),
                "ask": raw.get("ask"),
                "volume": raw.get("volume"),
                "open_interest": raw.get("open_interest"),
                "iv": raw.get("implied_volatility", raw.get("iv")),
            }
    return out


def rh_rows_by_side(chain) -> dict[str, dict[float, dict]]:
    """Map a ChainSnapshot.chain DataFrame (or list of dicts) the same way."""
    out: dict[str, dict[float, dict]] = {"call": {}, "put": {}}
    if chain is None:
        return out
    records = chain.to_dict("records") if hasattr(chain, "to_dict") else list(chain)
    for raw in records:
        side = str(raw.get("type", "")).lower()
        if side not in out or raw.get("strike") is None:
            continue
        k = strike_key(raw["strike"])
        out[side][k] = {
            "bid": raw.get("bid"),
            "ask": raw.get("ask"),
            "volume": raw.get("volume"),
            "open_interest": raw.get("open_interest"),
            "iv": raw.get("iv"),
        }
    return out


def compare_symbol(
    *,
    symbol: str,
    expiry: str | None,
    tv_spot: float | None,
    rh_spot: float | None,
    tv_vix: float | None,
    rh_vix: float | None,
    tv_chain: dict | None,
    rh_chain,
    vol_gate_allowed: bool | None = None,
    vol_gate_reason: str | None = None,
    iv_rel_warn: float = IV_REL_WARN,
    notes: list[str] | None = None,
) -> SymbolReport:
    notes = list(notes or [])
    spot = rh_spot if rh_spot is not None else tv_spot
    tv_map = tv_rows_by_side(tv_chain or {})
    rh_map = rh_rows_by_side(rh_chain)
    atm = None
    legs: list[LegDiff] = []
    if spot is not None:
        # Prefer a strike that exists on both sides of both tapes.
        tv_strikes = list(tv_map["call"]) or list(tv_map["put"])
        rh_strikes = list(rh_map["call"]) or list(rh_map["put"])
        atm = nearest_shared_strike(tv_strikes, rh_strikes, spot)
        if atm is None:
            notes.append("no shared strike between TV and RH chains")
        else:
            for side in ("call", "put"):
                tv_leg = tv_map[side].get(atm, {})
                rh_leg = rh_map[side].get(atm, {})
                if not tv_leg and not rh_leg:
                    continue
                if not tv_leg:
                    notes.append(f"TV missing {side} {atm:g}")
                if not rh_leg:
                    notes.append(f"RH missing {side} {atm:g}")
                legs.append(diff_leg(side, atm, tv_leg, rh_leg, iv_rel_warn))
    elif tv_chain or rh_chain is not None:
        notes.append("no spot — skipped ATM compare")

    return SymbolReport(
        symbol=symbol,
        expiry=expiry,
        tv_spot=tv_spot,
        rh_spot=rh_spot,
        spot_diff=_sub(rh_spot, tv_spot),
        tv_vix=tv_vix,
        rh_vix=rh_vix,
        vix_diff=_sub(rh_vix, tv_vix),
        vol_gate_allowed=vol_gate_allowed,
        vol_gate_reason=vol_gate_reason,
        atm_strike=atm,
        legs=legs,
        notes=notes,
    )


def report_to_dict(report: SymbolReport) -> dict:
    payload = asdict(report)
    return payload


def format_report(report: SymbolReport) -> str:
    lines = [
        f"{report.symbol}  expiry={report.expiry or '—'}",
        f"  spot  TV={_fmt(report.tv_spot)}  RH={_fmt(report.rh_spot)}  "
        f"diff={_fmt(report.spot_diff, signed=True)}",
        f"  VIX   TV={_fmt(report.tv_vix)}  RH={_fmt(report.rh_vix)}  "
        f"diff={_fmt(report.vix_diff, signed=True)}",
    ]
    if report.vol_gate_allowed is not None:
        gate = "ALLOW" if report.vol_gate_allowed else "REFUSE"
        extra = f" ({report.vol_gate_reason})" if report.vol_gate_reason else ""
        lines.append(f"  vol gate: {gate}{extra}")
    if report.atm_strike is not None:
        lines.append(f"  ATM strike {report.atm_strike:g}")
        lines.append(
            "  side  bid TV/RH        ask TV/RH        IV TV/RH          vol TV/RH     OI TV/RH"
        )
        for leg in report.legs:
            iv_mark = ""
            if leg.iv_warn:
                pct = 100.0 * (leg.iv_rel_diff or 0.0)
                iv_mark = f"  ** IV {pct:+.1f}%"
            if leg.iv_unit_suspect:
                iv_mark += "  (unit?)"
            lines.append(
                f"  {leg.side:<4}  "
                f"{_fmt(leg.tv_bid):>6}/{_fmt(leg.rh_bid):<6}  "
                f"{_fmt(leg.tv_ask):>6}/{_fmt(leg.rh_ask):<6}  "
                f"{_fmt(leg.tv_iv, digits=4):>7}/{_fmt(leg.rh_iv, digits=4):<7}  "
                f"{_fmt_int(leg.tv_volume):>7}/{_fmt_int(leg.rh_volume):<7}  "
                f"{_fmt_int(leg.tv_oi):>7}/{_fmt_int(leg.rh_oi):<7}"
                f"{iv_mark}"
            )
    for note in report.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def ensure_tv_import() -> None:
    """Make `tradingview_mcp` importable. Prefers a normal install, then the
    `uv tool install tradingview-mcp-server` environment."""
    try:
        import tradingview_mcp  # noqa: F401
        return
    except ImportError:
        pass
    tools = Path.home() / ".local/share/uv/tools/tradingview-mcp-server"
    for site in sorted(tools.glob("lib/python*/site-packages")):
        path = str(site)
        if path not in sys.path:
            sys.path.insert(0, path)
        try:
            import tradingview_mcp  # noqa: F401
            return
        except ImportError:
            continue
    raise ImportError(
        "tradingview_mcp is not importable. Install with:\n"
        "  uv tool install --python 3.12 tradingview-mcp-server"
    )


def fetch_tv(symbol: str, expiry: str | None) -> dict:
    """Live TradingView-MCP (Yahoo) fetch. Isolated so tests can stub it."""
    ensure_tv_import()
    from tradingview_mcp.core.services.yahoo_finance_service import get_price
    from tradingview_mcp.core.services.options_service import get_options_chain

    quote = get_price(symbol)
    vix = get_price("^VIX")
    chain = get_options_chain(symbol, expiry)
    return {
        "spot": _opt_float((quote or {}).get("price")),
        "vix": _opt_float((vix or {}).get("price")),
        "chain": chain if isinstance(chain, dict) and "error" not in chain else None,
        "chain_error": None if not isinstance(chain, dict) else chain.get("error"),
        "available_expiries": (chain or {}).get("available_expiries") or [],
    }


def fetch_rh(symbol: str, expiry: str | None, min_dte: int, max_dte: int,
             today: date) -> dict:
    """Live Robinhood MCP fetch."""
    from options_trader.data.mcp_provider import MCPDataProvider
    from options_trader.risk.vol_regime import check_vol_regime, fetch_vix_closes
    from options_trader.config import StrategyConfig

    provider = MCPDataProvider()
    spot = provider.get_spot(symbol)
    expirations = provider.get_expirations(symbol)
    chosen = expiry or pick_expiry(expirations, today, min_dte, max_dte)
    chain = None
    notes = []
    if chosen:
        snap = provider.get_chain(symbol, chosen)
        chain = snap.chain
        chosen = snap.expiration
    else:
        notes.append(f"no RH expiration in DTE {min_dte}-{max_dte}")

    cfg = StrategyConfig()
    closes = fetch_vix_closes(cfg.vix_spike_lookback_days)
    gate = None
    rh_vix = None
    if closes is None or len(closes) == 0:
        notes.append("RH/yfinance VIX unavailable")
    else:
        rh_vix = float(closes.iloc[-1])
        gate = check_vol_regime(
            cfg.vix_entry_ceiling, cfg.vix_spike_pct,
            cfg.vix_spike_lookback_days, vix_closes=closes,
        )
    return {
        "spot": float(spot),
        "vix": rh_vix,
        "expiry": chosen,
        "chain": chain,
        "expirations": expirations,
        "vol_gate_allowed": None if gate is None else gate.allowed,
        "vol_gate_reason": None if gate is None else gate.reason,
        "notes": notes,
    }


def run_compare(
    symbols: list[str],
    *,
    expiry: str | None = None,
    min_dte: int = 1,
    max_dte: int = 7,
    today: date | None = None,
    iv_rel_warn: float = IV_REL_WARN,
    fetch_tv_fn: Callable[..., dict] = fetch_tv,
    fetch_rh_fn: Callable[..., dict] = fetch_rh,
    skip_rh: bool = False,
) -> list[SymbolReport]:
    today = today or date.today()
    reports: list[SymbolReport] = []
    for symbol in symbols:
        notes: list[str] = []
        tv: dict = {}
        rh: dict = {}
        try:
            tv = fetch_tv_fn(symbol, expiry)
            if tv.get("chain_error"):
                notes.append(f"TV chain: {tv['chain_error']}")
        except Exception as e:
            notes.append(f"TV fetch failed: {type(e).__name__}: {e}")
            tv = {}

        chosen = expiry
        if not skip_rh:
            try:
                rh = fetch_rh_fn(symbol, expiry, min_dte, max_dte, today)
                notes.extend(rh.get("notes") or [])
                chosen = rh.get("expiry") or chosen
            except Exception as e:
                notes.append(f"RH fetch failed: {type(e).__name__}: {e}")
                rh = {}
        else:
            notes.append("RH skipped")

        if chosen is None and tv.get("available_expiries"):
            chosen = pick_expiry(tv["available_expiries"], today, min_dte, max_dte)

        tv_expiry = None
        if isinstance(tv.get("chain"), dict):
            tv_expiry = tv["chain"].get("requested_expiry")
        if chosen and chosen != tv_expiry:
            # TV defaults to nearest expiry (often 0DTE). Refetch the
            # in-window date so ATM legs are comparable.
            try:
                tv = fetch_tv_fn(symbol, chosen)
                if tv.get("chain_error"):
                    notes.append(f"TV chain: {tv['chain_error']}")
            except Exception as e:
                notes.append(f"TV refetch failed: {type(e).__name__}: {e}")

        reports.append(compare_symbol(
            symbol=symbol,
            expiry=chosen,
            tv_spot=tv.get("spot"),
            rh_spot=rh.get("spot"),
            tv_vix=tv.get("vix"),
            rh_vix=rh.get("vix"),
            tv_chain=tv.get("chain"),
            rh_chain=rh.get("chain"),
            vol_gate_allowed=rh.get("vol_gate_allowed"),
            vol_gate_reason=rh.get("vol_gate_reason"),
            iv_rel_warn=iv_rel_warn,
            notes=notes,
        ))
    return reports


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sub(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return a - b


def _fmt(value: float | None, digits: int = 2, signed: bool = False) -> str:
    if value is None:
        return "—"
    spec = f"+.{digits}f" if signed else f".{digits}f"
    return format(value, spec)


def _fmt_int(value: int | None) -> str:
    if value is None:
        return "—"
    return str(value)
