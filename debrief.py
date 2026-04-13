"""
debrief.py — end-of-mission analysis.

Generates a comprehensive debrief of the 7-day t-rade mission:
  - Objective result (hit / missed / by how much)
  - What the AI engine learned
  - Best and worst conditions found
  - Market context (BTC, F&G correlation)
  - Strategy evolution across param cycles
  - What to carry forward to the next mission
  - Recommended starting params for Mission 2

Run manually at end of mission:
  python debrief.py

Outputs debrief.md and sends a summary to Telegram.
"""

import re
import io
import json
import os
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID")

START_EQUITY  = 18.07
TARGET_EQUITY = 180.70   # 10× start equity
START_DATE    = datetime(2026, 4, 13)
TOTAL_DAYS    = 7

# ── Helpers ───────────────────────────────────────────────────────────────────

def read_file(path):
    try:
        with open(path) as f:
            return f.readlines()
    except FileNotFoundError:
        return []

def send_text(msg):
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT, "text": msg}).encode()
    req  = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=data
    )
    urllib.request.urlopen(req, timeout=10)

def send_photo(img_bytes, caption=""):
    boundary = b"----DebriefBoundary"
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
        part("photo", img_bytes, filename="debrief.png", content_type="image/png") +
        f"--{boundary.decode()}--\r\n".encode()
    )
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary.decode()}"}
    )
    urllib.request.urlopen(req, timeout=15)

# ── Data loading ──────────────────────────────────────────────────────────────

def load_trades():
    rows = []
    try:
        with open("trades.csv") as fh:
            for line in fh:
                parts = line.strip().split(",")
                if len(parts) < 17 or parts[4] != "sell":
                    continue
                try:
                    rows.append({
                        "ts":     datetime.strptime(parts[0][:19], "%Y-%m-%d %H:%M:%S"),
                        "asset":  parts[3],
                        "ind":    parts[16],
                        "pnl":    float(parts[9]) if parts[9] else 0.0,
                        "bal":    float(parts[17]) if len(parts) > 17 else 0.0,
                        "risk":   float(parts[22]) if len(parts) > 22 else 0.0,
                        "hold":   float(parts[25]) if len(parts) > 25 else 0.0,
                        "hour":   int(parts[1]) if parts[1].strip().isdigit() else 0,
                        "ma":     f"{parts[23]}/{parts[24]}" if len(parts) > 24 else "?",
                        "fg":     int(parts[26]) if len(parts) > 26 and parts[26].strip().isdigit() else None,
                        "trend":  parts[21] if len(parts) > 21 else "?",
                    })
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    return rows

def load_strategy_log():
    lines = read_file("strategy_log.md")
    cycles = []
    current = {}
    for line in lines:
        m = re.search(r"## [✓✗] (Cycle-\d+) — (.+)", line)
        if m:
            if current:
                cycles.append(current)
            current = {"name": m.group(1), "ts": m.group(2)}
        m = re.search(r"SL=([\d.]+).*?Target=([\d.]+).*?MA\((\d+)/(\d+)\)", line)
        if m:
            current["sl"] = m.group(1)
            current["tp"] = m.group(2)
            current["ma"] = f"{m.group(3)}/{m.group(4)}"
        m = re.search(r"Win Rate.*?([\d.]+)%.*?\((\d+)W / (\d+)L\)", line)
        if m:
            current["wr"] = float(m.group(1))
            current["wins"] = int(m.group(2))
            current["losses"] = int(m.group(3))
        m = re.search(r"Avg PnL.*?\$([-\d.]+)", line)
        if m:
            current["avg_pnl"] = float(m.group(1))
        m = re.search(r"Projected 7d.*?\$([-\d.]+)", line)
        if m:
            current["proj"] = float(m.group(1))
        m = re.search(r"Status.*?(on.track|below target)", line)
        if m:
            current["status"] = m.group(1)
    if current:
        cycles.append(current)
    return cycles

# ── Analysis ──────────────────────────────────────────────────────────────────

