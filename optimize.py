"""
Walk-forward optimiser for the LIVE baseline strategy (research only; changes nothing in the bot).

    python optimize.py          # needs data/<SYM>_1m_90d.csv for the 60-coin universe

Copies what main.py actually does:
  * candidates = top 10 by rolling 24h % change with >= $5M 24h quote volume (from a 60-coin universe)
  * the highest-ranked candidate passing the entry filters is bought; one position at a time
  * momentum / volume-risk use the ~9 candles main.py really has (30 fetched, first 21 dropped)
  * exits: initial stop, sticky "danger zone" stop once price is below entry, TP ratchet
Not copied: blacklist/whitelist/blue-chip fallback, the BTC and Fear & Greed gates (live only).

Protocol (fixed before looking at results): parameters are chosen on the first 2/3 of the period.
The last 1/3 is only used to judge what was chosen.
Fills: next bar open + slippage. Stops: checked against the bar low, filled at the stop (or the open
if gapped). Ratchet / danger-zone updates use the bar close (what a polling bot sees).
Caveat: the universe is today's most-traded list, which flatters results.
"""
import itertools
import numpy as np
import pandas as pd

import backtest

FEE, SLIP = 0.00075, 0.0005
TOP_N, VOL_MIN = 10, 5_000_000

LIVE_ENTRY = dict(fast=9, slow=21, min_split=0.36, risk_band=True, rising=True, widening=True,
                  btc="off", breadth=0.0, pool="top10")
LIVE_EXIT = dict(sl=0.988, tp=1.018, danger=0.996, trail=None)


# ── Data ──────────────────────────────────────────────────────────────────────
def load_panel():
    syms = backtest.top_universe(60)
    frames = {}
    for s in syms:
        try:
            frames[s] = backtest.load(s, "1m", 90)
        except Exception as e:
            print("skip", s, e)
    btc = frames["BTCUSDT"]
    idx = btc.index
    syms = list(frames)
    P = {k: np.column_stack([frames[s][k].reindex(idx).values for s in syms])
         for k in ["Open", "High", "Low", "Close", "Volume"]}
    close = pd.DataFrame(P["Close"], index=idx)
    quote24 = (close * pd.DataFrame(P["Volume"], index=idx)).rolling(1440, min_periods=1440).sum()
    chg24 = close / close.shift(1440) - 1
    eligible = chg24.where(quote24 >= VOL_MIN)
    rank = eligible.rank(axis=1, ascending=False, method="first").values
    P["rank"] = rank
    P["eligible"] = ~np.isnan(eligible.values)
    # market filters
    b = btc.Close
    b1h = b.ewm(span=60, adjust=False).mean()
    b4h = b.ewm(span=240, adjust=False).mean()
    P["btc"] = {"off": np.ones(len(idx), bool),
                "above_4h_ema": (b > b4h).values,
                "uptrend": ((b1h > b4h) & (b > b1h)).values}
    above = close.gt(close.ewm(span=240, adjust=False).mean()) & close.notna()
    P["breadth"] = (above.sum(axis=1) / close.notna().sum(axis=1)).values
    return idx, syms, P


_cache = {}


def features(P, fast, slow):
    if (fast, slow) in _cache:
        return _cache[(fast, slow)]
    c = pd.DataFrame(P["Close"])
    v = pd.DataFrame(P["Volume"])
    sf, ss = c.rolling(fast).mean(), c.rolling(slow).mean()
    diff = sf - ss
    split = diff / c * 100
    window = max(30 - slow, 9)                      # candles main.py really has after slicing
    mom = c > c.shift(window - 1)
    vsd = v.rolling(window).std()
    vz = ((v - v.rolling(window).mean()) / vsd).clip(lower=0) * 2
    risk = ((split.abs() * 10 + vz.fillna(0)) / 2).clip(upper=10)
    rising = ((c > c.shift(1)).astype(int) + (c.shift(1) > c.shift(2)).astype(int)
              + (c.shift(2) > c.shift(3)).astype(int)) >= 2
    f = dict(split=split.values, base=((diff > 0) & mom).values, risk=risk.values,
             rising=rising.values, widening=(diff > diff.shift(5)).values)
    _cache[(fast, slow)] = f
    return f


def signals_for(P, e):
    f = features(P, e["fast"], e["slow"])
    m = f["base"] & (f["split"] >= e["min_split"]) & P["eligible"]
    if e["pool"] == "top10":
        m &= P["rank"] <= TOP_N
    elif e["pool"] == "rank11_30":
        m &= (P["rank"] > 10) & (P["rank"] <= 30)
    if e["widening"]:
        m &= f["widening"]
    if e["rising"]:
        m &= f["rising"]
    if e["risk_band"]:
        r = f["risk"]
        m &= ~((r < 2) | ((r >= 6) & (r < 8)))
    market = P["btc"][e["btc"]] & (P["breadth"] >= e["breadth"])
    return m & market[:, None]


