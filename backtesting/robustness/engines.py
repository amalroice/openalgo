"""Trade-list engines for the robustness battery: NIFTY 2-candle and BANKNIFTY CPR.

Both replay the LIVE scripts in strategies/scripts/ (their config constants and
their pure functions: initial_stop / advance_leg_stop for NIFTY, replay() for
BANKNIFTY), with every tunable exposed as a keyword so the tests can perturb it.

NIFTY 2-candle (live from 2026-11-13): 2 lots (lock leg + EMA21 runner), VIX >= 12,
candle 2 <= 50 pts, both candles the trade's colour, SMA45 slope over 6 bars with the
trade, candle 2 closes >= 20 pts past the line, entries on bars closing 10:15..14:30,
max 2/day, square-off 15:00. Exits fill at the bar close (as live). Cost 3 pts a lot.
Frame: workspace/indicators/backtests/nifty_percent_trail_replay.py (synthetic-volume
VWAP), 2016-10..2026-11-12.

BANKNIFTY CPR (live from 2026-11-11): the script's replay() day by day on 5m BANKNIFTY
(workspace/indicators/data/long2016), stops/targets filled at their level, 5 pts a trade,
2016-01..2026-11-12.

R = net P&L of the trade / initial risk of the trade (all lots), both in index points.
"""

from __future__ import annotations

import dataclasses
import sys
from datetime import time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "yearly_nov"))
import run_all as ra  # noqa: E402

NF_LOT, BNF_LOT = 65, 30

# =============================================================================
# NIFTY
# =============================================================================

_NF = {}


def _nf_base():
    if not _NF:
        hz = ra.load(
            ra.ROOT / "workspace/indicators/backtests/nifty_percent_trail_replay.py", "nf_harness"
        )
        fr = hz.load_frame()
        fr.index = fr.index.tz_convert(ra.IST)
        fr["vix"] = ra.read5("INDIAVIX_NSE_INDEX")["close"].reindex(fr.index).ffill(limit=12)
        _NF["fr"] = fr
        _NF["script"] = ra.scripts("nifty_2candle_vwap_sma45_")
    return _NF["fr"], _NF["script"]


def _nf_module(overrides: dict):
    mod = ra.load(_nf_base()[1], "nf_live_rb")
    mod.log = lambda *_a, **_k: None
    for k, v in overrides.items():
        if not hasattr(mod, k):
            raise KeyError(k)
        setattr(mod, k, v)
    return mod