def analyse(trades, cycles):
    now         = datetime.now()
    elapsed     = (now - START_DATE).days
    wins        = [t for t in trades if t["ind"] == "p"]
    losses      = [t for t in trades if t["ind"] == "l"]
    total       = len(trades)
    win_rate    = len(wins) / total * 100 if total else 0
    total_pnl   = sum(t["pnl"] for t in trades)
    avg_pnl     = total_pnl / total if total else 0
    final_eq    = trades[-1]["bal"] if trades else START_EQUITY
    achieved    = final_eq / START_EQUITY
    target_mult = TARGET_EQUITY / START_EQUITY

    # Best/worst hours
    hour_stats = {}
    for t in trades:
        h = t["hour"]
        if h not in hour_stats:
            hour_stats[h] = {"w": 0, "l": 0, "pnl": 0.0}
        hour_stats[h]["w" if t["ind"] == "p" else "l"] += 1
        hour_stats[h]["pnl"] += t["pnl"]
    hour_avg = {h: s["pnl"] / (s["w"] + s["l"]) for h, s in hour_stats.items() if s["w"] + s["l"] >= 3}
    best_hour  = max(hour_avg, key=hour_avg.get) if hour_avg else "?"
    worst_hour = min(hour_avg, key=hour_avg.get) if hour_avg else "?"

    # Best/worst assets
    asset_stats = {}
    for t in trades:
        a = t["asset"]
        if a not in asset_stats:
            asset_stats[a] = {"w": 0, "l": 0, "pnl": 0.0}
        asset_stats[a]["w" if t["ind"] == "p" else "l"] += 1
        asset_stats[a]["pnl"] += t["pnl"]
    asset_avg = {a: s["pnl"] / (s["w"] + s["l"]) for a, s in asset_stats.items() if s["w"] + s["l"] >= 3}
    best_assets  = sorted(asset_avg, key=asset_avg.get, reverse=True)[:3]
    worst_assets = sorted(asset_avg, key=asset_avg.get)[:3]

    # Best MA config
    ma_stats = {}
    for t in trades:
        m = t["ma"]
        if m not in ma_stats:
            ma_stats[m] = {"w": 0, "l": 0, "pnl": 0.0}
        ma_stats[m]["w" if t["ind"] == "p" else "l"] += 1
        ma_stats[m]["pnl"] += t["pnl"]
    ma_avg = {m: s["pnl"] / (s["w"] + s["l"]) for m, s in ma_stats.items() if s["w"] + s["l"] >= 5}
    best_ma = max(ma_avg, key=ma_avg.get) if ma_avg else "9/21"

    # F&G correlation
    fg_stats = {}
    for t in trades:
        if t["fg"] is None:
            continue
        band = "Extreme Fear" if t["fg"] < 25 else "Fear" if t["fg"] < 45 else "Neutral" if t["fg"] < 55 else "Greed" if t["fg"] < 75 else "Extreme Greed"
        if band not in fg_stats:
            fg_stats[band] = {"w": 0, "l": 0, "pnl": 0.0}
        fg_stats[band]["w" if t["ind"] == "p" else "l"] += 1
        fg_stats[band]["pnl"] += t["pnl"]
    fg_avg = {b: s["pnl"] / (s["w"] + s["l"]) for b, s in fg_stats.items() if s["w"] + s["l"] >= 3}
    best_fg  = max(fg_avg, key=fg_avg.get) if fg_avg else "?"
    worst_fg = min(fg_avg, key=fg_avg.get) if fg_avg else "?"

    # Risk band
    risk_stats = {}
    for t in trades:
        band = f"{int(t['risk'] // 2) * 2}–{int(t['risk'] // 2) * 2 + 2}"
        if band not in risk_stats:
            risk_stats[band] = {"w": 0, "l": 0, "pnl": 0.0}
        risk_stats[band]["w" if t["ind"] == "p" else "l"] += 1
        risk_stats[band]["pnl"] += t["pnl"]
    risk_avg = {b: s["pnl"] / (s["w"] + s["l"]) for b, s in risk_stats.items() if s["w"] + s["l"] >= 3}
    best_risk = max(risk_avg, key=risk_avg.get) if risk_avg else "2–4"

    # Best AI cycle
    best_cycle = max(cycles, key=lambda c: c.get("avg_pnl", -999)) if cycles else {}

    # Consecutive loss streaks
    max_consec_loss = 0
    cur = 0
    for t in trades:
        if t["ind"] == "l":
            cur += 1
            max_consec_loss = max(max_consec_loss, cur)
        else:
            cur = 0

    # Hold time analysis
    win_hold  = sum(t["hold"] for t in wins) / len(wins) / 60 if wins else 0
    loss_hold = sum(t["hold"] for t in losses) / len(losses) / 60 if losses else 0

    return {
        "elapsed": elapsed, "final_eq": final_eq, "achieved": achieved,
        "target_mult": target_mult, "total": total, "win_rate": win_rate,
        "total_pnl": total_pnl, "avg_pnl": avg_pnl,
        "best_hour": best_hour, "worst_hour": worst_hour,
        "best_assets": best_assets, "worst_assets": worst_assets,
        "asset_avg": asset_avg, "hour_avg": hour_avg,
        "best_ma": best_ma, "ma_avg": ma_avg,
        "best_fg": best_fg, "worst_fg": worst_fg, "fg_avg": fg_avg,
        "best_risk": best_risk, "risk_avg": risk_avg,
        "best_cycle": best_cycle, "max_consec_loss": max_consec_loss,
        "win_hold": win_hold, "loss_hold": loss_hold,
        "asset_stats": asset_stats, "hour_stats": hour_stats,
    }

