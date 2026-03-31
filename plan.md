# T-RADE: 10× Equity Plan

**Goal**: Grow $18 → $180+ in 7 days via automated cryptocurrency trading on Binance.

---

## Phases

### Phase 1 — Simulation & Calibration
- All trades execute in **simulation mode** (no real money spent).
- The AI engine waits for at least **15 completed sim trades** before evaluating.
- Evaluation runs every **60 seconds**.

### Phase 2 — Parameter Optimisation
- If the current parameter set is not on track for 10×, the engine advances to the
  next entry in the parameter grid (8 combinations, cycled in order).
- Fitness score: `win_rate × avg_pnl_per_trade`.
- The best-scoring set across all cycles is tracked in `strategy_log.md`.

### Phase 3 — Live Activation
Live trading is enabled automatically when **all three** conditions hold:
1. Win rate ≥ 55 % (implied by positive projection)
2. Average P&L per trade > $0
3. Projected 7-day equity ≥ $180

If live performance later drops below the projection threshold, the engine
reverts to simulation and resumes parameter search.

---

## MA Split Logic

Every candidate is now filtered through two MA-based checks before a buy:

**1. Split magnitude (ROI gating)**
The normalised MA split (`(SMA_fast − SMA_slow) / price × 100`) must be ≥ 20% of
the target gain expressed as a percentage. For a 1% target this means the split must
be at least **0.20%** — ensuring there is enough trend energy to reach TP before the
spread collapses.

**2. Trend acceleration (position sizing)**
The current split is compared to the split 5 candles ago:
- **Widening** (▲): trend strengthening → full position size
- **Narrowing** (▼): trend fading → position reduced by 30%

Both checks are visible in the main status log and in the MA Diff label
(`MA(fast/slow) split=X.XXXX% ▲/▼`).

---

## Parameter Grid

| # | SL    | Target | MA fast | MA slow | Description                    |
|---|-------|--------|---------|---------|--------------------------------|
| 1 | 0.990 | 1.010  |  9      |  21     | Baseline                       |
| 2 | 0.988 | 1.012  |  9      |  21     | Wider SL/TP, same MAs          |
| 3 | 0.992 | 1.008  |  9      |  21     | Tight SL/TP, same MAs          |
| 4 | 0.990 | 1.010  |  5      |  13     | Fast MAs — more reactive       |
| 5 | 0.988 | 1.012  |  5      |  13     | Fast MAs, wider SL/TP          |
| 6 | 0.990 | 1.010  |  7      |  21     | Medium fast / standard slow    |
| 7 | 0.988 | 1.015  |  7      |  21     | Medium fast, wider TP          |
| 8 | 0.990 | 1.010  | 12      |  26     | MACD-style (stronger signals)  |
| 9 | 0.985 | 1.015  | 12      |  26     | MACD-style, aggressive SL/TP  |
|10 | 0.990 | 1.012  |  9      |  21     | Asymmetric TP, baseline MAs    |

---

## Monitoring

| Surface | What's shown |
|---------|--------------|
| Main window status log | Trading activity (buys, sells, SL/TP hits) |
| **AI Logs popup** (button in Asset Info panel) | AI evaluation cycles, param changes, milestones |
| `milestone_log.md` | Persistent record of every AI decision |
| `strategy_log.md` | Per-cycle results: params, win rate, avg P&L, projection |

---

## Key Files

| File | Purpose |
|------|---------|
| `ai_engine.py` | AIStrategyEngine class — runs in daemon thread |
| `main.py` | Trading bot + Tkinter GUI |
| `milestone_log.md` | Auto-generated AI milestone log |
| `strategy_log.md` | Auto-generated strategy performance history |
| `trades.csv` | Completed trade log (read by AI engine for evaluation) |
