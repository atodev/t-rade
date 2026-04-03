"""
daily_summary.py — 8am daily executive summary sent to Telegram.

Measures:
  - AI engine performance (param cycles, best params, eval trend)
  - t-rade vs market (BTC % move vs portfolio % move since session start)
  - Equity trajectory (actual vs required curve to hit target)
  - Objective status (days elapsed, days remaining, gap to target)
  - Session stats (total trades, win rate, avg P&L, best/worst tokens)
  - Risk profile (dominant risk band, avg hold time)
  - Warnings

Run via crontab (8am NZST = 7pm UTC previous day, adjust for your tz):
  0 20 * * * cd /home/tombutler/t-rade && python daily_summary.py >> /tmp/daily_summary.out 2>&1
"""

import urllib.request
import urllib.parse
import io
import json
import re
import os
import csv
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID")

START_EQUITY    = 18.0
TARGET_EQUITY   = 180.76
START_DATE      = datetime(2026, 3, 31)   # day bot first ran
TOTAL_DAYS      = 7

# ── Helpers ───────────────────────────────────────────────────────────────────

def send_text(msg):
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT, "text": msg}).encode()
    req  = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=data
    )
    urllib.request.urlopen(req, timeout=10)

def send_photo(img_bytes, caption=""):
    boundary = b"----DailySummaryBoundary"
    def part(name, value, filename=None, content_type=None):
        disp = f'Content-Disposition: form-data; name="{name}"'
        if filename:
            disp += f'; filename="{filename}"'
        header = f"--{boundary.decode()}\r\n{disp}\r\n"
        if content_type:
            header += f"Content-Type: {content_type}\r\n"
        return header.encode() + b"\r\n" + (value if isinstance(value, bytes) else value.encode()) + b"\r\n"
    body = (
        part("chat_id", TELEGRAM_CHAT) +
        part("caption", caption) +
        part("photo", img_bytes, filename="equity.png", content_type="image/png") +
        f"--{boundary.decode()}--\r\n".encode()
    )
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary.decode()}"}
    )
    urllib.request.urlopen(req, timeout=15)

def read_file(path):
    try:
        with open(path) as f:
            return f.readlines()
    except FileNotFoundError:
        return []

