"""
Event study: does perpetual-futures funding predict forward SPOT returns?

    python funding_research.py [--n 40] [--days 730] [--hold 24]

Public endpoints only. For each symbol: funding history (futures) + cached spot 1h candles.
Funding z-score uses only PAST funding observations (rolling 90 obs ≈ 30 days) and a
signal is usable only from the funding settlement time onward (no look-ahead).
Forward return = open of next 1h bar -> close `hold` hours later, minus 0.25% round trip.
Also reports market-adjusted return (minus equal-weight average of all symbols at that time).
"""
import argparse
import json
import os
import sys
import numpy as np
import pandas as pd
from binance.client import Client

import backtest

COST = 2 * (0.00075 + 0.0005)       # fee + slippage, both sides
Z_WIN = 90


def funding_history(client, symbol, days):
    path = os.path.join(backtest.DATA_DIR, f"funding_{symbol}_{days}d.csv")
    if os.path.exists(path):
        return pd.read_csv(path, index_col=0, parse_dates=True)
    start = int((pd.Timestamp.now("UTC") - pd.Timedelta(days=days)).timestamp() * 1000)
    rows = []
    while True:
        r = client.futures_funding_rate(symbol=symbol, startTime=start, limit=1000)
        if not r:
            break
        rows += r
        nxt = r[-1]["fundingTime"] + 1
        if len(r) < 1000 or nxt <= start:
            break
        start = nxt
    if not rows:
        return None
    f = pd.DataFrame(rows)
    f["t"] = pd.to_datetime(f.fundingTime, unit="ms").dt.round("min")
    f = f.set_index("t")[["fundingRate"]].astype(float)
    f = f[~f.index.duplicated()]
    f.to_csv(path)
    return f


def build(symbols, days, hold):
    c = Client("", "")
    frames = []
    for s in symbols:
        try:
            f = funding_history(c, s, days)
        except Exception as e:
            print(f"  skip {s}: no futures funding ({str(e)[:50]})", file=sys.stderr)
            continue
        if f is None or len(f) < Z_WIN + 30:
            continue
        px = backtest.load(s, "1h", days)
        # funding observation -> usable at the next full hour after settlement
        z = (f.fundingRate - f.fundingRate.rolling(Z_WIN).mean()) / f.fundingRate.rolling(Z_WIN).std()
        obs = pd.DataFrame({"fr": f.fundingRate, "z": z}).dropna()
        obs.index = obs.index.ceil("h")
        obs = obs[~obs.index.duplicated(keep="last")]
        fwd = px.Close.shift(-hold) / px.Open.shift(-1) - 1      # next-open -> close after `hold` bars
        d = pd.DataFrame({"fwd": fwd}).join(obs, how="inner").dropna()
        d["symbol"] = s
        frames.append(d)
    return pd.concat(frames) if frames else pd.DataFrame()


def cluster_t(d, col):
    w = d.groupby(d.index.to_period("W"))[col].mean()
    if len(w) < 8 or w.std() == 0:
        return float("nan")
    return w.mean() / (w.std() / np.sqrt(len(w)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--hold", type=int, default=24)
    a = ap.parse_args()
    syms = backtest.top_universe(a.n)
    d = build(syms, a.days, a.hold)
    if d.empty:
        print("no data"); return
    d["adj"] = d.fwd - d.groupby(d.index).fwd.transform("mean")     # market-adjusted
    nsym = d.symbol.nunique()
    avg_fr = d.fr.mean()
    print(f"{nsym} symbols with funding, {len(d)} observations, hold={a.hold}h, cost={COST*100:.2f}%")
    print(f"mean funding/8h = {avg_fr*100:.4f}%  (≈ {avg_fr*3*365*100:.1f}%/yr annualised carry, longs pay shorts when positive)")
    bins = [-np.inf, -2, -1, 1, 2, np.inf]
    labels = ["z<-2", "-2..-1", "-1..1", "1..2", "z>2"]
    d["bucket"] = pd.cut(d.z, bins, labels=labels)
    print(f"\n{'bucket':<8}{'n':>7}{'raw fwd':>10}{'net of cost':>13}{'clustered t':>13}{'mkt-adj':>10}{'adj t':>8}")
    for b in labels:
        g = d[d.bucket == b]
        if len(g) < 30:
            continue
        net = g.fwd - COST
        print(f"{b:<8}{len(g):>7}{g.fwd.mean()*100:>9.3f}%{net.mean()*100:>12.3f}%"
              f"{cluster_t(g.assign(net=net), 'net'):>13.2f}{g.adj.mean()*100:>9.3f}%{cluster_t(g, 'adj'):>8.2f}")
    # absolute funding level (not z): very negative funding, e.g. < -0.02%/8h
    print("\nBy absolute funding rate per 8h:")
    for lo, hi, lab in [(-1, -0.0002, "< -0.02%"), (-0.0002, 0, "-0.02..0"), (0, 0.0001, "0..0.01%"),
                        (0.0001, 0.0003, "0.01..0.03%"), (0.0003, 1, "> 0.03%")]:
        g = d[(d.fr >= lo) & (d.fr < hi)]
        if len(g) < 30:
            continue
        net = g.fwd - COST
        print(f"  {lab:<12} n={len(g):>6} raw={g.fwd.mean()*100:+.3f}% net={net.mean()*100:+.3f}% "
              f"t={cluster_t(g.assign(net=net), 'net'):+.2f}  adj={g.adj.mean()*100:+.3f}% t={cluster_t(g, 'adj'):+.2f}")
    # stability: first vs second half for the most extreme-negative bucket
    g = d[d.bucket == "z<-2"]
    if len(g) > 60:
        mid = g.index.min() + (g.index.max() - g.index.min()) / 2
        for lab, h in [("1st half", g[g.index <= mid]), ("2nd half", g[g.index > mid])]:
            print(f"  z<-2 {lab}: n={len(h)} raw={h.fwd.mean()*100:+.3f}% net={(h.fwd-COST).mean()*100:+.3f}%")


if __name__ == "__main__":
    main()
