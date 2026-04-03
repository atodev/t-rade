import tkinter as tk
from tkinter import ttk
import threading
import time
import pandas as pd
from sqlalchemy import create_engine
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.animation import FuncAnimation
from dotenv import load_dotenv
import os
import sys
import math
from binance.client import Client
from termcolor import colored
from queue import Queue
from ai_engine import AIStrategyEngine
import analytics
import urllib.request
import json as _json
from datetime import datetime, timedelta

# --- Global Variables and Initializations ---

# Database Engines
engine = create_engine("sqlite:///LivepriceDB.db")
engine2 = create_engine("sqlite:///currpriceDB.db")
engine3 = create_engine("sqlite:///loggerDB.db")

# Migrate loggerDB — add any columns introduced after initial creation
with engine3.connect() as _conn:
    _existing = [row[1] for row in _conn.execute(__import__("sqlalchemy").text("PRAGMA table_info(log)")).fetchall()]
    if _existing:  # table exists
        for _col, _typedef in [("fear_greed", "INTEGER")]:
            if _col not in _existing:
                _conn.execute(__import__("sqlalchemy").text(f"ALTER TABLE log ADD COLUMN {_col} {_typedef}"))
                _conn.commit()

# Trading State
pc = 0
lc = 0
adjm = 0
call_count = 0
new_bp = 0
last_asset = ""
trade = False
action = ""
last_trade = False
cross = False
freq = 0
consec_pc = 0
consec_lc = 0
df_log = pd.DataFrame()
usdt_bal = 0
total_pnl = 0.0
initial_balance = 50.0
total_adjm = 0
in_trade = False
current_buyprice = 0.0
current_qty = 0

# --- UI Communication Globals ---
currently_analyzing_asset = ""
poll_sleep = 10
analysis_count = 0
status_queue = Queue()

# --- AI Engine Globals ---
ai_log_queue   = Queue()
ai_log_buffer  = []       # persists messages so popup can show history
current_sl     = 0.990
current_target = 1.010
BNB_FEE_RATE   = 0.00075   # 0.075% Binance fee with BNB discount

# Token blacklist / whitelist
TOKEN_LIST_FILE          = "token_lists.json"
STALL_CHECK_INTERVAL     = 10   # minutes in trade before first stall check
STALL_RANGE_PCT          = 0.15 # price must stay within this % of benchmark to be "stalling"
BLACKLIST_LOSSES_TRIGGER = 2    # losses within window to blacklist
BLACKLIST_WINDOW_HOURS   = 1    # rolling window to count losses
BLACKLIST_DURATION_HOURS = 3    # how long the blacklist lasts
WHITELIST_WINS_TRIGGER   = 2    # wins within window to whitelist
WHITELIST_DURATION_HOURS = 2    # how long the whitelist lasts
force_sim_override = False
current_ma_fast = 9
current_ma_slow = 21

# --- Session Globals ---
session_active = False
session_start_time = 0
session_counter_var = None
dark_mode = True
style = None
fig = None
ax = None
lighter_bg_color = None

# Binance Client
load_dotenv()
API_KEY        = os.getenv("API_KEY")
SECRET_KEY     = os.getenv("SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID")

