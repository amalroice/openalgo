"""Replay the BANKNIFTY two-candle strategy, sibling of the NIFTY harness.

Written 2026-09-20 to measure the entry-rule correction (candle 2 closing
beyond candle 1's RANGE rather than its close) on BANKNIFTY, which the NIFTY
harness cannot do: different constituents, stop scaling, entry window, trade
cap, cost, and the stretch rule that only this copy carries.

Mirrors strategies/scripts/banknifty_2candle_vwap_sma45_20260911210749.py.
Exits fill at the bar close, as the live script does. Absolute levels are not
comparable with the tables already in that script - those came from a harness
that was never committed - so compare settings within one run.

Usage:
    uv run python workspace/indicators/backtests/banknifty_percent_trail_replay.py
    uv run python .../banknifty_percent_trail_replay.py --entry-compare
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA = REPO_ROOT / "workspace" / "indicators" / "data" / "long2016"
IST = "Asia/Kolkata"

# Mirrors the live script's CONFIGURATION block. Keep these in step with it.
SMA_PERIOD = 45
MIN_CLEARANCE = 0.0
STOP_BUFFER = 25.0
LINE_PROXIMITY = 60.0
MAX_TRADES_PER_DAY = 3
VIX_MIN = None
VIX_MAX = None
STRETCH_VIX_LOW = None
STRETCH_VIX_HIGH = 14.0
STRETCH_MAX_PCT = 0.004
TRAIL_DIST_PCT = 0.005
TRAIL_STEP_PCT = 0.001
NO_NEW_ENTRY_BEFORE = pd.Timestamp("11:00").time()
NO_NEW_ENTRY_AFTER = pd.Timestamp("14:30").time()
SQUARE_OFF = pd.Timestamp("15:00").time()
COST_PTS = 4.0

CONSTITUENTS = ["AXISBANK", "HDFCBANK", "ICICIBANK", "INDUSINDBK",
                "KOTAKBANK", "SBIN"]

WINDOWS = {
    "last 2y": ("2024-09-11", "2026-09-16"),
    "last 5y": ("2021-09-11", "2026-09-16"),
    "2018-01..2021-09": ("2018-01-01", "2021-09-30"),
    "full": ("2018-01-01", "2026-09-16"),
}


def load_frame() -> pd.DataFrame:
    """Index bars with synthetic volume, VWAP, SMA and the clearance flags."""
    idx = pd.read_parquet(DATA / "BANKNIFTY_NSE_INDEX.parquet")
    idx = idx[["open", "high", "low", "close"]].sort_index()
    idx = idx[~idx.index.duplicated()]

    day = pd.Index(idx.index.tz_convert(IST).date)
    span = idx["high"] - idx["low"]
    med = pd.Series(span.values, index=day).groupby(level=0).median()
    idx = idx[span.values <= 10.0 * med.reindex(day).values]

    vol = pd.Series(0.0, index=idx.index)
    contrib = pd.Series(0, index=idx.index)
    for name in CONSTITUENTS:
        path = DATA / f"{name}_NSE.parquet"
        if not path.is_file():
            print(f"  MISSING {name}", file=sys.stderr)
            continue
        series = pd.read_parquet(path)["volume"].sort_index()
        series = series[~series.index.duplicated()].reindex(idx.index)
        vol = vol.add(series.fillna(0.0), fill_value=0.0)
        contrib = contrib.add(series.notna().astype(int), fill_value=0)

    frame = idx.copy()
    frame["volume"] = vol
    frame["contributors"] = contrib
    frame["sma"] = frame["close"].rolling(SMA_PERIOD).mean()
    frame = frame[frame["contributors"] > 0]

    sessions = pd.Index(frame.index.tz_convert(IST).date)
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    frame["vwap"] = ((typical * frame["volume"]).groupby(sessions).cumsum().values
                     / frame["volume"].groupby(sessions).cumsum().values)

    frame["upper"] = frame[["vwap", "sma"]].max(axis=1)
    frame["lower"] = frame[["vwap", "sma"]].min(axis=1)
    frame["above"] = frame["low"] > frame["upper"] + MIN_CLEARANCE
    frame["below"] = frame["high"] < frame["lower"] - MIN_CLEARANCE
    return frame.dropna(subset=["vwap", "sma"])


def load_vix(index) -> pd.Series:
    """India VIX aligned to the bar grid, forward filled up to an hour."""
    vix = pd.read_parquet(DATA / "INDIAVIX_NSE_INDEX.parquet")["close"].sort_index()
    return vix[~vix.index.duplicated()].reindex(index).ffill(limit=12)


def trail_level(entry, mfe, long, dist, step):
    """Trailing level, counting the best price in whole steps."""
    steps = int(mfe // (step * entry))
    if steps < 1:
        return None
    offset = steps * step * entry - dist * entry
    return entry + offset if long else entry - offset


def initial_stop(direction, c1_low, c1_high, line):
    """Candle 1's extreme, or the nearby line when it sits close to it."""
    if direction == "long":
        level = c1_low - STOP_BUFFER
        if 0 <= level - line <= LINE_PROXIMITY:
            return line - STOP_BUFFER
        return level
    level = c1_high + STOP_BUFFER
    if 0 <= line - level <= LINE_PROXIMITY:
        return line + STOP_BUFFER
    return level