# ── Chart ─────────────────────────────────────────────────────────────────────

def build_debrief_chart(trades, r):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import numpy as np
    except ImportError:
        return None

    fig = plt.figure(figsize=(12, 9), facecolor="#1a1a2e")
    gs  = fig.add_gridspec(3, 2, hspace=0.45, wspace=0.35)
    ax1 = fig.add_subplot(gs[0, :])   # equity curve — full width
    ax2 = fig.add_subplot(gs[1, 0])   # hourly performance
    ax3 = fig.add_subplot(gs[1, 1])   # asset performance
    ax4 = fig.add_subplot(gs[2, 0])   # MA config
    ax5 = fig.add_subplot(gs[2, 1])   # F&G correlation

    days_arr = np.linspace(0, TOTAL_DAYS, 200)
    required = START_EQUITY * (TARGET_EQUITY / START_EQUITY) ** (days_arr / TOTAL_DAYS)
    req_dates = [START_DATE + timedelta(days=d) for d in days_arr]
    ts_list  = [START_DATE] + [t["ts"] for t in trades]
    eq_list  = [START_EQUITY] + [t["bal"] for t in trades]

    def style(ax, title):
        ax.set_facecolor("#16213e")
        ax.spines[:].set_color("#333355")
        ax.tick_params(colors="#aaaaaa", labelsize=7)
        ax.set_title(title, color="#cccccc", fontsize=8, pad=4)

    # Equity curve
    style(ax1, "Mission Equity Trajectory")
    ax1.plot(req_dates, required, color="#ffaa00", linewidth=1.5, linestyle="--", label=f"Required (→${TARGET_EQUITY:.0f})")
    ax1.plot(ts_list, eq_list, color="#00c897", linewidth=2, label="Actual")
    ax1.axhline(TARGET_EQUITY, color="#ff6b6b", linewidth=0.8, linestyle=":")
    ax1.fill_between(ts_list, eq_list, alpha=0.12, color="#00c897")
    if eq_list:
        ax1.annotate(f"  Final: ${eq_list[-1]:.2f}", (ts_list[-1], eq_list[-1]),
                     color="#00c897", fontsize=8)
    ax1.set_xlim(START_DATE, START_DATE + timedelta(days=TOTAL_DAYS))
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax1.set_ylabel("Equity ($)", color="#aaaaaa", fontsize=8)
    ax1.legend(facecolor="#1a1a2e", edgecolor="none", labelcolor="white", fontsize=7)

    # Hourly P&L
    style(ax2, "Avg P&L by Hour (UTC)")
    if r["hour_avg"]:
        hours = sorted(r["hour_avg"].keys())
        vals  = [r["hour_avg"][h] for h in hours]
        colors = ["#00c897" if v >= 0 else "#e05c5c" for v in vals]
        ax2.bar([str(h) for h in hours], vals, color=colors, edgecolor="none")
        ax2.axhline(0, color="#555577", linewidth=0.8)
        ax2.set_xlabel("Hour", color="#aaaaaa", fontsize=7)

    # Asset P&L
    style(ax3, "Avg P&L by Asset (min 3 trades)")
    if r["asset_avg"]:
        assets = sorted(r["asset_avg"], key=r["asset_avg"].get, reverse=True)[:8]
        vals   = [r["asset_avg"][a] for a in assets]
        colors = ["#00c897" if v >= 0 else "#e05c5c" for v in vals]
        labels = [a.replace("USDT", "") for a in assets]
        ax3.barh(labels[::-1], vals[::-1], color=colors[::-1], edgecolor="none")
        ax3.axvline(0, color="#555577", linewidth=0.8)

    # MA config
    style(ax4, "Avg P&L by MA Config")
    if r["ma_avg"]:
        mas  = sorted(r["ma_avg"], key=r["ma_avg"].get, reverse=True)
        vals = [r["ma_avg"][m] for m in mas]
        colors = ["#00c897" if v >= 0 else "#e05c5c" for v in vals]
        ax4.bar(mas, vals, color=colors, edgecolor="none")
        ax4.axhline(0, color="#555577", linewidth=0.8)

    # F&G correlation
    style(ax5, "Avg P&L by Fear & Greed Band")
    if r["fg_avg"]:
        order = ["Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed"]
        fgs   = [b for b in order if b in r["fg_avg"]]
        vals  = [r["fg_avg"][b] for b in fgs]
        colors = ["#00c897" if v >= 0 else "#e05c5c" for v in vals]
        short = [b.replace(" Fear", " Fear").replace("Extreme ", "X.") for b in fgs]
        ax5.bar(short, vals, color=colors, edgecolor="none")
        ax5.axhline(0, color="#555577", linewidth=0.8)

    fig.suptitle("t-rade Mission Debrief", color="white", fontsize=13, fontweight="bold", y=0.98)
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()

