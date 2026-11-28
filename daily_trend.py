"""
Daily trend-following test on large coins (research only; changes nothing in the bot).

    python daily_trend.py

Rules and coins were fixed before looking at results; nothing is tuned.
  * Coins: large caps as of early 2020 (written from memory, includes later laggards such as EOS and XTZ)
  * Long or cash only (spot). Signal on the daily close, trade at the NEXT day's open.
  * Cost 0.125% per side (0.075% fee + 0.05% slippage). Cash earns nothing.
  * Portfolio = equal starting slice per coin, each compounding on its own (no rebalancing).
  * Benchmark = buying the same slices on day one and holding.
"""
import numpy as np
import pandas as pd

import backtest

COINS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "BCHUSDT", "LTCUSDT", "EOSUSDT",
         "BNBUSDT", "XTZUSDT", "LINKUSDT", "XLMUSDT", "ADAUSDT", "TRXUSDT"]
START = pd.Timestamp("2020-01-01")
COST = 0.00125


def sma(s, n):
    return s.rolling(n, min_periods=n).mean()


def hysteresis(enter, leave):
    """1 after `enter`, stays until `leave`."""
    pos, out = 0, []
    for e, x in zip(enter.values, leave.values):
        if pos == 0 and e:
            pos = 1
        elif pos == 1 and x:
            pos = 0
        out.append(pos)
    return pd.Series(out, index=enter.index)


# Each rule returns the wanted position (0/1) decided on that day's close.
RULES = {
    "price above 200-day average": lambda d, btc: (d.Close > sma(d.Close, 200)).astype(int),
    "50-day above 200-day average": lambda d, btc: (sma(d.Close, 50) > sma(d.Close, 200)).astype(int),
    "up over the last 100 days": lambda d, btc: (d.Close > d.Close.shift(100)).astype(int),
    "55-day high in, 20-day low out": lambda d, btc: hysteresis(
        d.Close > d.High.shift(1).rolling(55).max(), d.Close < d.Low.shift(1).rolling(20).min()),
    "coin AND BTC above 200-day average": lambda d, btc: (
        (d.Close > sma(d.Close, 200)) & (btc.Close > sma(btc.Close, 200)).reindex(d.index, fill_value=False)
    ).astype(int),
}


def load_all():
    data = {}
    today = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    for s in COINS:
        d = backtest.load(s, "1d", 3600)
        data[s] = d[d.index < today]                 # drop the unfinished candle
    return data


def slice_returns(d, want, calendar):
    """Daily strategy returns for one coin. Trades happen at the open after the signal day."""
    held = want.shift(1).fillna(0)                    # position during day t (entered at t's open)
    if d.index[-1] < calendar[-1]:                    # delisted/renamed: out at the last close
        held.iloc[-1] = 0
    prev = held.shift(1).fillna(0)
    gap = (d.Open / d.Close.shift(1) - 1).fillna(0)   # previous close -> today's open (old position)
    day = d.Close / d.Open - 1                        # today's open -> close (new position)
    r = (1 + prev * gap) * (1 - COST * (held - prev).abs()) * (1 + held * day) - 1
    r = r.reindex(calendar).fillna(0)
    inmkt = held.reindex(calendar).fillna(0)
    trades = ((held - prev) > 0).reindex(calendar, fill_value=False)
    return r, inmkt, trades


def run(data, rule, coins):
    btc = data["BTCUSDT"]
    calendar = btc.index[btc.index >= START]
    R, M, T = {}, {}, {}
    for s in coins:
        d = data[s]
        if rule is None:                              # buy and hold
            want = pd.Series(1, index=d.index)
            want[d.index < START - pd.Timedelta(days=1)] = 0
        else:
            want = rule(d, btc).copy()
            want[d.index < START - pd.Timedelta(days=1)] = 0
        R[s], M[s], T[s] = slice_returns(d, want, calendar)
    R, M, T = pd.DataFrame(R), pd.DataFrame(M), pd.DataFrame(T)
    equity = (1 + R).cumprod().mean(axis=1)
    return equity, M.mean(axis=1), int(T.values.sum())


