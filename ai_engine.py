"""
Autonomous AI Strategy Engine for t-rade.

Continuously evaluates trade performance and adjusts strategy parameters
to target 10X equity growth in 7 days.  Runs in its own daemon thread;
communicates with the UI via a Queue and controls the trading loop via
lightweight callbacks that read/write globals in main.py.
"""

import os
import time
import pandas as pd
from datetime import datetime
from queue import Queue

# ── Tuning constants ──────────────────────────────────────────────────────────
MIN_TRADES_FOR_EVAL = 15       # completed sells before first evaluation
TARGET_MULTIPLIER   = 10.0     # 10× the starting balance in …
PROJECTION_DAYS     = 7        # … this many days
EVAL_INTERVAL       = 60       # seconds between evaluation cycles
TRADES_PER_DAY_EST  = 24 * 60 / 15   # ~96 trade cycles per day (15 min avg)

# Parameter combinations to cycle through when below target.
# Each row: (SL, Target, ma_fast, ma_slow)
# MA split minimum is derived inside strategy() as (Target-1)*20% of price,
# so wider targets implicitly allow looser splits.
PARAM_GRID = [
    # SL     Target  fast slow  notes
    (0.990, 1.010,   9,  21),  # 1  baseline — standard 9/21
    (0.988, 1.012,   9,  21),  # 2  wider SL/TP, same MAs
    (0.992, 1.008,   9,  21),  # 3  tight SL/TP, same MAs
    (0.990, 1.010,   5,  13),  # 4  fast MAs — more reactive, same SL/TP
    (0.988, 1.012,   5,  13),  # 5  fast MAs, wider SL/TP
    (0.990, 1.010,   7,  21),  # 6  medium fast / standard slow
    (0.988, 1.015,   7,  21),  # 7  medium fast, wider TP
    (0.990, 1.010,  12,  26),  # 8  MACD-style (slower, stronger signals)
    (0.985, 1.015,  12,  26),  # 9  MACD-style, aggressive SL/TP
    (0.990, 1.012,   9,  21),  # 10 asymmetric TP, baseline MAs
]

TRADE_CSV_COLS = [
    "datetime", "hour", "day_of_week", "asset", "action", "session_mode",
    "price", "quantity", "cost", "pnl", "fee", "fee_asset",
    "profit_count", "loss_count", "consec_gain", "consec_loss", "indicator",
    "balance", "adjustment", "calls",
    "split_pct", "trend_dir", "risk_score", "ma_fast", "ma_slow", "hold_seconds",
]


