"""
Offline backtester with realistic costs and walk-forward reporting.

    python backtest.py --strategy baseline --interval 1m --days 30
    python backtest.py --strategy trend_breakout --interval 15m --days 90
    python backtest.py --strategy pullback --interval 15m --days 90
    python backtest.py --all

Public kline data only — no API keys needed. Data is cached in ./data/*.csv.
Fills: signal on bar close -> entry at NEXT bar open (+slippage). Stops fill at the
stop price (or the open if it gapped through), -slippage. Within a bar the stop is
assumed to hit before any target (conservative). Fees charged per side.
One position at a time across the whole universe (matches the live bot).
"""
import argparse
import os
import sys
import numpy as np
import pandas as pd

import signals

DATA_DIR = "data"
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
                   "ADAUSDT", "DOGEUSDT", "TRXUSDT", "AVAXUSDT", "LINKUSDT"]

# Exit configs per strategy.
#  ratchet : current bot — fixed stop, when high >= base*tp the base ratchets up and stop = base*ratchet_sl
#  atr     : stop_frac initial stop; fixed target tp_atr*ATR if set, else chandelier trail trail_atr*ATR
EXITS = {
    "baseline":       dict(mode="ratchet", tp=1.016, ratchet_sl=0.9915, max_bars=4000),
    "trend_breakout": dict(mode="atr", tp_atr=None, trail_atr=3.0, max_bars=200),
    "pullback":       dict(mode="atr", tp_atr=3.0, trail_atr=None, max_bars=200),
    "donchian_1h":    dict(mode="atr", tp_atr=None, trail_atr=3.0, max_bars=500),
    "donchian_4h":    dict(mode="atr", tp_atr=None, trail_atr=3.0, max_bars=300),
    "squeeze_1h":     dict(mode="atr", tp_atr=None, trail_atr=3.0, max_bars=300),
    "meanrev_1h":     dict(mode="atr", tp_atr=1.5, trail_atr=None, max_bars=24),
    "pullback_1h":    dict(mode="atr", tp_atr=3.0, trail_atr=None, max_bars=120),
    "trend_breakout_1h": dict(mode="atr", tp_atr=None, trail_atr=3.0, max_bars=300),
}

# strategy -> (interval, days). Single source of truth, also used by autotest.py.
DEFAULTS = {
    "baseline": ("1m", 30), "trend_breakout": ("15m", 90), "pullback": ("15m", 90),
    "donchian_1h": ("1h", 365), "donchian_4h": ("4h", 730), "squeeze_1h": ("1h", 365),
    "meanrev_1h": ("1h", 365), "pullback_1h": ("1h", 365), "trend_breakout_1h": ("1h", 365),
}


# ── Data ──────────────────────────────────────────────────────────────────────
MAX_AGE_H = None   # autotest sets this so cached candles are refreshed periodically


def load(symbol, interval, days):
    import time
    from binance.client import Client
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{symbol}_{interval}_{days}d.csv")
    if os.path.exists(path) and (MAX_AGE_H is None or time.time() - os.path.getmtime(path) < MAX_AGE_H * 3600):
        return pd.read_csv(path, index_col=0, parse_dates=True)
    print(f"  fetching {symbol} {interval} {days}d ...", file=sys.stderr)
    k = Client("", "").get_historical_klines(symbol, interval, f"{days} days ago UTC")
    df = pd.DataFrame(k).iloc[:, :6]
    df.columns = ["Time", "Open", "High", "Low", "Close", "Volume"]
    df["Time"] = pd.to_datetime(df["Time"], unit="ms")
    df = df.set_index("Time").astype(float)
    df.to_csv(path)
    return df


# ── Simulation ────────────────────────────────────────────────────────────────
FILLS = {"signals": 0, "filled": 0}


