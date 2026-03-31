# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Application

```bash
# Activate virtual environment
source .venv/bin/activate

# Run the trading bot
python main.py
```

Requires a `.env` file with `API_KEY` and `SECRET_KEY` (Binance credentials). The app will fall back to simulation mode if the USDT balance is below $5.

## Architecture Overview

T-RADE is a single-file (`main.py`) cryptocurrency trading bot with a real-time Tkinter GUI. The entire application is ~760 lines.

### Threading Model

Three concurrent threads:
1. **Main thread** — Tkinter event loop + GUI rendering
2. **Trading thread** — `trading_loop()` runs `strategy()` in an infinite loop; checks `session_active` flag
3. **UI update thread** — `check_status_queue()` polls a `queue.Queue` for messages from the trading thread to update the GUI safely (Tkinter is not thread-safe)

Graph animation runs via `FuncAnimation` every 2 seconds.

### Core Trading Strategy (`strategy()`, lines 149–313)

1. Scans top 10 USDT pairs by volume (≥$5M)
2. Entry conditions: 30-min cumulative price change > 1.0 AND SMA(9) > SMA(21)
3. Risk score (0–10) based on MA spread and volume z-score
4. Position sizing: 50% of balance minus risk adjustment
5. Stop loss at 0.99x (tightens to 0.9915x after a trailing TP hit), take profit at 1.01x
6. Dynamic polling: sleep 2–30s based on proximity to stop loss

### Database Persistence (SQLite via SQLAlchemy)

Three databases written to the working directory:
- `LivepriceDB.db` — active trade parameters (SL, TP, buy price, asset)
- `currpriceDB.db` — real-time price, SMA, volume data for the chart
- `loggerDB.db` — completed trade log

Trades are also appended to `trades.csv`.

### Key Global State

| Variable | Purpose |
|----------|---------|
| `session_active` | Controls whether the trading loop runs |
| `in_trade` | Whether a position is currently open |
| `trade` | `"live"` or `"sim"` mode |
| `current_buyprice`, `current_qty` | Open position details |
| `usdt_bal`, `total_pnl` | Portfolio metrics |
| `pc` / `lc` | Profit / loss count for the session |

### GUI Layout (Tkinter)

Split panel:
- **Left** — Candlestick chart with buy price line, SL/TP zone, animated every 2s
- **Right** — Session controls, asset info, trade stats, portfolio metrics, status log

### Data Flow

```
Binance API → getminutedata() → strategy() → buy()/sell()
                                    ↓               ↓
                             currpriceDB.db    loggerDB.db / trades.csv
                                    ↓
                          FuncAnimation → chart update
                                    ↓
                            status_queue → check_status_queue() → GUI labels
```