def fetch_json(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "t-rade/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception:
        return None

# ── Data collection ───────────────────────────────────────────────────────────

def load_trades():
    """Return list of sell rows as dicts."""
    rows = []
    try:
        with open("trades.csv") as fh:
            for line in fh:
                parts = line.strip().split(",")
                if len(parts) < 17 or parts[4] != "sell":
                    continue
                try:
                    ts  = datetime.strptime(parts[0][:19], "%Y-%m-%d %H:%M:%S")
                    pnl = float(parts[9]) if parts[9] else 0.0
                    bal = float(parts[17]) if len(parts) > 17 else 0.0
                    rows.append({
                        "ts":    ts,
                        "asset": parts[3],
                        "ind":   parts[16],   # p or l
                        "pnl":   pnl,
                        "bal":   bal,
                        "risk":  float(parts[22]) if len(parts) > 22 else 0.0,
                        "hold":  float(parts[25]) if len(parts) > 25 else 0.0,
                    })
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    return rows

def parse_strategy_log():
    lines = read_file("strategy_log.md")
    result = {"sl": "?", "target": "?", "ma": "?", "wr": "?",
              "avg_pnl": "?", "proj": "?", "cycle": "0", "status": "?",
              "best_params": "?", "param_switches": 0}
    switches = 0
    for line in lines:
        m = re.search(r"SL=([\d.]+).*?Target=([\d.]+)", line)
        if m:
            result["sl"] = m.group(1)
            result["target"] = m.group(2)
        m = re.search(r"MA\((\d+)/(\d+)\)", line)
        if m:
            result["ma"] = f"{m.group(1)}/{m.group(2)}"
        m = re.search(r"Win Rate.*?([\d.]+)%.*?\((\d+)W / (\d+)L\)", line)
        if m:
            result["wr"] = f"{m.group(1)}% ({m.group(2)}W/{m.group(3)}L)"
        m = re.search(r"Avg PnL.*?\$([-\d.]+)", line)
        if m:
            result["avg_pnl"] = f"${m.group(1)}"
        m = re.search(r"Projected 7d Equity.*?\$([-\d.]+)", line)
        if m:
            result["proj"] = f"${m.group(1)}"
        m = re.search(r"Status.*?(on.track|below target)", line)
        if m:
            result["status"] = m.group(1)
        m = re.search(r"Cycle-(\d+)", line)
        if m:
            result["cycle"] = m.group(1)
        if "Switching to param set" in line:
            switches += 1
        m = re.search(r"New best params.*?SL=([\d.]+).*?Target=([\d.]+).*?MA\((\d+)/(\d+)\)", line)
        if m:
            result["best_params"] = f"SL={m.group(1)} TP={m.group(2)} MA({m.group(3)}/{m.group(4)})"
    result["param_switches"] = switches
    return result

def parse_analytics():
    lines = read_file("analytics_report.md")
    result = {"best_hour": "?", "best_token": "?", "worst_token": "?",
              "best_risk": "?", "best_hold": "?"}
    section = None
    token_rows, risk_rows, hold_rows = [], [], []
    for line in lines:
        if "## Asset" in line:
            section = "asset"
        elif "## Risk Score Band" in line:
            section = "risk"
        elif "## Hold Time Band" in line:
            section = "hold"
        elif line.startswith("## "):
            section = None
        m = re.search(r"Best hour[^:]*:\s*(\d{2}):00 UTC", line)
        if m:
            result["best_hour"] = f"{m.group(1)}:00 UTC"
        if section == "asset":
            m = re.search(r"^([A-Z]+USDT)\s+(\d+)\s+\d+%\s+\$\s*[-\d.]+\s+\$\s*([-\d.]+)", line)
            if m and int(m.group(2)) >= 3:
                token_rows.append((m.group(1), float(m.group(3))))
        if section == "risk":
            m = re.search(r"^(\S+)\s+\d+\s+\d+%\s+\$\s*[-\d.]+\s+\$\s*([-\d.]+)", line)
            if m and re.match(r"[\d]", m.group(1)):
                risk_rows.append((m.group(1), float(m.group(2))))
        if section == "hold":
            m = re.search(r"^(<?\d+[^,\s]+)\s+\d+\s+\d+%\s+\$\s*[-\d.]+\s+\$\s*([-\d.]+)", line)
            if m:
                hold_rows.append((m.group(1), float(m.group(2))))
    if token_rows:
        token_rows.sort(key=lambda x: x[1], reverse=True)
        result["best_token"]  = f"{token_rows[0][0]} (${token_rows[0][1]:+.4f}/trade)"
        result["worst_token"] = f"{token_rows[-1][0]} (${token_rows[-1][1]:+.4f}/trade)"
    if risk_rows:
        best = max(risk_rows, key=lambda x: x[1])
        result["best_risk"] = f"{best[0]} (avg ${best[1]:+.4f})"
    if hold_rows:
        best = max(hold_rows, key=lambda x: x[1])
        result["best_hold"] = f"{best[0]} (avg ${best[1]:+.4f})"
    return result

def get_btc_24h():
    """Return BTC 24h price change %."""
    data = fetch_json("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT")
    if data:
        try:
            return float(data["priceChangePercent"])
        except Exception:
            pass
    return None

def get_fear_greed():
    data = fetch_json("https://api.alternative.me/fng/?limit=1")
    if data:
        try:
            e = data["data"][0]
            return int(e["value"]), e["value_classification"]
        except Exception:
            pass
    return None, "unknown"

def get_btc_dominance():
    data = fetch_json("https://api.coingecko.com/api/v3/global")
    if data:
        try:
            return round(data["data"]["market_cap_percentage"]["btc"], 1)
        except Exception:
            pass
    return None

# ── Equity trajectory chart ───────────────────────────────────────────────────

def build_equity_chart(trades):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import numpy as np
    except ImportError as e:
        print(f"matplotlib not available: {e}")
        return None

    now      = datetime.now()
    elapsed  = (now - START_DATE).total_seconds() / 86400
    days_arr = np.linspace(0, TOTAL_DAYS, 200)

    # Required curve: geometric growth from START to TARGET over TOTAL_DAYS
    required = START_EQUITY * (TARGET_EQUITY / START_EQUITY) ** (days_arr / TOTAL_DAYS)

    # Actual equity from trade log
    if trades:
        ts_list  = [START_DATE] + [t["ts"] for t in trades]
        eq_list  = [START_EQUITY] + [t["bal"] for t in trades]
    else:
        ts_list  = [START_DATE, now]
        eq_list  = [START_EQUITY, START_EQUITY]

    # Daily win rate bars (last 7 days)
    daily = {}
    for t in trades:
        day = t["ts"].date()
        if day not in daily:
            daily[day] = [0, 0]
        if t["ind"] == "p":
            daily[day][0] += 1
        else:
            daily[day][1] += 1

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 7), facecolor="#1a1a2e",
                                    gridspec_kw={"height_ratios": [3, 1]})

    # ── Top: equity trajectory ────────────────────────────────────────────────
    ax1.set_facecolor("#16213e")
    req_days = [START_DATE + timedelta(days=d) for d in days_arr]
    ax1.plot(req_days, required, color="#ffaa00", linewidth=1.5,
             linestyle="--", label=f"Required (→${TARGET_EQUITY:.0f})")
    ax1.plot(ts_list, eq_list, color="#00c897", linewidth=2, label="Actual equity")
    ax1.axhline(TARGET_EQUITY, color="#ff6b6b", linewidth=0.8, linestyle=":")
    ax1.fill_between(ts_list, eq_list, alpha=0.15, color="#00c897")

    # Mark current position
    if eq_list:
        ax1.scatter([ts_list[-1]], [eq_list[-1]], color="#00c897", s=60, zorder=5)
        ax1.annotate(f"  ${eq_list[-1]:.2f}", (ts_list[-1], eq_list[-1]),
                     color="#00c897", fontsize=9)

    ax1.set_xlim(START_DATE, START_DATE + timedelta(days=TOTAL_DAYS))
    ax1.set_ylim(0, TARGET_EQUITY * 1.1)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax1.tick_params(colors="#aaaaaa", labelsize=8)
    ax1.spines[:].set_color("#333355")
    ax1.set_ylabel("Equity ($)", color="#aaaaaa", fontsize=9)
    ax1.legend(facecolor="#1a1a2e", edgecolor="none", labelcolor="white", fontsize=8)
    ax1.set_title("t-rade Equity Trajectory", color="white", fontsize=11, fontweight="bold")

    # ── Bottom: daily W/L bars ────────────────────────────────────────────────
    ax2.set_facecolor("#16213e")
    if daily:
        days_sorted = sorted(daily.keys())
        x  = range(len(days_sorted))
        ws = [daily[d][0] for d in days_sorted]
        ls = [daily[d][1] for d in days_sorted]
        ax2.bar(x, ws, color="#00c897", label="W", edgecolor="none")
        ax2.bar(x, ls, bottom=ws, color="#e05c5c", label="L", edgecolor="none")
        ax2.set_xticks(list(x))
        ax2.set_xticklabels([d.strftime("%d %b") for d in days_sorted],
                            color="#aaaaaa", fontsize=7, rotation=20)
        ax2.tick_params(colors="#aaaaaa")
        ax2.spines[:].set_color("#333355")
        ax2.set_ylabel("Trades", color="#aaaaaa", fontsize=8)
        ax2.legend(facecolor="#1a1a2e", edgecolor="none", labelcolor="white",
                   fontsize=8, loc="upper left")
    ax2.set_title("Daily Win / Loss", color="#aaaaaa", fontsize=9)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()

