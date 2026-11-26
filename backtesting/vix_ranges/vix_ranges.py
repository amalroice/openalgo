"""VIX-range sweep for the three live strategies (2026-11-26).

Each strategy is replayed with its own VIX gate switched OFF, every trade is tagged
with India VIX at the signal bar, then trades are bucketed by VIX band and every
[lo, hi) window on a 1-point grid is scored.

  NIFTY 2-candle   engines.nifty(VIX_MIN=None)          (live gate: VIX >= 12)
  BANKNIFTY CPR    engines.bnf()                         (no VIX gate live)
  SENSEX cluster   2-lot replay below, VIX gate off      (live gate: VIX > 13 + breadth 2x)

VIX 5m data is garbage for 2016-01..2016-09 and a few 2021 bars (values < 5); those
bars use the previous session's daily close from sensex_cluster/vix_daily.csv.

Rupees: NIFTY lot 65, BANKNIFTY 30, SENSEX 20; net already includes each
backtest's points cost.

Run: uv run python backtesting/vix_ranges/vix_ranges.py
"""

from __future__ import annotations

import math
import sys
from datetime import time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backtesting/robustness"))
sys.path.insert(0, str(ROOT / "backtesting/yearly_nov"))
import engines  # noqa: E402
import run_all as ra  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)
LOT = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}


def clean_vix() -> pd.Series:
    v = ra.read5("INDIAVIX_NSE_INDEX")["close"].copy()
    d = pd.read_csv(ROOT / "backtesting/sensex_cluster/vix_daily.csv", parse_dates=["date"])
    d = d.set_index("date")["vix"].sort_index()
    bad = v < 5
    days = pd.DatetimeIndex(v.index[bad].date)
    uniq = days.unique()
    prev = pd.Series(d.asof(uniq - pd.Timedelta(days=1)).to_numpy(), index=uniq)
    v[bad] = prev.reindex(days).to_numpy()
    return v


VIX = clean_vix()


def vix_at(ts) -> float:
    s = VIX.loc[:ts]
    if len(s) and (ts - s.index[-1]) <= pd.Timedelta(hours=18):
        return float(s.iloc[-1])
    return float("nan")


# =============================================================================
# SENSEX 2-lot replay (live rules, VIX gate off, breadth gate on)
# =============================================================================


def sensex() -> pd.DataFrame:
    live = ra.load(ra.scripts("sensex_cluster_breakout_"), "sx_vix")
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
    br = ra.breadth_frame(bars.index, live.BREADTH_CONSTITUENTS)
    cap = live.BOOK_BOX_ATR_MAX.get("boxatr")
    cost = live.COST_PTS
    start, end = pd.Timestamp("2016-10-01").date(), ra.END
    rows = []

    def close_leg(pos, name, price):
        leg = pos["legs"].pop(name)
        g = (price - pos["entry"]) if pos["long"] else (pos["entry"] - price)
        pos["net"] += (g - cost) * leg["lots"]

    for d, fr in bars.groupby(bars.index.date):
        if d < start or d > end:
            continue
        n = len(fr)
        hi, lo, cl, op = (fr[k].to_numpy(float) for k in ("high", "low", "close", "open"))
        pos, count, st = None, 0, 0

        def finish(k):
            nonlocal pos, st
            rows.append(
                {
                    "date": pos["t"].date(),
                    "entry_time": pos["t"],
                    "dir": "long" if pos["long"] else "short",
                    "risk": pos["risk"],
                    "lots": pos["lots"],
                    "net": pos["net"],
                }
            )
            pos, st = None, k + 1

        for k in range(n):
            close_t = (fr.index[k] + pd.Timedelta(minutes=5)).time()
            if pos is not None:
                lng = pos["long"]
                if close_t > live.SQUARE_OFF:
                    for nm in list(pos["legs"]):
                        close_leg(pos, nm, op[k])
                    finish(k)
                    continue
                for nm in list(pos["legs"]):
                    s = pos["legs"][nm]["stop"]
                    if (lo[k] <= s) if lng else (hi[k] >= s):
                        close_leg(pos, nm, min(op[k], s) if lng else max(op[k], s))
                if not pos["legs"]:
                    finish(k)
                    continue
                best = (hi[k] - pos["entry"]) if lng else (pos["entry"] - lo[k])
                pos["mfe"] = max(pos["mfe"], best)
                lvl = live.trail_level(pos["entry"], pos["mfe"], lng)
                for nm, leg in pos["legs"].items():
                    new = leg["stop"]
                    if lvl is not None:
                        new = max(new, lvl) if lng else min(new, lvl)
                    at, lk = (
                        (live.LOCK_AT_ATR, live.LOCK_ATR)
                        if nm == "lock"
                        else (live.RUNNER_LOCK_AT_ATR, live.RUNNER_LOCK_ATR)
                    )
                    if at is not None and pos["mfe"] >= at * pos["atr"]:
                        fl = pos["entry"] + lk * pos["atr"] if lng else pos["entry"] - lk * pos["atr"]
                        new = max(new, fl) if lng else min(new, fl)
                    leg["stop"] = new
                if close_t == live.SQUARE_OFF:
                    for nm in list(pos["legs"]):
                        close_leg(pos, nm, cl[k])
                    finish(k)
                continue
            if close_t >= live.SQUARE_OFF:
                continue
            sig, _ = live.scan(fr.iloc[: k + 1], st)
            if not sig:
                continue
            side = sig["direction"]
            lng = side == "long"
            if not (live.NO_NEW_ENTRY_BEFORE <= close_t <= live.NO_NEW_ENTRY_AFTER):
                continue
            if count >= live.MAX_TRADES_PER_DAY:
                continue
            if (
                live.MIN_CLOSE_LOCATION is not None
                and live.close_location(fr.iloc[k], lng) < live.MIN_CLOSE_LOCATION
            ):
                continue
            stamp = fr.index[k]
            b = br.loc[stamp]
            if live.BREADTH_MIN is not None:
                if b.readable < live.BREADTH_MIN_STOCKS:
                    continue
                ratio = max(b.adv, b.dec) / max(min(b.adv, b.dec), 1)
                if ratio < live.BREADTH_MIN:
                    continue
            a = float(atr.at[stamp])
            if not math.isfinite(a) or a <= 0:
                continue
            if cap is not None and sig["high"] - sig["low"] > cap * a:
                continue
            if live.MIN_BAR_ATR is not None and hi[k] - lo[k] < live.MIN_BAR_ATR * a:
                continue
            if live.MIN_DAY_POSITION is not None:
                dp = live.day_position(fr.iloc[: k + 1], lng)
                if dp is None or dp < live.MIN_DAY_POSITION:
                    continue
            entry = cl[k]
            stop = live.initial_index_stop(entry, sig["low"], sig["high"], a, lng)
            risk = (entry - stop) if lng else (stop - entry)
            if risk <= 0:
                continue
            legs = live.initial_legs(stop)
            pos = {
                "long": lng,
                "entry": entry,
                "risk": risk,
                "atr": a,
                "mfe": 0.0,
                "t": stamp,
                "legs": legs,
                "lots": sum(x["lots"] for x in legs.values()),
                "net": 0.0,
            }
            count += 1
        if pos is not None:
            for nm in list(pos["legs"]):
                close_leg(pos, nm, cl[-1])
            finish(n - 1)
    df = pd.DataFrame(rows)
    df["R"] = df["net"] / (df["risk"] * df["lots"])
    return df