# ── Write markdown report ─────────────────────────────────────────────────────

def write_report(trades, cycles, r):
    now    = datetime.now().strftime("%Y-%m-%d %H:%M")
    result = "TARGET ACHIEVED ✓" if r["final_eq"] >= TARGET_EQUITY else f"TARGET MISSED — {r['achieved']:.2f}× achieved vs {r['target_mult']:.1f}× target"

    # Best AI cycle details
    bc = r["best_cycle"]
    best_cycle_str = (
        f"Cycle {bc.get('name','?')} — SL={bc.get('sl','?')} TP={bc.get('tp','?')} MA({bc.get('ma','?')}) "
        f"WR={bc.get('wr',0):.1f}% AvgPnL=${bc.get('avg_pnl',0):.4f}"
        if bc else "Insufficient data"
    )

    # Recommended params for Mission 2
    m2_ma   = r["best_ma"]
    m2_risk = r["best_risk"]
    m2_hour = r["best_hour"]

    lines = [
        f"# t-rade Mission Debrief",
        f"",
        f"_Generated: {now}_",
        f"",
        f"---",
        f"",
        f"## Mission Result",
        f"",
        f"| | |",
        f"|---|---|",
        f"| Start equity | ${START_EQUITY:.2f} |",
        f"| Final equity | ${r['final_eq']:.2f} |",
        f"| Return | {(r['final_eq'] - START_EQUITY) / START_EQUITY * 100:+.1f}% |",
        f"| Target | ${TARGET_EQUITY:.2f} ({r['target_mult']:.0f}×) |",
        f"| Result | **{result}** |",
        f"| Duration | {r['elapsed']} days |",
        f"| Total trades | {r['total']} |",
        f"| Win rate | {r['win_rate']:.1f}% |",
        f"| Total P&L | ${r['total_pnl']:.4f} |",
        f"| Avg P&L/trade | ${r['avg_pnl']:.4f} |",
        f"",
        f"---",
        f"",
        f"## What the Market Did",
        f"",
        f"Market context for this mission — refer to Fear & Greed data logged with each trade.",
        f"The BTC filter blocks live entries during active downtrends; sim runs regardless.",
        f"The F&G gate (min 25) protects capital during Extreme Fear conditions.",
        f"",
        f"Best F&G condition for trading: **{r['best_fg']}**",
        f"Worst F&G condition: **{r['worst_fg']}**",
        f"",
        f"| F&G Band | Avg P&L/trade |",
        f"|---|---|",
    ] + [
        f"| {b} | ${v:.4f} |" for b, v in sorted(r["fg_avg"].items(), key=lambda x: x[1], reverse=True)
    ] + [
        f"",
        f"---",
        f"",
        f"## What the AI Engine Learned",
        f"",
        f"- Ran **{len(cycles)} evaluation cycles** across the mission",
        f"- Best performing parameter set: {best_cycle_str}",
        f"- Best MA configuration by evidence: **{r['best_ma']}**",
        f"- Best risk band: **{r['best_risk']}**",
        f"- Param switching was blocked during BTC downtrend (no new trades = no new data)",
        f"",
        f"### Parameter cycle results",
        f"",
        f"| Cycle | Params | WR | Avg PnL | Proj 7d |",
        f"|---|---|---|---|---|",
    ] + [
        f"| {c.get('name','?')} | SL={c.get('sl','?')} TP={c.get('tp','?')} MA({c.get('ma','?')}) | {c.get('wr',0):.1f}% | ${c.get('avg_pnl',0):.4f} | ${c.get('proj',0):.2f} |"
        for c in cycles
    ] + [
        f"",
        f"---",
        f"",
        f"## Best Conditions Found",
        f"",
        f"| Dimension | Best | Worst |",
        f"|---|---|---|",
        f"| Hour (UTC) | {r['best_hour']:02d}:00 (avg ${r['hour_avg'].get(r['best_hour'], 0):.4f}) | {r['worst_hour']:02d}:00 (avg ${r['hour_avg'].get(r['worst_hour'], 0):.4f}) |"
        if r['best_hour'] != "?" else
        f"| Hour (UTC) | ? (insufficient data) | ? (insufficient data) |",
        f"| Asset | {', '.join(r['best_assets'])} | {', '.join(r['worst_assets'])} |",
        f"| MA config | {r['best_ma']} | — |",
        f"| Risk band | {r['best_risk']} | — |",
        f"| F&G | {r['best_fg']} | {r['worst_fg']} |",
        f"",
        f"**Win hold time**: {r['win_hold']:.1f}m avg  |  **Loss hold time**: {r['loss_hold']:.1f}m avg",
        f"",
        f"> Wins were held {abs(r['win_hold'] - r['loss_hold']):.1f}m {'longer' if r['win_hold'] > r['loss_hold'] else 'shorter'} than losses on average.",
        f"",
        f"**Max consecutive losses**: {r['max_consec_loss']}",
        f"",
        f"---",
        f"",
        f"## What We Would Do Differently",
        f"",
        f"1. **CSV duplication bug (M1 fix)** — Each sell re-appended entire session history. Fixed: only the new sell row is written per trade.",
        f"",
        f"2. **AI cycling too fast (M1 fix)** — Duplicated rows inflated trade counts, causing param switches before 5 new trades were collected. Fixed: dedup on load.",
        f"",
        f"3. **F&G entry gate (M1 fix)** — Added: live entries blocked when F&G < 25. Sim still runs to collect bear-market data.",
        f"",
        f"4. **Position sizing at low balance** — With ~$18 balance the `MIN_NOTIONAL` floor of $10 overrides risk-based sizing for high-risk entries. Risk scaling is only meaningful above ~$50.",
        f"",
        f"5. **Token reputation system** — Needs a full mission of data to prove itself.",
        f"",
        f"6. **Mid-trade reassessment** — 50% score threshold may be too aggressive; monitor false switches.",
        f"",
        f"---",
        f"",
        f"## Recommended Starting Config for Mission 2",
        f"",
        f"Based on evidence from this mission:",
        f"",
        f"| Parameter | Value | Reason |",
        f"|---|---|---|",
        f"| MA | {m2_ma} | Best avg P&L across all configs |",
        f"| SL | 0.990 | Baseline — tighten after trailing TP |",
        f"| TP | 1.010 | Baseline — 1% target |",
        f"| Risk band | {m2_risk} | Highest win rate and avg P&L |",
        f"| Best hour | {m2_hour} | Highest avg P&L |",
        f"| Min F&G | 25 | Block Extreme Fear entries |",
        f"| BTC filter | 5m spread | Only block active downtrend (negative + widening) |",
        f"| Position size | 60%→30% | Scales with risk — meaningful above $50 balance |",
        f"",
        f"---",
        f"",
        f"## Key Numbers to Beat in Mission 2",
        f"",
        f"| Metric | Mission 1 | Target for Mission 2 |",
        f"|---|---|---|",
        f"| Win rate | {r['win_rate']:.1f}% | >50% |",
        f"| Avg P&L/trade | ${r['avg_pnl']:.4f} | >$0.05 |",
        f"| Max consec losses | {r['max_consec_loss']} | <4 |",
        f"| Return | {(r['final_eq'] - START_EQUITY) / START_EQUITY * 100:+.1f}% | >900% |",
        f"| Total trades | {r['total']} | — |",
        f"",
        f"---",
        f"",
        f"_t-rade is a live experiment. Past performance does not guarantee future results._",
    ]

    report = "\n".join(lines)
    with open("debrief.md", "w") as fh:
        fh.write(report)
    return report

