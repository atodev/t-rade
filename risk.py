"""
Risk safeguards for t-rade: position sizing + circuit breakers.

    python risk.py status     # show halt state
    python risk.py reset      # clear a hard (drawdown) halt after you've reviewed it

Sizing   qty is chosen so a stop-out loses ~RISK_PER_TRADE of equity (fees included), capped at
         MAX_POSITION_FRAC of equity. If the exchange minimum notional forces a bigger position than
         that (small accounts), the trade is allowed only while the resulting loss stays under
         HARD_RISK_CAP, otherwise it is skipped.
Breakers (LIVE trading only — sim keeps running so data collection continues):
         * daily loss  >= DAILY_LOSS_LIMIT of the day's starting equity  -> no live entries until next UTC day
         * CONSEC_LOSS_LIMIT live losses in a row                         -> no live entries until next UTC day
         * drawdown    >= MAX_DRAWDOWN from peak equity                   -> HARD halt, stays until `reset`
State is persisted in risk_state.json so a restart (or kill -9) cannot clear a halt.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

RISK_PER_TRADE    = 0.015   # target loss on a stop-out, fraction of equity
MAX_POSITION_FRAC = 0.50    # never size above this fraction of equity (when above the exchange minimum)
HARD_RISK_CAP     = 0.02    # never take a trade whose stop-out loses more than this
DAILY_LOSS_LIMIT  = 0.03
MAX_DRAWDOWN      = 0.10
CONSEC_LOSS_LIMIT = 4
MIN_NOTIONAL_USD  = 10.0    # exchange minimum order value used by the bot
BALANCE_USE_CAP   = 0.98    # never size above this fraction of the balance (leave room for fees)
FEE_RATE          = 0.00075  # per side (BNB discount)


class RiskManager:
    def __init__(self, path="risk_state.json", now=None, **overrides):
        self.path = path
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.p = dict(risk_per_trade=RISK_PER_TRADE, max_position_frac=MAX_POSITION_FRAC,
                      hard_risk_cap=HARD_RISK_CAP, daily_loss_limit=DAILY_LOSS_LIMIT,
                      max_drawdown=MAX_DRAWDOWN, consec_loss_limit=CONSEC_LOSS_LIMIT,
                      min_notional=MIN_NOTIONAL_USD, fee_rate=FEE_RATE)
        self.p.update(overrides)
        self.s = self._load()

    # ── persistence ──────────────────────────────────────────────────────────
    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except Exception:
            return {"peak": None, "day": None, "day_start": None, "consec": 0,
                    "day_halt": None, "hard_halt": None}

    def _save(self):
        try:
            d = os.path.dirname(os.path.abspath(self.path))
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".risk_", suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(self.s, f, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ── equity tracking ──────────────────────────────────────────────────────
    def _today(self):
        return self._now().strftime("%Y-%m-%d")

    def on_equity(self, equity):
        """Roll the day, track the peak, and trip the hard drawdown halt."""
        if equity is None or equity <= 0:
            return
        s, today = self.s, self._today()
        if s["day"] != today:                       # new UTC day: fresh daily budget
            s["day"], s["day_start"] = today, equity
            s["consec"], s["day_halt"] = 0, None
        if s["peak"] is None or equity > s["peak"]:
            s["peak"] = equity
        dd = (s["peak"] - equity) / s["peak"]
        if dd >= self.p["max_drawdown"] and not s["hard_halt"]:
            s["hard_halt"] = (f"drawdown {dd*100:.1f}% from peak ${s['peak']:.2f} "
                              f"(limit {self.p['max_drawdown']*100:.0f}%) — run `python risk.py reset` to resume live")
        self._save()

    def record_live_trade(self, pnl, equity_after):
        """Call after every LIVE close with the real balance."""
        self.on_equity(equity_after)
        s = self.s
        s["consec"] = s["consec"] + 1 if pnl < 0 else 0
        day_loss = (s["day_start"] - equity_after) / s["day_start"] if s["day_start"] else 0
        if day_loss >= self.p["daily_loss_limit"]:
            s["day_halt"] = f"daily loss {day_loss*100:.1f}% (limit {self.p['daily_loss_limit']*100:.0f}%) — resumes next UTC day"
        elif s["consec"] >= self.p["consec_loss_limit"]:
            s["day_halt"] = f"{s['consec']} consecutive live losses — resumes next UTC day"
        self._save()

    def live_allowed(self, equity):
        """(ok, reason). Call before each scan; False => force simulation."""
        self.on_equity(equity)
        if self.s["hard_halt"]:
            return False, f"HARD HALT: {self.s['hard_halt']}"
        if self.s["day_halt"]:
            return False, f"halt: {self.s['day_halt']}"
        return True, "ok"

    def reset_hard_halt(self, equity=None):
        self.s["hard_halt"] = None
        self.s["day_halt"] = None
        self.s["consec"] = 0
        self.s["peak"] = equity                      # None => re-baselines at the next equity update
        self._save()

    # ── sizing ───────────────────────────────────────────────────────────────
    def size(self, equity, price, stop_frac):
        """Return (qty, notional, reason). qty is None when the trade should be skipped."""
        p = self.p
        if equity <= 0 or price <= 0:
            return None, 0.0, "no equity/price"
        eff_stop = max(stop_frac, 0.0) + 2 * p["fee_rate"]          # loss fraction incl. round-trip fees
        notional = min(equity * p["risk_per_trade"] / eff_stop, equity * p["max_position_frac"])
        if notional < p["min_notional"]:
            notional = p["min_notional"]                              # exchange floor (small accounts)
        if notional > equity * BALANCE_USE_CAP:
            return None, notional, (f"balance ${equity:.2f} too small for the ${p['min_notional']:.0f} "
                                    f"exchange minimum")
        qty = round(notional / price)
        if qty < 1:
            return None, notional, f"price ${price:.4f} too high for ${notional:.2f} position"
        notional = qty * price                                        # actual after rounding
        if notional > equity * BALANCE_USE_CAP:
            qty -= 1
            notional = qty * price
            if qty < 1:
                return None, notional, f"price ${price:.4f} too high for balance"
        risk_frac = notional * eff_stop / equity
        if risk_frac > p["hard_risk_cap"]:
            return None, notional, (f"stop-out would lose {risk_frac*100:.1f}% of equity "
                                    f"(cap {p['hard_risk_cap']*100:.0f}%)")
        return qty, notional, f"risk {risk_frac*100:.2f}% of equity"


if __name__ == "__main__":
    rm = RiskManager()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "reset":
        rm.reset_hard_halt()
        print("Halts cleared. Peak equity will re-baseline on the next live scan.")
    else:
        s = rm.s
        print(f"day={s['day']}  day_start={s['day_start']}  peak={s['peak']}  consec_live_losses={s['consec']}")
        print(f"daily halt: {s['day_halt'] or 'none'}")
        print(f"HARD halt : {s['hard_halt'] or 'none'}")