def telegram_notify(message: str):
    """Send a message to Telegram. Silently fails if token not configured."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = _json.dumps({"chat_id": TELEGRAM_CHAT, "text": message, "parse_mode": "HTML"}).encode()
        req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

# --- Functions ---

def load_token_lists():
    try:
        with open(TOKEN_LIST_FILE) as f:
            data = _json.load(f)
        now = datetime.now()
        data["blacklist"] = {k: v for k, v in data.get("blacklist", {}).items()
                             if datetime.fromisoformat(v) > now}
        data["whitelist"] = {k: v for k, v in data.get("whitelist", {}).items()
                             if datetime.fromisoformat(v) > now}
        return data
    except Exception:
        return {"blacklist": {}, "whitelist": {}}

def save_token_lists(data):
    try:
        with open(TOKEN_LIST_FILE, "w") as f:
            _json.dump(data, f, indent=2)
    except Exception:
        pass

def update_token_lists(asset, won):
    """Called after every sell. Adds to blacklist or whitelist based on recent performance."""
    lists = load_token_lists()
    now   = datetime.now()
    # Read only the columns we need by position — robust to variable column counts
    # (old rows: 26 cols, new rows: 27 cols with fear_greed)
    r_wins, r_losses = 0, 0
    try:
        cutoff = now - timedelta(hours=BLACKLIST_WINDOW_HOURS)
        with open("trades.csv") as fh:
            for line in fh:
                parts = line.strip().split(",")
                if len(parts) < 17:
                    continue
                if parts[3] != asset or parts[4] != "sell":
                    continue
                try:
                    ts = pd.to_datetime(parts[0], errors="coerce")
                    if pd.isna(ts) or ts < cutoff:
                        continue
                except Exception:
                    continue
                ind = parts[16]
                if ind == "p":
                    r_wins += 1
                elif ind == "l":
                    r_losses += 1
    except Exception as e:
        status_queue.put(f"token_lists read error: {e}")

    if r_losses >= BLACKLIST_LOSSES_TRIGGER:
        expiry = (now + timedelta(hours=BLACKLIST_DURATION_HOURS)).isoformat()
        lists["blacklist"][asset] = expiry
        lists["whitelist"].pop(asset, None)
        status_queue.put(
            f"⛔ {asset} blacklisted {BLACKLIST_DURATION_HOURS}h "
            f"({r_losses} losses in last {BLACKLIST_WINDOW_HOURS}h)"
        )
    elif r_wins >= WHITELIST_WINS_TRIGGER:
        expiry = (now + timedelta(hours=WHITELIST_DURATION_HOURS)).isoformat()
        lists["whitelist"][asset] = expiry
        lists["blacklist"].pop(asset, None)
        status_queue.put(
            f"⭐ {asset} whitelisted {WHITELIST_DURATION_HOURS}h "
            f"({r_wins} wins in last {BLACKLIST_WINDOW_HOURS}h)"
        )
    save_token_lists(lists)

def score_candidate(symbol, ma_fast, ma_slow, min_split_pct):
    """
    Quick scoring of a candidate for mid-trade reassessment.
    Returns a float score (higher = better opportunity) or None if disqualified.
    Score = split_pct * price_confidence / risk  — favours wide, confident, moderate-risk setups.
    """
    try:
        df_c = getminutedata(symbol, "1m", "30", ma_fast, ma_slow)
        if df_c.empty:
            return None
    except Exception:
        return None

    if not (((df_c.Close.pct_change() + 1).cumprod()).iloc[-1] > 1):
        return None
    if not (df_c['SMA_fast'].iloc[-1] > df_c['SMA_slow'].iloc[-1]):
        return None

    close_now   = df_c['Close'].iloc[-1]
    split_now   = df_c['SMA_fast'].iloc[-1] - df_c['SMA_slow'].iloc[-1]
    norm_diff   = (split_now / close_now) * 100 if close_now > 0 else 0
    if norm_diff < min_split_pct:
        return None

    split_prev = df_c['SMA_fast'].iloc[-6] - df_c['SMA_slow'].iloc[-6] if len(df_c) >= 6 else split_now
    if split_now <= split_prev:   # narrowing
        return None

    closes = df_c['Close'].iloc[-4:].values
    rising = sum(closes[i] > closes[i-1] for i in range(1, len(closes)))
    if rising == 0:
        return None
    confidence = rising / (len(closes) - 1)

    vol_mean = df_c['Volume'].mean()
    vol_std  = df_c['Volume'].std()
    vol_risk = max(0, (df_c['Volume'].iloc[-1] - vol_mean) / vol_std) * 2 if vol_std > 0 else 0
    risk     = min(10, (abs(norm_diff) * 10 + vol_risk) / 2)
    if risk < 2 or (6 <= risk < 8):
        return None

    return (norm_diff * confidence) / max(risk, 0.1)

def get_fear_greed():
    """Fetch current Fear & Greed index (0–100) from alternative.me. Returns (value, label)."""
    try:
        req = urllib.request.Request("https://api.alternative.me/fng/?limit=1")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = _json.loads(r.read())
        entry = data["data"][0]
        return int(entry["value"]), entry["value_classification"]
    except Exception:
        return None, "unknown"

def log(dt, asset, action, trade, benchmark, qty, cost, pc, lc, consec_pc, consec_lc, ind, usdt_bal, adjm, call_count,
        pnl=0.0, split_pct=0.0, trend_dir="–", risk_score=0.0, ma_fast=9, ma_slow=21, hold_seconds=0,
        fee=0.0, fee_asset="", fear_greed=None):
    ts = pd.Timestamp(dt) if not isinstance(dt, pd.Timestamp) else dt
    new_row = pd.DataFrame.from_records(
        [{
            "datetime":     dt,
            "hour":         ts.hour,
            "day_of_week":  ts.day_name(),
            "asset":        asset,
            "action":       action,
            "session_mode": "live" if trade else "sim",
            "price":        benchmark,
            "quantity":     qty,
            "cost":         cost,
            "pnl":          round(pnl, 6),
            "fee":          round(fee, 8),
            "fee_asset":    fee_asset,
            "profit_count": pc,
            "loss_count":   lc,
            "consec_gain":  consec_pc,
            "consec_loss":  consec_lc,
            "indicator":    ind,
            "balance":      usdt_bal,
            "adjustment":   adjm,
            "calls":        call_count,
            "split_pct":    round(split_pct, 6),
            "trend_dir":    trend_dir,
            "risk_score":   round(risk_score, 4),
            "ma_fast":      ma_fast,
            "ma_slow":      ma_slow,
            "hold_seconds": hold_seconds,
            "fear_greed":   fear_greed,
        }], index=[0]
    )
    return new_row

def getminutedata(symbol, interval, lookback, ma_fast=None, ma_slow=None):
    global current_ma_fast, current_ma_slow
    if ma_fast is None:
        ma_fast = current_ma_fast
    if ma_slow is None:
        ma_slow = current_ma_slow
    utc_offset = time.localtime().tm_gmtoff
    klines = client.get_historical_klines(symbol, interval, lookback + "min ago UTC")

    if not klines:
        empty_df = pd.DataFrame(columns=["Time", "Open", "High", "Low", "Close", "Volume", "SMA_fast", "SMA_slow"])
        empty_df = empty_df.set_index("Time")
        return empty_df

    frame = pd.DataFrame(klines)
    frame = frame.iloc[:, :6]
    frame.columns = ["Time", "Open", "High", "Low", "Close", "Volume"]
    frame["SMA_fast"] = frame["Close"].rolling(window=ma_fast).mean()
    frame["SMA_slow"] = frame["Close"].rolling(window=ma_slow).mean()
    frame = frame.iloc[ma_slow:]
    frame = frame.set_index("Time")
    frame.index = pd.to_datetime(frame.index, unit="ms")
    frame.index = frame.index + pd.Timedelta(seconds=utc_offset)
    frame = frame.astype(float)
    return frame

VOL_MIN = 5_000_000

CANDIDATE_COUNT = 10

def btc_is_bullish():
    """Block only when BTC is in an active downtrend on 5m.
    Uses spread = (SMA9 - SMA21) / price as a %.
    Blocks only if spread is negative AND widening downward (trend accelerating down).
    A sideways or recovering BTC is allowed through.
    """
    try:
        klines = client.get_historical_klines("BTCUSDT", "5m", "200 min ago UTC")
        if not klines:
            return True
        closes = pd.Series([float(k[4]) for k in klines])
        sma9  = closes.rolling(9).mean()
        sma21 = closes.rolling(21).mean()
        price = closes.iloc[-1]
        spread_now  = (sma9.iloc[-1]  - sma21.iloc[-1])  / price * 100
        spread_prev = (sma9.iloc[-6]  - sma21.iloc[-6])  / price * 100
        downtrend_active = spread_now < 0 and spread_now < spread_prev
        if downtrend_active:
            status_queue.put(
                f"BTC 5m spread {spread_now:.3f}% (was {spread_prev:.3f}%) — downtrend active, skipping."
            )
            return False
        return True
    except Exception:
        return True

def top_symbols():
    all_pairs = pd.DataFrame(client.get_ticker())
    relev = all_pairs[all_pairs.symbol.str.contains("USDT")]
    non_lev = relev[~((relev.symbol.str.contains("UP")) | (relev.symbol.str.contains("DOWN")))]
    non_lev = non_lev.copy()
    non_lev['quoteVolume'] = non_lev['quoteVolume'].astype(float)
    non_lev['priceChangePercent'] = non_lev['priceChangePercent'].astype(float)
    non_lev = non_lev[non_lev['quoteVolume'] >= VOL_MIN]
    non_lev = non_lev[non_lev['symbol'].str.isascii()]
    ranked = non_lev.sort_values('priceChangePercent', ascending=False).head(CANDIDATE_COUNT)
    return list(zip(ranked['symbol'].values, ranked['priceChangePercent'].values))

def get_top_symbol():
    candidates = top_symbols()
    if not candidates:
        raise ValueError("No candidates found")
    return candidates[0][0], float(candidates[0][1])

def _parse_order(order: dict) -> dict:
    """Extract verified execution details from a Binance order response."""
    filled_qty  = float(order.get("executedQty", 0))
    total_cost  = float(order.get("cummulativeQuoteQty", 0))   # USDT spent/received
    avg_price   = total_cost / filled_qty if filled_qty > 0 else 0.0
    status      = order.get("status", "UNKNOWN")
    order_id    = order.get("orderId", "–")

    # Sum fees across all partial fills
    total_fee      = 0.0
    fee_asset      = ""
    for fill in order.get("fills", []):
        total_fee += float(fill.get("commission", 0))
        fee_asset  = fill.get("commissionAsset", "")

    return {
        "order_id":   order_id,
        "status":     status,
        "filled_qty": filled_qty,
        "avg_price":  avg_price,
        "total_cost": total_cost,
        "fee":        total_fee,
        "fee_asset":  fee_asset,
    }

def buy(asset, qty, is_trade):
    """Place a market buy. Returns a verification dict (or None for sim)."""
    if is_trade:
        if qty <= 0:
            status_queue.put(f"BUY skipped: qty is 0 for {asset}")
            return None
        try:
            order  = client.create_order(symbol=asset, side="BUY", type="MARKET", quantity=qty)
            verify = _parse_order(order)
            bal_after = get_usdt()
            status_queue.put(
                f"✔ BUY {asset} | "
                f"filled {verify['filled_qty']} @ avg ${verify['avg_price']:.6f} | "
                f"cost ${verify['total_cost']:.4f} | "
                f"fee {verify['fee']:.6f} {verify['fee_asset']} | "
                f"status {verify['status']} | "
                f"bal after ${bal_after:.4f}"
            )
            return verify
        except Exception as e:
            status_queue.put(f"BUY order failed: {e}")
            return None
    else:
        status_queue.put(f"Simulated BUY: {asset} qty={qty}")
        return None

def sell(asset, qty, is_trade):
    """Place a market sell. Returns a verification dict (or None for sim)."""
    if is_trade:
        try:
            order  = client.create_order(symbol=asset, side="SELL", type="MARKET", quantity=qty)
            verify = _parse_order(order)
            bal_after = get_usdt()
            status_queue.put(
                f"✔ SELL {asset} | "
                f"filled {verify['filled_qty']} @ avg ${verify['avg_price']:.6f} | "
                f"received ${verify['total_cost']:.4f} | "
                f"fee {verify['fee']:.6f} {verify['fee_asset']} | "
                f"status {verify['status']} | "
                f"bal after ${bal_after:.4f}"
            )
            return verify
        except Exception as e:
            status_queue.put(f"SELL order failed: {e}")
            return None
    else:
        status_queue.put(f"Simulated SELL: {asset} qty={qty}")
        return None

def strategy(SL=None, Target=None, percent_var=None, risk_var=None, in_trade_var=None, force_sim=False):
    global lc, pc, adjm, trade, last_asset, call_count, usdt_bal, consec_lc, consec_pc, df_log, total_pnl, total_adjm
    global currently_analyzing_asset, analysis_count, current_sl, current_target, current_ma_fast, current_ma_slow
    if SL is None:
        SL = current_sl
    if Target is None:
        Target = current_target

    # Minimum MA split as a % of price that must be present to justify the trade.
    # Requires the gap to cover at least 20% of the target gain, ensuring enough
    # momentum headroom to reach TP before the trend fades.
    MIN_SPLIT_PCT = (Target - 1) * 100 * 0.20

    # ── Evidence-based filters (from analytics report) ────────────────────────
    # MA is controlled by the AI engine (default 9/21, best performer historically).
    # Do not override here — let the AI cycle through PARAM_GRID entries.

    # Fetch Fear & Greed once per strategy call — logged with every trade.
    fg_value, fg_label = get_fear_greed()
    if fg_value is not None:
        status_queue.put(f"Fear & Greed: {fg_value} ({fg_label})")

    # 2. BTC macro filter: only enter when BTC 30-min trend is bullish.
    #    Altcoins follow BTC — entering longs during a BTC downtrend is the
    #    primary driver of losses.
    if not btc_is_bullish():
        status_queue.put("BTC bearish (SMA9 < SMA21 on 30m) — skipping scan.")
        time.sleep(60)
        return

    # Trading window: unrestricted — need 200+ trades per hour before locking a window.
    # ─────────────────────────────────────────────────────────────────────────

    analysis_count += 1

    try:
        candidates = top_symbols()
    except Exception as e:
        status_queue.put(f"Error fetching candidates: {e}")
        time.sleep(20)
        return

    asset, percentage, df, total_risk, qty = None, 0, None, 0, 0
    MIN_NOTIONAL = 10.0
    norm_diff, trend_label, price_confidence = 0.0, "widening", 0.0

    token_lists = load_token_lists()
    blacklist   = token_lists.get("blacklist", {})
    whitelist   = token_lists.get("whitelist", {})

    for candidate, pct in candidates:
        if candidate == last_asset:
            status_queue.put(f"{candidate} skipped — previous loss.")
            continue
        if candidate in blacklist:
            status_queue.put(f"{candidate} skipped — blacklisted.")
            continue
        try:
            df_c = getminutedata(candidate, "1m", "30", current_ma_fast, current_ma_slow)
            if df_c.empty:
                time.sleep(2)
                continue
        except Exception:
            time.sleep(2)
            continue
        time.sleep(2)

        momentum_condition = ((df_c.Close.pct_change() + 1).cumprod()).iloc[-1] > 1
        ma_condition = df_c['SMA_fast'].iloc[-1] > df_c['SMA_slow'].iloc[-1]

        if not momentum_condition or not ma_condition:
            reason = "MA cross negative" if not ma_condition else "momentum insufficient"
            status_queue.put(f"{candidate} skipped — {reason}.")
            continue

        # ── MA split quality checks ──────────────────────────────────────────
        close_now    = df_c['Close'].iloc[-1]
        split_now    = df_c['SMA_fast'].iloc[-1] - df_c['SMA_slow'].iloc[-1]
        norm_diff    = (split_now / close_now) * 100 if close_now > 0 else 0

        # 1. Split magnitude: is there enough spread to cover the target ROI?
        if norm_diff < MIN_SPLIT_PCT:
            status_queue.put(
                f"{candidate} skipped — split {norm_diff:.3f}% below min {MIN_SPLIT_PCT:.3f}%."
            )
            continue

        # 2. Trend acceleration: compare current split to 5 candles ago.
        split_prev     = df_c['SMA_fast'].iloc[-6] - df_c['SMA_slow'].iloc[-6] if len(df_c) >= 6 else split_now
        trend_widening = split_now > split_prev
        trend_label    = "widening" if trend_widening else "narrowing"

        # HARD FILTER: block narrowing-trend entries — 0% WR across 82 trades.
        if not trend_widening:
            status_queue.put(f"{candidate} skipped — trend narrowing (0% WR filter).")
            continue

        # Compute risk score before the risk-band filter
        ma_diff_risk = abs(norm_diff) * 10
        vol_mean = df_c['Volume'].mean()
        vol_std  = df_c['Volume'].std()
        volume_risk = 0
        if vol_std > 0:
            volume_z_score = (df_c['Volume'].iloc[-1] - vol_mean) / vol_std
            volume_risk = max(0, volume_z_score) * 2
        total_risk = min(10, (ma_diff_risk + volume_risk) / 2)

        # HARD FILTER: block risk bands 0–2 (11% WR, -$17 total) and 6–8 (0% WR).
        if total_risk < 2 or (6 <= total_risk < 8):
            status_queue.put(f"{candidate} skipped — risk {total_risk:.1f} in dead zone (0–2 or 6–8).")
            continue

        # ── Price trajectory confidence ──────────────────────────────────────
        # Count how many of the last 3 candles closed higher than the previous.
        # 0/2 = falling, 1/2 = mixed, 2/2 = rising. Block on 0/2 (both falling).
        closes = df_c['Close'].iloc[-4:].values   # 4 closes → 3 consecutive pairs
        rising_steps = sum(closes[i] > closes[i-1] for i in range(1, len(closes)))
        price_confidence = rising_steps / (len(closes) - 1)   # 0.0 – 1.0

        if rising_steps == 0:
            status_queue.put(
                f"{candidate} skipped — price falling all 3 candles (conf=0%)."
            )
            continue
        # ─────────────────────────────────────────────────────────────────────

        status_queue.put(
            f"{candidate} MA({current_ma_fast}/{current_ma_slow}) "
            f"split={norm_diff:.3f}% trend={trend_label} risk={total_risk:.1f} "
            f"conf={price_confidence:.0%}"
        )
        # ─────────────────────────────────────────────────────────────────────

        # Passed all checks — use this candidate
        asset, percentage, df = candidate, float(pct), df_c

        buy_proportion = 0.5 - total_risk * 0.045
        buy_amt = max(MIN_NOTIONAL, int(buy_proportion * usdt_bal))
        qty = round(buy_amt / df.Close.iloc[-1]) if df.Close.iloc[-1] > 0 else 0
        break

    if asset is None and whitelist:
        status_queue.put(f"No candidate in top scan — trying {len(whitelist)} whitelisted token(s).")
        for wl_asset in list(whitelist.keys()):
            try:
                df_c = getminutedata(wl_asset, "1m", "30", current_ma_fast, current_ma_slow)
                if df_c.empty:
                    continue
            except Exception:
                continue
            time.sleep(2)
            momentum_condition = ((df_c.Close.pct_change() + 1).cumprod()).iloc[-1] > 1
            ma_condition = df_c['SMA_fast'].iloc[-1] > df_c['SMA_slow'].iloc[-1]
            if not momentum_condition or not ma_condition:
                continue
            close_now  = df_c['Close'].iloc[-1]
            split_now  = df_c['SMA_fast'].iloc[-1] - df_c['SMA_slow'].iloc[-1]
            norm_diff  = (split_now / close_now) * 100 if close_now > 0 else 0
            if norm_diff < MIN_SPLIT_PCT:
                continue
            split_prev     = df_c['SMA_fast'].iloc[-6] - df_c['SMA_slow'].iloc[-6] if len(df_c) >= 6 else split_now
            trend_widening = split_now > split_prev
            if not trend_widening:
                continue
            trend_label = "widening"
            wl_closes       = df_c['Close'].iloc[-4:].values
            rising_steps    = sum(wl_closes[i] > wl_closes[i-1] for i in range(1, len(wl_closes)))
            price_confidence = rising_steps / (len(wl_closes) - 1)
            if rising_steps == 0:
                continue
            ma_diff_risk = abs(norm_diff) * 10
            vol_mean = df_c['Volume'].mean()
            vol_std  = df_c['Volume'].std()
            wl_vol_risk = 0
            if vol_std > 0:
                wl_vol_risk = max(0, (df_c['Volume'].iloc[-1] - vol_mean) / vol_std) * 2
            total_risk = min(10, (ma_diff_risk + wl_vol_risk) / 2)
            if total_risk < 2 or (6 <= total_risk < 8):
                continue
            status_queue.put(
                f"⭐ Whitelist: {wl_asset} split={norm_diff:.3f}% risk={total_risk:.1f} conf={price_confidence:.0%}"
            )
            asset, percentage, df = wl_asset, 0.0, df_c
            buy_proportion = 0.5 - total_risk * 0.045
            buy_amt = max(MIN_NOTIONAL, int(buy_proportion * usdt_bal))
            qty = round(buy_amt / df.Close.iloc[-1]) if df.Close.iloc[-1] > 0 else 0
            break

    if asset is None:
        status_queue.put("No valid candidate found — waiting 60s before next scan.")
        time.sleep(60)
        return

    currently_analyzing_asset = asset
    if percent_var:
        percent_var.set(f"Change: {percentage:.2f}%")
    if risk_var:
        risk_var.set(f"Risk: {total_risk:.2f}/10")
    status_queue.put(f"Selected: {asset} ({percentage:.2f}%)")

    trade = pc >= lc
    if pc >= lc and consec_lc > 3: trade = False
    if lc > pc and consec_lc > 5: trade = True
    if adjm > lc: trade = True
    if force_sim: trade = False

    buyprice = df.Close.iloc[-1]
    benchmark = buyprice

    buy_verify = buy(asset, qty, trade)

    # Use verified fill price from Binance if available, else fall back to last candle
    if buy_verify and buy_verify["filled_qty"] > 0:
        buyprice   = buy_verify["avg_price"]
        qty        = buy_verify["filled_qty"]
        buy_fee    = buy_verify["fee"]
        buy_fee_asset = buy_verify["fee_asset"]
        # Warn if Binance fill qty differs from requested qty
        if abs(qty - buy_verify["filled_qty"]) > 0:
            status_queue.put(f"⚠ Fill qty mismatch: requested {qty}, filled {buy_verify['filled_qty']}")
    else:
        buy_fee = qty * buyprice * BNB_FEE_RATE   # 0.075% sim fee (BNB discount, expressed as USDT cost)
        buy_fee_asset = "USDT"

    global in_trade, current_buyprice, current_qty
    in_trade = True
    current_buyprice = buyprice
    current_qty = qty

    dt = df.index[-1]
    buy_time = time.time()
    cost = qty * buyprice
    if in_trade_var:
        in_trade_var.set(f"Purchase Price: {buyprice:.6f}\nQty: {qty}\nCost: ${cost:.4f}\nFee: {buy_fee:.6f} {buy_fee_asset}")

    new_row = log(dt, asset, "buy", trade, benchmark, qty, cost, pc, lc, consec_pc, consec_lc, "b", usdt_bal, adjm, call_count,
                  pnl=0.0, split_pct=norm_diff, trend_dir=trend_label, risk_score=total_risk,
                  ma_fast=current_ma_fast, ma_slow=current_ma_slow, hold_seconds=0,
                  fee=buy_fee, fee_asset=buy_fee_asset, fear_greed=fg_value)
    df_log = pd.concat([df_log, new_row])
    telegram_notify(
        f"🔵 <b>BUY [{'LIVE' if trade else 'SIM'}]</b> {asset}\n"
        f"Price: <code>${buyprice:.6f}</code>  Qty: {qty}\n"
        f"Cost: ${cost:.4f}  Fee: {buy_fee:.6f} {buy_fee_asset}\n"
        f"Split: {norm_diff:.3f}%  Risk: {total_risk:.1f}/10\n"
        f"W/L: {pc}/{lc}  Bal: ${usdt_bal:.4f}"
    )

    open_position = True
    call_count = 0
    while open_position:
        if not session_active:
            open_position = False
            break
        try:
            df = getminutedata(asset, "1m", "60")
            if df.empty:
                time.sleep(20)
                continue
        except Exception as e:
            status_queue.put(f"Error during trade loop: {e}")
            time.sleep(20)
            continue

        call_count += 1
        current_price = df.Close.iloc[-1]

        if current_price > buyprice * Target:
            adjm += 1
            total_adjm += 1
            buyprice = current_price
            current_buyprice = current_price
            status_queue.put("!TAKE_PROFIT!")
            if adjm == 1:
                # Ensure SL covers round-trip fees from original entry.
                # SL must yield at least: benchmark * (1 + buy_fee + sell_fee rate)
                fee_cover_sl = benchmark * (1 + 2 * BNB_FEE_RATE) / buyprice
                SL = max(0.9915, fee_cover_sl)
            else:
                SL = 0.99

        df_current = df[["Close", "SMA_fast", "SMA_slow", "Volume"]]
        df_current.to_sql("coin", engine2, if_exists="replace", index=False)

        dfa = pd.DataFrame([buyprice * SL, buyprice * Target, buyprice, asset, benchmark])
        dfa.to_sql("coin", engine, if_exists="replace", index=False)

        sl_price = buyprice * SL
        sl_band = buyprice - sl_price
        distance_to_sl = current_price - sl_price
        global poll_sleep
        poll_sleep = max(2, min(30, 2 + (distance_to_sl / sl_band) * 28)) if sl_band > 0 else 10

        # ── Mid-trade reassessment ────────────────────────────────────────────
        # After STALL_CHECK_INTERVAL minutes, if price is sawing around the entry
        # without progress, scan for a better opportunity and switch if found.
        hold_elapsed = time.time() - buy_time
        price_range_pct = abs(current_price - benchmark) / benchmark * 100
        is_stalling = (
            hold_elapsed >= STALL_CHECK_INTERVAL * 60
            and price_range_pct < STALL_RANGE_PCT
            and adjm == 0          # no trailing TP hit yet — still at original benchmark
        )
        if is_stalling:
            status_queue.put(
                f"⏳ {asset} stalling {hold_elapsed/60:.0f}m "
                f"(price {price_range_pct:.3f}% from entry) — scanning alternatives."
            )
            current_score = (norm_diff * price_confidence) / max(total_risk, 0.1)
            try:
                alt_candidates = top_symbols()
            except Exception:
                alt_candidates = []
            best_alt, best_alt_score = None, current_score * 1.5  # must be 50% better
            for alt, _ in alt_candidates:
                if alt == asset:
                    continue
                sc = score_candidate(alt, current_ma_fast, current_ma_slow, MIN_SPLIT_PCT)
                time.sleep(1)
                if sc is not None and sc > best_alt_score:
                    best_alt_score = sc
                    best_alt = alt
            if best_alt:
                # Only exit early if current price covers round-trip fees
                min_exit_price = benchmark * (1 + 2 * BNB_FEE_RATE)
                if current_price >= min_exit_price:
                    status_queue.put(
                        f"🔀 Switching {asset}→{best_alt} "
                        f"(score {current_score:.3f}→{best_alt_score:.3f})"
                    )
                    # Force exit via SL path by setting SL just above current price
                    SL = (current_price / buyprice) * 0.9999
                else:
                    status_queue.put(
                        f"⏳ Better alt {best_alt} found but fee not yet covered "
                        f"(need ${min_exit_price:.6f}, have ${current_price:.6f}) — holding."
                    )
        # ─────────────────────────────────────────────────────────────────────

        time.sleep(poll_sleep)

        if current_price <= buyprice * SL or call_count > 3999:
            sell_verify = sell(asset, qty, trade)
            if in_trade_var:
                in_trade_var.set("Not in trade")

            # Use verified fill price from Binance if available
            if sell_verify and sell_verify["filled_qty"] > 0:
                sellprice  = sell_verify["avg_price"]
                sell_fee   = sell_verify["fee"]
                sell_fee_asset = sell_verify["fee_asset"]
            else:
                sellprice      = current_price
                sell_fee       = sellprice * qty * BNB_FEE_RATE   # 0.075% sim fee
                sell_fee_asset = "USDT"

            total_fees = buy_fee + sell_fee
            # PnL net of both buy and sell fees (fees in quote asset already deducted by Binance,
            # but if fees are in BNB we add them as an approximate cost note)
            pnl = (sellprice - benchmark) * qty
            if buy_fee_asset == "USDT":
                pnl -= buy_fee
            if sell_fee_asset == "USDT":
                pnl -= sell_fee

            total_pnl += pnl
            hold_secs = int(time.time() - buy_time)
            if sellprice < benchmark:
                msg = (
                    f"✘ LOSS {asset} | sell ${sellprice:.6f} vs buy ${benchmark:.6f} | "
                    f"PnL ${pnl:.4f} | fees {total_fees:.6f} {sell_fee_asset or buy_fee_asset}"
                )
                status_queue.put(msg)
                telegram_notify(
                    f"🔴 <b>LOSS [{'LIVE' if trade else 'SIM'}]</b> {asset}\n"
                    f"Sell: <code>${sellprice:.6f}</code>  Buy: <code>${benchmark:.6f}</code>\n"
                    f"PnL: ${pnl:.4f}  Fees: {total_fees:.6f} {sell_fee_asset or buy_fee_asset}\n"
                    f"Hold: {hold_secs//60}m {hold_secs%60}s  W/L: {pc}/{lc+1}  Bal: ${usdt_bal:.4f}"
                )
                lc += 1
                consec_lc += 1
                consec_pc = 0
                ind = "l"
            else:
                msg = (
                    f"✔ PROFIT {asset} | sell ${sellprice:.6f} vs buy ${benchmark:.6f} | "
                    f"PnL ${pnl:.4f} | fees {total_fees:.6f} {sell_fee_asset or buy_fee_asset}"
                )
                status_queue.put(msg)
                telegram_notify(
                    f"🟢 <b>PROFIT [{'LIVE' if trade else 'SIM'}]</b> {asset}\n"
                    f"Sell: <code>${sellprice:.6f}</code>  Buy: <code>${benchmark:.6f}</code>\n"
                    f"PnL: +${pnl:.4f}  Fees: {total_fees:.6f} {sell_fee_asset or buy_fee_asset}\n"
                    f"Hold: {hold_secs//60}m {hold_secs%60}s  W/L: {pc+1}/{lc}  Bal: ${usdt_bal:.4f}"
                )
                pc += 1
                consec_pc += 1
                consec_lc = 0
                ind = "p"

            usdt_bal = get_usdt()
            dt = df.index[-1]
            new_row = log(dt, asset, "sell", trade, sellprice, qty, cost, pc, lc, consec_pc, consec_lc, ind, usdt_bal, adjm, call_count,
                          pnl=pnl, split_pct=norm_diff, trend_dir=trend_label, risk_score=total_risk,
                          ma_fast=current_ma_fast, ma_slow=current_ma_slow, hold_seconds=hold_secs,
                          fee=sell_fee, fee_asset=sell_fee_asset, fear_greed=fg_value)
            df_log = pd.concat([df_log, new_row])
            df_log.to_csv("trades.csv", mode="a", index=False, header=False)
            df_log.to_sql("log", engine3, if_exists="append", index=False)

            try:
                analytics.generate_report()
            except Exception:
                pass

            update_token_lists(asset, ind == "p")

            in_trade = False
            current_buyprice = 0.0
            current_qty = 0

            adjm = 0
            last_asset = asset if ind == "l" else ""
            open_position = False

def get_usdt():
    try:
        bal = client.get_asset_balance(asset="USDT")
        return float(bal["free"])
    except Exception:
        status_queue.put("Error getting USDT balance.")
        return 0

def manual_buy():
    global in_trade, current_buyprice, current_qty, currently_analyzing_asset, df_log, pc, lc, consec_pc, consec_lc
    if in_trade:
        return
    asset = currently_analyzing_asset
    if not asset:
        return
    df = getminutedata(asset, "1m", "30")
    if df.empty:
        return
    buyprice = df.Close.iloc[-1]
    bal = get_usdt()
    buy_amt = 0.5 * bal
    qty = round(buy_amt / buyprice) if buyprice > 0 else 0
    if qty == 0:
        return
    buy(asset, qty, True)
    in_trade = True
    current_buyprice = buyprice
    current_qty = qty
    dt = df.index[-1]
    cost = qty * buyprice
    new_row = log(dt, asset, "buy", True, buyprice, qty, cost, pc, lc, consec_pc, consec_lc, "b", bal, 0, 0)
    df_log = pd.concat([df_log, new_row])
    if in_trade_var:
        in_trade_var.set(f"Price: {buyprice:.4f}\nQty: {qty}\nCost: ${cost:.2f}")

def manual_sell():
    global in_trade, current_buyprice, current_qty, total_pnl, currently_analyzing_asset, df_log, pc, lc, consec_pc, consec_lc
    if not in_trade:
        return
    asset = currently_analyzing_asset
    df = getminutedata(asset, "1m", "30")
    if df.empty:
        return
    sellprice = df.Close.iloc[-1]
    pnl = (sellprice - current_buyprice) * current_qty
    total_pnl += pnl
    sell(asset, current_qty, True)
    dt = df.index[-1]
    bal = get_usdt()
    new_row = log(dt, asset, "sell", True, sellprice, current_qty, current_buyprice * current_qty, pc, lc, consec_pc, consec_lc, "p" if pnl > 0 else "l", bal, 0, 0,
                  pnl=pnl)
    df_log = pd.concat([df_log, new_row])
    df_log.to_csv("trades.csv", mode="a", index=False, header=False)
    df_log.to_sql("log", engine3, if_exists="append", index=False)
    try:
        analytics.generate_report()
    except Exception:
        pass
    in_trade = False
    current_buyprice = 0.0
    current_qty = 0
    if in_trade_var:
        in_trade_var.set("Not in trade")

def start_session():
    global session_active, session_start_time, currently_analyzing_asset, initial_balance
    if session_active:
        return
    session_active = True
    session_start_time = time.time()
    initial_balance = get_usdt()
    try:
        asset, percentage = get_top_symbol()
        currently_analyzing_asset = asset
        status_queue.put(f"Session started with {asset}")
    except Exception as e:
        status_queue.put(f"Error starting session: {e}")

def stop_session():
    global session_active, in_trade, current_buyprice, current_qty, total_pnl, currently_analyzing_asset, df_log, pc, lc, consec_pc, consec_lc
    if not session_active:
        return
    session_active = False
    if in_trade:
        if trade:
            asset = currently_analyzing_asset
            df = getminutedata(asset, "1m", "30")
            if not df.empty:
                sellprice = df.Close.iloc[-1]
                pnl = (sellprice - current_buyprice) * current_qty
                total_pnl += pnl
                sell(asset, current_qty, True)
                dt = df.index[-1]
                new_row = log(dt, asset, "sell", True, sellprice, current_qty, current_buyprice * current_qty, pc, lc, consec_pc, consec_lc, "p" if pnl > 0 else "l", initial_balance + total_pnl, 0, 0)
                df_log = pd.concat([df_log, new_row])
                df_log.to_csv("trades.csv", mode="a", index=False, header=False)
                df_log.to_sql("log", engine3, if_exists="append", index=False)
                try:
                    analytics.generate_report()
                except Exception:
                    pass
                status_queue.put(f"Closed live position: {asset} | PnL: ${pnl:.2f}")
        in_trade = False
        current_buyprice = 0.0
        current_qty = 0
    status_queue.put("Session ended")

def trading_loop(args):
    (asset_var, percent_var, count_var, status_text, roi_var, risk_var, adj_var, ma_diff_var, ma_diff_label,
     ratio_var, pnl_var, equity_var, profit_icon_label, in_trade_var) = args
    global usdt_bal
    while True:
        if not session_active:
            time.sleep(1)
            continue
        usdt_bal = get_usdt()
        force_sim = usdt_bal < 5 or force_sim_override
        if usdt_bal < 5:
            status_queue.put("Insufficient balance — running in simulation mode.")
        elif force_sim_override:
            status_queue.put("AI override — simulation mode active.")

        strategy(percent_var=percent_var, risk_var=risk_var, in_trade_var=in_trade_var, force_sim=force_sim)
        time.sleep(5)

def apply_theme(root, style, dark_mode):
    global lighter_bg_color
    if dark_mode:
        bg_color_str = '#2E2E2E'
        fg_color_str = 'white'
        lighter_bg_color_str = '#3C3C3C'
        accent_color = '#007BFF'
        button_bg = '#4A4A4A'
        button_fg = 'white'
    else:
        bg_color_str = '#F8F9FA'
        fg_color_str = '#212529'
        lighter_bg_color_str = '#FFFFFF'
        accent_color = '#007BFF'
        button_bg = '#E9ECEF'
        button_fg = '#495057'
    
    root.configure(bg=bg_color_str)
    style.configure('.', font=('Helvetica', 10), background=bg_color_str, foreground=fg_color_str)
    style.configure('TFrame', background=bg_color_str)
    style.configure('TLabel', background=bg_color_str, foreground=fg_color_str)
    style.configure('TLabelframe', background=bg_color_str, foreground=fg_color_str)
    style.configure('TLabelframe.Label', background=bg_color_str, foreground=fg_color_str, font=('Helvetica', 11, 'bold'))
    style.configure('TButton', background=button_bg, foreground=button_fg, borderwidth=1, relief='flat', padding=5)
    style.map('TButton', background=[('active', accent_color), ('pressed', '#0056B3')])
    
    rgb_lighter = root.winfo_rgb(lighter_bg_color_str)
    lighter_bg_color = (rgb_lighter[0]/65535, rgb_lighter[1]/65535, rgb_lighter[2]/65535)
    
    return bg_color_str, fg_color_str, lighter_bg_color_str

def toggle_dark_mode(root, style, profit_icon_label, status_text):
    global dark_mode, fig, ax
    dark_mode = not dark_mode
    bg_color_str, fg_color_str, lighter_bg_color_str = apply_theme(root, style, dark_mode)
    
    # Update profit icon
    profit_icon_fg = '#28A745' if not dark_mode else 'green'
    profit_icon_label.config(bg=bg_color_str, fg=profit_icon_fg)
    
    # Update status text
    status_text.config(bg=lighter_bg_color_str, fg=fg_color_str)
    
    # Update chart
    if fig and ax:
        rgb_lighter = root.winfo_rgb(lighter_bg_color_str)
        lighter_bg_color = (rgb_lighter[0]/65535, rgb_lighter[1]/65535, rgb_lighter[2]/65535)
        fig.patch.set_facecolor(lighter_bg_color)
        ax.set_facecolor(lighter_bg_color)
        if dark_mode:
            plt.style.use('dark_background')
        else:
            plt.style.use('Solarize_Light2')
        fig.canvas.draw()

def drain_ai_queue(root):
    """Continuously drain ai_log_queue into ai_log_buffer (runs even when popup is closed)."""
    while not ai_log_queue.empty():
        msg = ai_log_queue.get_nowait()
        ai_log_buffer.append(msg)
        if len(ai_log_buffer) > 2000:
            ai_log_buffer.pop(0)
    root.after(500, lambda: drain_ai_queue(root))


def open_ai_log_popup(root, bg_color_str, fg_color_str, lighter_bg_color_str):
    """Open (or reopen) the AI activity log popup window."""
    popup = tk.Toplevel(root)
    popup.title("AI Activity Log")
    popup.geometry("700x460")
    popup.configure(bg=bg_color_str)

    frame = ttk.Frame(popup, padding="8")
    frame.pack(fill=tk.BOTH, expand=True)

    scrollbar = ttk.Scrollbar(frame)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    log_text = tk.Text(
        frame, state="disabled", wrap="word", font=("Courier", 9),
        bg=lighter_bg_color_str, fg=fg_color_str,
        yscrollcommand=scrollbar.set,
    )
    log_text.pack(fill=tk.BOTH, expand=True)
    scrollbar.config(command=log_text.yview)

    # Seed with buffered history
    shown_index = [len(ai_log_buffer)]
    log_text.config(state="normal")
    for msg in ai_log_buffer:
        log_text.insert(tk.END, msg + "\n")
    log_text.config(state="disabled")
    log_text.see(tk.END)

    def refresh():
        new_msgs = ai_log_buffer[shown_index[0]:]
        if new_msgs:
            log_text.config(state="normal")
            for msg in new_msgs:
                log_text.insert(tk.END, msg + "\n")
            shown_index[0] = len(ai_log_buffer)
            log_text.config(state="disabled")
            log_text.see(tk.END)
        popup.after(500, refresh)

    refresh()


def create_main_window():
    global style
    root = tk.Tk()
    root.title("TradeDesk")
    root.geometry("1200x600")

    style = ttk.Style(root)
    style.theme_use('clam')
    
    bg_color_str, fg_color_str, lighter_bg_color_str = apply_theme(root, style, dark_mode)

    rgb = root.winfo_rgb(bg_color_str)
    bg_color_normalized = (rgb[0]/65535, rgb[1]/65535, rgb[2]/65535)
    
    rgb_lighter = root.winfo_rgb(lighter_bg_color_str)
    lighter_bg_color = (rgb_lighter[0]/65535, rgb_lighter[1]/65535, rgb_lighter[2]/65535)

    main_frame = ttk.Frame(root, padding="10")
    main_frame.pack(fill=tk.BOTH, expand=True)

    graph_frame = ttk.Frame(main_frame, width=800)
    graph_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

    # --- Profit Icon ---
    profit_icon_fg = '#28A745' if not dark_mode else 'green'
    global profit_icon_label
    profit_icon_label = tk.Label(graph_frame, text="$", font=("Helvetica", 8), fg=profit_icon_fg, bg=bg_color_str)
    # We don't pack or place it yet, it will be placed on demand

    session_frame = ttk.Frame(main_frame, width=400)
    session_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))
    session_frame.pack_propagate(False)
    
    top_panel = ttk.LabelFrame(session_frame, text="Asset Info", padding="10")
    top_panel.pack(fill=tk.X, pady=5)
    
    asset_var = tk.StringVar(value="Asset: N/A")
    percent_var = tk.StringVar(value="Change: N/A")
    count_var = tk.StringVar(value="Count: 0")
    throttle_var = tk.StringVar(value="Poll: --s")
    ttk.Label(top_panel, textvariable=asset_var).pack(anchor="w")
    ttk.Label(top_panel, textvariable=percent_var).pack(anchor="w")
    ttk.Label(top_panel, textvariable=throttle_var).pack(anchor="w")
    ttk.Label(top_panel, textvariable=count_var).pack(anchor="w")

    # Session buttons and counter
    global session_counter_var
    session_counter_var = tk.StringVar(value="Session Time: 00:00:00")
    ttk.Label(top_panel, textvariable=session_counter_var).pack(anchor="w")
    button_frame = ttk.Frame(top_panel)
    button_frame.pack(anchor="w", pady=5)
    ttk.Button(button_frame, text="start", command=start_session, width=4).pack(side=tk.LEFT)
    ttk.Button(button_frame, text="stop", command=stop_session, width=4).pack(side=tk.LEFT)
    ttk.Button(
        button_frame, text="AI Logs", width=6,
        command=lambda: open_ai_log_popup(root, bg_color_str, fg_color_str, lighter_bg_color_str),
    ).pack(side=tk.LEFT, padx=(6, 0))


    mid_panel = ttk.LabelFrame(session_frame, text="Trade Stats", padding="10")
    mid_panel.pack(fill=tk.X, pady=5)

    roi_var = tk.StringVar(value="Wins/Losses: 0/0")
    risk_var = tk.StringVar(value="Risk: N/A")
    adj_var = tk.StringVar(value="Adjustments: 0")
    ma_diff_var = tk.StringVar(value="MA Diff: N/A")
    ratio_var = tk.StringVar(value="P/L Ratio: N/A")
    
    ttk.Label(mid_panel, textvariable=roi_var).pack(anchor="w")
    ttk.Label(mid_panel, textvariable=risk_var).pack(anchor="w")
    ttk.Label(mid_panel, textvariable=adj_var).pack(anchor="w")
    ma_diff_label = ttk.Label(mid_panel, textvariable=ma_diff_var)
    ma_diff_label.pack(anchor="w")
    ttk.Label(mid_panel, textvariable=ratio_var).pack(anchor="w")

    in_trade_frame = ttk.LabelFrame(session_frame, text="In Trade", padding="5")
    in_trade_frame.pack(fill=tk.X, pady=5)
    current_price_label = tk.Label(in_trade_frame, text="Current Price: --", fg="white", bg="#2E2E2E", font=("Helvetica", 10, "bold"))
    current_price_label.pack(anchor="w")
    in_trade_var = tk.StringVar(value="Not in trade")
    ttk.Label(in_trade_frame, textvariable=in_trade_var).pack(anchor="w")
    trade_mode_label = tk.Label(in_trade_frame, text="[sim]", fg="yellow", bg="#2E2E2E", font=("Helvetica", 10, "bold"))
    trade_mode_label.pack(anchor="w")

    bot_panel = ttk.LabelFrame(session_frame, text="Portfolio", padding="10")
    bot_panel.pack(fill=tk.X, pady=5)
    
    pnl_var = tk.StringVar(value="PNL: $0.00")
    equity_var = tk.StringVar(value=f"Equity: ${get_usdt():.2f}")
    ttk.Label(bot_panel, textvariable=pnl_var).pack(anchor="w")
    ttk.Label(bot_panel, textvariable=equity_var).pack(anchor="w")

    status_log_frame = ttk.LabelFrame(session_frame, text="Status Log", padding="10")
    status_log_frame.pack(fill=tk.X, pady=5)
    
    global status_text
    status_text = tk.Text(status_log_frame, height=3, state='disabled', wrap='word', font=("Helvetica", 9), bg=lighter_bg_color_str, fg=fg_color_str)
    status_text.pack(fill=tk.X, expand=True)

    return (root, graph_frame, bg_color_normalized, asset_var, percent_var, count_var, status_text,
            roi_var, risk_var, adj_var, ma_diff_var, ma_diff_label, ratio_var, pnl_var, equity_var, profit_icon_label, in_trade_var, throttle_var, trade_mode_label, current_price_label)

def setup_graph(parent_frame, bg_color):
    global fig, ax
    if dark_mode:
        plt.style.use('dark_background')
    else:
        plt.style.use('Solarize_Light2')
    
    fig = plt.Figure(figsize=(8, 6), dpi=100)
    fig.patch.set_facecolor(bg_color)
    
    ax = fig.add_subplot(111)
    ax.set_facecolor(bg_color)
    
    canvas = FigureCanvasTkAgg(fig, master=parent_frame)
    canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
    
    return fig, ax, canvas

def check_status_queue(root, status_text_widget, profit_icon_label):
    while not status_queue.empty():
        message = status_queue.get_nowait()
        
        if message == "!TAKE_PROFIT!":
            root.bell()
            profit_icon_label.place(relx=0.5, rely=0.5, anchor='center')
            root.after(1000, lambda: profit_icon_label.place_forget())
            # Also log the text message
            message = "Take Profit target hit! Trailing..."

        status_text_widget.config(state='normal')
        status_text_widget.insert('1.0', message + '\n')
        if int(status_text_widget.index('end-1c').split('.')[0]) > 50:
            status_text_widget.delete('51.0', 'end')
        status_text_widget.config(state='disabled')
        status_text_widget.see("1.0")

    root.after(100, lambda: check_status_queue(root, status_text_widget, profit_icon_label))

def animate_graph(i, ax, canvas, args):
    (asset_var, count_var, roi_var, risk_var, adj_var, ma_diff_var, ma_diff_label,
     ratio_var, pnl_var, equity_var, throttle_var, trade_mode_label, current_price_label) = args
    global currently_analyzing_asset, analysis_count, adjm, pc, lc, total_pnl, initial_balance, total_adjm, in_trade, current_buyprice, current_qty, session_active, session_start_time, session_counter_var, lighter_bg_color, poll_sleep, trade, current_ma_fast, current_ma_slow
    
    asset_var.set(f"Analyzing: {currently_analyzing_asset}")
    count_var.set(f"Analyses: {analysis_count}")
    adj_var.set(f"Adjustments: {adjm}/{total_adjm}")
    if trade:
        trade_mode_label.config(text="[live]", fg="green")
    else:
        trade_mode_label.config(text="[sim]", fg="yellow")
    throttle_var.set(f"Poll: {poll_sleep:.0f}s")

    # Update session counter
    if session_active and session_start_time > 0:
        elapsed = time.time() - session_start_time
        hours, rem = divmod(int(elapsed), 3600)
        minutes, seconds = divmod(rem, 60)
        session_counter_var.set(f"Session Time: {hours:02d}:{minutes:02d}:{seconds:02d}")
    else:
        session_counter_var.set("Session Time: 00:00:00")

    if pc > 0 or lc > 0:
        gcd = math.gcd(pc, lc)
        sim_pc = pc // gcd
        sim_lc = lc // gcd
        ratio_var.set(f"P/L Ratio: {sim_pc}:{sim_lc}")
    else:
        ratio_var.set("P/L Ratio: N/A")

    params_df = pd.DataFrame()
    price_df = pd.DataFrame()
    try:
        price_df = pd.read_sql("SELECT * FROM coin", engine2)
        params_df = pd.read_sql("SELECT * FROM coin", engine)
    except Exception as e:
        # Hold display until data is available
        return

    if params_df.empty or price_df.empty:
        ax.set_title("Waiting for trade...", color='#D3D3D3')
        canvas.draw()
        pnl_var.set(f"PNL: ${total_pnl:.2f}")
        equity_var.set(f"Equity: ${initial_balance + total_pnl:.2f}")
        return

    if 'SMA_fast' in price_df.columns and 'SMA_slow' in price_df.columns and 'Volume' in price_df.columns and len(price_df) > 1:
        latest_close = price_df['Close'].iloc[-1]
        latest_fast  = price_df['SMA_fast'].iloc[-1]
        latest_slow  = price_df['SMA_slow'].iloc[-1]

        diff      = latest_fast - latest_slow
        norm_diff = (diff / latest_close) * 100 if latest_close > 0 else 0
        ma_diff_risk = abs(norm_diff) * 10

        # Trend acceleration: is the split widening?
        if len(price_df) >= 6:
            split_prev = price_df['SMA_fast'].iloc[-6] - price_df['SMA_slow'].iloc[-6]
            trend_dir  = "▲" if diff > split_prev else "▼"
        else:
            trend_dir = "–"

        vol_mean = price_df['Volume'].mean()
        vol_std = price_df['Volume'].std()
        volume_risk = 0
        if vol_std > 0:
            volume_z_score = (price_df['Volume'].iloc[-1] - vol_mean) / vol_std
            volume_risk = max(0, volume_z_score) * 2

        total_risk = min(10, (ma_diff_risk + volume_risk) / 2)
        risk_var.set(f"Risk: {total_risk:.2f}/10")

        ma_diff_var.set(
            f"MA({current_ma_fast}/{current_ma_slow}) split={norm_diff:.4f}% {trend_dir}"
        )

        if norm_diff > 0:
            ma_diff_label.config(foreground='green')
        else:
            ma_diff_label.config(foreground='red')

    params = params_df.values.tolist()
    sl, target, buyprice, asset, benchmark = [p[0] for p in params]
    sl, target, buyprice, benchmark = float(sl), float(target), float(buyprice), float(benchmark)

    ax.clear()
    ax.set_facecolor(lighter_bg_color)
    ax.axhline(y=buyprice, color="#d62728", label="Buy Price")
    ax.axhline(y=benchmark, color="#d6d628", linestyle="dashed", label="Benchmark")
    ax.axhspan(sl, target, facecolor="#2ca02c", alpha=0.2)
    ax.plot(price_df.index, price_df['Close'], label="Price")
    
    ax.set_xlabel("Time")
    ax.set_ylabel("Price")
    ax.set_title(asset, color='#D3D3D3')
    ax.yaxis.grid(True, color='lightgray', linestyle='dashed', alpha=0.4)
    ax.xaxis.grid(True, color='lightgray', linestyle='dashed', alpha=0.4)
    ax.legend(facecolor=lighter_bg_color)
    fig.autofmt_xdate()
    fig.tight_layout()
    canvas.draw()

    asset_var.set(f"Asset: {asset}")

    count_var.set(f"Trade Calls: {call_count}")

    current_price = price_df['Close'].iloc[-1]
    if in_trade and current_buyprice > 0:
        colour = "green" if current_price >= current_buyprice else "yellow"
        current_price_label.config(text=f"Current Price: {current_price:.4f}", fg=colour)
    else:
        current_price_label.config(text="Current Price: --", fg="white")
    if in_trade and current_qty > 0:
        unrealized_pnl = (current_price - current_buyprice) * current_qty
        display_pnl = total_pnl + unrealized_pnl
    else:
        display_pnl = total_pnl
    pnl_var.set(f"PNL: ${display_pnl:.2f}")
    equity_var.set(f"Equity: ${initial_balance + display_pnl:.2f}")

    roi_var.set(f"Wins/Losses: {pc}/{lc}")

def _start_shared_threads():
    """Start trading + AI threads. Used by both GUI and headless modes."""
    global session_active, initial_balance

    def _get_initial_bal():
        return usdt_bal if usdt_bal > 0 else initial_balance

    def _get_strategy_params():
        return {"SL": current_sl, "Target": current_target,
                "ma_fast": current_ma_fast, "ma_slow": current_ma_slow}

    def _set_strategy_params(params):
        global current_sl, current_target, current_ma_fast, current_ma_slow
        current_sl      = params.get("SL",      current_sl)
        current_target  = params.get("Target",  current_target)
        current_ma_fast = params.get("ma_fast", current_ma_fast)
        current_ma_slow = params.get("ma_slow", current_ma_slow)

    def _set_force_sim(val):
        global force_sim_override
        force_sim_override = val

    # Headless trading_loop uses stub Tkinter vars (StringVar-like objects)
    class _Stub:
        def set(self, v): pass
        def get(self): return ""

    trader_args = tuple(_Stub() for _ in range(14))
    t = threading.Thread(target=trading_loop, args=(trader_args,), daemon=True)
    t.start()

    ai_eng = AIStrategyEngine(ai_log_queue, initial_balance=get_usdt())
    at = threading.Thread(
        target=ai_eng.run,
        args=(_get_initial_bal, _get_strategy_params, _set_strategy_params, _set_force_sim),
        daemon=True,
    )
    at.start()
    return ai_eng


def _run_headless():
    """Run without any GUI. Suitable for Raspberry Pi / SSH.
    All output goes to stdout so it can be piped:
        python main.py --headless 2>&1 | tee -a t-rade.log
    """
    global session_active, initial_balance

    print("[t-rade] Headless mode — starting session automatically.")
    sys.stdout.flush()

    session_active  = True
    session_start_time_val = time.time()
    initial_balance = get_usdt()
    print(f"[t-rade] Balance: ${initial_balance:.4f}")
    sys.stdout.flush()

    _start_shared_threads()

    # Drain both queues to stdout every 2 seconds
    try:
        while True:
            time.sleep(2)
            for q in (status_queue, ai_log_queue):
                while not q.empty():
                    msg = q.get_nowait()
                    ai_log_buffer.append(msg)
                    print(msg, flush=True)
    except KeyboardInterrupt:
        print("[t-rade] Shutting down.")
        session_active = False


if __name__ == "__main__":
    headless = "--headless" in sys.argv

    try:
        client = Client(API_KEY, SECRET_KEY)
        client.ping()
        print("Binance client connected.")
        sys.stdout.flush()
    except Exception as e:
        print(f"Could not connect to Binance. Live trading disabled. Error: {e}")
        sys.stderr.flush()
        client = None

    if headless or not client:
        if not client:
            print("[t-rade] No Binance connection — headless sim mode.")
        _run_headless()
    else:
        (root, graph_frame, bg_color, asset_var, percent_var, count_var, status_text,
         roi_var, risk_var, adj_var, ma_diff_var, ma_diff_label, ratio_var, pnl_var, equity_var,
         profit_icon_label, in_trade_var, throttle_var, trade_mode_label, current_price_label) = create_main_window()

        fig, ax, canvas = setup_graph(graph_frame, bg_color)

        trader_args = (asset_var, percent_var, count_var, status_text, roi_var, risk_var, adj_var,
                       ma_diff_var, ma_diff_label, ratio_var, pnl_var, equity_var, profit_icon_label, in_trade_var)
        animation_args = (asset_var, count_var, roi_var, risk_var, adj_var, ma_diff_var, ma_diff_label,
                          ratio_var, pnl_var, equity_var, throttle_var, trade_mode_label, current_price_label)

        def _get_initial_bal():
            return usdt_bal if usdt_bal > 0 else initial_balance

        def _get_strategy_params():
            return {"SL": current_sl, "Target": current_target,
                    "ma_fast": current_ma_fast, "ma_slow": current_ma_slow}

        def _set_strategy_params(params):
            global current_sl, current_target, current_ma_fast, current_ma_slow
            current_sl      = params.get("SL",      current_sl)
            current_target  = params.get("Target",  current_target)
            current_ma_fast = params.get("ma_fast", current_ma_fast)
            current_ma_slow = params.get("ma_slow", current_ma_slow)

        def _set_force_sim(val):
            global force_sim_override
            force_sim_override = val

        trader_thread = threading.Thread(target=trading_loop, args=(trader_args,), daemon=True)
        trader_thread.start()

        ai_eng = AIStrategyEngine(ai_log_queue, initial_balance=get_usdt())
        ai_thread = threading.Thread(
            target=ai_eng.run,
            args=(_get_initial_bal, _get_strategy_params, _set_strategy_params, _set_force_sim),
            daemon=True,
        )
        ai_thread.start()

        root.after(100, lambda: check_status_queue(root, status_text, profit_icon_label))
        root.after(500, lambda: drain_ai_queue(root))

        ani = FuncAnimation(fig, animate_graph, fargs=(ax, canvas, animation_args), interval=2000, cache_frame_data=False)

        root.mainloop()