"""Nov-to-Nov yearly backtest of the four strategies, on their CURRENT live rules.

Years run 1 Nov to 31 Oct: Y1 = 2020-11-01..2021-10-31, ... Y6 = 2025-11-01..2026-11-12
(the last year is cut at the data end).

Each replay reads its rules from the live script in strategies/scripts/ (config
constants and, where they are pure, the functions themselves), so it reflects
what will trade from 2026-11-13:

  NIFTY 2-candle   1 lot, point-for-point trailing stop (POINT_TRAIL), VIX >= 12,
                   candle 2 <= 50 pts, entries on bars closing 10:15..14:30, max 2/day,
                   square-off 15:00. Signals and stops on the bar close, exits fill at
                   the bar close (as live). Frame: synthetic-volume VWAP + SMA45 from
                   workspace/indicators/backtests/nifty_percent_trail_replay.py.
  BANKNIFTY CPR    the live replay() itself, on 1m bars resampled to 5m from 09:15;
                   stops/targets fill at their level.
  SENSEX cluster   live scan(), close_location(), initial_index_stop(), trail_level and
                   the ATR lock (advance_legs rules), boxatr gate: VIX > 13, NIFTY 50
                   breadth >= 2x (>= 40 quotes), box <= 2 x ATR14; entry at the
                   breakout bar's close, entries 09:30..12:30, max 2/day, square-off
                   15:00; stops fill at the stop (gap-through at the bar open).
  Sector 920       09:20 snapshot vs previous daily close, greenest/reddest sector,
                   biggest F&O mover 0..4% with NIFTY, 10:15 confirmation (stock
                   >= 0.5% beyond its 09:20 price with the trade and NIFTY the same
                   way), entry at 10:15, stop 1%, target 1.5% (LIVE_BOOK) with the
                   psychological-level rule, square-off 15:15. Stock % move, 0.05% cost.

All P&L is on the underlying (index points / stock %), not option premium.
Costs: NIFTY 3 pts, BANKNIFTY 5 pts, SENSEX 10 pts a trade, Sector 0.05%.

Run:  uv run python backtesting/yearly_nov/run_all.py
"""

from __future__ import annotations

import importlib.util
import math
import sqlite3
import sys
from datetime import time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
LONG = ROOT / "workspace/indicators/data/long2016"
IST = "Asia/Kolkata"
START, END = pd.Timestamp("2020-11-01").date(), pd.Timestamp("2026-11-12").date()
YEARS = [
    (
        pd.Timestamp(f"{y}-11-01").date(),
        pd.Timestamp(f"{y + 1}-10-31").date(),
        f"Nov {y} - Oct {y + 1}",
    )
    for y in range(2020, 2026)
]
YEARS[-1] = (YEARS[-1][0], END, "Nov 2025 - Nov 12 2026")
FIXUP = {"BAJAJ-AUTO": "BAJAJ_AUTO", "M&M": "M_AMP_M"}


def load(path: Path, name: str):
    # Live scripts import siblings (live_guard.py from 2026-11-17), so their folder must be importable.
    if str(Path(path).parent) not in sys.path:
        sys.path.insert(0, str(Path(path).parent))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def scripts(prefix: str) -> Path:
    return next((ROOT / "strategies/scripts").glob(prefix + "*.py"))


def read5(name: str) -> pd.DataFrame:
    df = pd.read_parquet(LONG / f"{name}.parquet").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df.index = df.index.tz_convert(IST)
    return df


def breadth_frame(index: pd.DatetimeIndex, symbols: list[str]) -> pd.DataFrame:
    """Advancers/decliners/readable per bar: each stock's bar close vs its previous session close."""
    day = pd.Index(index.date)
    adv = np.zeros(len(index), int)
    dec = np.zeros(len(index), int)
    rd = np.zeros(len(index), int)
    for s in symbols:
        p = LONG / f"{FIXUP.get(s, s)}_NSE.parquet"
        if not p.is_file():
            continue
        c = read5(p.stem)["close"]
        prev_by_day = c.groupby(c.index.date).last().shift(1)
        close = c.reindex(index).to_numpy()
        prev = pd.Series(day).map(prev_by_day).to_numpy(dtype=float)
        ok = np.isfinite(close) & np.isfinite(prev) & (close > 0) & (prev > 0)
        rd += ok
        adv += ok & (close > prev)
        dec += ok & (close < prev)
    return pd.DataFrame({"adv": adv, "dec": dec, "readable": rd}, index=index)


