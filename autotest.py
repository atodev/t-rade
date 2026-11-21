"""
Periodic strategy gate. Run from cron / systemd timer (e.g. every 12h):

    0 */12 * * *  cd /home/pi/t-rade && .venv/bin/python autotest.py >> autotest.log 2>&1

Re-runs the walk-forward backtest for every strategy in signals.STRATEGIES on fresh
candles and writes strategy_state.json, which main.py reads before every scan.

main.py only trades LIVE when the state file says live_ok=True AND is fresh.
Anything else (no file, stale, gate failed, error) => simulation only.

Gate (all must hold for a strategy to count as a pass this run):
  * out-of-sample (last 1/3) n >= MIN_OOS_TRADES and expectancy > MIN_OOS_EXP
  * full-sample expectancy > 0
  * at least 2 of 3 chronological thirds have positive expectancy
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
DATA_MAX_AGE_H = 6
CONFIG = {                  # strategy -> (interval, days)
    "baseline":       ("1m", 30),
    "trend_breakout": ("15m", 90),
    "pullback":       ("15m", 90),
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


def evaluate(name, symbols):
    interval, days = CONFIG[name]
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
    backtest.MAX_AGE_H = DATA_MAX_AGE_H
    symbols = backtest.DEFAULT_SYMBOLS
    prev = load_state()
    results = []
    for name in CONFIG:
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
                 gate=dict(min_oos_trades=MIN_OOS_TRADES, min_oos_exp=MIN_OOS_EXP, passes_required=PASSES_REQUIRED))
    save_state(state)

    for r in results:
        print(f"[{now_iso()}] {r['strategy']:<15} pass={r['passed']!s:<5} n={r.get('n')} "
              f"oos_exp={r.get('oos_exp')} -> {r['reason']}")
    summary = (f"LIVE APPROVED: {state['strategy']}" if state["live_ok"]
               else f"candidate {state['candidate']} pass {state['consecutive_passes']}/{PASSES_REQUIRED}, sim only"
               if state["candidate"] else "no strategy passed gate — sim only")
    print(f"[{now_iso()}] {summary}")
    if (prev.get("live_ok"), prev.get("strategy")) != (state["live_ok"], state["strategy"]):
        milestone(f"autotest: {summary}")


if __name__ == "__main__":
    sys.exit(main())