def stretch_allowed(stretch: float, vix: float | None) -> bool:
    """The live script's stretch_allowed, verbatim in behaviour."""
    if STRETCH_MAX_PCT is None:
        return True
    if vix is not None:
        above_low = STRETCH_VIX_LOW is None or vix > STRETCH_VIX_LOW
        below_high = STRETCH_VIX_HIGH is None or vix <= STRETCH_VIX_HIGH
        if not (above_low and below_high):
            return True
    return stretch <= STRETCH_MAX_PCT


def replay(frame, vix, dist=TRAIL_DIST_PCT, step=TRAIL_STEP_PCT,
           breakout="close", lock_pts=None, lock_trail=None) -> pd.DataFrame:
    """One pass. ``breakout``: 'close' beyond candle 1's close, 'extreme' its range."""
    hi, lo, cl = frame["high"].values, frame["low"].values, frame["close"].values
    above, below = frame["above"].values, frame["below"].values
    upper, lower = frame["upper"].values, frame["lower"].values
    stamps = frame.index
    days = np.array(frame.index.tz_convert(IST).date)
    walls = np.array([(s + pd.Timedelta(minutes=5)).tz_convert(IST).time()
                      for s in stamps])
    vixv = vix.values

    trades: list[dict] = []
    pos = None
    n_today = 0
    cur_day = None
    day_start = 0

    for i in range(len(frame)):
        wall = walls[i]
        if days[i] != cur_day:
            cur_day, n_today, pos, day_start = days[i], 0, None, i

        if pos is not None:
            if wall >= SQUARE_OFF:
                pos["exit_i"], pos["reason"] = i, "squareoff"
                trades.append(pos)
                pos = None
                continue
            excursion = ((hi[i] - pos["entry"]) if pos["long"]
                         else (pos["entry"] - lo[i]))
            pos["mfe"] = max(pos["mfe"], excursion)
            breached = ((lo[i] <= pos["stop"]) if pos["long"]
                        else (hi[i] >= pos["stop"]))
            if breached:
                pos["exit_i"], pos["reason"] = i, "stop"
                trades.append(pos)
                pos = None
            else:
                level = trail_level(pos["entry"], pos["mfe"], pos["long"], dist, step)
                if level is not None:
                    pos["stop"] = (max(pos["stop"], level) if pos["long"]
                                   else min(pos["stop"], level))
                if lock_pts is not None and pos["mfe"] >= lock_pts:
                    if lock_trail is None:
                        floor = pos["entry"]
                    else:
                        best = (pos["entry"] + pos["mfe"] if pos["long"]
                                else pos["entry"] - pos["mfe"])
                        floor = (best - lock_trail if pos["long"]
                                 else best + lock_trail)
                    pos["stop"] = (max(pos["stop"], floor) if pos["long"]
                                   else min(pos["stop"], floor))
                continue

        if pos is not None or n_today >= MAX_TRADES_PER_DAY:
            continue
        if not (NO_NEW_ENTRY_BEFORE <= wall < NO_NEW_ENTRY_AFTER):
            continue
        if i < 2 or days[i - 1] != days[i]:
            continue

        for direction, flag in (("long", above), ("short", below)):
            if not (flag[i - 1] and flag[i]):
                continue
            if days[i - 2] == days[i] and flag[i - 2]:
                continue
            ref_long = cl[i - 1] if breakout == "close" else hi[i - 1]
            ref_short = cl[i - 1] if breakout == "close" else lo[i - 1]
            if direction == "long" and cl[i] <= ref_long:
                continue
            if direction == "short" and cl[i] >= ref_short:
                continue

            entry = cl[i]
            value = vixv[i]
            vix_now = float(value) if np.isfinite(value) else None
            # Day extremes so far, including the signal bar, as the live
            # script measures them off today's frame up to that bar.
            if direction == "long":
                stretch = (entry - lo[day_start:i + 1].min()) / entry
            else:
                stretch = (hi[day_start:i + 1].max() - entry) / entry
            if not stretch_allowed(stretch, vix_now):
                break

            line = upper[i - 1] if direction == "long" else lower[i - 1]
            stop = initial_stop(direction, lo[i - 1], hi[i - 1], line)
            risk = (entry - stop) if direction == "long" else (stop - entry)
            if risk <= 0:
                break
            pos = {"long": direction == "long", "entry": entry, "stop": stop,
                   "risk": risk, "mfe": 0.0, "entry_i": i}
            n_today += 1
            break

    rows = []
    for t in trades:
        ex = cl[t["exit_i"]]
        pts = (ex - t["entry"]) if t["long"] else (t["entry"] - ex)
        rows.append({"entry_time": stamps[t["entry_i"]],
                     "exit_time": stamps[t["exit_i"]],
                     "direction": "long" if t["long"] else "short",
                     "gross": pts, "net": pts - COST_PTS, "reason": t["reason"],
                     "mfe": t["mfe"]})
    return pd.DataFrame(rows)


