"""ATR vs implied vol — realized range next to the options tape.

ATR is backward-looking (typical daily range). ATM IV is forward-looking.
This module computes both, converts them to the same units, and classifies
the ATR-trend × IV-richness bucket from the ATR/Greeks playbook. It does
not place orders or pick a live trade.

ATR annualization treats mean true range as if it were a 1-sigma daily
move, which overstates realized vol relative to close-to-close HV. The
ratio `atr / (spot * IV / sqrt(252))` is the practical comparison:
>1 means recent ranges exceed the option market's daily implied move.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import log, sqrt

import pandas as pd

from .math import TRADING_DAYS_PER_YEAR

ATR_PERIOD = 14
ATR_SMA_PERIOD = 20
ATR_TREND_LOOKBACK = 10          # trading days
HV_PERIOD = 20
ATR_TREND_THRESH = 0.10          # ±10% ATR change → rising/falling
IV_VS_HV_THRESH = 0.10           # IV 10% below/above HV → cheap/rich
RANGE_VS_IMPLIED_THRESH = 0.10   # ATR vs implied daily move
TARGET_PUT_DELTA = 0.15


@dataclass
class AtrIvSnapshot:
    symbol: str
    spot: float
    atr_14: float
    atr_20: float
    atr_pct: float                 # ATR-14 / spot
    atr_10d_ago: float | None
    atr_trend_pct: float | None    # (atr_14 / atr_10d_ago) - 1
    atr_trend: str                 # rising | falling | flat
    hv_20: float | None            # annualized close-to-close
    atm_iv: float | None           # decimal, ATM call/put average
    iv_dte: int | None
    iv_expiry: str | None
    implied_daily_move: float | None   # spot * IV / sqrt(252)
    atr_vs_implied: float | None       # ATR / implied_daily_move
    iv_vs_hv: float | None             # IV / HV - 1
    band_2atr_down: float
    band_2atr_up: float
    put_015_delta_strike: float | None
    put_015_inside_2atr: bool | None
    iv_context: str                # cheap | rich | fair | unknown
    bucket: str
    playbook: str
    notes: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def true_range_series(high: pd.Series, low: pd.Series,
                      close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev).abs(), (low - prev).abs()],
        axis=1,
    ).max(axis=1)
    return tr


def wilder_atr(tr: pd.Series, period: int = ATR_PERIOD) -> pd.Series:
    """Wilder ATR: SMA seed, then (prev*(n-1) + tr) / n."""
    if period < 1:
        raise ValueError("period must be >= 1")
    vals = tr.astype(float)
    out = pd.Series(float("nan"), index=vals.index)
    clean = vals.dropna()
    if len(clean) < period:
        return out
    prev = float(clean.iloc[:period].mean())
    out.loc[clean.index[period - 1]] = prev
    for label in clean.index[period:]:
        prev = (prev * (period - 1) + float(clean.loc[label])) / period
        out.loc[label] = prev
    return out


def sma_atr(tr: pd.Series, period: int = ATR_SMA_PERIOD) -> pd.Series:
    return tr.rolling(period, min_periods=period).mean()


def realized_vol(close: pd.Series, period: int = HV_PERIOD) -> float | None:
    """Annualized close-to-close HV over `period` log returns."""
    c = close.astype(float)
    logs: list[float] = []
    prev = None
    for x in c:
        if prev is not None and prev > 0 and x > 0 and not pd.isna(x):
            logs.append(log(x / prev))
        prev = x if not pd.isna(x) else prev
    if len(logs) < period:
        return None
    std = float(pd.Series(logs[-period:]).std(ddof=1))
    if std <= 0:
        return None
    return std * sqrt(TRADING_DAYS_PER_YEAR)


def classify_atr_trend(atr_now: float, atr_then: float | None,
                       thresh: float = ATR_TREND_THRESH) -> tuple[str, float | None]:
    if atr_then is None or atr_then <= 0 or atr_now <= 0:
        return "unknown", None
    change = (atr_now / atr_then) - 1.0
    if change >= thresh:
        return "rising", change
    if change <= -thresh:
        return "falling", change
    return "flat", change


def classify_iv_context(atm_iv: float | None, hv: float | None,
                        atr_vs_implied: float | None) -> str:
    """cheap = IV lagging realized range/vol; rich = IV ahead of it."""
    votes_cheap = 0
    votes_rich = 0
    votes = 0
    if atm_iv is not None and hv is not None and hv > 0:
        votes += 1
        gap = (atm_iv / hv) - 1.0
        if gap <= -IV_VS_HV_THRESH:
            votes_cheap += 1
        elif gap >= IV_VS_HV_THRESH:
            votes_rich += 1
    if atr_vs_implied is not None:
        votes += 1
        if atr_vs_implied >= 1.0 + RANGE_VS_IMPLIED_THRESH:
            votes_cheap += 1
        elif atr_vs_implied <= 1.0 - RANGE_VS_IMPLIED_THRESH:
            votes_rich += 1
    if votes == 0:
        return "unknown"
    if votes_cheap > votes_rich:
        return "cheap"
    if votes_rich > votes_cheap:
        return "rich"
    return "fair"


def classify_bucket(atr_trend: str, iv_context: str) -> tuple[str, str]:
    """Playbook bucket: expansion → buy vol; contraction → sell vol."""
    if atr_trend == "rising" and iv_context == "cheap":
        return (
            "expansion",
            "Buy vol: long straddle/strangle (neutral) or long call/put (directional)",
        )
    if atr_trend == "falling" and iv_context == "rich":
        return (
            "contraction",
            "Sell vol: iron condor / iron fly (neutral) or credit spreads (directional)",
        )
    if atr_trend == "rising" and iv_context == "rich":
        return (
            "trending_volatile",
            "High realized range already in the premium: OTM debit vertical, not naked long or short premium",
        )
    if atr_trend == "falling" and iv_context == "cheap":
        return (
            "quiet_cheap",
            "Range shrinking and options still cheap — no clean playbook sleeve; wait or small defined-risk",
        )
    if atr_trend == "flat":
        return ("mixed", "ATR not trending; do not force a vol-expansion/contraction sleeve")
    return ("mixed", "ATR trend and IV richness disagree or are unknown")


def snapshot_from_ohlc(
    symbol: str,
    ohlc: pd.DataFrame,
    *,
    atm_iv: float | None = None,
    iv_dte: int | None = None,
    iv_expiry: str | None = None,
    put_015_delta_strike: float | None = None,
) -> AtrIvSnapshot:
    """`ohlc` needs High, Low, Close; oldest row first."""
    notes: list[str] = []
    need = {"High", "Low", "Close"}
    missing = need - set(ohlc.columns)
    if missing:
        raise ValueError(f"ohlc missing columns {sorted(missing)}")
    high, low, close = ohlc["High"], ohlc["Low"], ohlc["Close"]
    spot = float(close.iloc[-1])
    tr = true_range_series(high, low, close)
    atr14_s = wilder_atr(tr, ATR_PERIOD)
    atr20_s = sma_atr(tr, ATR_SMA_PERIOD)
    atr_14 = float(atr14_s.iloc[-1]) if pd.notna(atr14_s.iloc[-1]) else float("nan")
    atr_20 = float(atr20_s.iloc[-1]) if pd.notna(atr20_s.iloc[-1]) else float("nan")
    if pd.isna(atr_14):
        raise ValueError(f"{symbol}: not enough bars for ATR-{ATR_PERIOD}")
    atr_10d_ago = None
    if len(atr14_s.dropna()) > ATR_TREND_LOOKBACK:
        prev = atr14_s.dropna().iloc[-1 - ATR_TREND_LOOKBACK]
        if pd.notna(prev):
            atr_10d_ago = float(prev)
    trend, trend_pct = classify_atr_trend(atr_14, atr_10d_ago)
    hv = realized_vol(close, HV_PERIOD)
    implied_daily = None
    atr_vs_implied = None
    if atm_iv is not None and atm_iv > 0 and spot > 0:
        implied_daily = spot * atm_iv / sqrt(TRADING_DAYS_PER_YEAR)
        if implied_daily > 0:
            atr_vs_implied = atr_14 / implied_daily
    else:
        notes.append("no ATM IV — cannot compare ATR to implied daily move")
    iv_vs_hv = None
    if atm_iv is not None and hv is not None and hv > 0:
        iv_vs_hv = (atm_iv / hv) - 1.0
    iv_context = classify_iv_context(atm_iv, hv, atr_vs_implied)
    bucket, playbook = classify_bucket(trend, iv_context)
    inside = None
    if put_015_delta_strike is not None and spot > 0:
        inside = put_015_delta_strike >= (spot - 2.0 * atr_14)
        if inside:
            notes.append(
                f"0.15Δ put strike {put_015_delta_strike:g} sits inside "
                f"2×ATR ({spot - 2.0 * atr_14:.2f}); playbook says go further OTM"
            )
    return AtrIvSnapshot(
        symbol=symbol,
        spot=spot,
        atr_14=atr_14,
        atr_20=atr_20 if not pd.isna(atr_20) else float("nan"),
        atr_pct=atr_14 / spot if spot else float("nan"),
        atr_10d_ago=atr_10d_ago,
        atr_trend_pct=trend_pct,
        atr_trend=trend,
        hv_20=hv,
        atm_iv=atm_iv,
        iv_dte=iv_dte,
        iv_expiry=iv_expiry,
        implied_daily_move=implied_daily,
        atr_vs_implied=atr_vs_implied,
        iv_vs_hv=iv_vs_hv,
        band_2atr_down=spot - 2.0 * atr_14,
        band_2atr_up=spot + 2.0 * atr_14,
        put_015_delta_strike=put_015_delta_strike,
        put_015_inside_2atr=inside,
        iv_context=iv_context,
        bucket=bucket,
        playbook=playbook,
        notes=notes,
    )


def atr_lookup_from_ohlc(symbol: str, ohlc: pd.DataFrame) -> dict[tuple[str, str], float]:
    """Map (symbol, YYYY-MM-DD) -> Wilder ATR-14 using bars up to that day."""
    need = {"High", "Low", "Close"}
    if need - set(ohlc.columns):
        raise ValueError(f"ohlc missing {sorted(need - set(ohlc.columns))}")
    tr = true_range_series(ohlc["High"], ohlc["Low"], ohlc["Close"])
    atr = wilder_atr(tr, ATR_PERIOD)
    out: dict[tuple[str, str], float] = {}
    for idx, val in atr.dropna().items():
        day = pd.Timestamp(idx).strftime("%Y-%m-%d")
        if float(val) > 0:
            out[(symbol, day)] = float(val)
    return out


def fetch_atr_lookup(symbols: list[str], start: str, end: str) -> dict[tuple[str, str], float]:
    """yfinance OHLC → ATR-14 lookup covering start..end (with warmup)."""
    from datetime import date as date_cls, timedelta
    import yfinance as yf
    warmup = (date_cls.fromisoformat(start) - timedelta(days=60)).isoformat()
    end_excl = (date_cls.fromisoformat(end) + timedelta(days=5)).isoformat()
    out: dict[tuple[str, str], float] = {}
    for symbol in symbols:
        hist = yf.Ticker(symbol).history(
            start=warmup, end=end_excl, auto_adjust=False,
        )
        if hist.empty:
            continue
        bars = hist[["High", "Low", "Close"]].dropna()
        out.update(atr_lookup_from_ohlc(symbol, bars))
    return out


def fetch_atr14(symbol: str, period: str = "3mo") -> float | None:
    """Live Wilder ATR-14 from yfinance daily bars, or None on any failure."""
    try:
        import yfinance as yf
        hist = yf.Ticker(symbol).history(period=period, auto_adjust=False)
        bars = hist[["High", "Low", "Close"]].dropna()
        if len(bars) < ATR_PERIOD + 1:
            return None
        tr = true_range_series(bars["High"], bars["Low"], bars["Close"])
        atr = wilder_atr(tr, ATR_PERIOD)
        last = atr.iloc[-1]
        if pd.isna(last) or float(last) <= 0:
            return None
        return float(last)
    except Exception:
        return None


def format_snapshot(s: AtrIvSnapshot) -> str:
    def pct(x, digits=1):
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return "—"
        return f"{100.0 * x:.{digits}f}%"

    def num(x, digits=2):
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return "—"
        return f"{x:.{digits}f}"

    trend = s.atr_trend
    if s.atr_trend_pct is not None:
        trend = f"{s.atr_trend} ({s.atr_trend_pct:+.1%} over {ATR_TREND_LOOKBACK}d)"
    lines = [
        f"{s.symbol}  spot {num(s.spot)}",
        f"  ATR-14 ${num(s.atr_14)}  ({pct(s.atr_pct)} of spot)   "
        f"ATR-20 ${num(s.atr_20)}   trend {trend}",
        f"  2×ATR band  {num(s.band_2atr_down)} – {num(s.band_2atr_up)}",
        f"  HV-20 {pct(s.hv_20, 1)}   ATM IV {pct(s.atm_iv, 1)}"
        + (f"  ({s.iv_dte} DTE, {s.iv_expiry})" if s.iv_expiry else ""),
        f"  implied daily move ${num(s.implied_daily_move)}   "
        f"ATR / implied {num(s.atr_vs_implied)}   "
        f"IV vs HV {pct(s.iv_vs_hv, 1) if s.iv_vs_hv is not None else '—'}",
        f"  IV context: {s.iv_context}   bucket: {s.bucket}",
        f"  playbook: {s.playbook}",
    ]
    if s.put_015_delta_strike is not None:
        flag = "INSIDE 2×ATR" if s.put_015_inside_2atr else "outside 2×ATR"
        lines.append(f"  0.15Δ put strike {num(s.put_015_delta_strike)} ({flag})")
    for n in s.notes:
        lines.append(f"  note: {n}")
    return "\n".join(lines)
