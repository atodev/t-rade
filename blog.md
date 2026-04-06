# Building an Autonomous Crypto Trading Bot on a Raspberry Pi

## The Goal

Turn $18 into $180 in 7 days using a fully autonomous trading bot running 24/7 on a Raspberry Pi. No manual intervention, no watching charts — just a system that learns, adapts, and reports back via Telegram every hour.

This is t-rade.

---

## The Approach

The core idea is simple: find crypto assets in the early stages of a momentum move, enter with a calculated position, trail the stop loss to lock in gains, and exit cleanly. Do this repeatedly, let the AI optimise the parameters, and compound the results.

The bot runs on a Raspberry Pi connected to Binance. Every cycle it:

1. Checks whether the macro environment is suitable for long entries (BTC filter)
2. Scans the top 10 USDT pairs by 24h volume
3. Scores each candidate against a set of entry conditions
4. Buys the best candidate if one qualifies
5. Manages the position with a dynamic stop loss and trailing take profit
6. Logs the trade, updates analytics, and the AI engine evaluates performance

---

## Entry Conditions

A trade is only opened when all of the following are true:

### 1. BTC Macro Filter
Before scanning any altcoin, the bot checks the BTC 5-minute trend using SMA9 and SMA21. It calculates the spread between the two moving averages as a percentage of price. The entry is blocked only if the spread is **negative and widening** — meaning BTC is in an active downtrend. A sideways or recovering BTC is allowed through.

The reason is straightforward: altcoins follow BTC. Entering long positions during a BTC sell-off is the single biggest driver of losses.

### 2. Momentum Condition
The cumulative 30-minute price change must be positive — the asset must be moving up, not drifting sideways.

### 3. MA Crossover
The fast SMA must be above the slow SMA. Default configuration is SMA9 / SMA21, though the AI engine cycles through other configurations including 5/13, 7/21, and 12/26 (MACD-style).

### 4. MA Split Magnitude
It's not enough for the fast MA to be above the slow MA — the **gap between them** must be meaningful. The minimum split is set at 20% of the target gain. If the target is 1% profit, the MAs must be at least 0.2% apart in price terms. This ensures there's enough momentum headroom to actually reach the take profit before the trend fades.

### 5. Trend Acceleration
The split is compared to what it was 5 candles ago. If the gap is **narrowing**, the entry is blocked. Analytics showed 0% win rate on narrowing-trend entries across 82 trades — a hard filter with clear evidence behind it.

### 6. Risk Score Band
A risk score from 0–10 is computed from the MA spread magnitude and the current volume z-score (how unusual the current volume is vs the recent average). Two dead zones are blocked:
- **0–2 (low risk)**: 11% win rate, -$17 total P&L across 190 trades
- **6–8**: 0% win rate across 32 trades

The sweet spot is **2–4**, which showed 57% win rate and +$20 total P&L.

### 7. Price Trajectory Confidence
The last 3 one-minute candles must show at least one higher close than the previous. If all three candles are falling, the entry is blocked regardless of the MA picture. This catches the case where the MA crossover is valid but price has already started reversing.

---

## Position Management

**Position sizing** is dynamic: the proportion of balance allocated scales down as risk increases. At risk score 2 it's ~41% of balance; at risk score 5 it's ~27%. Minimum notional is $10 to stay above Binance's order floor.

**Stop loss** is set at 0.99× the buy price (1% below entry) by default.

**Take profit** is 1.01× (1% above entry). When price hits the take profit, instead of selling, the bot **trails**: it resets the reference price to the current price and tightens the stop loss. On the first trail the stop is set to a level that guarantees the trade closes profitably even if it reverses — calculated as at least `entry × (1 + 2 × fee_rate)`. Subsequent trails use a fixed 0.99× stop.

**Fees** are 0.075% per side (Binance BNB discount). Both buy and sell fees are calculated in simulation and deducted from P&L, so the reported numbers are realistic net figures.

---

## The AI Engine

The AI engine runs in its own thread and evaluates performance every 60 seconds. It maintains a parameter grid of 10 configurations covering different SL/TP widths and MA period combinations.

After every evaluation cycle it:
- Calculates win rate, average P&L, and projects 7-day equity based on current trade frequency
- Records the result to `strategy_log.md`
- If the projection meets the 10× target with positive average P&L, it enables live trading
- If below target, it advances to the next parameter set and logs the switch
- If an error occurs in any cycle, it logs the error and continues — no silent crashes

The AI engine's parameter changes flow directly into the trading strategy. The MA configuration, SL, and TP are all live variables that the engine can update between trades.

---

## Market Context: Fear & Greed

