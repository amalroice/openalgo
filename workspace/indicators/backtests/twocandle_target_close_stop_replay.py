"""Two-year backtest of the 2026-09-21 exit changes on both 2-candle strategies.

Reuses the committed harnesses' frame builders and helpers, and replays with
switches for: VIX floor (strict < skip), 1.5R target (R = entry - candle 1 low),
close stop (close beyond candle 1's extreme), trail kept throughout.

Fill model: touch stop, close stop and square-off at the bar close (as the
harnesses do); target at the target price itself (the live watch sells within
seconds of the index trading there). A bar that hits both the target and the
touch stop is ambiguous on 5m data; it is scored as the stop (conservative)
and the count is reported, with an optimistic bound as well.

Produced the table at TARGET_R in strategies/examples/*2candle*. Imports the
two sibling harnesses for their frames, so it needs the same long2016/ data.

Usage:
    uv run python workspace/indicators/backtests/twocandle_target_close_stop_replay.py
"""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BT = Path(__file__).resolve().parent
IST = "Asia/Kolkata"


def load(name):
    spec = importlib.util.spec_from_file_location(name, BT / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def replay(h, frame, vix, *, vix_floor, vix_old_le, target_r, close_stop,
           stretch, optimistic=False):
    hi, lo, cl = frame["high"].values, frame["low"].values, frame["close"].values
    above, below = frame["above"].values, frame["below"].values
    upper, lower = frame["upper"].values, frame["lower"].values
    stamps = frame.index
    days = np.array(frame.index.tz_convert(IST).date)
    walls = np.array([(s + pd.Timedelta(minutes=5)).tz_convert(IST).time() for s in stamps])
    vixv = vix.values
    dist, step = 0.005, h.TRAIL_STEP_PCT if hasattr(h, "TRAIL_STEP_PCT") else 0.002

    trades, pos, n_today, cur_day, day_start, ambiguous = [], None, 0, None, 0, 0

    def close_out(i, px, reason):
        nonlocal pos
        pos.update(exit_i=i, exit_px=px, reason=reason)
        trades.append(pos)
        pos = None

    for i in range(len(frame)):
        wall = walls[i]
        if days[i] != cur_day:
            cur_day, n_today, pos, day_start = days[i], 0, None, i

        if pos is not None:
            L = pos["long"]
            tgt = pos["target"]
            tgt_hit = tgt is not None and ((hi[i] >= tgt) if L else (lo[i] <= tgt))
            if wall >= h.SQUARE_OFF:
                # The live watch is still running during the 14:55 bar.
                if tgt_hit:
                    close_out(i, tgt, "target")
                else:
                    close_out(i, cl[i], "squareoff")
                continue
            pos["mfe"] = max(pos["mfe"], (hi[i] - pos["entry"]) if L else (pos["entry"] - lo[i]))
            breached = (lo[i] <= pos["stop"]) if L else (hi[i] >= pos["stop"])
            if breached and tgt_hit:
                ambiguous += 1
                if optimistic:
                    close_out(i, tgt, "target")
                else:
                    close_out(i, cl[i], "stop")
                continue
            if tgt_hit:
                close_out(i, tgt, "target")
                continue
            if breached:
                close_out(i, cl[i], "stop")
                continue
            if close_stop and ((cl[i] < pos["c1_low"]) if L else (cl[i] > pos["c1_high"])):
                close_out(i, cl[i], "close stop")
                continue
            level = h.trail_level(pos["entry"], pos["mfe"], L, dist, step)
            if level is not None:
                pos["stop"] = max(pos["stop"], level) if L else min(pos["stop"], level)
            continue

        if n_today >= h.MAX_TRADES_PER_DAY:
            continue
        if not (h.NO_NEW_ENTRY_BEFORE <= wall < h.NO_NEW_ENTRY_AFTER):
            continue
        if i < 2 or days[i - 1] != days[i]:
            continue
        for direction, flag in (("long", above), ("short", below)):
            if not (flag[i - 1] and flag[i]):
                continue
            if days[i - 2] == days[i] and flag[i - 2]:
                continue
            if direction == "long" and cl[i] <= hi[i - 1]:
                continue
            if direction == "short" and cl[i] >= lo[i - 1]:
                continue
            v = vixv[i]
            v = float(v) if np.isfinite(v) else None
            if vix_floor is not None:
                if v is None:
                    break
                if (v <= vix_floor) if vix_old_le else (v < vix_floor):
                    break
            entry = cl[i]
            if stretch:
                s = ((entry - lo[day_start:i + 1].min()) / entry if direction == "long"
                     else (hi[day_start:i + 1].max() - entry) / entry)
                if not h.stretch_allowed(s, v):
                    break
            line = upper[i - 1] if direction == "long" else lower[i - 1]
            stop = h.initial_stop(direction, lo[i - 1], hi[i - 1], line)
            risk = (entry - stop) if direction == "long" else (stop - entry)
            if risk <= 0:
                break
            L = direction == "long"
            r_pts = (entry - lo[i - 1]) if L else (hi[i - 1] - entry)
            target = None
            if target_r is not None:
                target = entry + target_r * r_pts if L else entry - target_r * r_pts
            pos = {"long": L, "entry": entry, "stop": stop, "mfe": 0.0, "entry_i": i,
                   "c1_low": lo[i - 1], "c1_high": hi[i - 1], "target": target, "r": r_pts}
            n_today += 1
            break

    rows = []
    for t in trades:
        pts = (t["exit_px"] - t["entry"]) if t["long"] else (t["entry"] - t["exit_px"])
        rows.append({"entry_time": stamps[t["entry_i"]], "direction": "long" if t["long"] else "short",
                     "gross": pts, "net": pts - h.COST_PTS, "reason": t["reason"], "r": t["r"]})
    return pd.DataFrame(rows), ambiguous


def stats(df):
    if df.empty:
        return {"n": 0}
    eq = df["net"].cumsum()
    wins, losses = df.loc[df.net > 0, "net"].sum(), -df.loc[df.net <= 0, "net"].sum()
    return {"n": len(df), "net": df.net.sum(), "avg": df.net.mean(), "win": 100 * (df.net > 0).mean(),
                "pf": wins / losses if losses else float("inf"), "dd": (eq - eq.cummax()).min()}


def main():
    start = pd.Timestamp("2024-09-11", tz=IST)
    for key, mod, qty, old_vix in (("NIFTY", "nifty_percent_trail_replay", 65, 12.0),
                                   ("BANKNIFTY", "banknifty_percent_trail_replay", 30, None)):
        h = load(mod)
        print(f"\n######## {key}  (building frame ...)", file=sys.stderr)
        frame = h.load_frame()
        vix = h.load_vix(frame.index)
        end = frame.index[-1].tz_convert(IST)
        stretch = key == "BANKNIFTY"
        variants = {
            "OLD (as traded 2026-09-21)": {"vix_floor": old_vix, "vix_old_le": True, "target_r": None, "close_stop": False},
            "old + VIX>=10 only": {"vix_floor": 10.0, "vix_old_le": False, "target_r": None, "close_stop": False},
            "old + close stop only": {"vix_floor": old_vix, "vix_old_le": True, "target_r": None, "close_stop": True},
            "old + 1.5R target only": {"vix_floor": old_vix, "vix_old_le": True, "target_r": 1.5, "close_stop": False},
            "NEW (all three)": {"vix_floor": 10.0, "vix_old_le": False, "target_r": 1.5, "close_stop": True},
        }
        print(f"\n==== {key}: {start:%Y-%m-%d} -> {end:%Y-%m-%d}, net index pts per lot after "
              f"{h.COST_PTS:.0f} pts/trade; Rs = pts x {qty} qty x 0.5 delta (rough)")
        print(f"{'variant':30}{'trades':>7}{'net pts':>10}{'~Rs':>11}{'avg':>8}{'win%':>7}{'PF':>6}{'maxDD':>9}")
        keep = {}
        for label, kw in variants.items():
            df, amb = replay(h, frame, vix, stretch=stretch, **kw)
            df = df[df.entry_time >= start]
            keep[label] = (df, amb, kw)
            s = stats(df)
            print(f"{label:30}{s['n']:>7}{s['net']:>+10,.0f}{s['net'] * qty * 0.5:>+11,.0f}"
                  f"{s['avg']:>+8.2f}{s['win']:>7.1f}{s['pf']:>6.2f}{s['dd']:>+9,.0f}")
        df, amb, kw = keep["NEW (all three)"]
        opt, _ = replay(h, frame, vix, stretch=stretch, optimistic=True, **kw)
        opt = opt[opt.entry_time >= start]
        print(f"  NEW: {amb} bars (whole history) hit target and touch stop together; scoring them "
              f"as targets instead gives {opt.net.sum():+,.0f} pts over 2y")
        print("  NEW exits:  " + "  ".join(
            f"{r} {n} ({df.loc[df.reason == r, 'net'].mean():+.1f} avg)"
            for r, n in df.reason.value_counts().items()))
        print(f"  NEW median R {df.r.median():.1f} pts, so a median target is {1.5 * df.r.median():.1f} pts away")
        old = keep["OLD (as traded 2026-09-21)"][0]
        yr = lambda d: d.groupby(d.entry_time.dt.tz_convert(IST).dt.year)["net"].agg(["sum", "count"])  # noqa: E731
        a, b = yr(old), yr(df)
        print(f"  {'year':>6}{'OLD pts':>10}{'(n)':>6}{'NEW pts':>10}{'(n)':>6}")
        for y in sorted(set(a.index) | set(b.index)):
            print(f"  {y:>6}{a['sum'].get(y, 0):>+10,.0f}{int(a['count'].get(y, 0)):>6}"
                  f"{b['sum'].get(y, 0):>+10,.0f}{int(b['count'].get(y, 0)):>6}")


if __name__ == "__main__":
    main()