# ── Build and send ────────────────────────────────────────────────────────────

now     = datetime.now()
today   = now.strftime("%Y-%m-%d")
elapsed = max(1, (now - START_DATE).days)
remaining = max(0, TOTAL_DAYS - elapsed)

trades  = load_trades()
sl      = parse_strategy_log()
an      = parse_analytics()
btc_24h = get_btc_24h()
fg_val, fg_label = get_fear_greed()
btc_dom = get_btc_dominance()

# ── Performance metrics ───────────────────────────────────────────────────────
sells       = [t for t in trades]
wins        = sum(1 for t in sells if t["ind"] == "p")
losses      = sum(1 for t in sells if t["ind"] == "l")
total       = wins + losses
win_rate    = wins / total * 100 if total else 0
total_pnl   = sum(t["pnl"] for t in sells)
avg_pnl     = total_pnl / total if total else 0
current_eq  = sells[-1]["bal"] if sells else START_EQUITY
portfolio_pct = (current_eq - START_EQUITY) / START_EQUITY * 100

# t-rade vs market
if btc_24h is not None:
    # annualise both to same timebase: daily rate
    pnl_today = sum(t["pnl"] for t in sells
                    if t["ts"].date() == now.date())
    eq_yesterday = current_eq - pnl_today
    portfolio_24h = (pnl_today / eq_yesterday * 100) if eq_yesterday else 0
    vs_market = portfolio_24h - btc_24h
    market_str = (
        f"  t-rade 24h: {portfolio_24h:+.2f}%\n"
        f"  BTC 24h:    {btc_24h:+.2f}%\n"
        f"  Alpha:      {vs_market:+.2f}%"
    )
