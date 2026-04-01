"""
checkin.py — run by the remote Claude agent every hour.
Reads log files, builds a real summary, sends it to Telegram.
"""
import urllib.request
import urllib.parse
import os
import sys
import re
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID")
TARGET_EQUITY  = 180.76

def read(path, tail=0):
    try:
        with open(path) as f:
            lines = f.readlines()
        return lines[-tail:] if tail else lines
    except FileNotFoundError:
        return []

def send(msg):
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT, "text": msg}).encode()
    req  = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data=data
    )
    urllib.request.urlopen(req, timeout=10)

def parse_strategy_log():
    lines = read("strategy_log.md")
    result = {"sl": "?", "target": "?", "ma": "9/21", "wr": "?", "avg_pnl": "?",
              "proj": "?", "trades": "?", "status": "?", "cycle": "?"}
    # Walk all lines; last match wins (most recent cycle at end of file)
    for line in lines:
        # Params line: SL=0.99, Target=1.01 or SL=0.99, Target=1.01, MA(9/21)
        m = re.search(r"SL=([\d.]+).*?Target=([\d.]+)", line)
        if m:
            result["sl"]     = m.group(1)
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
    return result

def parse_analytics():
    lines = read("analytics_report.md")
    result = {"best_hour": "?", "best_day": "?", "best_ma": "?", "best_risk": "?", "total_pnl": "?", "total_trades": "?"}
    for line in lines:
        m = re.search(r"Best trading windows.*?(\d{2}):00", line)
        if m:
            result["best_hour"] = f"{m.group(1)}:00 UTC"
        m = re.search(r"Best day.*?:\s*(\w+)", line)
        if m:
            result["best_day"] = m.group(1)
        m = re.search(r"Best MA config.*?:\s*([\d/]+)", line)
        if m:
            result["best_ma"] = m.group(1)
        m = re.search(r"Best risk band.*?:\s*([\d\-\s\(\w]+)", line)
        if m:
            result["best_risk"] = m.group(1).strip()
        m = re.search(r"Total P&L.*?\$([-\d.]+)", line)
        if m:
            result["total_pnl"] = f"${m.group(1)}"
        m = re.search(r"Total trades.*?(\d+)", line)
        if m:
            result["total_trades"] = m.group(1)
    return result

def parse_trades():
    rows = read("trades.csv", tail=30)
    wins = sum(1 for r in rows if ",p," in r)
    losses = sum(1 for r in rows if ",l," in r)
    # Check consecutive losses at end
    consec = 0
    for r in reversed(rows):
        if ",l," in r:
            consec += 1
        elif ",p," in r:
            break
    return wins, losses, consec

def parse_milestone():
    lines = read("milestone_log.md")
    for line in reversed(lines):
        m = re.search(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
        if m:
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                age_mins = (datetime.now(timezone.utc) - ts).seconds // 60
                return age_mins
            except Exception:
                pass
    return 9999

# ── Build summary ─────────────────────────────────────────────────────────────

sl   = parse_strategy_log()
an   = parse_analytics()
w, l, consec = parse_trades()
milestone_age = parse_milestone()

warnings = []
wr_num = float(re.search(r"([\d.]+)%", sl["wr"]).group(1)) if "%" in sl["wr"] else 0
if wr_num > 0 and wr_num < 40:
    warnings.append(f"Win rate {wr_num:.0f}% below 40%")
if consec >= 3:
    warnings.append(f"{consec} consecutive losses")
if milestone_age > 120:
    warnings.append(f"AI engine stalled ({milestone_age}m since last milestone)")
proj_num = float(re.sub(r"[^\d.-]", "", sl["proj"])) if sl["proj"] != "?" else 0
if proj_num > 0 and proj_num < TARGET_EQUITY * 0.5:
    warnings.append(f"Projected equity ${proj_num:.2f} far below ${TARGET_EQUITY} target")
warn_str = "; ".join(warnings) if warnings else "NONE"

msg = (
    f"t-rade check-in (cycle {sl['cycle']})\n\n"
    f"STATUS: {sl['status']} | SL={sl['sl']} TP={sl['target']} MA({sl['ma']})\n\n"
    f"PERFORMANCE:\n"
    f"  Win rate: {sl['wr']}\n"
    f"  Avg PnL: {sl['avg_pnl']} | Total: {an['total_pnl']}\n"
    f"  Trades: {an['total_trades']} | Projected 7d: {sl['proj']} / ${TARGET_EQUITY}\n\n"
    f"BEST CONDITIONS:\n"
    f"  Hour: {an['best_hour']} | Day: {an['best_day']}\n"
    f"  MA: {an['best_ma']} | Risk: {an['best_risk']}\n\n"
    f"WARNINGS: {warn_str}"
)

print(msg)
send(msg)
print("Telegram message sent.")
