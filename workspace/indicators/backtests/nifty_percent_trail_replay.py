"""Replay the NIFTY two-candle strategy with a percent trailing stop.

Rebuilt on 2026-09-20 to settle whether BANKNIFTY's 0.5%/0.1% trail holds up on
NIFTY. It does not - see the table in the strategy script. The harness that
produced the ORIGINAL 0.2% table was never committed, which is why the figures
here differ from it by 3-26% per window; that gap is the harness, not the
strategy, so compare settings within one run rather than against those numbers.

Mirrors strategies/scripts/nifty_2candle_vwap_sma45_20260908211324.py:
``build_frame`` (synthetic-volume VWAP + SMA45, drop_absurd, contributor
filter), ``latest_signal``, ``initial_stop``, ``trail_level`` and ``manage``.
Exits fill at the bar close, as the live script does.

Data comes from workspace/indicators/data/long2016/ - 5m bars from 2016 for the
index, INDIAVIX and the 50 constituents. Refetch with the data/ fetch scripts.

Usage:
    uv run python workspace/indicators/backtests/nifty_percent_trail_replay.py
    uv run python .../nifty_percent_trail_replay.py --sweep
    uv run python .../nifty_percent_trail_replay.py --annual
    uv run python .../nifty_percent_trail_replay.py --dist 0.005 --step 0.002
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
STOP_BUFFER = 10.0
LINE_PROXIMITY = 25.0
MAX_TRADES_PER_DAY = 2
VIX_MIN = 12.0
NO_NEW_ENTRY_BEFORE = pd.Timestamp("10:30").time()
NO_NEW_ENTRY_AFTER = pd.Timestamp("14:30").time()
SQUARE_OFF = pd.Timestamp("15:00").time()
COST_PTS = 3.0

# Parquet names differ from the tickers for the punctuated ones.
FILE_FIXUP = {"BAJAJ-AUTO": "BAJAJ_AUTO", "M&M": "M_AMP_M"}
CONSTITUENTS = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL", "CIPLA",
    "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH",
    "HDFCBANK", "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDUNILVR",
    "ICICIBANK", "INDUSINDBK", "INFY", "ITC", "JIOFIN", "JSWSTEEL",
    "KOTAKBANK", "LT", "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA",
    "TATACONSUM", "TMPV", "TATASTEEL", "TCS", "TECHM", "TITAN", "TRENT",
    "ULTRACEMCO", "WIPRO",
]

WINDOWS = {
    "last 2y": ("2024-09-11", "2026-09-10"),
    "last 5y": ("2021-09-11", "2026-09-10"),
    "2018-01..2021-09": ("2018-01-01", "2021-09-30"),
    "full": ("2016-10-03", "2026-09-10"),
}


def load_frame() -> pd.DataFrame:
    """Index bars with synthetic volume, VWAP, SMA and the clearance flags.

    Returns:
        Frame indexed by bar open time, one row per usable 5m bar.
    """
    idx = pd.read_parquet(DATA / "NIFTY_NSE_INDEX.parquet")
    idx = idx[["open", "high", "low", "close"]].sort_index()
    idx = idx[~idx.index.duplicated()]

    # drop_absurd, per session: the live script runs on one session at a time,
    # so the median it compares against is that session's.
    day = pd.Index(idx.index.tz_convert(IST).date)
    span = idx["high"] - idx["low"]
    med = pd.Series(span.values, index=day).groupby(level=0).median()
    idx = idx[span.values <= 10.0 * med.reindex(day).values]

    vol = pd.Series(0.0, index=idx.index)
    contrib = pd.Series(0, index=idx.index)
    for name in CONSTITUENTS:
        path = DATA / f"{FILE_FIXUP.get(name, name)}_NSE.parquet"
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
    frame["sma"] = frame["close"].rolling(SMA_PERIOD).mean()   # spans sessions
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


def trail_level(entry: float, mfe: float, long: bool,
                dist: float, step: float) -> float | None:
    """Trailing level, counting the best price in whole steps. See the script."""
    steps = int(mfe // (step * entry))
    if steps < 1:
        return None
    offset = steps * step * entry - dist * entry
    return entry + offset if long else entry - offset


def initial_stop(direction: str, c1_low: float, c1_high: float,
                 line: float) -> float:
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


def replay(frame: pd.DataFrame, vix: pd.Series, dist: float, step: float,
           vix_min: float | None = VIX_MIN,
           directions: tuple[str, ...] = ("long", "short"),
           breakout: str = "close") -> pd.DataFrame:
    """One pass over the frame.

    Returns:
        One row per round trip, with gross and net index points.
    """
    hi, lo, cl = frame["high"].values, frame["low"].values, frame["close"].values
    above, below = frame["above"].values, frame["below"].values
    upper, lower = frame["upper"].values, frame["lower"].values
    stamps = frame.index
    days = np.array(frame.index.tz_convert(IST).date)
    # The live loop processes a bar at its close, i.e. its open stamp plus 5m.
    walls = np.array([(s + pd.Timedelta(minutes=5)).tz_convert(IST).time()
                      for s in stamps])
    vixv = vix.values

    trades: list[dict] = []
    pos: dict | None = None
    n_today = 0
    cur_day = None

    for i in range(len(frame)):
        wall = walls[i]
        if days[i] != cur_day:
            cur_day, n_today, pos = days[i], 0, None

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
                # Ratchets against the NEXT bar, as the live manage() does.
                level = trail_level(pos["entry"], pos["mfe"], pos["long"], dist, step)
                if level is not None:
                    pos["stop"] = (max(pos["stop"], level) if pos["long"]
                                   else min(pos["stop"], level))
                continue

        if pos is not None or n_today >= MAX_TRADES_PER_DAY:
            continue
        if not (NO_NEW_ENTRY_BEFORE <= wall < NO_NEW_ENTRY_AFTER):
            continue
        if i < 2 or days[i - 1] != days[i]:
            continue

        for direction, flag in (("long", above), ("short", below)):
            if direction not in directions:
                continue
            if not (flag[i - 1] and flag[i]):
                continue
            # The run has to begin at candle 1.
            if days[i - 2] == days[i] and flag[i - 2]:
                continue
            # 'close': candle 2 closes beyond candle 1's CLOSE (as shipped).
            # 'extreme': beyond candle 1's HIGH for a long, LOW for a short.
            ref_long = cl[i - 1] if breakout == "close" else hi[i - 1]
            ref_short = cl[i - 1] if breakout == "close" else lo[i - 1]
            if direction == "long" and cl[i] <= ref_long:
                continue
            if direction == "short" and cl[i] >= ref_short:
                continue
            if vix_min is not None:
                value = vixv[i]
                # An unreadable VIX skips the signal, as the live script does.
                if not np.isfinite(value) or value <= vix_min:
                    break
            line = upper[i - 1] if direction == "long" else lower[i - 1]
            stop = initial_stop(direction, lo[i - 1], hi[i - 1], line)
            entry = cl[i]
            risk = (entry - stop) if direction == "long" else (stop - entry)
            if risk <= 0:
                break
            pos = {"long": direction == "long", "entry": entry, "stop": stop,
                   "risk": risk, "mfe": 0.0, "entry_i": i}
            n_today += 1
            break

    rows = []
    for trade in trades:
        exit_price = cl[trade["exit_i"]]
        points = ((exit_price - trade["entry"]) if trade["long"]
                  else (trade["entry"] - exit_price))
        rows.append({
            "entry_time": stamps[trade["entry_i"]],
            "exit_time": stamps[trade["exit_i"]],
            "direction": "long" if trade["long"] else "short",
            "entry": trade["entry"], "exit": exit_price, "risk": trade["risk"],
            "gross": points, "net": points - COST_PTS, "reason": trade["reason"],
        })
    return pd.DataFrame(rows)


def summarize(trades: pd.DataFrame) -> dict[str, tuple[float, int, float, float]]:
    """Net points, trade count, win rate and max drawdown per window."""
    out = {}
    for name, (start, end) in WINDOWS.items():
        sub = trades[(trades["entry_time"] >= pd.Timestamp(start, tz=IST))
                     & (trades["entry_time"] <= pd.Timestamp(end, tz=IST)
                        + pd.Timedelta(days=1))]
        equity = sub["net"].cumsum()
        drawdown = (equity - equity.cummax()).min() if len(sub) else 0.0
        out[name] = (sub["net"].sum(), len(sub),
                     100.0 * (sub["net"] > 0).mean() if len(sub) else 0.0,
                     drawdown)
    return out


def main(argv: list[str] | None = None) -> int:
    """Returns: process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=float, default=0.005)
    parser.add_argument("--step", type=float, default=0.002)
    parser.add_argument("--sweep", action="store_true",
                        help="grid over 0.4-0.6%% distance x 0.1-0.3%% step")
    parser.add_argument("--annual", action="store_true",
                        help="year-by-year 0.2%% against 0.1%%")
    args = parser.parse_args(argv)

    print("building frame ...")
    frame = load_frame()
    print(f"  {len(frame):,} bars  {frame.index[0]:%Y-%m-%d} -> {frame.index[-1]:%Y-%m-%d}")
    vix = load_vix(frame.index)
    print(f"  VIX coverage {100 * vix.notna().mean():.1f}%\n")

    if args.sweep:
        print(f"{'dist':>6}{'step':>7}" + "".join(f"{w:>19}" for w in WINDOWS)
              + f"{'maxDD(full)':>13}")
        for dist in (0.004, 0.005, 0.006):
            for step in (0.001, 0.0015, 0.002, 0.0025, 0.003):
                stats = summarize(replay(frame, vix, dist, step))
                print(f"{dist:>6.3%}{step:>7.2%}"
                      + "".join(f"{stats[w][0]:>+19,.0f}" for w in WINDOWS)
                      + f"{stats['full'][3]:>13,.0f}")
            print()
        return 0

    if args.annual:
        wide = replay(frame, vix, 0.005, 0.002)
        tight = replay(frame, vix, 0.005, 0.001)
        year = lambda t: t.groupby(  # noqa: E731
            t["entry_time"].dt.tz_convert(IST).dt.year)["net"].sum()
        a, b = year(wide), year(tight)
        print(f"{'year':>6}{'0.2% step':>12}{'0.1% step':>12}{'diff':>10}")
        for y in sorted(set(a.index) | set(b.index)):
            va, vb = a.get(y, 0.0), b.get(y, 0.0)
            print(f"{y:>6}{va:>+12,.0f}{vb:>+12,.0f}{vb - va:>+10,.0f}")
        print(f"\ntotals: 0.2% {a.sum():+,.0f}   0.1% {b.sum():+,.0f}")
        return 0

    trades = replay(frame, vix, args.dist, args.step)
    stats = summarize(trades)
    print(f"trail {args.dist:.1%} / {args.step:.1%}   "
          f"{len(trades)} trades, net of {COST_PTS:.0f} pts a trade\n")
    for name in WINDOWS:
        net, count, win, drawdown = stats[name]
        print(f"  {name:<20} net {net:>+9,.0f}   {count:>4} trades   "
              f"win {win:4.1f}%   maxDD {drawdown:>8,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