class AIStrategyEngine:
    """
    Autonomous parameter optimiser.

    Lifecycle
    ---------
    1. Starts with force_sim=True (simulation only).
    2. Waits until MIN_TRADES_FOR_EVAL completed sells exist in trades.csv.
    3. Every EVAL_INTERVAL seconds it scores the current parameter set.
    4. If below target it advances to the next entry in PARAM_GRID.
    5. When the projection meets the 10× target it enables live trading.
    6. If live performance later drops, it reverts to simulation.
    """

    def __init__(self, ai_queue: Queue, initial_balance: float = 18.0):
        self.ai_queue       = ai_queue
        self.initial_balance = initial_balance
        self.running        = False
        self.eval_cycle     = 0
        self.param_index    = 0
        self.strategy_log   = []
        self.live_enabled   = False
        self.best_score     = -999.0
        self.best_params    = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _post(self, message: str):
        """Send message to the AI log queue and append to milestone_log.md."""
        ts   = datetime.now().strftime("%H:%M:%S")
        full = f"[AI {ts}] {message}"
        self.ai_queue.put(full)
        with open("milestone_log.md", "a") as fh:
            fh.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")

    def _load_recent_trades(self, n: int = 100) -> pd.DataFrame:
        try:
            df = pd.read_csv("trades.csv", header=None, names=TRADE_CSV_COLS)
            return df.tail(n * 2)
        except Exception:
            return pd.DataFrame(columns=TRADE_CSV_COLS)

    def _evaluate(self, trades_df: pd.DataFrame):
        """Return evaluation dict or None if not enough data."""
        sells = trades_df[trades_df["action"] == "sell"]
        buys  = trades_df[trades_df["action"] == "buy"]
        if len(sells) < MIN_TRADES_FOR_EVAL:
            return None

        wins   = int((sells["indicator"] == "p").sum())
        losses = int((sells["indicator"] == "l").sum())
        total  = wins + losses
        win_rate = wins / total if total > 0 else 0.0

        # Pair buys → sells to compute per-trade P&L
        b = buys.reset_index(drop=True)
        s = sells.reset_index(drop=True)
        pnl_list = []
        for i in range(min(len(b), len(s))):
            bp  = float(b.iloc[i]["price"])
            sp  = float(s.iloc[i]["price"])
            qty = float(b.iloc[i]["quantity"])
            pnl_list.append((sp - bp) * qty)

        avg_pnl = sum(pnl_list) / len(pnl_list) if pnl_list else 0.0

        current_equity = (
            float(sells.iloc[-1]["balance"])
            if "balance" in sells.columns and total > 0
            else self.initial_balance
        )

        # Project equity over PROJECTION_DAYS
        projected = current_equity
        for _ in range(int(TRADES_PER_DAY_EST * PROJECTION_DAYS)):
            projected += avg_pnl

        return {
            "win_rate":        win_rate,
            "avg_pnl":         avg_pnl,
            "projected_equity": max(0.0, projected),
            "current_equity":  current_equity,
            "total_trades":    total,
            "wins":            wins,
            "losses":          losses,
        }

    def _score(self, result: dict) -> float:
        """Higher is better. Penalise strategies with negative avg P&L."""
        if result["avg_pnl"] <= 0:
            return result["win_rate"] - 1.0
        return result["win_rate"] * result["avg_pnl"] * 100

    def _save_strategy_log(self):
        with open("strategy_log.md", "w") as fh:
            fh.write("# Strategy Log\n\n")
            fh.write(f"_Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_\n\n")
            for e in self.strategy_log:
                icon = "✓" if e["success"] else "✗"
                fh.write(f"## {icon} {e['name']} — {e['timestamp']}\n")
                fh.write(f"- **Params**: SL={e['sl']}, Target={e['target']}, MA({e.get('ma_fast','?')}/{e.get('ma_slow','?')})\n")
                fh.write(f"- **Win Rate**: {e['win_rate']:.1%} ({e['wins']}W / {e['losses']}L)\n")
                fh.write(f"- **Avg PnL/trade**: ${e['avg_pnl']:.4f}\n")
                fh.write(f"- **Current Equity**: ${e['current_equity']:.2f}\n")
                fh.write(f"- **Projected 7d Equity**: ${e['projected_equity']:.2f}\n")
                fh.write(f"- **Status**: {e['status']}\n\n")

    # ── Public interface ──────────────────────────────────────────────────────

    def run(self, get_initial_bal, get_strategy_params, set_strategy_params, set_force_sim):
        """
        Main loop — called from a daemon thread.

        Parameters
        ----------
        get_initial_bal      : callable() → float
        get_strategy_params  : callable() → {"SL": float, "Target": float}
        set_strategy_params  : callable({"SL": float, "Target": float})
        set_force_sim        : callable(bool)
        """
        self.running = True

        # Initialise milestone log if absent
        if not os.path.exists("milestone_log.md"):
            with open("milestone_log.md", "w") as fh:
                fh.write("# Milestone Log\n\n")

        self._post(
            f"Engine online. Starting equity: ${get_initial_bal():.2f}. "
            f"Target: ${get_initial_bal() * TARGET_MULTIPLIER:.2f} in {PROJECTION_DAYS} days. "
            f"Simulation mode active."
        )
        set_force_sim(True)

        while self.running:
            time.sleep(EVAL_INTERVAL)
            if not self.running:
                break

            try:
                self.eval_cycle       += 1
                self.initial_balance   = get_initial_bal()
                trades_df              = self._load_recent_trades(100)
                result                 = self._evaluate(trades_df)

                if result is None:
                    seen = (
                        len(trades_df[trades_df["action"] == "sell"])
                        if not trades_df.empty else 0
                    )
                    self._post(
                        f"Gathering sim data… {seen}/{MIN_TRADES_FOR_EVAL} completed trades."
                    )
                    continue

                target_equity = self.initial_balance * TARGET_MULTIPLIER
                on_track      = result["projected_equity"] >= target_equity and result["avg_pnl"] > 0
                score         = self._score(result)
                params        = get_strategy_params()

                self._post(
                    f"Eval #{self.eval_cycle} | "
                    f"SL={params['SL']} TP={params['Target']} "
                    f"MA({params['ma_fast']}/{params['ma_slow']}) | "
                    f"WR={result['win_rate']:.1%} AvgPnL=${result['avg_pnl']:.4f} | "
                    f"Proj7d=${result['projected_equity']:.2f} (need ${target_equity:.2f})"
                )

                # Record this cycle
                entry = {
                    "name":             f"Cycle-{self.eval_cycle}",
                    "timestamp":        datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "sl":               params["SL"],
                    "target":           params["Target"],
                    "ma_fast":          params["ma_fast"],
                    "ma_slow":          params["ma_slow"],
                    "win_rate":         result["win_rate"],
                    "avg_pnl":          result["avg_pnl"],
                    "current_equity":   result["current_equity"],
                    "projected_equity": result["projected_equity"],
                    "wins":             result["wins"],
                    "losses":           result["losses"],
                    "success":          on_track,
                    "status":           (
                        "on_track — live enabled"
                        if on_track
                        else "below target — continuing sim"
                    ),
                }
                self.strategy_log.append(entry)
                self._save_strategy_log()

                if score > self.best_score:
                    self.best_score  = score
                    self.best_params = dict(params)
                    self._post(
                        f"New best params: SL={params['SL']}, Target={params['Target']}, "
                        f"MA({params['ma_fast']}/{params['ma_slow']}) (score={score:.4f})"
                    )

                if on_track and not self.live_enabled:
                    self.live_enabled = True
                    set_force_sim(False)
                    self._post(
                        f"MILESTONE: Simulation meets 10× target! "
                        f"WR={result['win_rate']:.1%}, Proj7d=${result['projected_equity']:.2f}. "
                        f"Live trading enabled."
                    )
                elif not on_track:
                    if self.live_enabled:
                        self.live_enabled = False
                        set_force_sim(True)
                        self._post(
                            "Performance fell below target. Reverting to simulation."
                        )
                    # Advance to next parameter combination
                    self.param_index = (self.param_index + 1) % len(PARAM_GRID)
                    new_sl, new_tp, new_fast, new_slow = PARAM_GRID[self.param_index]
                    set_strategy_params({"SL": new_sl, "Target": new_tp, "ma_fast": new_fast, "ma_slow": new_slow})
                    self._post(
                        f"Switching to param set {self.param_index + 1}/{len(PARAM_GRID)}: "
                        f"SL={new_sl}, Target={new_tp}, MA({new_fast}/{new_slow})"
                    )

            except Exception as e:
                self._post(f"ERROR in eval cycle {self.eval_cycle}: {e} — continuing.")

    def stop(self):
        self.running = False
        self._post("Engine stopped.")
