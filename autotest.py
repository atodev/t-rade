"""
Periodic strategy gate. Run from cron / systemd timer (e.g. every 12h):

    0 */12 * * *  cd /home/pi/t-rade && .venv/bin/python autotest.py >> autotest.log 2>&1

Re-runs the walk-forward backtest for every strategy in signals.STRATEGIES on fresh
candles and writes strategy_state.json, which main.py reads before every scan.

main.py only trades LIVE when the state file says live_ok=True AND is fresh.
Anything else (no file, stale, gate failed, error) => simulation only.

Two kinds of strategy, two gates (both include fees + slippage):

 FAST (1m/15m, 10 pairs, one position at a time — how the bot trades):
  * out-of-sample (last 1/3) n >= MIN_OOS_TRADES and expectancy > MIN_OOS_EXP
  * full-sample expectancy > 0
  * at least 2 of 3 chronological thirds have positive expectancy

 SLOW (1h/4h, top-40 pairs, 2 years): a one-position bot only gets ~50 trades/year from these, so the
 FAST gate could never pass them. They are judged on ALL signals across the wider universe instead:
  * >= WIDE_MIN_TRADES signals, out-of-sample expectancy > MIN_OOS_EXP
  * weekly-clustered t-stat >= WIDE_MIN_T (trades in the same week are correlated). Set at 2.8, not
    2.0, because six slow strategies are tested every run — a Bonferroni-style allowance for that.
  * at least 2 of 3 thirds positive
  * AND the bot-realistic one-position-at-a-time result must be positive too (it was worse than the
    all-signals average in every earlier test).
Slow strategies run on candles up to SLOW_DATA_MAX_AGE_H old; the universe is frozen in data/universe_40.json.
NOTE: main.py can only execute strategies listed in main.LIVE_IMPLEMENTED (currently baseline). A slow strategy
passing here is logged as "approved but not implemented" until it is built into main.py.
Hysteresis: live_ok only after PASSES_REQUIRED consecutive passing runs by the same
strategy; a single failing run resets it to 0 immediately.
The job never searches parameters — it only evaluates the fixed list of strategies.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

import pandas as pd

import backtest
import signals

STATE_FILE = "strategy_state.json"
MIN_OOS_TRADES = 150
MIN_OOS_EXP = 0.0015        # +0.15% per trade after costs
PASSES_REQUIRED = 2
FEE, SLIP = 0.00075, 0.0005
DATA_MAX_AGE_H = 6          # fast strategies: candles refreshed at most this stale
SLOW_DATA_MAX_AGE_H = 23    # slow strategies: 40 pairs x 2y is heavy to download, refresh ~daily
WIDE_UNIVERSE = 40
WIDE_MIN_TRADES = 150
WIDE_MIN_T = 2.8
CONFIG = {                  # strategy -> (interval, days, kind)
    "baseline":          ("1m", 30, "fast"),
    "trend_breakout":    ("15m", 90, "fast"),
    "pullback":          ("15m", 90, "fast"),
    "donchian_1h":       ("1h", 730, "slow"),
    "donchian_4h":       ("4h", 730, "slow"),
    "squeeze_1h":        ("1h", 730, "slow"),
    "meanrev_1h":        ("1h", 730, "slow"),
    "pullback_1h":       ("1h", 730, "slow"),
    "trend_breakout_1h": ("1h", 730, "slow"),
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    fd, tmp = tempfile.mkstemp(dir=".", prefix=".state_", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)        # atomic: main.py never sees a half-written file


def milestone(msg):
    with open("milestone_log.md", "a") as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def evaluate_slow(name, symbols):
    """All-signals evaluation on the wide universe + clustered t-stat + bot-realistic check."""
    interval, days, _ = CONFIG[name]
    t = backtest.run_strategy(name, interval, days, symbols, FEE, SLIP)
    if t.empty:
        return dict(strategy=name, kind="slow", passed=False, reason="no trades", n=0)
    lo, hi = t.entry_time.min(), t.entry_time.max()
    oos = t[t.entry_time > lo + (hi - lo) * (2 / 3)]
    tstat, weeks = backtest.weekly_t(t)
    th = backtest.thirds(t)                                   # percent per third
    thirds_pos = sum(1 for x in th if pd.notna(x) and x > 0)
    serial = backtest.one_at_a_time(t)
    r = dict(
        strategy=name, kind="slow", interval=interval, days=days, symbols=len(symbols),
        n=int(len(t)), exp_all=float(t.ret.mean()),
        oos_n=int(len(oos)), oos_exp=float(oos.ret.mean()) if len(oos) else None,
        t_stat=None if pd.isna(tstat) else float(tstat), weeks=int(weeks),
        thirds_positive=int(thirds_pos),
        serial_n=int(len(serial)), serial_exp=float(serial.ret.mean()) if len(serial) else None,
    )
    reasons = []
    if len(t) < WIDE_MIN_TRADES:
        reasons.append(f"n {len(t)} < {WIDE_MIN_TRADES}")
    if not len(oos) or oos.ret.mean() <= MIN_OOS_EXP:
        reasons.append("oos expectancy below gate")
    if pd.isna(tstat) or tstat < WIDE_MIN_T:
        reasons.append(f"t-stat {0 if pd.isna(tstat) else tstat:.2f} < {WIDE_MIN_T}")
    if thirds_pos < 2:
        reasons.append(f"only {thirds_pos}/3 windows positive")
    if not len(serial) or serial.ret.mean() <= 0:
        reasons.append("one-position-at-a-time expectancy <= 0")
    r["passed"] = not reasons
    r["reason"] = "; ".join(reasons) or "ok"
    return r


def evaluate(name, symbols):
    interval, days, kind = CONFIG[name]
    if kind == "slow":
        return evaluate_slow(name, backtest.top_universe(WIDE_UNIVERSE))
    t = backtest.run_strategy(name, interval, days, symbols, FEE, SLIP)
    if t.empty:
        return dict(strategy=name, passed=False, reason="no trades", n=0)
    t = backtest.one_at_a_time(t)
    lo, hi = t.entry_time.min(), t.entry_time.max()
    split = lo + (hi - lo) * (2 / 3)
    oos = t[t.entry_time > split]
    edges = [lo + (hi - lo) * k / 3 for k in range(4)]
    thirds = [t[(t.entry_time > edges[k]) & (t.entry_time <= edges[k + 1])].ret.mean() for k in range(3)]
    thirds_pos = sum(1 for x in thirds if pd.notna(x) and x > 0)
    r = dict(
        strategy=name, interval=interval, days=days,
        n=int(len(t)), exp_all=float(t.ret.mean()),
        oos_n=int(len(oos)), oos_exp=float(oos.ret.mean()) if len(oos) else None,
        thirds_positive=int(thirds_pos),
    )
    reasons = []
    if len(oos) < MIN_OOS_TRADES:
        reasons.append(f"oos n {len(oos)} < {MIN_OOS_TRADES}")
    if not len(oos) or oos.ret.mean() <= MIN_OOS_EXP:
        reasons.append("oos expectancy below gate")
    if t.ret.mean() <= 0:
        reasons.append("full-sample expectancy <= 0")
    if thirds_pos < 2:
        reasons.append(f"only {thirds_pos}/3 windows positive")
    r["passed"] = not reasons
    r["reason"] = "; ".join(reasons) or "ok"
    return r


def main():
    symbols = backtest.DEFAULT_SYMBOLS
    prev = load_state()
    results = []
    for name in CONFIG:
        backtest.MAX_AGE_H = SLOW_DATA_MAX_AGE_H if CONFIG[name][2] == "slow" else DATA_MAX_AGE_H
        try:
            results.append(evaluate(name, symbols))
        except Exception as e:                       # fail closed
            results.append(dict(strategy=name, passed=False, reason=f"error: {e}", n=0))

    passing = [r for r in results if r["passed"]]
    best = max(passing, key=lambda r: r["oos_exp"]) if passing else None

    if best:
        same = prev.get("candidate") == best["strategy"]
        passes = (prev.get("consecutive_passes", 0) + 1) if same else 1
        live_ok = passes >= PASSES_REQUIRED
        state = dict(candidate=best["strategy"], consecutive_passes=passes, live_ok=live_ok,
                     strategy=best["strategy"] if live_ok else None)
    else:
        state = dict(candidate=None, consecutive_passes=0, live_ok=False, strategy=None)

    state.update(updated=now_iso(), results=results,
                 gate=dict(min_oos_trades=MIN_OOS_TRADES, min_oos_exp=MIN_OOS_EXP, passes_required=PASSES_REQUIRED,
                           wide_min_t=WIDE_MIN_T, wide_min_trades=WIDE_MIN_TRADES, wide_universe=WIDE_UNIVERSE))
    save_state(state)

    for r in results:
        extra = (f" t={r['t_stat']:.2f} serial_exp={r['serial_exp']:+.4f}"
                 if r.get("kind") == "slow" and r.get("t_stat") is not None and r.get("serial_exp") is not None else "")
        print(f"[{now_iso()}] {r['strategy']:<17} pass={r['passed']!s:<5} n={r.get('n')} "
              f"oos_exp={r.get('oos_exp')}{extra} -> {r['reason']}")
    runnable = state["live_ok"] and state["strategy"] in signals.LIVE_IMPLEMENTED
    summary = (f"LIVE APPROVED: {state['strategy']}" if runnable
               else f"APPROVED but not executable yet: {state['strategy']} (main.py has no live implementation)"
               if state["live_ok"]
               else f"candidate {state['candidate']} pass {state['consecutive_passes']}/{PASSES_REQUIRED}, sim only"
               if state["candidate"] else "no strategy passed gate — sim only")
    print(f"[{now_iso()}] {summary}")
    if (prev.get("live_ok"), prev.get("strategy")) != (state["live_ok"], state["strategy"]):
        milestone(f"autotest: {summary}")


if __name__ == "__main__":
    sys.exit(main())
