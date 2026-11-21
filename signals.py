"""
Pure indicator + entry-signal functions shared by backtest.py (and, later, main.py).

Every strategy function takes an OHLCV DataFrame (columns Open High Low Close Volume,
DatetimeIndex) and returns a DataFrame with:
    entry      bool   — signal on the CLOSE of this bar (fill happens next bar open)
    stop_frac  float  — initial stop distance as a fraction of entry price
    atr_frac   float  — ATR(14)/close, used by ATR-based exits
No look-ahead: every value on row i uses data up to and including bar i only.
"""
import numpy as np
import pandas as pd


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df, n=14):
    pc = df.Close.shift(1)
    tr = pd.concat([df.High - df.Low, (df.High - pc).abs(), (df.Low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


# ── Strategy 0: current main.py logic (control) ───────────────────────────────
def baseline(df, fast=9, slow=21, tp=1.016, sl=0.990):
    """Mirrors strategy() entry filters on 1m bars (candidate ranking by 24h gainers
    cannot be reproduced from klines, so it is applied over a fixed universe)."""
    c = df.Close
    sma_f, sma_s = c.rolling(fast).mean(), c.rolling(slow).mean()
    split = (sma_f - sma_s) / c * 100
    min_split = (tp - 1) * 100 * 0.20
    mom = (c / c.shift(29)) > 1                          # 30-candle cumulative change > 0
    widening = (sma_f - sma_s) > (sma_f - sma_s).shift(5)
    rising = ((c > c.shift(1)).astype(int) + (c.shift(1) > c.shift(2)).astype(int)
              + (c.shift(2) > c.shift(3)).astype(int)) >= 2
    vz = ((df.Volume - df.Volume.rolling(30).mean()) / df.Volume.rolling(30).std()).clip(lower=0) * 2
    risk = ((split.abs() * 10 + vz) / 2).clip(upper=10)
    risk_ok = ~((risk < 2) | ((risk >= 6) & (risk < 8)))
    entry = (sma_f > sma_s) & mom & (split >= min_split) & widening & rising & risk_ok
    return pd.DataFrame({"entry": entry.fillna(False), "stop_frac": 1 - sl, "atr_frac": atr(df) / c})


# ── Strategy A: trend breakout, volatility stops ──────────────────────────────
def trend_breakout(df, lookback=20, trend_fast=20, trend_slow=50):
    c = df.Close
    up = ema(c, trend_fast) > ema(c, trend_slow)
    brk = c > df.High.shift(1).rolling(lookback).max()
    a = atr(df)
    return pd.DataFrame({"entry": (up & brk).fillna(False), "stop_frac": 1.5 * a / c, "atr_frac": a / c})


# ── Strategy B: pullback in an established uptrend ────────────────────────────
def pullback(df, trend_fast=50, trend_slow=200, rsi_lo=40):
    c = df.Close
    up = (ema(c, trend_fast) > ema(c, trend_slow)) & (c > ema(c, trend_slow))
    r = rsi(c)
    dipped = (r.shift(1) < rsi_lo) | (r.shift(2) < rsi_lo)
    reclaim = (r >= rsi_lo) & (r > r.shift(1))
    a = atr(df)
    return pd.DataFrame({"entry": (up & dipped & reclaim).fillna(False),
                         "stop_frac": 1.5 * a / c, "atr_frac": a / c})


# ── Strategy C: Donchian trend-following (slow, fat-tailed winners) ───────────
def donchian(df, lookback=55, trend_fast=50, trend_slow=200, stop_atr=2.0):
    c = df.Close
    up = ema(c, trend_fast) > ema(c, trend_slow)
    brk = c > df.High.shift(1).rolling(lookback).max()
    a = atr(df)
    return pd.DataFrame({"entry": (up & brk).fillna(False), "stop_frac": stop_atr * a / c, "atr_frac": a / c})


# ── Strategy D: volatility squeeze breakout ───────────────────────────────────
def squeeze(df, n=20, pct_window=100, pct=0.2):
    c = df.Close
    mid, sd = c.rolling(n).mean(), c.rolling(n).std()
    bw = (4 * sd) / mid
    tight = bw.shift(1) <= bw.shift(1).rolling(pct_window).quantile(pct)
    brk = c > (mid + 2 * sd)
    up = ema(c, 50) > ema(c, 200)
    a = atr(df)
    return pd.DataFrame({"entry": (tight & brk & up).fillna(False), "stop_frac": 2.0 * a / c, "atr_frac": a / c})


# ── Strategy E: short-term mean reversion inside a long-term uptrend ──────────
def meanrev(df, rsi_n=2, thresh=10, trend=200):
    c = df.Close
    up = c > ema(c, trend)
    oversold = rsi(c, rsi_n) < thresh
    a = atr(df)
    return pd.DataFrame({"entry": (up & oversold).fillna(False), "stop_frac": 2.5 * a / c, "atr_frac": a / c})


STRATEGIES = {
    "baseline": baseline,
    "trend_breakout": trend_breakout,
    "pullback": pullback,
    "donchian_1h": donchian,
    "donchian_4h": donchian,
    "squeeze_1h": squeeze,
    "meanrev_1h": meanrev,
    "pullback_1h": pullback,
    "trend_breakout_1h": trend_breakout,
}