# =============================================================================
# NIFTY 2-candle
# =============================================================================


def nifty() -> pd.DataFrame:
    live = load(scripts("nifty_2candle_vwap_sma45_"), "nf_live")
    hz = load(ROOT / "workspace/indicators/backtests/nifty_percent_trail_replay.py", "nf_harness")
    assert live.POINT_TRAIL and live.LOTS == 1, "replay assumes the 1-lot point-trail rules"
    fr = hz.load_frame()
    fr.index = fr.index.tz_convert(IST)
    vix = read5("INDIAVIX_NSE_INDEX")["close"].reindex(fr.index).ffill(limit=12).to_numpy()
    o, h, l, c = (fr[k].to_numpy(float) for k in ("open", "high", "low", "close"))  # noqa: E741
    above, below = fr["above"].to_numpy(bool), fr["below"].to_numpy(bool)
    upper, lower = fr["upper"].to_numpy(float), fr["lower"].to_numpy(float)
    days = np.array(fr.index.date)
    close_t = [(t + pd.Timedelta(minutes=5)).time() for t in fr.index]
    trades, pos, count, cur = [], None, 0, None
    for i in range(len(fr)):
        d = days[i]
        if d < START - pd.Timedelta(days=5) or d > END:
            continue
        if d != cur:
            if pos is not None:  # data gap: close at the last bar of the old day
                pos = None
            cur, count = d, 0
        t = close_t[i]
        if pos is not None:
            if t >= live.SQUARE_OFF:
                trades.append(_nf_close(pos, c[i], fr.index[i], "square-off"))
                pos = None
                continue
            lng = pos["long"]
            if (l[i] <= pos["stop"]) if lng else (h[i] >= pos["stop"]):
                trades.append(
                    _nf_close(pos, c[i], fr.index[i], "stop" if not pos["moved"] else "trail")
                )
                pos = None
            else:
                exc = (h[i] - pos["entry"]) if lng else (pos["entry"] - l[i])
                pos["mfe"] = max(pos["mfe"], exc)
                new = pos["stop0"] + pos["mfe"] if lng else pos["stop0"] - pos["mfe"]
                if (new > pos["stop"]) if lng else (new < pos["stop"]):
                    pos["stop"], pos["moved"] = new, True
                continue
        if t >= live.SQUARE_OFF or count >= live.MAX_TRADES_PER_DAY:
            continue
        if not (live.NO_NEW_ENTRY_BEFORE <= t < live.NO_NEW_ENTRY_AFTER):
            continue
        if i < 2 or days[i - 1] != d:
            continue
        for direction, flag in (("long", above), ("short", below)):
            if direction not in live.DIRECTIONS or not (flag[i - 1] and flag[i]):
                continue
            if days[i - 2] == d and flag[i - 2]:
                continue
            if (c[i] <= h[i - 1]) if direction == "long" else (c[i] >= l[i - 1]):
                continue
            if live.MAX_SIGNAL_BAR_PTS is not None and h[i] - l[i] > live.MAX_SIGNAL_BAR_PTS:
                break
            if live.VIX_MIN is not None and not (np.isfinite(vix[i]) and vix[i] >= live.VIX_MIN):
                break
            sig = {
                "direction": direction,
                "c1_low": l[i - 1],
                "c1_high": h[i - 1],
                "line": upper[i - 1] if direction == "long" else lower[i - 1],
            }
            stop = live.initial_stop(sig)
            risk = (c[i] - stop) if direction == "long" else (stop - c[i])
            if risk <= 0:
                break
            pos = {
                "long": direction == "long",
                "entry": c[i],
                "stop": stop,
                "stop0": stop,
                "mfe": 0.0,
                "moved": False,
                "t": fr.index[i],
            }
            count += 1
            break
    df = pd.DataFrame(trades)
    df["net"] = df["gross"] - hz.COST_PTS
    return df