# ── Main ──────────────────────────────────────────────────────────────────────

print("Loading trade data...")
trades = load_trades()
cycles = load_strategy_log()

if not trades:
    print("No trades found in trades.csv — cannot generate debrief.")
    exit(1)

print(f"Loaded {len(trades)} trades, {len(cycles)} AI cycles.")

r = analyse(trades, cycles)

print("Writing debrief.md...")
report = write_report(trades, cycles, r)
print("debrief.md written.")

# Telegram summary (condensed — full detail in debrief.md)
result_icon = "✅" if r["final_eq"] >= TARGET_EQUITY else "❌"
msg = f"""{result_icon} t-rade MISSION DEBRIEF

RESULT: ${r['final_eq']:.2f} final ({(r['final_eq'] - START_EQUITY) / START_EQUITY * 100:+.1f}%)
TARGET: ${TARGET_EQUITY:.2f} ({r['target_mult']:.0f}×) — {'ACHIEVED' if r['final_eq'] >= TARGET_EQUITY else 'NOT ACHIEVED'}

TRADES: {r['total']} | WR: {r['win_rate']:.1f}% | P&L: ${r['total_pnl']:.4f}
Avg/trade: ${r['avg_pnl']:.4f} | Max consec losses: {r['max_consec_loss']}

BEST CONDITIONS:
  Hour: {f"{r['best_hour']:02d}:00" if r['best_hour'] != '?' else '?'} UTC
  MA: {r['best_ma']} | Risk: {r['best_risk']}
  F&G: {r['best_fg']}
  Tokens: {', '.join(r['best_assets'][:2])}

MISSION 2 START PARAMS:
  MA {r['best_ma']} | SL=0.990 | TP=1.010
  Risk band {r['best_risk']} | Min F&G 25

Full debrief → debrief.md"""

print("Sending Telegram summary...")
try:
    send_text(msg)
    print("Summary sent.")
except Exception as e:
    print(f"Telegram send failed: {e}")

print("Building debrief chart...")
try:
    chart = build_debrief_chart(trades, r)
    if chart:
        send_photo(chart, caption="Mission 1 Debrief — Performance Analysis")
        print("Chart sent.")
    else:
        print("Chart build returned None.")
except Exception as e:
    import traceback
    print(f"Chart error: {e}")
    traceback.print_exc()

print("\nDebrief complete. See debrief.md for the full report.")
