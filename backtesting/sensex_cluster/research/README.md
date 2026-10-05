# SENSEX cluster breakout research

Reproduces and improves `strategies/examples/sensex_cluster_breakout.py`.

## Result

The live BoxATR book (box cap 2.0 x ATR, trail step 0.1%) replays at **44.2%
win, PF 1.72** over the gated 650-session window. Relaxing the box cap to
**3.0 x ATR** and widening the trail step to **0.2%** lifts it to **48.3% win,
PF 1.96** (+30,760 pts vs +20,254, avg +95 a trade), and PF stays **1.81 at a
20-pt cost**. Both changes are in the strategy file since 2026-10-04.

## Files

- `harness.py` - the replay: same 5m resample, cluster walk, gate inputs, ATR
  window and fills as `sensex_cluster_boxatr_650sessions.py`, plus switches for
  a profit target, breakeven, trail shape, box cap, entry window and sides.
- `exp_baseline.py` - reproduces the published 650-session replay (matches to
  the trade: 292 trades, 44.2% win, +20,253.7 net).
- `exp_sweep.py`, `exp_combo.py` - the sweeps and combinations.
- `exp_refine.py` - the cap plateau, cost stress, monthly profile, and optional initial-risk ATR cap.

## Run

```bash
uv run python backtesting/sensex_cluster/research/exp_baseline.py
uv run python backtesting/sensex_cluster/research/exp_refine.py
```

Gate inputs come from `../vix_daily.csv` and `../nifty50_daily_closes.csv`;
the 1m SENSEX archive is `sensex_1m.parquet` at the repo root. The breadth
gate is only readable from 2023-12-15, which is why the gated study starts in
2024.