def _nf_close(pos, price, stamp, why):
    g = (price - pos["entry"]) if pos["long"] else (pos["entry"] - price)
    return {
        "date": pos["t"].date(),
        "entry_time": pos["t"],
        "exit_time": stamp,
        "dir": "long" if pos["long"] else "short",
        "entry": pos["entry"],
        "exit": price,
        "why": why,
        "gross": g,
    }


# =============================================================================
# BANKNIFTY CPR
# =============================================================================


def banknifty() -> pd.DataFrame:
    cpr = load(scripts("banknifty_cpr_price_action_"), "cpr_live")
    one = pd.read_parquet(ROOT / "workspace/indicators/data/bnf1m/BANKNIFTY.parquet").sort_index()
    one = one[~one.index.duplicated(keep="last")]
    one.index = one.index.tz_convert(IST)
    one = one.between_time("09:15", "15:29")
    groups = dict(list(one.groupby(one.index.date)))
    keys = sorted(groups)
    rows = []
    for k in range(1, len(keys)):
        d = keys[k]
        if d < START or d > END:
            continue
        prev = groups[keys[k - 1]]
        lv = cpr.cpr_levels(prev.high.max(), prev.low.min(), prev.close.iloc[-1])
        g = groups[d]
        b = (
            g.resample("5min", origin=pd.Timestamp(f"{d} 09:15", tz=IST), label="left")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
            .dropna()
        )
        mins = [x.hour * 60 + x.minute for x in b.index]
        r = cpr.replay(mins, b.open.values, b.high.values, b.low.values, b.close.values, lv)
        for t in r["trades"]:
            ex = t.get("exit", b.close.values[-1])
            gross = (ex - t["entry"]) if t["long"] else (t["entry"] - ex)
            rows.append(
                {
                    "date": d,
                    "tier": t["tier"],
                    "setup": t["setup"],
                    "dir": "long" if t["long"] else "short",
                    "entry": t["entry"],
                    "exit": ex,
                    "why": t.get("why", "eod"),
                    "gross": gross,
                    "net": gross - cpr.COST_PTS,
                }
            )
    return pd.DataFrame(rows)


# =============================================================================
# SENSEX cluster breakout (paper book)
# =============================================================================


def sensex() -> pd.DataFrame:
    live = load(scripts("sensex_cluster_breakout_"), "sx_live")
    bars = read5("SENSEX_BSE_INDEX")[["open", "high", "low", "close"]]
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
    vix = read5("INDIAVIX_NSE_INDEX")["close"].reindex(bars.index).ffill(limit=12)
    br = breadth_frame(bars.index, live.BREADTH_CONSTITUENTS)
    cap = live.BOOK_BOX_ATR_MAX.get("boxatr")
    trades = []
    for d, fr in bars.groupby(bars.index.date):
        if d < START or d > END:
            continue
        n = len(fr)
        hi, lo, cl, op = (fr[k].to_numpy(float) for k in ("high", "low", "close", "open"))
        pos, count, start = None, 0, 0
        for k in range(n):
            close_t = (fr.index[k] + pd.Timedelta(minutes=5)).time()
            if pos is not None:
                lng = pos["long"]
                if close_t > live.SQUARE_OFF:  # the 14:55 bar was the last one held
                    trades.append(_sx_close(pos, op[k], "square-off"))
                    pos, start = None, k + 1
                    continue
                stop = pos["stop"]
                if (lo[k] <= stop) if lng else (hi[k] >= stop):
                    fill = min(op[k], stop) if lng else max(op[k], stop)
                    trades.append(_sx_close(pos, fill, "stop" if not pos["moved"] else "trail"))
                    pos, start = None, k + 1
                else:
                    best = (hi[k] - pos["entry"]) if lng else (pos["entry"] - lo[k])
                    pos["mfe"] = max(pos["mfe"], best)
                    lvl = live.trail_level(pos["entry"], pos["mfe"], lng)
                    new = stop if lvl is None else (max(stop, lvl) if lng else min(stop, lvl))
                    if live.LOCK_AT_ATR is not None and pos["mfe"] >= live.LOCK_AT_ATR * pos["atr"]:
                        fl = (
                            pos["entry"] + live.LOCK_ATR * pos["atr"]
                            if lng
                            else pos["entry"] - live.LOCK_ATR * pos["atr"]
                        )
                        new = max(new, fl) if lng else min(new, fl)
                    if new != stop:
                        pos["stop"], pos["moved"] = new, True
                    if close_t == live.SQUARE_OFF:
                        trades.append(_sx_close(pos, cl[k], "square-off"))
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
            if (
                live.MIN_CLOSE_LOCATION is not None
                and live.close_location(fr.iloc[k], side == "long") < live.MIN_CLOSE_LOCATION
            ):
                continue
            stamp = fr.index[k]
            v = vix.at[stamp]
            b = br.loc[stamp]
            breadth = (int(b.adv), int(b.dec)) if b.readable >= live.BREADTH_MIN_STOCKS else None
            ok, _ = live.active_day(
                None if pd.isna(v) else float(v), breadth, live.BOOK_VIX_MIN["boxatr"]
            )
            if not ok:
                continue
            a = float(atr.at[stamp])
            if (
                not math.isfinite(a)
                or a <= 0
                or (cap is not None and sig["high"] - sig["low"] > cap * a)
            ):
                continue
            lng = side == "long"
            entry = cl[k]
            stop = live.initial_index_stop(entry, sig["low"], sig["high"], a, lng)
            if ((entry - stop) if lng else (stop - entry)) <= 0:
                continue
            pos = {
                "long": lng,
                "entry": entry,
                "stop": stop,
                "atr": a,
                "mfe": 0.0,
                "moved": False,
                "t": stamp,
            }
            count += 1
        if pos is not None:
            trades.append(_sx_close(pos, cl[-1], "square-off"))
    df = pd.DataFrame(trades)
    df["net"] = df["gross"] - live.COST_PTS
    return df