def metrics(eq):
    r = eq.pct_change().fillna(eq.iloc[0] - 1)
    years = len(eq) / 365
    full = pd.concat([pd.Series([1.0]), eq])
    return dict(total=(eq.iloc[-1] - 1) * 100,
                cagr=(eq.iloc[-1] ** (1 / years) - 1) * 100,
                dd=(1 - full / full.cummax()).max() * 100,
                sharpe=r.mean() / r.std() * np.sqrt(365) if r.std() else 0.0)


def yearly(eq):
    ends = eq.groupby(eq.index.year).last()
    starts = ends.shift(1).fillna(1.0)
    return (ends / starts - 1) * 100


def sharpe_gap_interval(a, b, block=60, n=4000, seed=1):
    """Block-bootstrap 90% interval for (strategy Sharpe - benchmark Sharpe)."""
    ra, rb = a.pct_change().fillna(0).values, b.pct_change().fillna(0).values
    rng = np.random.default_rng(seed)
    size = len(ra)
    gaps = []
    for _ in range(n):
        starts = rng.integers(0, size - block, size // block + 1)
        ix = np.concatenate([np.arange(s, s + block) for s in starts])[:size]
        x, y = ra[ix], rb[ix]
        gaps.append((x.mean() / x.std() - y.mean() / y.std()) * np.sqrt(365))
    return np.percentile(gaps, [5, 95])


def block(title, data, coins):
    print(f"\n================ {title} ================")
    hold, _, _ = run(data, None, coins)
    curves = {"BUY AND HOLD": hold}
    print(f"{'rule':<36}{'total':>9}{'per yr':>8}{'worst fall':>12}{'Sharpe':>8}{'in mkt':>8}{'buys/yr':>9}"
          f"{'since 2023/yr':>15}   Sharpe minus hold (90% range)")
    for name, rule in [("BUY AND HOLD", None)] + list(RULES.items()):
        eq, inm, trades = run(data, rule, coins)
        curves[name] = eq
        m = metrics(eq)
        recent = eq[eq.index >= "2023-01-01"] / eq[eq.index < "2023-01-01"].iloc[-1]
        extra = ""
        if rule is not None:
            lo, hi = sharpe_gap_interval(eq, hold)
            extra = f"   {lo:+.2f} to {hi:+.2f}"
        print(f"{name:<36}{m['total']:>8.0f}%{m['cagr']:>7.1f}%{m['dd']:>11.0f}%{m['sharpe']:>8.2f}"
              f"{inm.mean() * 100:>7.0f}%{trades / (len(eq) / 365):>9.1f}{metrics(recent)['cagr']:>14.1f}%{extra}")
    print("\nReturn by calendar year (%):")
    table = pd.DataFrame({k: yearly(v) for k, v in curves.items()}).T
    print(table.round(0).astype(int).to_string())


def main():
    data = load_all()
    for s, d in data.items():
        print(f"  {s:<9} {d.index[0]:%Y-%m-%d} → {d.index[-1]:%Y-%m-%d}")
    block("12 large coins (early-2020 list)", data, COINS)
    block("BTC only (what a very small account could hold)", data, ["BTCUSDT"])
    block("12 coins WITHOUT BNB (does one winner carry it?)", data, [c for c in COINS if c != "BNBUSDT"])
    print("\n================ Is the average length a lucky choice? (12 coins, price above N-day average) ================")
    for n in (50, 100, 150, 200, 250, 300):
        eq, _, _ = run(data, lambda d, btc, n=n: (d.Close > sma(d.Close, n)).astype(int), COINS)
        m = metrics(eq)
        print(f"  N={n:<4} per yr {m['cagr']:>6.1f}%   worst fall {m['dd']:>3.0f}%   Sharpe {m['sharpe']:.2f}")


if __name__ == "__main__":
    main()
