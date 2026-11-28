<!-- Hand-written research notes. Nothing in the bot writes to this file. strategy_log.md is auto-generated: ai_engine.py overwrites it on every restart. -->

# Strategy log (offline backtests, 2026-11-21)
Costs: fee 0.075%/side, slippage 0.05%/side, next-bar-open fills, one position at a time, 10 blue-chip USDT pairs. Gate: OOS expectancy > +0.15%/trade with n>=150.

| Strategy | TF / window | n | WR | Exp/trade | PF | OOS exp | Result |
|---|---|---|---|---|---|---|---|
| baseline (current main.py logic) | 1m / 30d | 92 | 32.6% | -0.49% | 0.39 | -0.64% | FAIL — matches live mission-1 (36% WR, negative): cost model validated |
| trend_breakout (20-bar breakout, EMA20>50, 1.5 ATR stop, 3 ATR chandelier trail) | 15m / 90d | 316 | 19.3% | -0.16% | 0.68 | -0.38% | FAIL |
| pullback (EMA50>200, RSI<40 reclaim, 1.5 ATR stop, 3 ATR TP) | 15m / 90d | 199 | 26.6% | -0.26% | 0.54 | -0.42% | FAIL |

## Round 2 — longer timeframes (1h x 365d, 4h x 730d), same costs (0.075% fee + 0.05% slip per side)
| Strategy | n | WR | Exp/trade | OOS exp (n) | Result |
|---|---|---|---|---|---|
| donchian_1h (55-bar breakout, EMA50>200, 2 ATR stop, 3 ATR trail) | 163 | 29% | -0.04% | -0.01% (61) | FAIL |
| donchian_4h | 87 | 33% | +0.32% | -0.40% (39) | FAIL — in-sample edge vanished OOS |
| squeeze_1h (BB squeeze breakout) | 119 | 37% | -0.04% | -0.07% (46) | FAIL |
| meanrev_1h (RSI2<10 above EMA200) | 353 | 54% | -0.37% | -0.39% (117) | FAIL — high WR, avg loss 2x avg win |
| pullback_1h | 218 | 35% | -0.21% | -0.44% (76) | FAIL |
| trend_breakout_1h | 277 | 26% | -0.21% | +0.02% (92) | FAIL |

Cost sensitivity (fee 0.05%, slip 0.01% per side — i.e. maker-like fills): only donchian_1h turns positive
(ALL +0.105%/tr, OOS +0.47% n=58), squeeze_1h marginal (+0.07%). Everything else stays negative => no gross edge.
Caveat: breakout entries rarely fill as passive limit orders and stops are market orders, so this cost level is optimistic.
Takeaway: price-only signals on large-cap pairs have ~zero gross edge; fees+slippage (0.25% round trip) decide the sign.

## Round 3 — wide-universe confirmation (top 40 USDT pairs, 1h x 730d, weekly-clustered t-stat)
Candidates were pre-registered (donchian_1h, squeeze_1h), no parameter tuning. Spot fees are 0.075%/side even for limit orders
(maker == taker at standard tier), so a limit entry only saves slippage; limit fills modelled (entry at signal close, missed if not touched).
Universe is today's top-volume list => survivor-biased (flatters results); includes some tokenized-stock tickers.

| Strategy | Scenario | n | Exp/trade | clustered t | OOS exp | Bot-realistic (1 pos) |
|---|---|---|---|---|---|---|
| donchian_1h | taker slip .05 | 2645 | -0.058% | -2.01 | -0.04% | $50->$45.5, DD 32% |
| donchian_1h | taker slip .10 | 2660 | -0.154% | -2.57 | -0.14% | $50->$37.6 |
| donchian_1h | limit entry (99% fill) | 2609 | -0.014% | -1.81 | -0.01% | $50->$39.7 |
| squeeze_1h | taker slip .05 | 1825 | -0.060% | -1.32 | +0.10% | $50->$44.5, DD 34% |
| squeeze_1h | limit entry | 1773 | -0.002% | -1.05 | +0.17% | $50->$32.2 |

Result: FAIL. The earlier +0.1% donchian_1h result (10 pairs, fee .05%) was selection luck + an unavailable fee tier.
Gross edge ~0 for price-only breakout/trend signals on liquid USDT pairs; even before survivorship correction nothing clears costs.
Also: one-position-at-a-time execution is worse than the all-signals average (first-signal-wins picks, drawdowns 30-50%).