# =============================================================================
# Scoring
# =============================================================================


def stats(df: pd.DataFrame, lot: int) -> dict:
    if df.empty:
        return {"n": 0, "win": np.nan, "pf": np.nan, "rs": 0.0, "rs_tr": np.nan, "avgR": np.nan}
    net = df["net"].to_numpy(float)
    gw, gl = net[net > 0].sum(), -net[net < 0].sum()
    return {
        "n": len(df),
        "win": 100 * (net > 0).mean(),
        "pf": gw / gl if gl > 0 else np.inf,
        "rs": net.sum() * lot,
        "rs_tr": net.mean() * lot,
        "avgR": df["R"].mean(),
    }


BANDS = [0, 10, 11, 12, 13, 14, 15, 16, 18, 20, 25, 100]


def band_table(df, lot):
    out = []
    for lo, hi in zip(BANDS[:-1], BANDS[1:], strict=True):
        sub = df[(df.vix >= lo) & (df.vix < hi)]
        out.append({"band": f"{lo}-{hi}", **stats(sub, lot)})
    return pd.DataFrame(out)


def windows(df, lot, min_trades):
    out = []
    edges = list(range(9, 31)) + [100]
    for i, lo in enumerate(edges[:-1]):
        for hi in edges[i + 1 :]:
            sub = df[(df.vix >= lo) & (df.vix < hi)]
            if len(sub) < min_trades:
                continue
            s = stats(sub, lot)
            # same window, last 2 years and 2016-2023 for stability
            r2 = stats(sub[sub.date >= pd.Timestamp("2024-11-12").date()], lot)
            r0 = stats(sub[sub.date < pd.Timestamp("2023-01-01").date()], lot)
            out.append(
                {
                    "lo": lo,
                    "hi": hi if hi < 100 else "inf",
                    **s,
                    "pf_2y": r2["pf"],
                    "n_2y": r2["n"],
                    "pf_pre23": r0["pf"],
                }
            )
    return pd.DataFrame(out)


def main():
    books = {}
    nf = engines.nifty(VIX_MIN=None)
    books["NIFTY"] = nf
    books["BANKNIFTY"] = engines.bnf()
    books["SENSEX"] = sensex()
    pd.set_option("display.width", 220)
    for name, df in books.items():
        df = df.copy()
        df["vix"] = [vix_at(t) for t in df["entry_time"]]
        df = df.dropna(subset=["vix"])
        df.to_pickle(OUT / f"{name.lower()}_trades.pkl")
        lot = LOT[name]
        print(f"\n===== {name}: {len(df)} trades {df.date.min()}..{df.date.max()} =====")
        print("ALL:", {k: round(v, 2) for k, v in stats(df, lot).items()})
        bt = band_table(df, lot)
        print(bt.round(2).to_string(index=False))
        bt.to_csv(OUT / f"{name.lower()}_bands.csv", index=False)
        w = windows(df, lot, min_trades=max(40, len(df) // 8))
        w.to_csv(OUT / f"{name.lower()}_windows.csv", index=False)
        print("-- top windows by rupees --")
        print(w.sort_values("rs", ascending=False).head(8).round(2).to_string(index=False))
        print("-- top windows by PF --")
        print(w.sort_values("pf", ascending=False).head(8).round(2).to_string(index=False))
        print("-- top windows by win% --")
        print(w.sort_values("win", ascending=False).head(8).round(2).to_string(index=False))


if __name__ == "__main__":
    main()