# ── Simulation ────────────────────────────────────────────────────────────────
def exit_trade(o, h, l, c, j, ep, x, fee, slip, max_bars=4000):
    stop = ep * x["sl"]
    base, peak, adj = ep, ep, 0
    n = len(c)
    end = min(n, j + max_bars)
    for k in range(j, end):
        if l[k] != l[k]:                            # missing bar
            continue
        if l[k] <= stop:
            px = min(stop, o[k]) if k > j else stop
            return k, px * (1 - slip) / ep - 1 - 2 * fee, adj
        ck = c[k]
        if x["trail"] is None:                      # live: stepped ratchet
            if ck > base * x["tp"]:
                adj += 1
                base = ck
                stop = base * (max(0.9915, ep * (1 + 2 * FEE) / base) if adj == 1 else 0.99)
        else:                                       # alternative: continuous trail once target reached
            peak = max(peak, ck)
            if adj or ck > ep * x["tp"]:
                adj = 1
                stop = max(stop, peak * (1 - x["trail"]), ep * (1 + 2 * FEE))
        if x["danger"] and adj == 0 and ck < ep:
            stop = max(stop, ep * x["danger"])
    k = end - 1
    while c[k] != c[k]:
        k -= 1
    return k, c[k] * (1 - slip) / ep - 1 - 2 * fee, adj


def simulate(P, sig, x, lo, hi, fee=FEE, slip=SLIP):
    """One position at a time over bars [lo, hi). Returns arrays of (entry_bar, ret, adj, bars)."""
    rows, cols = np.nonzero(sig[lo:hi - 2])
    rows = rows + lo
    order = np.lexsort((P["rank"][rows, cols], rows))
    rows, cols = rows[order], cols[order]
    out, free, skip = [], lo, -1
    i, n = 0, len(rows)
    while i < n:
        t = rows[i]
        if t < free:
            i = np.searchsorted(rows, free)
            continue
        s = cols[i]
        if s == skip:                               # main.py skips the asset it just lost on
            i += 1
            continue
        j = t + 1
        ep = P["Open"][j, s] * (1 + slip)
        if ep != ep:
            i += 1
            continue
        k, ret, adj = exit_trade(P["Open"][:hi, s], P["High"][:hi, s], P["Low"][:hi, s],
                                 P["Close"][:hi, s], j, ep, x, fee, slip)
        out.append((j, ret, adj, k - j + 1))
        skip = s if ret < 0 else -1
        free = k + 1
        i += 1
    return np.array(out) if out else np.zeros((0, 4))


def stats(t, days):
    if len(t) == 0:
        return dict(n=0, exp=np.nan, wr=np.nan, per_day=0.0, total=0.0)
    r = t[:, 1]
    return dict(n=len(r), exp=r.mean() * 100, wr=(r > 0).mean() * 100, per_day=len(r) / days, total=r.sum() * 100)


