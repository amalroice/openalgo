"""SENSEX cluster: skip a breakout when SENSEX has already moved too far its way.

User's idea, 2026-11-27: the 11:00 short that day entered after SENSEX had
already fallen ~0.6% from the open. Test "do not enter if SENSEX has already
moved 0.5% in the breakout direction".

"Moved" is measured at the breakout bar's close (the live entry price), three ways:
  open   - vs today's 09:15 open        (short: (open - entry) / open)
  prev   - vs the previous session close
  extreme- vs today's opposite extreme so far (short: from the day's high so far,
           long: from the day's low so far), i.e. how far the move has run

The replay is run_all.sensex() (live script rules, bar-time NIFTY 50 breadth,
10 pts cost) with one extra gate, applied in the loop so a skipped breakout
frees the day's trade slot exactly as it would live.

Run: uv run python backtesting/yearly_nov/sensex_moved_filter.py
"""

from __future__ import annotations

import math
import sys
from datetime import time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_all as ra  # noqa: E402

START = pd.Timestamp("2016-10-10").date()
END = pd.Timestamp("2026-11-19").date()


def prepare():
    live = ra.load(ra.scripts("sensex_cluster_breakout_"), "sx_live")
    bars = ra.read5("SENSEX_BSE_INDEX")[["open", "high", "low", "close"]]
    bars = bars[(bars.index.time >= dtime(9, 15)) & (bars.index.time <= dtime(15, 25))]
    day = pd.Index(bars.index.date)
    span = bars["high"] - bars["low"]
    med = pd.Series(span.values, index=day).groupby(level=0).transform("median").values
    size = pd.Series(1, index=day).groupby(level=0).transform("sum").values
    bars = bars[~((span.values > 10 * med) & (bars.index.time != dtime(9, 15)) & (size >= 5))]
    prev = bars["close"].shift(1)
    tr = pd.concat(
        [bars["high"] - bars["low"], (bars["high"] - prev).abs(), (bars["low"] - prev).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1 / live.ATR_PERIOD, adjust=False).mean()
    vix = ra.read5("INDIAVIX_NSE_INDEX")["close"].reindex(bars.index).ffill(limit=12)
    br = ra.breadth_frame(bars.index, live.BREADTH_CONSTITUENTS)
    prev_close = bars["close"].groupby(bars.index.date).last().shift(1)
    return live, bars, atr, vix, br, prev_close


def replay(live, bars, atr, vix, br, prev_close, mode: str | None, limit: float | None):
    cap = live.BOOK_BOX_ATR_MAX.get("boxatr")
    trades = []
    for d, fr in bars.groupby(bars.index.date):
        if d < START or d > END:
            continue
        n = len(fr)
        hi, lo, cl, op = (fr[k].to_numpy(float) for k in ("high", "low", "close", "open"))
        day_open = op[0]
        pc = prev_close.get(d, np.nan)
        pos, count, start = None, 0, 0
        for k in range(n):
            close_t = (fr.index[k] + pd.Timedelta(minutes=5)).time()
            if pos is not None:
                lng = pos["long"]
                if close_t > live.SQUARE_OFF:
                    trades.append(close(pos, op[k], "square-off"))
                    pos, start = None, k + 1
                    continue
                stop = pos["stop"]
                if (lo[k] <= stop) if lng else (hi[k] >= stop):
                    fill = min(op[k], stop) if lng else max(op[k], stop)
                    trades.append(close(pos, fill, "stop" if not pos["moved"] else "trail"))
                    pos, start = None, k + 1
                else:
                    best = (hi[k] - pos["entry"]) if lng else (pos["entry"] - lo[k])
                    pos["mfe"] = max(pos["mfe"], best)
                    lvl = live.trail_level(pos["entry"], pos["mfe"], lng)
                    new = stop if lvl is None else (max(stop, lvl) if lng else min(stop, lvl))
                    if live.LOCK_AT_ATR is not None and pos["mfe"] >= live.LOCK_AT_ATR * pos["atr"]:
                        fl = (pos["entry"] + live.LOCK_ATR * pos["atr"] if lng
                              else pos["entry"] - live.LOCK_ATR * pos["atr"])
                        new = max(new, fl) if lng else min(new, fl)
                    if new != stop:
                        pos["stop"], pos["moved"] = new, True
                    if close_t == live.SQUARE_OFF:
                        trades.append(close(pos, cl[k], "square-off"))
                        pos, start = None, k + 1
                    continue
            if close_t >= live.SQUARE_OFF:
                continue
            sig, _ = live.scan(fr.iloc[: k + 1], start)
            if not sig:
                continue
            side = sig["direction"]
            if not (live.NO_NEW_ENTRY_BEFORE <= close_t <= live.NO_NEW_ENTRY_AFTER):
                continue
            if count >= live.MAX_TRADES_PER_DAY:
                continue
            if (live.MIN_CLOSE_LOCATION is not None
                    and live.close_location(fr.iloc[k], side == "long") < live.MIN_CLOSE_LOCATION):
                continue
            stamp = fr.index[k]
            v = vix.at[stamp]
            b = br.loc[stamp]
            breadth = (int(b.adv), int(b.dec)) if b.readable >= live.BREADTH_MIN_STOCKS else None
            ok, _ = live.active_day(None if pd.isna(v) else float(v), breadth,
                                    live.BOOK_VIX_MIN["boxatr"])
            if not ok:
                continue
            a = float(atr.at[stamp])
            if not math.isfinite(a) or a <= 0 or (cap is not None and sig["high"] - sig["low"] > cap * a):
                continue
            lng = side == "long"
            entry = cl[k]
            # how far SENSEX has already gone the trade's way, in %
            if lng:
                moves = {"open": (entry - day_open) / day_open,
                         "prev": (entry - pc) / pc if pc == pc else np.nan,
                         "extreme": (entry - lo[: k + 1].min()) / lo[: k + 1].min()}
            else:
                moves = {"open": (day_open - entry) / day_open,
                         "prev": (pc - entry) / pc if pc == pc else np.nan,
                         "extreme": (hi[: k + 1].max() - entry) / hi[: k + 1].max()}
            if mode is not None and moves[mode] * 100 >= limit:
                continue
            stop = live.initial_index_stop(entry, sig["low"], sig["high"], a, lng)
            if ((entry - stop) if lng else (stop - entry)) <= 0:
                continue
            pos = {"long": lng, "entry": entry, "stop": stop, "atr": a, "mfe": 0.0,
                   "moved": False, "t": stamp, **{f"mv_{m}": 100 * x for m, x in moves.items()}}
            count += 1
        if pos is not None:
            trades.append(close(pos, cl[-1], "square-off"))
    df = pd.DataFrame(trades)
    df["net"] = df["gross"] - live.COST_PTS
    return df


def close(pos, price, why):
    g = (price - pos["entry"]) if pos["long"] else (pos["entry"] - price)
    return {"date": pos["t"].date(), "entry_time": pos["t"], "dir": "long" if pos["long"] else "short",
            "entry": pos["entry"], "exit": price, "why": why, "gross": g,
            "mv_open": pos["mv_open"], "mv_prev": pos["mv_prev"], "mv_extreme": pos["mv_extreme"]}


def summary(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"trades": 0, "win%": np.nan, "PF": np.nan, "net": 0.0, "maxDD": 0.0}
    w, l_ = df.net[df.net > 0].sum(), -df.net[df.net <= 0].sum()
    curve = df.sort_values("entry_time").net.cumsum()
    return {"trades": len(df), "win%": round(100 * (df.net > 0).mean(), 1),
            "PF": round(w / l_, 2) if l_ else np.inf, "net": round(df.net.sum()),
            "maxDD": round(float((curve - curve.cummax()).min()))}


PERIODS = {
    "10y": (START, END),
    "2016-22": (START, pd.Timestamp("2022-12-31").date()),
    "2023-26": (pd.Timestamp("2023-01-01").date(), END),
    "last 2y": (pd.Timestamp("2024-11-20").date(), END),
    "last 1y": (pd.Timestamp("2025-11-20").date(), END),
}


def table(df: pd.DataFrame, label: str) -> list[dict]:
    rows = []
    for p, (a, b) in PERIODS.items():
        sub = df[(df.date >= a) & (df.date <= b)]
        rows.append({"rule": label, "period": p, **summary(sub)})
    return rows


def main() -> int:
    pd.set_option("display.width", 220)
    data = prepare()
    out = Path(__file__).resolve().parent
    base_csv = out / "sensex_moved_base_trades.csv"
    if base_csv.exists():
        base = pd.read_csv(base_csv, parse_dates=["entry_time"])
        base["date"] = pd.to_datetime(base["date"]).dt.date
    else:
        base = replay(*data, None, None)
        base.to_csv(base_csv, index=False)
    rows = table(base, "live (no filter)")
    limits = [float(x) for x in sys.argv[1:]] or [0.3, 0.5, 0.75, 1.0]
    for mode in ("open", "prev", "extreme"):
        for lim in limits:
            df = replay(*data, mode, lim)
            rows += table(df, f"skip if {mode} >= {lim}%")
    res = pd.DataFrame(rows)
    res.to_csv(out / "sensex_moved_filter_results.csv", index=False)
    for p in PERIODS:
        print(f"\n=== {p} ===")
        print(res[res.period == p].drop(columns="period").to_string(index=False))
    # what the 0.5% rule would remove, on the live trade list
    print("\n=== live trades split by move at entry (0.5%) ===")
    for mode in ("open", "prev", "extreme"):
        col = f"mv_{mode}"
        for name, sub in (("< 0.5%", base[base[col] < 0.5]), (">= 0.5%", base[base[col] >= 0.5])):
            for p in ("10y", "last 2y"):
                a, b = PERIODS[p]
                s = sub[(sub.date >= a) & (sub.date <= b)]
                print(f"{mode:8s} {name:8s} {p:8s} {summary(s)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