def simulate_symbol(df, sig, ex, fee, slip, symbol, entry_mode="market", slip_in=None):
    """entry_mode 'market': buy next open + slip_in. 'limit': resting buy at signal close;
    fills only if next bar's low <= limit (at min(open, limit)), else the signal is missed."""
    if slip_in is None:
        slip_in = slip
    o, h, l = df.Open.values, df.High.values, df.Low.values
    cl = df.Close.values
    entry = sig.entry.values
    stop_frac = sig.stop_frac.values
    atr_frac = sig.atr_frac.values
    idx = df.index
    n = len(df)
    trades = []
    i = 0
    while i < n - 2:
        if not entry[i] or not np.isfinite(stop_frac[i]) or stop_frac[i] <= 0:
            i += 1
            continue
        j = i + 1                                   # entry bar
        FILLS["signals"] += 1
        if entry_mode == "limit":
            if l[j] > cl[i]:                        # limit never touched -> missed trade
                i += 1
                continue
            ep = min(o[j], cl[i])
        else:
            ep = o[j] * (1 + slip_in)
        FILLS["filled"] += 1
        sf = stop_frac[i]
        stop = ep * (1 - sf)
        a_abs = atr_frac[i] * ep if np.isfinite(atr_frac[i]) else 0.0
        base = ep
        hh = ep
        tp_price = ep + ex["tp_atr"] * a_abs if ex["mode"] == "atr" and ex.get("tp_atr") else None
        exit_px, exit_k, reason = None, None, "time"
        k = j
        while k < n and k - j < ex["max_bars"]:
            # stop first (conservative)
            if l[k] <= stop:
                exit_px = min(stop, o[k]) if k > j else stop
                exit_k, reason = k, "stop"
                break
            if ex["mode"] == "ratchet":
                tp = base * ex["tp"]
                if h[k] >= tp:
                    base = tp
                    stop = max(stop, base * ex["ratchet_sl"])
            else:
                if tp_price is not None and h[k] >= tp_price:
                    exit_px, exit_k, reason = tp_price, k, "tp"
                    break
                if ex.get("trail_atr"):
                    hh = max(hh, h[k])
                    stop = max(stop, hh - ex["trail_atr"] * a_abs)
            k += 1
        if exit_px is None:
            exit_k = min(k, n - 1)
            exit_px = df.Close.values[exit_k]
        xp = exit_px * (1 - slip)
        ret = xp / ep - 1 - 2 * fee
        trades.append(dict(symbol=symbol, entry_time=idx[j], exit_time=idx[exit_k],
                           ret=ret, stop_frac=sf, reason=reason, bars=exit_k - j))
        i = exit_k + 1
    return trades


def run_strategy(name, interval, days, symbols, fee, slip, params=None, entry_mode="market", slip_in=None):
    fn = signals.STRATEGIES[name]
    ex = {**EXITS[name], **(params or {})}
    all_trades = []
    for s in symbols:
        try:
            df = load(s, interval, days)
        except Exception as e:
            print(f"  skip {s}: {e}", file=sys.stderr)
            continue
        sig = fn(df)
        all_trades += simulate_symbol(df, sig, ex, fee, slip, s, entry_mode, slip_in)
    t = pd.DataFrame(all_trades)
    if t.empty:
        return t
    return t.sort_values("entry_time").reset_index(drop=True)


def one_at_a_time(t):
    """Keep only trades that start after the previous kept trade exited."""
    keep, free = [], pd.Timestamp.min
    for r in t.itertuples():
        if r.entry_time > free:
            keep.append(r.Index)
            free = r.exit_time
    return t.loc[keep].reset_index(drop=True)


# ── Wide universe ─────────────────────────────────────────────────────────────
STABLE_BASES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "EURI", "AEUR", "USD1", "XUSD", "BFUSD", "PAXG", "WBTC", "WBETH", "BNSOL"}


def top_universe(n=40, refresh=False):
    """Top-n USDT pairs by 24h quote volume, frozen to data/universe.json so results are reproducible.
    NOTE: today's top-volume list is survivor-biased (coins that died aren't in it)."""
    import json
    from binance.client import Client
    os.makedirs(DATA_DIR, exist_ok=True)
    p = os.path.join(DATA_DIR, f"universe_{n}.json")
    if os.path.exists(p) and not refresh:
        return json.load(open(p))
    t = pd.DataFrame(Client("", "").get_ticker())
    t = t[t.symbol.str.endswith("USDT")].copy()
    t["base"] = t.symbol.str[:-4]
    t = t[~t.base.isin(STABLE_BASES) & ~t.base.str.endswith(("UP", "DOWN", "BULL", "BEAR"))]
    t = t[t.base.str.isascii()]
    t["qv"] = t.quoteVolume.astype(float)
    syms = list(t.sort_values("qv", ascending=False).head(n).symbol)
    json.dump(syms, open(p, "w"))
    return syms


def weekly_t(t):
    """t-stat of mean trade return, clustered by entry week (trades in the same week are correlated)."""
    w = t.groupby(t.entry_time.dt.to_period("W")).ret.mean()
    if len(w) < 8 or w.std() == 0:
        return float("nan"), len(w)
    return w.mean() / (w.std() / np.sqrt(len(w))), len(w)


def thirds(t):
    lo, hi = t.entry_time.min(), t.entry_time.max()
    e = [lo + (hi - lo) * k / 3 for k in range(4)]
    return [t[(t.entry_time > e[k]) & (t.entry_time <= e[k + 1])].ret.mean() * 100 for k in range(3)]