def line(label, s):
    return (f"{label:<64} n={s['n']:>5}  WR={s['wr']:5.1f}%  avg={s['exp']:+.3f}%/trade  "
            f"{s['per_day']:5.1f}/day")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    idx, syms, P = load_panel()
    n = len(idx)
    start, split = 1440, 1440 + (n - 1440) * 2 // 3
    dtr, dte = (split - start) / 1440, (n - split) / 1440
    print(f"{len(syms)} coins | train {idx[start]:%d %b} → {idx[split]:%d %b} ({dtr:.0f}d) | "
          f"test {idx[split]:%d %b} → {idx[-1]:%d %b} ({dte:.0f}d)")

    live_sig = signals_for(P, LIVE_ENTRY)
    print("\n== 1. Calibration: current live settings (compare Pi sim: WR 22%, about -0.26%/trade, no slippage)")
    for label, kw in [("train, with slippage", dict(slip=SLIP)), ("train, no slippage (like Pi sim)", dict(slip=0.0))]:
        t = simulate(P, live_sig, LIVE_EXIT, start, split, **kw)
        print(line(label, stats(t, dtr)))
    t = simulate(P, live_sig, LIVE_EXIT, start, split, fee=0.0, slip=0.0)
    print(line("train, NO costs at all (gross edge)", stats(t, dtr)))

    print("\n== 2. Do the entry signals predict anything? mean forward return, gross, vs any top-10 coin at any time")
    close = pd.DataFrame(P["Close"])
    pool = (P["rank"] <= TOP_N) & P["eligible"]
    for mins in (5, 15, 60, 240):
        fwd = (close.shift(-mins) / close - 1).values * 100
        parts = []
        for name, a, b in [("train", start, split), ("test", split, n - mins)]:
            sv = np.nanmean(fwd[a:b][live_sig[a:b]])
            bv = np.nanmean(fwd[a:b][pool[a:b]])
            parts.append(f"{name}: signal {sv:+.3f}% vs pool {bv:+.3f}% (edge {sv - bv:+.3f}%)")
        print(f"  {mins:>3} min  " + " | ".join(parts))
    print("  (a round trip costs about 0.25%)")

    # ── Stage A: exits, with live entries ─────────────────────────────────────
    exits = []
    for sl, tp, danger, trail in itertools.product(
            [0.990, 0.988, 0.980, 0.970], [1.008, 1.018, 1.030, 1.050],
            [None, 0.996, 0.992], [None, 0.006, 0.012, 0.025]):
        exits.append(dict(sl=sl, tp=tp, danger=danger, trail=trail))
    res_x = sorted(((stats(simulate(P, live_sig, x, start, split), dtr), x) for x in exits),
                   key=lambda r: r[0]["exp"] if r[0]["n"] >= 200 else -1e9, reverse=True)
    print(f"\n== 3. Exit settings: {len(exits)} combinations on TRAIN (live entries)")
    print(f"  combinations with a positive average: {sum(r[0]['exp'] > 0 for r in res_x)}")
    for s, x in res_x[:5]:
        print(line(f"  {x}", s))

    # ── Stage B: entries with the best exits, then the 15 best entries with the next two exit sets ──
    entries = []
    for (fast, slow), ms, rb, rs, btc, br, pool_name in itertools.product(
            [(9, 21), (21, 55)], [0.0, 0.18, 0.36, 0.60], [True, False], [True, False],
            ["off", "above_4h_ema", "uptrend"], [0.0, 0.5, 0.65], ["top10", "rank11_30", "all"]):
        entries.append(dict(fast=fast, slow=slow, min_split=ms, risk_band=rb, rising=rs, widening=True,
                            btc=btc, breadth=br, pool=pool_name))
    best_exits = [x for _, x in res_x[:3]]
    res_e = []
    for i, e in enumerate(entries):
        sig = signals_for(P, e)
        for x in best_exits[:1]:
            res_e.append((stats(simulate(P, sig, x, start, split), dtr), e, x))
        if i % 100 == 99:
            print(f"  ... {i + 1}/{len(entries)} entry combinations", flush=True)
    ok = lambda r: r[0]["exp"] if r[0]["n"] >= 200 else -1e9
    res_e.sort(key=ok, reverse=True)
    # re-try the 15 best entries with the other good exits
    for s, e, _ in res_e[:15]:
        sig = signals_for(P, e)
        for x in best_exits[1:]:
            res_e.append((stats(simulate(P, sig, x, start, split), dtr), e, x))
    res_e.sort(key=ok, reverse=True)
    print(f"\n== 4. Entry + exit: {len(res_e)} combinations on TRAIN")
    print(f"  combinations with a positive average (n>=200): {sum(r[0]['exp'] > 0 and r[0]['n'] >= 200 for r in res_e)}")
    for s, e, x in res_e[:5]:
        print(line(f"  split>={e['min_split']} MA{e['fast']}/{e['slow']} pool={e['pool']} btc={e['btc']} "
                   f"breadth>={e['breadth']}", s))
        print(f"      rising={e['rising']} risk_band={e['risk_band']} exits={x}")

    # ── Judgement on unseen data ──────────────────────────────────────────────
    print("\n== 5. TEST (unseen last third)")
    picks = [("current live settings", LIVE_ENTRY, LIVE_EXIT),
             ("best exits, live entries", LIVE_ENTRY, res_x[0][1]),
             ("BEST overall chosen on train", res_e[0][1], res_e[0][2])]
    for label, e, x in picks:
        sig = signals_for(P, e)
        print(line(f"{label} [train]", stats(simulate(P, sig, x, start, split), dtr)))
        print(line(f"{label} [TEST]", stats(simulate(P, sig, x, split, n), dte)))
    top = res_e[:20]
    te = [stats(simulate(P, signals_for(P, e), x, split, n), dte)["exp"] for _, e, x in top]
    print(f"\nTop 20 train combinations → average on TEST: {np.nanmean(te):+.3f}%/trade "
          f"(train average {np.mean([s['exp'] for s, _, _ in top]):+.3f}%); positive on test: "
          f"{sum(v > 0 for v in te)}/20")
    print("\nBest overall settings:", res_e[0][1], res_e[0][2])


if __name__ == "__main__":
    main()