## Round 4 — perpetual funding rate as a predictor of forward SPOT returns (event study)
25 USDT-perp symbols (of the top-40 spot universe), 2 years, 57.5k funding observations, 24h hold, net of 0.25% round-trip cost.
Funding z-score uses only past observations (90-obs rolling), usable from next full hour after settlement. Weekly-clustered t-stats.

| Funding z-bucket | n | raw fwd 24h | net of cost | clustered t | market-adj | adj t |
|---|---|---|---|---|---|---|
| z < -2 (shorts crowded) | 2994 | +0.096% | -0.154% | -0.01 | +0.056% | 0.59 |
| -2..-1 | 5563 | +0.010% | -0.240% | -0.73 | -0.011% | -0.73 |
| -1..1 | 44205 | -0.011% | -0.261% | -2.24 | +0.002% | 0.34 |
| 1..2 | 4600 | -0.032% | -0.282% | -2.65 | -0.043% | 1.01 |
| z > 2 | 146 | -0.123% | -0.373% | -1.63 | +0.042% | -0.06 |

Stability: z<-2 bucket was +0.50% raw in the 1st half of the sample and -0.32% in the 2nd — the sign flips, so no durable effect.
Result: FAIL. No bucket beats costs; market-adjusted effects are insignificant (|t|<1.2).
Sanity: BTC funding averages 4.9%/yr (2024-11..2026-11) — data is sound. Funding carry (cash-and-carry) at ~5%/yr is the only
"edge" here and needs a futures/margin account, not spot, and is far below the 10x goal.

## Round 5 — walk-forward optimisation of the live baseline (2026-11-28, `python optimize.py`)
60-coin universe, 1m x 90d. Candidates rebuilt like main.py (top 10 by rolling 24h change, >= $5M volume), live exits
including the sticky danger-zone stop. Fee 0.075% + slip 0.05% per side. Chosen on first 59d, judged on last 30d.
Universe is today's most-traded list (flatters results). One split only. Not modelled: blacklist/whitelist, blue-chip fallback.
Calibration: current settings without slippage = -0.10%/trade train, WR 19% (Pi sim since 23 Nov: about -0.26%, WR 22%);
with slippage -0.21% train, -0.30% test.

| Setting (chosen on train) | train n / avg | TEST n / avg |
|---|---|---|
| current live | 1462 / -0.207% | 870 / -0.295% |
| best of 894 + 192 exit runs: SL .980, TP 1.05, no danger stop, 1.2% trail (live entries) | 233 / +0.219% | 145 / -0.442% |

Top 20 train combinations averaged -0.396%/trade on test; 1 of 20 positive. Only 5/192 exit and 7/894 entry+exit runs were
positive even on train. Entry signal vs same-pool forward return (gross): edge changes sign between train and test at
5, 15, 60 and 240 min.
Result: FAIL. No setting to deploy; the train winner did worse than current settings on unseen data.

## Round 6 — daily trend-following on large coins (2026-11-28, `python daily_trend.py`)
12 coins that were large in early 2020 (list written from memory), daily bars 2020-01-01 → 2026-11-27, long/cash, next-open fills,
0.125% per side, equal starting slices. Rules fixed beforehand, no tuning. Cash earns 0.

| 12 coins | per year | worst fall | Sharpe | per year since 2023 |
|---|---|---|---|---|
| buy and hold | 36.7% | 78% | 0.81 | 20.3% |
| price > 200d avg | 22.1% | 65% | 0.65 | -2.1% |
| 50d > 200d avg | 12.2% | 70% | 0.49 | -6.4% |
| 100d momentum | 17.0% | 65% | 0.57 | 4.0% |
| 55d high in / 20d low out | 31.5% | 58% | 0.85 | 15.6% |
| coin and BTC > 200d avg | 23.5% | 70% | 0.69 | 6.1% |

BTC only: hold 35.9%/yr (fall 77%, Sharpe 0.82); price > 200d 30.9% (64%, 0.84); 55/20 21.9% (47%, 0.71).
Block-bootstrap 90% range of (rule Sharpe - hold Sharpe) includes zero for every rule, 12-coin and BTC-only.
Average-length check (12 coins): N=50 Sharpe 1.05, N=100..300 0.65-0.77. N=50 was seen after the fact; not a finding.
Result: NO EVIDENCE of beating buy-and-hold. Some rules fell less, with less time in the market; falls remain 47-70%.
Not wired into autotest.py or main.py.