def wide_report(name, interval, days, symbols, scenarios):
    for label, kw in scenarios:
        FILLS["signals"] = FILLS["filled"] = 0
        t = run_strategy(name, interval, days, symbols, **kw)
        if t.empty:
            print(f"[{name}/{label}] no trades"); continue
        fill = FILLS["filled"] / max(FILLS["signals"], 1)
        span = max((t.entry_time.max() - t.entry_time.min()).days, 1)
        tt, nw = weekly_t(t)
        oos = t[t.entry_time > t.entry_time.min() + (t.entry_time.max() - t.entry_time.min()) * (2 / 3)]
        ott, onw = weekly_t(oos)
        w = t[t.ret > 0].ret
        lo_ = t[t.ret <= 0].ret
        pf = w.sum() / abs(lo_.sum()) if len(lo_) else float("inf")
        ser = one_at_a_time(t)
        eq, mdd = equity_curve(ser, 0.015)
        print(f"\n--- {name} | {label} | {len(symbols)} symbols | {interval} {days}d | fill-rate {fill:.0%}")
        print(f"  ALL signals: n={len(t)} WR={len(w)/len(t):.1%} exp={t.ret.mean()*100:+.3f}%/tr PF={pf:.2f} "
              f"weekly-clustered t={tt:+.2f} ({nw}wk)  thirds={['%+.2f%%' % x for x in thirds(t)]}")
        print(f"  OOS (last 1/3): n={len(oos)} exp={oos.ret.mean()*100:+.3f}%/tr t={ott:+.2f} ({onw}wk)")
        print(f"  bot-realistic (1 position at a time): n={len(ser)} exp={ser.ret.mean()*100:+.3f}%/tr  $50->${eq:,.2f} maxDD={mdd:.1%}")
        by = t.groupby("symbol").ret.agg(["count", "mean"]).sort_values("mean")
        pos = (by["mean"] > 0).sum()
        print(f"  symbols with positive expectancy: {pos}/{len(by)}")


# ── Metrics ───────────────────────────────────────────────────────────────────
def equity_curve(t, risk=0.015, cap=0.5, start=50.0):
    eq, peak, mdd, curve = start, start, 0.0, []
    for r in t.itertuples():
        frac = min(cap, risk / r.stop_frac) if r.stop_frac > 0 else 0
        eq *= 1 + frac * r.ret
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
        curve.append(eq)
    return eq, mdd


def report(label, t, risk, span_days):
    if t is None or len(t) == 0:
        print(f"{label:<28} no trades")
        return
    w = t[t.ret > 0].ret
    lo = t[t.ret <= 0].ret
    pf = w.sum() / abs(lo.sum()) if len(lo) and lo.sum() != 0 else float("inf")
    eq, mdd = equity_curve(t, risk)
    print(f"{label:<28} n={len(t):>4}  WR={len(w)/len(t):5.1%}  exp={t.ret.mean()*100:+.3f}%/tr  "
          f"PF={pf:4.2f}  avgW={w.mean()*100 if len(w) else 0:+.2f}% avgL={lo.mean()*100 if len(lo) else 0:+.2f}%  "
          f"trades/day={len(t)/max(span_days,1):.1f}  $50→${eq:,.2f}  maxDD={mdd:.1%}")


def evaluate(name, interval, days, symbols, fee, slip, risk, params=None, quiet=False):
    t = run_strategy(name, interval, days, symbols, fee, slip, params)
    if t.empty:
        print(f"[{name}] no trades")
        return None
    t = one_at_a_time(t)
    split = t.entry_time.min() + (t.entry_time.max() - t.entry_time.min()) * (2 / 3)
    train, test = t[t.entry_time <= split], t[t.entry_time > split]
    span = (t.entry_time.max() - t.entry_time.min()).days or 1
    print(f"\n=== {name} | {interval} | {days}d | fee={fee*100:.3f}%/side slip={slip*100:.3f}%/side | risk={risk:.1%} ===")
    report("ALL", t, risk, span)
    report("train (first 2/3)", train, risk, span * 2 / 3)
    report("TEST  (last 1/3, OOS)", test, risk, span / 3)
    if not quiet:
        print("  exit reasons:", t.reason.value_counts().to_dict())
    gate = len(test) >= 150 and test.ret.mean() > 0.0015
    print(f"  GATE (OOS exp>0.15%, n>=150): {'PASS' if gate else 'FAIL'}"
          f"  [OOS n={len(test)}, exp={test.ret.mean()*100 if len(test) else 0:+.3f}%]")
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="baseline", choices=list(signals.STRATEGIES))
    ap.add_argument("--interval", default=None)
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--fee", type=float, default=0.00075)
    ap.add_argument("--slip", type=float, default=0.0005)
    ap.add_argument("--risk", type=float, default=0.015)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--wide", type=int, default=0, help="evaluate on top-N universe, e.g. --wide 40")
    a = ap.parse_args()
    if a.wide:
        syms = top_universe(a.wide)
        iv, dy = a.interval or DEFAULTS[a.strategy][0], a.days or DEFAULTS[a.strategy][1]
        dy = a.days or 730
        scen = [
            ("taker  fee .075 slip .05/.05", dict(fee=0.00075, slip=0.0005)),
            ("taker  fee .075 slip .10/.10 (small caps)", dict(fee=0.00075, slip=0.0010)),
            ("limit-entry fee .075 exit slip .05", dict(fee=0.00075, slip=0.0005, entry_mode="limit", slip_in=0.0)),
        ]
        wide_report(a.strategy, iv, dy, syms, scen)
        return
    syms = a.symbols.split(",")
    defaults = DEFAULTS
    names = list(signals.STRATEGIES) if a.all else [a.strategy]
    for nme in names:
        iv, dy = defaults[nme]
        evaluate(nme, a.interval or iv, a.days or dy, syms, a.fee, a.slip, a.risk)


if __name__ == "__main__":
    main()
