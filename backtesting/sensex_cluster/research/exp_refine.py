"""Refine the cap plateau, combine with the trail step, and stress costs."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd  # noqa: E402
from harness import Config, archive_days, load_bars, load_gates, run, stats  # noqa: E402


def pf_at_cost(t, cost):
    if t.empty:
        return float("nan")
    net = t.gross_pts - cost
    winners, losers = net[net > 0], net[net <= 0]
    return round(winners.sum() / abs(losers.sum()), 2) if len(losers) else float("inf")


def main() -> int:
    days = [d for d in archive_days() if d <= pd.Timestamp("2026-09-15").date()]
    window = [d for d in days if d >= pd.Timestamp("2024-01-08").date()]
    bars = load_bars(pd.Timestamp(window[0]), pd.Timestamp(window[-1]))
    vix_map, breadth_map = load_gates(window)
    cache: dict = {}
    years = {
        "2024": [d for d in window if d.year == 2024],
        "2025": [d for d in window if d.year == 2025],
        "2026": [d for d in window if d.year == 2026],
    }

    cfgs = [
        Config(label="base cap2.0"),
        Config(box_atr_max=2.25, label="cap2.25"),
        Config(box_atr_max=2.5, label="cap2.5"),
        Config(box_atr_max=2.75, label="cap2.75"),
        Config(box_atr_max=3.0, label="cap3.0"),
        Config(box_atr_max=3.25, label="cap3.25"),
        Config(box_atr_max=3.5, label="cap3.5"),
        Config(box_atr_max=3.0, trail_step_pct=0.002, label="cap3.0+trail.2%"),
        Config(box_atr_max=3.0, trail_step_pct=0.002, max_initial_risk_atr=2.5,
               label="cap3.0+trail.2%+risk2.5ATR"),
        Config(box_atr_max=2.5, trail_step_pct=0.002, label="cap2.5+trail.2%"),
        Config(box_atr_max=3.0, min_candles=3, label="cap3.0+min3"),
        Config(box_atr_max=3.0, target_r=2.0, label="cap3.0+t2R"),
        Config(box_atr_max=3.5, trail_step_pct=0.002, label="cap3.5+trail.2%"),
    ]
    print("config                  n    win%     net    avg     PF   PF@15  PF@20  | per-year")
    for cfg in cfgs:
        t = run(cfg, bars, window, vix_map, breadth_map, atr_cache=cache)
        s = stats(t, cfg.label)
        yr = []
        for yn, yw in years.items():
            ty = run(cfg, bars, yw, vix_map, breadth_map, atr_cache=cache)
            sy = stats(ty, yn)
            yr.append(f"{yn} {sy['win%']:>4}% PF{sy['PF']:>5} n{sy['trades']:>3}")
        print(
            f"{cfg.label:<20} {s['trades']:>4}  {s['win%']:>5}%  {s['net']:>7.0f}  {s['avg']:>6.2f}  "
            f"{s['PF']:>5}  {pf_at_cost(t, 15):>5}  {pf_at_cost(t, 20):>5}  |  " + "  ".join(yr)
        )

    # the leader's monthly profile
    for name, cfg in (
        ("cap3.0", Config(box_atr_max=3.0)),
        ("cap3.0+trail.2%", Config(box_atr_max=3.0, trail_step_pct=0.002)),
    ):
        t = run(cfg, bars, window, vix_map, breadth_map, atr_cache=cache)
        t = t.copy()
        t["month"] = pd.to_datetime(t["date"].astype(str)).dt.strftime("%Y-%m")
        g = t.groupby("month").agg(n=("net_pts", "size"), net=("net_pts", "sum"))
        g["win%"] = (100 * t.groupby("month")["net_pts"].apply(lambda x: (x > 0).mean())).round(1)
        print(f"\n{name} monthly:")
        print(g.to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