def _sx_close(pos, price, why):
    g = (price - pos["entry"]) if pos["long"] else (pos["entry"] - price)
    return {
        "date": pos["t"].date(),
        "entry_time": pos["t"],
        "dir": "long" if pos["long"] else "short",
        "entry": pos["entry"],
        "exit": price,
        "why": why,
        "gross": g,
    }


# =============================================================================
# Sector 920
# =============================================================================


def sector() -> pd.DataFrame:
    live = load(scripts("sector_920_momentum_"), "sec_live")
    hz = load(ROOT / "backtesting/sector920/research/harness.py", "sec_harness")
    bars = hz.load_bars()
    daily = hz.load_daily()
    table = hz.build_table(bars, daily)
    con = sqlite3.connect(ROOT / "db/openalgo.db")
    fno = {
        r[0]
        for r in con.execute(
            "select distinct name from symtoken where exchange='NFO' and instrumenttype='CE'"
        )
    }
    con.close()
    target_pct = live.BOOKS[live.LIVE_BOOK]
    rows = []

    def price_at(sym, day, hhmm):
        e = bars.get(sym)
        if e is None:
            return None, None
        ts, o, h, l, c = hz.day_slice(e, day)  # noqa: E741
        mins = (ts.astype("datetime64[m]").astype("int64") // 60 % 24) * 100 + ts.astype(
            "datetime64[m]"
        ).astype("int64") % 60
        idx = np.flatnonzero(mins == hhmm)
        return (int(idx[0]) if len(idx) else None), (ts, o, h, l, c, mins)

    for day in table["days"]:
        if day < START or day > END:
            continue
        nifty_pct = table["nifty_pct"].get(day)
        if nifty_pct is None or pd.isna(nifty_pct) or nifty_pct == 0:
            continue
        side = 1 if nifty_pct > 0 else -1
        scores = {n: col.get(day) for n, col in table["scores"].items()}
        scores = {n: v for n, v in scores.items() if v is not None and not pd.isna(v)}
        if not scores:
            continue
        sec_name, score = sorted(scores.items(), key=lambda kv: kv[1], reverse=(side == 1))[0]
        if not side * score > 0:
            continue
        pct = table["pct"]
        moves = sorted(
            (
                (float(pct.at[day, s]), s)
                for s in table["sectors"][sec_name]
                if s in pct.columns and not pd.isna(pct.at[day, s])
            ),
            reverse=(side == 1),
        )
        pick = None
        for p, s in moves:
            if side * p > live.MAX_MOVE_PCT:
                continue
            if not side * p > 0:
                break
            if s not in fno:
                continue
            pick = (s, p)
            break
        if pick is None:
            continue
        sym = pick[0]
        i20, sd = price_at(sym, day, 920)
        n20, nd = price_at("NIFTY", day, 920)
        i15, _ = price_at(sym, day, 1015)
        n15, _ = price_at("NIFTY", day, 1015)
        if None in (i20, n20, i15, n15):
            continue
        ts, o, h, l, c, mins = sd  # noqa: E741
        ref, nref = float(o[i20]), float(nd[1][n20])
        stock, nf = float(o[i15]), float(nd[1][n15])
        if side * (stock / ref - 1) * 100 < live.CONFIRM_PCT or side * (nf / nref - 1) * 100 <= 0:
            continue
        lng = side == 1
        stop, target, _ = live.levels(stock, lng, target_pct)
        ex = why = None
        for k in range(i15, len(ts)):
            if mins[k] >= 1515:
                ex, why = float(o[k]), "square-off"
                break
            bo, bh, bl = float(o[k]), float(h[k]), float(l[k])
            if lng:
                if bo <= stop and k > i15:
                    ex, why = bo, "stop"
                elif bl <= stop:
                    ex, why = stop, "stop"
                elif bh >= target:
                    ex, why = target, "target"
            else:
                if bo >= stop and k > i15:
                    ex, why = bo, "stop"
                elif bh >= stop:
                    ex, why = stop, "stop"
                elif bl <= target:
                    ex, why = target, "target"
            if ex is not None:
                break
        if ex is None:
            ex, why = float(c[-1]), "square-off"
        gross = ((ex / stock - 1) if lng else (1 - ex / stock)) * 100
        rows.append(
            {
                "date": day,
                "symbol": sym,
                "sector": sec_name,
                "dir": "long" if lng else "short",
                "entry": stock,
                "exit": ex,
                "why": why,
                "gross": gross,
                "net": gross - live.COST_PCT,
            }
        )
    return pd.DataFrame(rows)


