"""
Analytics reporter for t-rade.

Reads trades.csv and writes analytics_report.md with breakdowns by:
  - Hour of day
  - Day of week
  - Asset
  - MA configuration
  - Trend direction at entry
  - Risk score band
  - Hold-time band
  - Session mode (live vs sim)

Called after every sell so the report is always fresh.
Can also be run standalone: python analytics.py
"""

import pandas as pd
from datetime import datetime

CSV_PATH    = "trades.csv"
REPORT_PATH = "analytics_report.md"

COLS = [
    "datetime", "hour", "day_of_week", "asset", "action", "session_mode",
    "price", "quantity", "cost", "pnl", "fee", "fee_asset",
    "profit_count", "loss_count", "consec_gain", "consec_loss", "indicator",
    "balance", "adjustment", "calls",
    "split_pct", "trend_dir", "risk_score", "ma_fast", "ma_slow", "hold_seconds", "fear_greed",
]

DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# ── helpers ───────────────────────────────────────────────────────────────────

def _load() -> pd.DataFrame:
    try:
        df = pd.read_csv(CSV_PATH, header=None, names=COLS)
    except FileNotFoundError:
        return pd.DataFrame(columns=COLS)

    # Coerce numeric columns — old rows may be missing new columns
    for col in ["hour", "pnl", "split_pct", "risk_score", "ma_fast", "ma_slow", "hold_seconds"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Backfill hour from datetime for rows logged before the column existed
    mask = df["hour"].isna() & df["datetime"].notna()
    if mask.any():
        df.loc[mask, "hour"] = pd.to_datetime(df.loc[mask, "datetime"], errors="coerce").dt.hour

    return df


def _sells(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["action"] == "sell"].copy()


def _fmt_row(wins, losses, total_pnl, avg_pnl, avg_hold_min=None) -> str:
    total  = wins + losses
    wr     = f"{wins / total:.0%}" if total else "–"
    pnl_s  = f"${total_pnl:.4f}"
    avg_s  = f"${avg_pnl:.4f}"
    hold_s = f"{avg_hold_min:.1f}m" if avg_hold_min is not None else "–"
    return f"{wins}W / {losses}L ({wr}) | tot P&L {pnl_s} | avg {avg_s} | hold {hold_s}"


def _section(title: str, rows: list[tuple]) -> str:
    lines = [f"## {title}\n"]
    lines.append(f"{'Label':<28} {'Trades':>7}  {'Win%':>6}  {'Total P&L':>12}  {'Avg P&L':>10}  {'Avg Hold':>9}")
    lines.append("-" * 80)
    for label, wins, losses, total_pnl, avg_pnl, avg_hold in rows:
        total = wins + losses
        wr    = f"{wins / total:.0%}" if total else "–"
        lines.append(
            f"{str(label):<28} {total:>7}  {wr:>6}  ${total_pnl:>10.4f}  ${avg_pnl:>8.4f}  {avg_hold:>7.1f}m"
        )
    lines.append("")
    return "\n".join(lines)


def _group_stats(sells: pd.DataFrame, col: str, order=None) -> list[tuple]:
    rows = []
    groups = sells.groupby(col)
    keys = order if order else sorted(sells[col].dropna().unique())
    for key in keys:
        if key not in groups.groups:
            continue
        g     = groups.get_group(key)
        wins  = int((g["indicator"] == "p").sum())
        losses = int((g["indicator"] == "l").sum())
        tp    = float(g["pnl"].fillna(0).sum()) if "pnl" in g.columns else 0.0
        ap    = float(g["pnl"].fillna(0).mean()) if "pnl" in g.columns else 0.0
        ah    = float(g["hold_seconds"].mean()) / 60 if "hold_seconds" in g and g["hold_seconds"].notna().any() else 0.0
        rows.append((key, wins, losses, round(tp, 6), round(ap, 6), round(ah, 2)))
    return rows


# ── main report ───────────────────────────────────────────────────────────────

def generate_report() -> str:
    df    = _load()
    sells = _sells(df)

    if sells.empty:
        msg = "_No completed trades yet. Report will populate as trades close._\n"
        with open(REPORT_PATH, "w") as fh:
            fh.write(f"# Analytics Report\n\n_{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_\n\n{msg}")
        return msg

    total_trades = len(sells)
    total_wins   = int((sells["indicator"] == "p").sum())
    total_losses = int((sells["indicator"] == "l").sum())
    total_pnl    = float(sells["pnl"].fillna(0).sum()) if "pnl" in sells.columns else 0.0
    avg_pnl      = float(sells["pnl"].fillna(0).mean()) if "pnl" in sells.columns else 0.0

    # Fee summary across both buys and sells
    all_rows = df[df["fee"].notna()] if "fee" in df.columns else pd.DataFrame()
    total_fees_usdt = 0.0
    total_fees_bnb  = 0.0
    if not all_rows.empty:
        usdt_fees = all_rows[all_rows["fee_asset"] == "USDT"]["fee"].fillna(0)
        bnb_fees  = all_rows[all_rows["fee_asset"] == "BNB"]["fee"].fillna(0)
        total_fees_usdt = float(usdt_fees.sum())
        total_fees_bnb  = float(bnb_fees.sum())

    lines = [
        "# Analytics Report",
        f"\n_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_\n",
        "## Summary\n",
        f"- **Total trades**: {total_trades}",
        f"- **Win / Loss**: {total_wins}W / {total_losses}L  "
        f"({total_wins/total_trades:.0%} win rate)",
        f"- **Total P&L** (net of USDT fees): ${total_pnl:.4f}",
        f"- **Avg P&L / trade**: ${avg_pnl:.4f}",
        f"- **Total fees paid**: ${total_fees_usdt:.6f} USDT  |  {total_fees_bnb:.6f} BNB",
        "",
    ]

    # ── Hour of day ──────────────────────────────────────────────────────────
    sells_h = sells.dropna(subset=["hour"]).copy()
    sells_h["hour"] = sells_h["hour"].astype(int)
    hour_rows = _group_stats(sells_h, "hour", order=list(range(24)))
    # Annotate best and worst hours
    if hour_rows:
        scored = [(r[0], r[4]) for r in hour_rows if r[1] + r[2] > 0]
        if scored:
            best_h  = max(scored, key=lambda x: x[1])
            worst_h = min(scored, key=lambda x: x[1])
            lines.append(
                f"> **Best hour**: {best_h[0]:02d}:00 UTC (avg P&L ${best_h[1]:.4f})  |  "
                f"**Worst hour**: {worst_h[0]:02d}:00 UTC (avg P&L ${worst_h[1]:.4f})\n"
            )
    lines.append(_section("Hour of Day (UTC)", hour_rows))

    # ── Day of week ──────────────────────────────────────────────────────────
    sells_d = sells.dropna(subset=["day_of_week"])
    day_rows = _group_stats(sells_d, "day_of_week", order=DAY_ORDER)
    if day_rows:
        scored_d = [(r[0], r[4]) for r in day_rows if r[1] + r[2] > 0]
        if scored_d:
            best_d  = max(scored_d, key=lambda x: x[1])
            worst_d = min(scored_d, key=lambda x: x[1])
            lines.append(
                f"> **Best day**: {best_d[0]} (avg ${best_d[1]:.4f})  |  "
                f"**Worst day**: {worst_d[0]} (avg ${worst_d[1]:.4f})\n"
            )
    lines.append(_section("Day of Week", day_rows))

    # ── Asset ────────────────────────────────────────────────────────────────
    asset_rows = _group_stats(sells, "asset")
    asset_rows.sort(key=lambda r: r[4], reverse=True)   # sort by avg P&L
    lines.append(_section("Asset", asset_rows))

    # ── MA configuration ─────────────────────────────────────────────────────
    sells_ma = sells.dropna(subset=["ma_fast", "ma_slow"]).copy()
    sells_ma["ma_config"] = sells_ma["ma_fast"].astype(int).astype(str) + "/" + sells_ma["ma_slow"].astype(int).astype(str)
    ma_rows = _group_stats(sells_ma, "ma_config")
    ma_rows.sort(key=lambda r: r[4], reverse=True)
    lines.append(_section("MA Configuration (fast/slow)", ma_rows))

    # ── Trend direction at entry ──────────────────────────────────────────────
    sells_t = sells.dropna(subset=["trend_dir"])
    trend_rows = _group_stats(sells_t, "trend_dir")
    lines.append(_section("Trend Direction at Entry", trend_rows))

    # ── Risk score band ───────────────────────────────────────────────────────
    sells_r = sells.dropna(subset=["risk_score"]).copy()
    sells_r["risk_band"] = pd.cut(
        sells_r["risk_score"],
        bins=[0, 2, 4, 6, 8, 10],
        labels=["0–2 (low)", "2–4", "4–6", "6–8", "8–10 (high)"],
        right=True,
    ).astype(str)
    risk_rows = _group_stats(sells_r, "risk_band",
                             order=["0–2 (low)", "2–4", "4–6", "6–8", "8–10 (high)"])
    lines.append(_section("Risk Score Band", risk_rows))

    # ── Hold time band ────────────────────────────────────────────────────────
    sells_hold = sells.dropna(subset=["hold_seconds"]).copy()
    sells_hold["hold_min"] = sells_hold["hold_seconds"] / 60
    sells_hold["hold_band"] = pd.cut(
        sells_hold["hold_min"],
        bins=[0, 5, 15, 30, 60, float("inf")],
        labels=["<5m", "5–15m", "15–30m", "30–60m", ">60m"],
        right=True,
    ).astype(str)
    hold_rows = _group_stats(sells_hold, "hold_band",
                             order=["<5m", "5–15m", "15–30m", "30–60m", ">60m"])
    lines.append(_section("Hold Time Band", hold_rows))

    # ── Fear & Greed band ─────────────────────────────────────────────────────
    sells_fg = sells.dropna(subset=["fear_greed"]).copy()
    sells_fg["fear_greed"] = pd.to_numeric(sells_fg["fear_greed"], errors="coerce")
    sells_fg = sells_fg.dropna(subset=["fear_greed"])
    if not sells_fg.empty:
        sells_fg["fg_band"] = pd.cut(
            sells_fg["fear_greed"],
            bins=[0, 25, 45, 55, 75, 100],
            labels=["Extreme Fear (0–25)", "Fear (25–45)", "Neutral (45–55)", "Greed (55–75)", "Extreme Greed (75–100)"],
            right=True, include_lowest=True,
        ).astype(str)
        fg_rows = _group_stats(sells_fg, "fg_band",
                               order=["Extreme Fear (0–25)", "Fear (25–45)", "Neutral (45–55)", "Greed (55–75)", "Extreme Greed (75–100)"])
        if fg_rows:
            scored_fg = [(r[0], r[4]) for r in fg_rows if r[1] + r[2] >= 3]
            if scored_fg:
                best_fg = max(scored_fg, key=lambda x: x[1])
                lines.append(f"> **Best F&G condition**: {best_fg[0]} (avg P&L ${best_fg[1]:.4f})\n")
            lines.append(_section("Fear & Greed Index Band", fg_rows))

    # ── Session mode ──────────────────────────────────────────────────────────
    sells_s = sells.dropna(subset=["session_mode"])
    mode_rows = _group_stats(sells_s, "session_mode")
    lines.append(_section("Session Mode", mode_rows))

    # ── Trading window recommendation ─────────────────────────────────────────
    if len(hour_rows) >= 3:
        scored_hours = [(r[0], r[1] + r[2], r[4]) for r in hour_rows if r[1] + r[2] >= 3]
        scored_hours.sort(key=lambda x: x[2], reverse=True)
        top3 = scored_hours[:3]
        bot3 = scored_hours[-3:] if len(scored_hours) > 3 else []
        lines.append("## Strategy Recommendations\n")
        if top3:
            windows = ", ".join(f"{h:02d}:00–{h:02d}:59 UTC" for h, _, _ in top3)
            lines.append(f"- **Best trading windows** (by avg P&L): {windows}")
        if bot3:
            avoid = ", ".join(f"{h:02d}:00 UTC" for h, _, _ in bot3)
            lines.append(f"- **Avoid** (worst avg P&L): {avoid}")

        # Risk band recommendation
        scored_risk = [(r[0], r[4]) for r in risk_rows if r[1] + r[2] >= 3]
        if scored_risk:
            best_risk = max(scored_risk, key=lambda x: x[1])
            lines.append(f"- **Best risk band**: {best_risk[0]} (avg ${best_risk[1]:.4f})")

        # MA recommendation
        scored_ma = [(r[0], r[4]) for r in ma_rows if r[1] + r[2] >= 3]
        if scored_ma:
            best_ma = max(scored_ma, key=lambda x: x[1])
            lines.append(f"- **Best MA config**: {best_ma[0]} (avg ${best_ma[1]:.4f})")

        lines.append("")

    report = "\n".join(lines)
    with open(REPORT_PATH, "w") as fh:
        fh.write(report)
    return report


if __name__ == "__main__":
    print(generate_report())
    print(f"\nReport written to {REPORT_PATH}")