Every trade is tagged with the current [Alternative.me Fear & Greed Index](https://alternative.me/crypto/fear-and-greed-index/) (0–100). Over time, analytics will show whether the strategy performs better in fear conditions (market oversold, potential bounces) or greed conditions (trending markets, momentum trades work better). Current market reading appears in every hourly Telegram report.

---

## Analytics

After every sell, the bot regenerates a full analytics report broken down by:

| Dimension | What it shows |
|---|---|
| Hour of day (UTC) | Which hours have the best win rate and avg P&L |
| Day of week | Tuesday vs Wednesday performance patterns |
| Asset | Which tokens trade most profitably |
| MA configuration | Best fast/slow MA combination by evidence |
| Trend direction | Widening vs narrowing spread at entry |
| Risk score band | Which risk zones actually make money |
| Hold time | Whether short or long holds are more profitable |
| Fear & Greed band | Performance by market sentiment |

The best hour, best day, best MA config, and best risk band are surfaced in every hourly Telegram report as **BEST CONDITIONS**.

---

## Monitoring

The bot runs headless on a Raspberry Pi. Monitoring happens through:

**Telegram** — every trade sends a notification (BUY / PROFIT / LOSS) with price, qty, P&L, fees, hold time, and current balance. Every hour a summary report is sent covering the current parameter set, win rate, total P&L, projected 7-day equity, market sentiment, and any active warnings.

**Warnings are raised automatically for:**
- Win rate below 40%
- 3 or more consecutive losses
- AI engine stalled (no milestone written in 2+ hours)
- No trades logged in 2+ hours (process may be hung)
- Projected 7-day equity below 50% of the $180.76 target

**GitHub** is used as a log relay — the Pi pushes `strategy_log.md`, `analytics_report.md`, `milestone_log.md`, and `trades.csv` hourly so logs are accessible without SSH.

---

## Key Variables at a Glance

| Variable | Default | Description |
|---|---|---|
| `SL` | 0.990 | Stop loss multiplier (1% below entry) |
| `Target` | 1.010 | Trailing TP trigger (1% above entry) |
| `ma_fast` | 9 | Fast SMA period |
| `ma_slow` | 21 | Slow SMA period |
| `MIN_SPLIT_PCT` | `(Target-1) × 20%` | Minimum MA spread to enter |
| `BNB_FEE_RATE` | 0.00075 | 0.075% per side with BNB discount |
| `VOL_MIN` | $5,000,000 | Minimum 24h USDT volume for candidates |
| `CANDIDATE_COUNT` | 10 | Top N pairs scanned each cycle |
| `MIN_TRADES_FOR_EVAL` | 15 | Trades before AI first evaluates |
| `EVAL_INTERVAL` | 60s | AI evaluation frequency |
| `TARGET_MULTIPLIER` | 10× | Equity growth target |
| `PROJECTION_DAYS` | 7 | Projection window |
| `BLACKLIST_LOSSES_TRIGGER` | 2 | Losses within window to blacklist a token |
| `BLACKLIST_WINDOW_HOURS` | 1 | Rolling window to count losses |
| `BLACKLIST_DURATION_HOURS` | 3 | How long a blacklist lasts |
| `WHITELIST_WINS_TRIGGER` | 2 | Wins within window to whitelist a token |
| `WHITELIST_DURATION_HOURS` | 2 | How long a whitelist lasts |
| `BLUECHIP_WHITELIST` | 10 tokens | Permanent fallback list — BTC/ETH/XRP/BNB/SOL/ADA/DOGE/TRX/AVAX/DOT |
| `STALL_CHECK_INTERVAL` | 10m | Minutes in trade before stall reassessment |
| `STALL_RANGE_PCT` | 0.15% | Price range threshold to consider a trade stalling |

---

## Token Reputation System

Not all tokens behave equally. Some enter a losing streak — momentum exhausted, spread collapsing, getting chopped. Others enter a hot streak where conditions stay favourable for extended periods. The token reputation system tracks this in real time.

### Blacklisting

After every sell, the bot looks back over the last hour of trades for that specific token. If it has lost **2 or more times** in that window, it is blacklisted for **3 hours**. During that period it is skipped entirely in the candidate scan, regardless of its MA or momentum signals. The status log records the event:

```
⛔ THEUSDT blacklisted 3h (2 losses in last 1h)
```

This prevents the bot from repeatedly re-entering a token that is clearly in a bad condition — a pattern that is responsible for a disproportionate share of consecutive losses.

### Whitelisting

The reverse applies. If a token wins **2 or more times** in an hour, it is whitelisted for **2 hours**:

```
⭐ DUSDT whitelisted 2h (2 wins in last 1h)
```

Whitelisted tokens are not automatically traded — they still must pass every entry filter (BTC macro, MA crossover, split magnitude, trend acceleration, risk band, price trajectory). The whitelist is used as a **fallback pool**: if the main top-10 volume scan produces no valid candidates, the bot tries whitelisted tokens. This means when market conditions are difficult and the regular scan is dry, the bot can still find opportunities in tokens it has recently had success with.

### Persistence

The lists are stored in `token_lists.json` and survive restarts. Entries carry an expiry timestamp and are pruned automatically on each load — no manual management required.

### Hourly Report

Best and worst performing tokens appear in every Telegram report:

```
TOKENS:
  Best:  DUSDT(+$0.158), ONTUSDT(+$0.078)
  Worst: THEUSDT(-$0.106), KERNELUSDT(-$0.113)
```

This gives a quick read on which assets are currently working and which to watch for blacklisting.

---

## Remote Control via Telegram

The bot is fully controllable from Telegram without needing SSH. A companion script (`bounce.py`) runs every minute via cron, polls for commands, and acts as a process watchdog.

### Commands

| Command | What it does |
|---|---|
| `/status` | Sends the last 25 lines of the live log (`/tmp/t-rade.out`) |
| `/start` | Starts the trading session |
| `/stop` | Stops the trading session |
| `/bounce` | Kills the process and restarts it in headless mode with auto-start |
| `/menu` | Lists all available commands |

`/start` and `/stop` work by writing a command to a `.t-rade-cmd` file. The trading loop polls this file at the top of every iteration and calls `start_session()` or `stop_session()` accordingly, then deletes the file.

### Process Watchdog

`bounce.py` checks whether `main.py` is running on every cron tick. If the process has died:
- A one-time alert is sent: *"t-rade has stalled — send /bounce to restart it"*
- The alert is not repeated until the process recovers (flag file prevents spam)
- `/bounce` kills any zombie process, relaunches in headless mode, confirms it's alive after 8 seconds, and sends a crash log to Telegram if the relaunch fails

The hourly check-in report also raises a warning if no trades have been logged in the last 2 hours — a separate signal that the process may have stalled without dying.

### Auto-start on Bounce

When relaunched via `/bounce`, main.py is started with `--autostart` which calls `start_session()` automatically 2 seconds after the GUI/headless loop initialises — no manual button press required.

---

## Blue-Chip Permanent Whitelist

Alongside the dynamic reputation-based whitelist, the bot maintains a **permanent blue-chip list** of the top 10 cryptocurrencies by market cap:

> BTC, ETH, XRP, BNB, SOL, ADA, DOGE, TRX, AVAX, DOT

These are scanned as a third-tier fallback — after the top-volume scan and the dynamic whitelist both produce no valid candidates. They apply the full set of entry filters (MA crossover, momentum, split magnitude, trend acceleration, trajectory confidence, risk band), so they only trade when conditions are genuinely favourable.

Blue-chips **cannot be blacklisted**. Even if XRP has two recent losses, it remains eligible for the blue-chip fallback scan. The logic: these assets have the deepest liquidity and are most likely to recover quickly — a temporary losing streak is less meaningful than it would be for a micro-cap token.

The status log marks blue-chip entries with a 💎 icon for easy identification.

---

## What We've Learned So Far

- **BTC dominance is real.** When BTC drops, everything drops. The macro filter has prevented entries during several significant sell-offs.
- **Narrowing trends are a trap.** A positive MA crossover on a narrowing spread looks like momentum but is actually the trend fading. Hard-blocking these improved win rate measurably.
- **Risk bands 0–2 are deceptively quiet.** Low volatility looks safe but the bot was losing consistently in this zone — the volume and spread just aren't sufficient to reach take profit before reversing.
- **Hour of day matters significantly.** 19:00 UTC showed 67% win rate and +$0.14 avg P&L. 17:00 UTC showed 0% win rate across 63 trades. The market structure at different hours is genuinely different.
- **Fees matter in sim.** Running the simulation without fee deduction overstated performance. At 0.075% × 2 sides, a 1% target trade nets roughly 0.85% — the breakeven is higher than it looks.

---

## Stack

- **Python 3** — single-file bot (`main.py`), AI engine (`ai_engine.py`), analytics (`analytics.py`)
- **Binance API** via `python-binance` — market orders, balance queries, order verification
- **Tkinter** — GUI with live candlestick chart (optional, bypassed in headless mode)
- **SQLite / SQLAlchemy** — three databases for live trade state, chart data, and trade log
- **Telegram Bot API** — trade notifications and hourly reports via `urllib.request` (no library)
- **Raspberry Pi 4** — runs headless 24/7, cron job pushes logs to GitHub hourly
- **Alternative.me API** — Fear & Greed index, polled each strategy cycle

---

*t-rade is a live experiment. Past sim performance does not guarantee live results. Trade responsibly.*