else:
    market_str = "  Market data unavailable"

# ── Objective status ──────────────────────────────────────────────────────────
gap         = TARGET_EQUITY - current_eq
required_now = START_EQUITY * (TARGET_EQUITY / START_EQUITY) ** (elapsed / TOTAL_DAYS)
on_curve    = current_eq >= required_now
daily_req   = (TARGET_EQUITY / current_eq) ** (1 / max(remaining, 1)) - 1  # % per day needed
proj_num    = float(re.sub(r"[^\d.-]", "", sl["proj"])) if sl["proj"] != "?" else 0

if on_curve:
    trajectory = f"ON CURVE (need ${required_now:.2f}, have ${current_eq:.2f})"
else:
    deficit = required_now - current_eq
    trajectory = f"BEHIND (${deficit:.2f} below required curve)"

# ── AI engine summary ─────────────────────────────────────────────────────────
ai_lines = read_file("milestone_log.md")
ai_errors = sum(1 for l in ai_lines if "ERROR" in l)
ai_milestones = sum(1 for l in ai_lines if "MILESTONE" in l)
ai_live_enabled = any("Live trading enabled" in l for l in ai_lines)

# ── Warnings ──────────────────────────────────────────────────────────────────
warnings = []
if win_rate < 40 and total > 10:
    warnings.append(f"Win rate {win_rate:.0f}% below 40%")
consec_l = 0
for t in reversed(sells):
    if t["ind"] == "l":
        consec_l += 1
    else:
        break
if consec_l >= 3:
    warnings.append(f"{consec_l} consecutive losses")
if not on_curve:
    warnings.append(f"Behind required trajectory by ${required_now - current_eq:.2f}")
if ai_errors > 0:
    warnings.append(f"AI engine logged {ai_errors} error(s)")
warn_str = "\n  ".join(warnings) if warnings else "NONE"

# ── Compose message ───────────────────────────────────────────────────────────
msg = f"""📊 t-rade DAILY EXECUTIVE SUMMARY — {today}

━━━ OBJECTIVE ━━━
  Start: ${START_EQUITY:.2f}  →  Target: ${TARGET_EQUITY:.2f} (10×)
  Day {elapsed} of {TOTAL_DAYS} | {remaining} day(s) remaining
  Current equity: ${current_eq:.2f} ({portfolio_pct:+.1f}% vs start)
  Trajectory: {trajectory}
  Required daily growth: {daily_req*100:.2f}%/day from here
  Projected 7d: {sl['proj']}

━━━ vs MARKET ━━━
{market_str}
  BTC Dominance: {f'{btc_dom}%' if btc_dom else '?'} | F&G: {fg_val} ({fg_label})

━━━ SESSION PERFORMANCE ━━━
  Trades: {total} ({wins}W / {losses}L — {win_rate:.1f}% WR)
  Total P&L: ${total_pnl:.4f} | Avg/trade: ${avg_pnl:.4f}
  Best token:  {an['best_token']}
  Worst token: {an['worst_token']}
  Best hour:   {an['best_hour']}
  Best risk band: {an['best_risk']}
  Best hold time: {an['best_hold']}

━━━ AI ENGINE ━━━
  Eval cycles: {sl['cycle']} | Param switches: {sl['param_switches']}
  Current params: SL={sl['sl']} TP={sl['target']} MA({sl['ma']})
  Best params found: {sl['best_params']}
  Live trading: {'ENABLED' if ai_live_enabled else 'SIM ONLY'}
  Engine errors: {ai_errors} | Milestones hit: {ai_milestones}

━━━ WARNINGS ━━━
  {warn_str}"""

print(msg)
send_text(msg)
print("Daily summary sent.")

try:
    chart = build_equity_chart(trades)
    if chart:
        send_photo(chart, caption=f"Equity trajectory — Day {elapsed}/{TOTAL_DAYS}")
        print("Equity chart sent.")
    else:
        print("Chart build returned None — check matplotlib install.")
except Exception as e:
    import traceback
    print(f"Chart error: {e}")
    traceback.print_exc()