def nifty(
    sma_period: int = 45,
    slope_bars: int | None = 6,
    past_pts: float | None = 20.0,
    same_color: bool = True,
    cost: float = 3.0,
    start=None,
    end=None,
    random_p: float | None = None,
    seed: int = 0,
    **overrides,
) -> pd.DataFrame:
    """Live NIFTY 2-lot replay. overrides patch script constants (VIX_MIN, BREAKEVEN_AT_PTS, ...).

    random_p: RANDOM-ENTRY benchmark. Signals are ignored; on each flat bar inside the entry
    window (and under MAX_TRADES_PER_DAY) a trade opens with probability random_p in a random
    direction, its stop built the live way from the previous bar as candle 1. Exits unchanged.
    """
    fr0, _ = _nf_base()
    m = _nf_module(overrides)
    fr = fr0.copy()
    if sma_period != 45:
        fr["sma"] = fr["close"].rolling(sma_period).mean()
        fr["upper"] = fr[["vwap", "sma"]].max(axis=1)
        fr["lower"] = fr[["vwap", "sma"]].min(axis=1)
        fr["above"] = fr["low"] > fr["upper"] + m.MIN_CLEARANCE
        fr["below"] = fr["high"] < fr["lower"] - m.MIN_CLEARANCE
        fr = fr.dropna(subset=["sma"])
    ema_span = m.RUNNER_EMA_PERIOD or 21
    ema = fr["close"].ewm(span=ema_span, adjust=False).mean().to_numpy(float)
    o, h, l, c, sma = (fr[k].to_numpy(float) for k in ("open", "high", "low", "close", "sma"))  # noqa: E741
    vix = fr["vix"].to_numpy(float)
    above, below = fr["above"].to_numpy(bool), fr["below"].to_numpy(bool)
    upper, lower = fr["upper"].to_numpy(float), fr["lower"].to_numpy(float)
    days = np.array(fr.index.date)
    close_t = np.array([(t + pd.Timedelta(minutes=5)).time() for t in fr.index])
    start = start or pd.Timestamp("2016-10-01").date()
    end = end or ra.END
    rng = np.random.default_rng(seed)
    rows, pos, count, cur = [], None, 0, None

    def book(leg_name, price):
        leg = pos["legs"].pop(leg_name)
        g = (price - pos["entry"]) if pos["long"] else (pos["entry"] - price)
        pos["gross"] += g * leg["lots"]
        pos["net"] += (g - cost) * leg["lots"]
        if not pos["legs"]:
            rows.append(
                {
                    "date": pos["t"].date(),
                    "entry_time": pos["t"],
                    "dir": "long" if pos["long"] else "short",
                    "entry": pos["entry"],
                    "risk": pos["risk"],
                    "lots": pos["lots"],
                    "gross": pos["gross"],
                    "net": pos["net"],
                    "exit_time": fr.index[i],
                    "vix": vix[pos["i"]],
                }
            )

    for i in range(len(fr)):
        d = days[i]
        if d < start or d > end:
            continue
        if d != cur:
            pos, cur, count = None, d, 0
        t = close_t[i]
        if pos is not None:
            lng, entry = pos["long"], pos["entry"]
            if t >= m.SQUARE_OFF:
                for n in list(pos["legs"]):
                    book(n, c[i])
                pos = None
                continue
            exc = (h[i] - entry) if lng else (entry - l[i])
            pos["mfe"] = max(pos["mfe"], exc)
            for n in [
                n
                for n, lg in pos["legs"].items()
                if ((l[i] <= lg["stop"]) if lng else (h[i] >= lg["stop"]))
            ]:
                book(n, c[i])
            if (
                pos["legs"]
                and m.RUNNER_EMA_PERIOD
                and pos.get("ema_armed")
                and "runner" in pos["legs"]
            ):
                if (c[i] < ema[i]) if lng else (c[i] > ema[i]):
                    book("runner", c[i])
            if pos["legs"]:
                bar = {"open": o[i], "close": c[i], "high": h[i], "low": l[i]}
                for n, lg in pos["legs"].items():
                    lg["stop"] = m.advance_leg_stop(n, float(lg["stop"]), pos, entry, lng, bar)
                if (
                    m.RUNNER_EMA_PERIOD
                    and "runner" in pos["legs"]
                    and not pos.get("ema_armed")
                    and m.BREAKEVEN_AT_PTS is not None
                    and pos["mfe"] >= m.BREAKEVEN_AT_PTS
                ):
                    pos["ema_armed"] = True
                continue
            pos = None
        if t >= m.SQUARE_OFF or count >= m.MAX_TRADES_PER_DAY:
            continue
        if not (m.NO_NEW_ENTRY_BEFORE <= t < m.NO_NEW_ENTRY_AFTER):
            continue
        if i < 2 or days[i - 1] != d:
            continue
        if random_p is not None:
            if rng.random() >= random_p:
                continue
            direction = "long" if rng.random() < 0.5 else "short"
            cands = [direction]
        else:
            cands = ["long", "short"]
        for direction in cands:
            if random_p is None:
                flag = above if direction == "long" else below
                if direction not in m.DIRECTIONS or not (flag[i - 1] and flag[i]):
                    continue
                if days[i - 2] == d and flag[i - 2]:
                    continue
                if (c[i] <= h[i - 1]) if direction == "long" else (c[i] >= l[i - 1]):
                    continue
                if same_color:
                    if direction == "long" and not (c[i - 1] > o[i - 1] and c[i] > o[i]):
                        continue
                    if direction == "short" and not (c[i - 1] < o[i - 1] and c[i] < o[i]):
                        continue
                if slope_bars:
                    s = sma[i] - sma[i - slope_bars]
                    if not ((s > 0) if direction == "long" else (s < 0)):
                        continue
                if past_pts is not None:
                    past = (c[i] - upper[i]) if direction == "long" else (lower[i] - c[i])
                    if not past >= past_pts:
                        continue
                if m.MAX_SIGNAL_BAR_PTS is not None and h[i] - l[i] > m.MAX_SIGNAL_BAR_PTS:
                    break
                if m.VIX_MIN is not None and not (np.isfinite(vix[i]) and vix[i] >= m.VIX_MIN):
                    break
            sig = {
                "direction": direction,
                "c1_low": l[i - 1],
                "c1_high": h[i - 1],
                "line": upper[i - 1] if direction == "long" else lower[i - 1],
            }
            stop = m.initial_stop(sig)
            risk = (c[i] - stop) if direction == "long" else (stop - c[i])
            if risk <= 0:
                break
            legs = m.initial_legs(stop)
            pos = {
                "long": direction == "long",
                "entry": c[i],
                "stop": stop,
                "stop0": stop,
                "risk": risk,
                "mfe": 0.0,
                "t": fr.index[i],
                "i": i,
                "legs": legs,
                "lots": sum(lg["lots"] for lg in legs.values()),
                "gross": 0.0,
                "net": 0.0,
            }
            count += 1
            break
    return _finish(pd.DataFrame(rows))