def summarize(trades: pd.DataFrame):
    """Net points, trade count, win rate and max drawdown per window."""
    out = {}
    for name, (start, end) in WINDOWS.items():
        sub = trades[(trades["entry_time"] >= pd.Timestamp(start, tz=IST))
                     & (trades["entry_time"] <= pd.Timestamp(end, tz=IST)
                        + pd.Timedelta(days=1))]
        equity = sub["net"].cumsum()
        dd = (equity - equity.cummax()).min() if len(sub) else 0.0
        out[name] = (sub["net"].sum(), len(sub),
                     100.0 * (sub["net"] > 0).mean() if len(sub) else 0.0, dd)
    return out


def main(argv=None) -> int:
    """Returns: process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--entry-compare", action="store_true")
    args = parser.parse_args(argv)

    print("building frame ...")
    frame = load_frame()
    print(f"  {len(frame):,} bars  {frame.index[0]:%Y-%m-%d} -> {frame.index[-1]:%Y-%m-%d}")
    vix = load_vix(frame.index)
    print(f"  VIX coverage {100 * vix.notna().mean():.1f}%\n")

    modes = ([("close > c1 CLOSE  (old)", "close"),
              ("close > c1 RANGE  (new)", "extreme")]
             if args.entry_compare else [("live setting", "extreme")])

    results = {label: summarize(replay(frame, vix, breakout=bk))
               for label, bk in modes}

    print(f"Net index points per lot, after {COST_PTS:.0f} pts a trade\n")
    print(f"{'':28}" + "".join(f"{w:>19}" for w in WINDOWS))
    for label, stats in results.items():
        print(f"{label:28}" + "".join(f"{stats[w][0]:>+19,.0f}" for w in WINDOWS))
    print()
    for label, stats in results.items():
        print(f"{label}:")
        for w in WINDOWS:
            net, n, win, dd = stats[w]
            avg = net / n if n else 0.0
            print(f"    {w:<20} net {net:>+8,.0f}  {n:>4} trades  "
                  f"avg {avg:>+6.2f}  win {win:4.1f}%  maxDD {dd:>7,.0f}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