# =============================================================================
# report
# =============================================================================


def year_table(df: pd.DataFrame, unit: str) -> pd.DataFrame:
    rows = []
    for a, b, label in YEARS + [(START, END, "ALL (Nov 2020 - Nov 12 2026)")]:
        t = df[(df["date"] >= a) & (df["date"] <= b)]
        if t.empty:
            rows.append({"year": label, "trades": 0})
            continue
        w, ls = t.net[t.net > 0].sum(), -t.net[t.net <= 0].sum()
        eq = t.net.cumsum()
        rows.append(
            {
                "year": label,
                "trades": len(t),
                "days": t["date"].nunique(),
                "win%": round(100 * (t.net > 0).mean(), 1),
                "PF": round(w / ls, 2) if ls else float("inf"),
                f"net {unit}": round(t.net.sum(), 1),
                f"avg {unit}": round(t.net.mean(), 2),
                f"maxDD {unit}": round((eq - eq.cummax()).min(), 1),
                "best month": _month(t, max),
                "worst month": _month(t, min),
            }
        )
    return pd.DataFrame(rows)


def _month(t, fn):
    m = t.groupby(pd.to_datetime(t["date"]).dt.strftime("%Y-%m")).net.sum()
    k = fn(m.index, key=lambda x: m[x])
    return f"{k} {m[k]:+.1f}"


def main() -> int:
    which = sys.argv[1:] or ["nifty", "banknifty", "sensex", "sector"]
    units = {"nifty": "pts", "banknifty": "pts", "sensex": "pts", "sector": "%"}
    pd.set_option("display.width", 250)
    for name in which:
        print(f"\n######## {name} ########", flush=True)
        df = globals()[name]()
        df.to_csv(OUT / f"{name}_trades.csv", index=False)
        tab = year_table(df, units[name])
        tab.to_csv(OUT / f"{name}_years.csv", index=False)
        print(tab.to_string(index=False), flush=True)
        print("exits:", df["why"].value_counts().to_dict(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
