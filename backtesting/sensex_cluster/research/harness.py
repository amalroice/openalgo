"""Parameterised replay of the SENSEX cluster BoxATR strategy.

Replicates backtesting/sensex_cluster/sensex_cluster_boxatr_650sessions.py
exactly (same 5m resample, cluster walk, gate inputs, ATR window, fills), then
adds switches the live strategy does not have - a profit target, a breakeven
stop, direction limits, and tunable trail/box/entry parameters - so variants
can be tested from one place.

No repainting: signals and every level read completed bars only; a bar that
touches both the stop and the target is scored as a stop (pessimistic).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DATA = ROOT / "sensex_1m.parquet"
VIX_CSV = HERE.parent / "vix_daily.csv"
CLOSES_CSV = HERE.parent / "nifty50_daily_closes.csv"

BAR_FROM, BAR_TO = pd.Timestamp("09:15").time(), pd.Timestamp("15:25").time()


@dataclass
class Config:
    min_candles: int = 4
    box_pct: float = 0.0025
    trail_dist_pct: float = 0.005
    trail_step_pct: float = 0.001
    max_trades: int = 2
    breakeven_at_pts: float | None = None
    target_r: float | None = None
    target_pts: float | None = None
    box_atr_max: float | None = 2.0
    max_initial_risk_atr: float | None = None
    atr_period: int = 14
    atr_warmup_days: int = 10
    entry_from: object = pd.Timestamp("09:30").time()
    entry_to: object = pd.Timestamp("14:30").time()
    square_off: object = pd.Timestamp("15:00").time()
    directions: tuple = ("long", "short")
    cost_pts: float = 10.0
    gated: bool = True
    vix_min: float = 13.0
    breadth_min: float = 2.0
    label: str = "cfg"
    notes: str = field(default="", compare=False)


def load_bars(first: pd.Timestamp, last: pd.Timestamp, atr_warmup_days: int = 10) -> pd.DataFrame:
    """Five-minute bars from `first` to `last`, left-labelled, weekday sessions only."""
    bars = pd.read_parquet(DATA).sort_index()
    bars = bars.loc[
        str((first - timedelta(days=atr_warmup_days + 5)).date()) : str(last.date()),
        ["open", "high", "low", "close"],
    ]
    bars = (
        bars.resample("5min", origin="start_day")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna()
    )
    bars = bars[bars.index.weekday < 5]
    return bars[(bars.index.time >= BAR_FROM) & (bars.index.time <= BAR_TO)]


def load_gates(dates: list) -> tuple[dict, dict]:
    """Per-session (vix, breadth) readings; an absent date is unreadable."""
    vix = pd.read_csv(VIX_CSV, parse_dates=["date"])
    vix_map = {row.date.date(): float(row.vix) for row in vix.itertuples()}
    closes = pd.read_csv(CLOSES_CSV, index_col=0, parse_dates=True)
    prev = closes.shift(1)
    adv = ((closes > prev) & closes.notna() & prev.notna()).sum(axis=1)
    dec = ((closes < prev) & closes.notna() & prev.notna()).sum(axis=1)
    readable = (closes.notna() & prev.notna()).sum(axis=1)
    breadth_map = {
        d.date(): (int(a), int(dc), int(r))
        for d, a, dc, r in zip(closes.index, adv, dec, readable, strict=True)
        if r >= 40
    }
    return {d: vix_map.get(d) for d in dates}, {d: breadth_map.get(d) for d in dates}


def archive_days() -> list:
    probe = pd.read_parquet(DATA).sort_index()
    return sorted({d for d in probe.index.date if d.weekday() < 5})


def _atr_at(bars: pd.DataFrame, stamp: pd.Timestamp, warmup_days: int, period: int) -> float | None:
    window = bars[
        (bars.index.date >= (stamp - timedelta(days=warmup_days)).date()) & (bars.index <= stamp)
    ]
    if len(window) < 3 * period:
        return None
    prev = window.close.shift(1)
    tr = pd.concat(
        [window.high - window.low, (window.high - prev).abs(), (window.low - prev).abs()], axis=1
    ).max(axis=1)
    return float(tr.ewm(alpha=1 / period, adjust=False).mean().iloc[-1])


def run(
    cfg: Config,
    bars: pd.DataFrame,
    window: list,
    vix_map: dict,
    breadth_map: dict,
    atr_cache: dict | None = None,
) -> pd.DataFrame:
    """Replay `window` under `cfg`; returns the trade rows.

    `atr_cache` may be shared across configs that keep the same ATR period and
    warm-up, which is most of a sweep.
    """
    trades: list[dict] = []
    cache = {} if atr_cache is None else atr_cache
    period, warm = cfg.atr_period, cfg.atr_warmup_days

    replay = bars[pd.Index(bars.index.date).isin(window)]
    for date, day in replay.groupby(replay.index.date):
        day = day.copy()
        start, position, trades_today = 0, None, 0
        hi, lo, cl, op = (
            day.high.to_numpy(float),
            day.low.to_numpy(float),
            day.close.to_numpy(float),
            day.open.to_numpy(float),
        )
        n = len(day)
        for i in range(n):
            stamp = day.index[i]
            if stamp.time() >= cfg.square_off:
                if position:
                    trades.append(
                        _close(position, cl[i - 1], "square-off", hi[i - 1], lo[i - 1], cfg)
                    )
                break
            if position:
                long = position["direction"] == "long"
                # pessimistic: a bar touching both levels is a stop
                if (lo[i] <= position["stop"]) if long else (hi[i] >= position["stop"]):
                    trades.append(_close(position, position["stop"], "stop", hi[i], lo[i], cfg))
                    position = None
                    start = i + 1
                    continue
                if position.get("target") is not None:
                    hit = (hi[i] >= position["target"]) if long else (lo[i] <= position["target"])
                    if hit:
                        trades.append(
                            _close(position, position["target"], "target", hi[i], lo[i], cfg)
                        )
                        position = None
                        start = i + 1
                        continue
                mfe = (
                    hi[position["entry_i"] : i + 1].max() - position["entry"]
                    if long
                    else position["entry"] - lo[position["entry_i"] : i + 1].min()
                )
                if cfg.breakeven_at_pts is not None and mfe >= cfg.breakeven_at_pts:
                    position["stop"] = (
                        max(position["stop"], position["entry"])
                        if long
                        else min(position["stop"], position["entry"])
                    )
                steps = int(mfe // (cfg.trail_step_pct * position["entry"]))
                if steps >= 1:
                    offset = (
                        steps * cfg.trail_step_pct * position["entry"]
                        - cfg.trail_dist_pct * position["entry"]
                    )
                    level = position["entry"] + offset if long else position["entry"] - offset
                    position["stop"] = (
                        max(position["stop"], level) if long else min(position["stop"], level)
                    )
                continue
            # signal on this completed bar, exactly as the live poll sees it
            if i - start < cfg.min_candles or i + 1 >= n:
                continue
            box_hi, box_lo = hi[start:i].max(), lo[start:i].min()
            direction = "long" if cl[i] > box_hi else "short" if cl[i] < box_lo else None
            while (
                start < i
                and hi[start : i + 1].max() - lo[start : i + 1].min() > cfg.box_pct * cl[i]
            ):
                start += 1
            if not direction or direction not in cfg.directions:
                continue
            entry_time = day.index[i + 1].time()
            if not (cfg.entry_from <= entry_time <= cfg.entry_to):
                continue
            if trades_today >= cfg.max_trades:
                continue
            if cfg.gated:
                v = vix_map.get(date)
                if v is None or not v > cfg.vix_min:
                    continue
                breadth = breadth_map.get(date)
                if breadth is None:
                    continue
                adv, dec, _ = breadth
                if max(adv, dec) / max(min(adv, dec), 1) < cfg.breadth_min:
                    continue
            if stamp not in cache:
                cache[stamp] = _atr_at(bars, stamp, warm, period)
            atr = cache[stamp]
            if atr is None:
                continue
            box = box_hi - box_lo
            if cfg.box_atr_max is not None and box > cfg.box_atr_max * atr:
                continue
            entry = float(op[i + 1])
            stop = box_lo if direction == "long" else box_hi
            if cfg.max_initial_risk_atr is not None:
                risk_cap = cfg.max_initial_risk_atr * atr
                capped_stop = entry - risk_cap if direction == "long" else entry + risk_cap
                stop = max(stop, capped_stop) if direction == "long" else min(stop, capped_stop)
            if (entry <= stop) if direction == "long" else (entry >= stop):
                continue
            risk = abs(entry - stop)
            target = None
            if cfg.target_r is not None:
                target = (
                    entry + cfg.target_r * risk
                    if direction == "long"
                    else entry - cfg.target_r * risk
                )
            elif cfg.target_pts is not None:
                target = entry + cfg.target_pts if direction == "long" else entry - cfg.target_pts
            position = {
                "date": date,
                "direction": direction,
                "signal_time": stamp,
                "entry_time": day.index[i + 1],
                "entry": entry,
                "stop": stop,
                "target": target,
                "box_pts": box,
                "atr": atr,
                "entry_i": i + 1,
            }
            trades_today += 1
    result = pd.DataFrame(trades)
    if not result.empty:
        result["gross_pts"] = np.where(
            result.direction.eq("long"), result.exit - result.entry, result.entry - result.exit
        )
        result["net_pts"] = result.gross_pts - cfg.cost_pts
    return result


def _close(pos: dict, exit_price: float, reason: str, high: float, low: float, cfg: Config) -> dict:
    long = pos["direction"] == "long"
    return {
        "date": pos["date"],
        "direction": pos["direction"],
        "signal_time": pos["signal_time"],
        "entry_time": pos["entry_time"],
        "entry": pos["entry"],
        "exit": exit_price,
        "stop": pos["stop"],
        "reason": reason,
        "box_pts": pos["box_pts"],
        "atr": pos["atr"],
        "mfe_pts": (high - pos["entry"]) if long else (pos["entry"] - low),
    }


def stats(t: pd.DataFrame, label: str = "") -> dict:
    if t is None or t.empty:
        return {
            "label": label,
            "trades": 0,
            "win%": np.nan,
            "net": np.nan,
            "avg": np.nan,
            "PF": np.nan,
            "maxDD": np.nan,
        }
    wins, losses = t[t.net_pts > 0], t[t.net_pts <= 0]
    pf = wins.net_pts.sum() / abs(losses.net_pts.sum()) if len(losses) else float("inf")
    curve = t.sort_values(["date", "signal_time"])["net_pts"].cumsum()
    return {
        "label": label,
        "trades": len(t),
        "win%": round(100 * len(wins) / len(t), 1),
        "net": round(t.net_pts.sum(), 1),
        "avg": round(t.net_pts.mean(), 2),
        "PF": round(pf, 2),
        "maxDD": round(float((curve - curve.cummax()).min()), 1),
    }


def show(rows: list[dict]) -> None:
    print(
        pd.DataFrame(rows)[["label", "trades", "win%", "net", "avg", "PF", "maxDD"]].to_string(
            index=False
        )
    )


def windows(days: list) -> dict:
    last650 = days[-650]
    y = {}
    for yr in (2024, 2025, 2026):
        ds = [d for d in days if d.year == yr and d >= pd.Timestamp(days[-650]).date()]
        if ds:
            y[str(yr)] = (str(ds[0]), str(ds[-1]))
    y["650"] = (str(last650), str(days[-1]))
    return y