# =============================================================================
# BANKNIFTY CPR
# =============================================================================

_BNF = {}


def _bnf_base():
    if not _BNF:
        b = ra.read5("BANKNIFTY_NSE_INDEX")[["open", "high", "low", "close"]]
        b = b[
            (b.index.dayofweek < 5)
            & (b.index.time >= dtime(9, 15))
            & (b.index.time <= dtime(15, 25))
        ]
        g0 = b.groupby(b.index.date)
        d = pd.DataFrame(
            {"high": g0.high.max(), "low": g0.low.min(), "close": g0.close.last(), "n": g0.size()}
        )
        d = d[d.n >= 40]
        groups = dict(list(g0))
        vix = ra.read5("INDIAVIX_NSE_INDEX")["close"].reindex(b.index).ffill(limit=12)
        days = list(d.index)
        pre = []
        for prev, day in zip(days[:-1], days[1:], strict=True):
            g = groups[day]
            pre.append(
                (
                    day,
                    d.loc[prev, ["high", "low", "close"]].to_numpy(float),
                    (g.index.hour * 60 + g.index.minute).to_numpy(),
                    g.open.to_numpy(float),
                    g.high.to_numpy(float),
                    g.low.to_numpy(float),
                    g.close.to_numpy(float),
                    g.index,
                    vix.reindex(g.index).to_numpy(float),
                )
            )
        _BNF["pre"] = pre
        _BNF["script"] = ra.scripts("banknifty_cpr_price_action_")
    return _BNF["pre"], _BNF["script"]


def bnf_module(
    overrides: dict | None = None, tier_scale: dict | None = None, tier_set: dict | None = None
):
    """Fresh copy of the live BNF script with constants patched.

    tier_scale: {field: factor} multiplies that Tier field in EVERY tier (ints rounded, min 1).
    tier_set:   {(tier_name, field): value} sets one field on one tier.
    """
    _, script = _bnf_base()
    mod = ra.load(script, "bnf_live_rb")
    for k, v in (overrides or {}).items():
        if not hasattr(mod, k):
            raise KeyError(k)
        setattr(mod, k, v)
    tiers = []
    for tr in mod.TIERS:
        ch = {}
        for f, fac in (tier_scale or {}).items():
            v = getattr(tr, f)
            if v is None:
                continue
            ch[f] = max(1, round(v * fac)) if isinstance(v, int) else v * fac
        for (tn, f), v in (tier_set or {}).items():
            if tn == tr.name:
                ch[f] = v
        tiers.append(dataclasses.replace(tr, **ch))
    mod.TIERS = tuple(tiers)
    return mod


def bnf(
    cost: float = 5.0,
    start=None,
    end=None,
    overrides: dict | None = None,
    tier_scale: dict | None = None,
    tier_set: dict | None = None,
    drop_tiers=(),
) -> pd.DataFrame:
    pre, _ = _bnf_base()
    m = bnf_module(overrides, tier_scale, tier_set)
    tiers = tuple(t for t in m.TIERS if t.name not in drop_tiers)
    start = start or pd.Timestamp("2016-01-01").date()
    end = end or ra.END
    rows = []
    for day, (ph, pl, pc), mins, o, h, l, c, idx, vix in pre:  # noqa: E741
        if day < start or day > end:
            continue
        lv = m.cpr_levels(ph, pl, pc)
        r = m.replay(mins, o, h, l, c, lv, tiers=tiers)
        for t in r["trades"]:
            if "exit_i" not in t:
                t.update(exit_i=len(c) - 1, exit=c[-1], why="eod")
            g = (t["exit"] - t["entry"]) if t["long"] else (t["entry"] - t["exit"])
            rows.append(
                {
                    "date": day,
                    "entry_time": idx[t["i"]],
                    "dir": "long" if t["long"] else "short",
                    "entry": t["entry"],
                    "risk": t["risk"],
                    "lots": 1,
                    "gross": g,
                    "net": g - cost,
                    "exit_time": idx[t["exit_i"]],
                    "vix": vix[t["i"]],
                    "tier": t["tier"],
                    "setup": t["setup"],
                    "why": t["why"],
                    "target": t["target"],
                }
            )
    return _finish(pd.DataFrame(rows))


def _finish(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df["R"] = df["net"] / (df["risk"] * df["lots"])
    df["R_gross"] = df["gross"] / (df["risk"] * df["lots"])
    return df.sort_values("entry_time").reset_index(drop=True)
