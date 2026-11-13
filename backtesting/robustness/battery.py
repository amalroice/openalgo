"""Robustness battery for NIFTY 2-candle and BANKNIFTY CPR (live rules), 15 checks.

Phases (results cached in backtesting/robustness/out/):
    params   one-at-a-time +-10 / +-20 % perturbation of every tunable (multiprocess)
    wf       anchored walk-forward, re-picking a small grid each calendar year
    random   random-entry benchmark with the live exits and sizing
    report   all statistics on the live trade lists + the three cached phases -> out/report.json

Run:  uv run python backtesting/robustness/battery.py <phase> [nifty|bnf]
"""

from __future__ import annotations

import json
import math
import sys
from datetime import time as dtime
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engines as E  # noqa: E402

OUT = HERE / "out"
OUT.mkdir(exist_ok=True)
# Rs per index point per lot, OPTION-equivalent (ITM option delta ~0.6 NIFTY / ~0.65 BANKNIFTY)
RS = {"nifty": E.NF_LOT * 0.60, "bnf": E.BNF_LOT * 0.65}
COST = {"nifty": 3.0, "bnf": 5.0}
FACTORS = (0.8, 0.9, 1.1, 1.2)


def run(strat: str, kw: dict) -> pd.DataFrame:
    return E.nifty(**kw) if strat == "nifty" else E.bnf(**kw)


# ----------------------------------------------------------------------------- parameter space


def _t(h, m):
    return dtime(h, m)


def _shift(t: dtime, minutes: int) -> dtime:
    x = t.hour * 60 + t.minute + minutes
    return dtime(x // 60, x % 60)


def nifty_params() -> list[tuple[str, list[tuple[str, dict]]]]:
    """(parameter, [(label, engine kwargs)]) at roughly -20/-10/+10/+20 %."""
    out = []
    ints = {
        "sma_period": (45, (36, 40, 50, 54)),
        "slope_bars": (6, (4, 5, 7, 8)),
        "RUNNER_EMA_PERIOD": (21, (17, 19, 23, 25)),
    }
    for k, (base, vals) in ints.items():
        out.append((f"{k}={base}", [(str(v), {k: v}) for v in vals]))
    floats = {
        "past_pts": 20.0,
        "VIX_MIN": 12.0,
        "MAX_SIGNAL_BAR_PTS": 50.0,
        "STOP_BUFFER": 10.0,
        "LINE_PROXIMITY": 25.0,
        "BREAKEVEN_AT_PTS": 40.0,
        "BREAKEVEN_LOCK_PTS": 15.0,
        "PUSH_BODY_PTS": 30.0,
        "PUSH_BUFFER_PTS": 10.0,
        "TRAIL_DIST_PCT": 0.004,
        "TRAIL_STEP_PCT": 0.001,
    }
    for k, base in floats.items():
        out.append((f"{k}={base:g}", [(f"{base * f:g}", {k: base * f}) for f in FACTORS]))
    # entry window: 60 min after the open (10:15) and 315 min (14:30), +-10/20 % on 5m bars
    out.append(
        (
            "NO_NEW_ENTRY_BEFORE=10:15",
            [
                (v.strftime("%H:%M"), {"NO_NEW_ENTRY_BEFORE": v})
                for v in (_t(10, 5), _t(10, 10), _t(10, 20), _t(10, 25))
            ],
        )
    )
    out.append(
        (
            "NO_NEW_ENTRY_AFTER=14:30",
            [
                (v.strftime("%H:%M"), {"NO_NEW_ENTRY_AFTER": v})
                for v in (_t(13, 30), _t(14, 0), _t(14, 45), _t(14, 55))
            ],
        )
    )
    out.append(
        (
            "MAX_TRADES_PER_DAY=2",
            [("1", {"MAX_TRADES_PER_DAY": 1}), ("3", {"MAX_TRADES_PER_DAY": 3})],
        )
    )
    # on/off filters (not a +-% test; shows what each filter contributes)
    out.append(
        (
            "filters off",
            [
                ("same_color off", {"same_color": False}),
                ("slope off", {"slope_bars": None}),
                ("past line off", {"past_pts": None}),
                ("VIX off", {"VIX_MIN": None}),
                ("candle-2 cap off", {"MAX_SIGNAL_BAR_PTS": None}),
                ("push off", {"PUSH_BODY_PTS": None}),
            ],
        )
    )
    return out


def bnf_params() -> list[tuple[str, list[tuple[str, dict]]]]:
    out = []
    for f in (
        "touch",
        "wick",
        "close_half",
        "stop_buf",
        "min_rr",
        "bo_margin",
        "bo_body",
        "bo_buf",
        "bo_rr",
        "max_risk",
        "entry_body",
    ):
        out.append(
            (f"tier.{f} (all tiers)", [(f"x{x:g}", {"tier_scale": {f: x}}) for x in FACTORS])
        )
    out.append(
        (
            "tier.retest_bars=3",
            [
                (
                    str(v),
                    {"tier_set": {(tn, "retest_bars"): v for tn in ("strict", "wide", "extra")}},
                )
                for v in (2, 4)
            ],
        )
    )
    out.append(
        (
            "INSIDE_TOL=0.0015",
            [(f"{0.0015 * x:g}", {"overrides": {"INSIDE_TOL": 0.0015 * x}}) for x in FACTORS],
        )
    )
    out.append(
        (
            "NO_NEW_ENTRY_BEFORE=09:20",
            [
                (v.strftime("%H:%M"), {"overrides": {"NO_NEW_ENTRY_BEFORE": v}})
                for v in (_t(9, 15), _t(9, 25), _t(9, 30))
            ],
        )
    )
    out.append(
        (
            "NO_NEW_ENTRY_AFTER=14:30",
            [
                (v.strftime("%H:%M"), {"overrides": {"NO_NEW_ENTRY_AFTER": v}})
                for v in (_t(13, 55), _t(14, 10), _t(14, 50))
            ],
        )
    )
    out.append(
        (
            "extra.last_entry=14:55",
            [
                (v.strftime("%H:%M"), {"tier_set": {("extra", "last_entry"): v}})
                for v in (_t(14, 0), _t(14, 30), _t(15, 0))
            ],
        )
    )
    out.append(
        (
            "no-entry window 13:00-13:55",
            [
                (lab, {"overrides": {"NO_ENTRY_WINDOWS": w}})
                for lab, w in (
                    ("12:45-13:40", ((_t(12, 45), _t(13, 40)),)),
                    ("12:50-13:45", ((_t(12, 50), _t(13, 45)),)),
                    ("13:10-14:05", ((_t(13, 10), _t(14, 5)),)),
                    ("13:15-14:10", ((_t(13, 15), _t(14, 10)),)),
                )
            ],
        )
    )
    out.append(
        (
            "SQUARE_OFF=15:10",
            [
                (v.strftime("%H:%M"), {"overrides": {"SQUARE_OFF": v}})
                for v in (_t(14, 45), _t(15, 0), _t(15, 20))
            ],
        )
    )
    out.append(
        (
            "MAX_TRADES_PER_DAY=2",
            [
                ("1", {"overrides": {"MAX_TRADES_PER_DAY": 1}}),
                ("3", {"overrides": {"MAX_TRADES_PER_DAY": 3}}),
            ],
        )
    )
    out.append(
        (
            "rules off",
            [
                ("no 13:00 window", {"overrides": {"NO_ENTRY_WINDOWS": ()}}),
                ("no stop-after-loss", {"overrides": {"STOP_AFTER_LOSS": False}}),
                ("trade all zones", {"overrides": {"SKIP_ZONES": frozenset()}}),
                ("strict tier only", {"drop_tiers": ("wide", "extra")}),
                ("no extra tier", {"drop_tiers": ("extra",)}),
            ],
        )
    )
    return out


# ----------------------------------------------------------------------------- statistics


def pf(x: pd.Series) -> float:
    w, lo = x[x > 0].sum(), -x[x <= 0].sum()
    return float(w / lo) if lo > 0 else float("inf")


def summary(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"n": 0}
    r = t.R
    eq = r.cumsum()
    return {
        "n": int(len(t)),
        "win": round(100 * float((r > 0).mean()), 1),
        "PF": round(pf(r), 3),
        "PF_pts": round(pf(t.net), 3),
        "meanR": round(float(r.mean()), 4),
        "netR": round(float(r.sum()), 1),
        "net_pts": round(float(t.net.sum()), 0),
        "maxDD_R": round(float((eq - eq.cummax()).min()), 1),
    }


def _worker(args):
    strat, label, kw = args
    t = run(strat, kw)
    return label, t[["date", "entry_time", "R", "net", "gross", "risk", "lots"]]


def pool_map(jobs):
    with Pool(4) as p:
        return p.map(_worker, jobs, chunksize=1)


def phase_params(strat: str, base: dict | None = None, tag: str = ""):
    """base: kwargs applied to every run (a variant of the live rule); a perturbed key wins."""
    space = nifty_params() if strat == "nifty" else bnf_params()
    jobs = [(strat, f"{p}|{lab}", {**(base or {}), **kw}) for p, vs in space for lab, kw in vs]
    res = pool_map(jobs)
    out = {lab: summary(t) for lab, t in res}
    (OUT / f"{strat}{tag}_params.json").write_text(json.dumps(out, indent=1, default=str))


def wf_grid(strat: str) -> dict[str, dict]:
    g = {}
    if strat == "nifty":
        for sb in (None, 3, 6, 12):
            for pp in (None, 10.0, 20.0, 30.0):
                for be in (25.0, 40.0, 55.0):
                    g[f"slope={sb} past={pp} be={be:g}"] = {
                        "slope_bars": sb,
                        "past_pts": pp,
                        "BREAKEVEN_AT_PTS": be,
                    }
    else:
        for eb in (0.2, 0.3, 0.5, None):
            for sbuf in (0.002, 0.003, 0.004):
                for win in (True, False):
                    for sal in (True, False):
                        kw = {"tier_set": {("wide", "stop_buf"): sbuf}, "overrides": {}}
                        if eb is None:
                            kw["drop_tiers"] = ("extra",)
                        else:
                            kw["tier_set"][("extra", "entry_body")] = eb
                        if not win:
                            kw["overrides"]["NO_ENTRY_WINDOWS"] = ()
                        kw["overrides"]["STOP_AFTER_LOSS"] = sal
                        g[
                            f"extra_body={eb} wide_stop={sbuf} window={win} stop_after_loss={sal}"
                        ] = kw
    return g


def phase_wf(strat: str, base: dict | None = None, tag: str = ""):
    g = wf_grid(strat)
    res = dict(pool_map([(strat, k, {**(base or {}), **kw}) for k, kw in g.items()]))
    pd.to_pickle(res, OUT / f"{strat}{tag}_wf_runs.pkl")


def phase_random(strat: str, runs: int = 300):
    live = pd.read_pickle(HERE / f"{'nf' if strat == 'nifty' else 'bnf'}_live.pkl")
    if strat == "nifty":
        # calibrate the entry probability so the random runs take about as many trades
        p = 0.0055
        for _ in range(3):
            n = len(E.nifty(random_p=p, seed=999))
            p *= len(live) / n
        res = pool_map([("nifty", s, {"random_p": p, "seed": s}) for s in range(runs)])
        rows = [{"seed": s, **summary(t)} for s, t in res]
    else:
        rows = bnf_random(live, runs=2000)
    pd.DataFrame(rows).to_csv(OUT / f"{strat}_random.csv", index=False)


def bnf_random(live: pd.DataFrame, runs: int) -> list[dict]:
    """Random BANKNIFTY entries: random day, an entry minute drawn from the live entry times,
    random side; stop and target distances drawn (as a pair, % of price) from the live trades;
    the live exit engine's fills (stop/target at level, gap at the open), square-off 15:10."""
    pre, _ = E._bnf_base()
    m = E.bnf_module()
    sq = m.SQUARE_OFF.hour * 60 + m.SQUARE_OFF.minute - m.BAR_MINUTES
    rng = np.random.default_rng(7)
    risk_pct = (live.risk / live.entry).to_numpy()
    rew_pct = ((live.target - live.entry).abs() / live.entry).to_numpy()
    ent_min = np.array([t.hour * 60 + t.minute for t in live.entry_time])
    days = [x for x in pre if x[0] >= pd.Timestamp("2016-01-01").date()]
    rows = []
    for s in range(runs):
        R = []
        for _ in range(len(live)):
            day, _lv, mins, o, h, l, c, _idx, _v = days[rng.integers(len(days))]  # noqa: E741 (l = candle lows)
            pos = np.searchsorted(mins, ent_min[rng.integers(len(ent_min))])
            if pos >= len(c) - 1:
                continue
            k = rng.integers(len(risk_pct))
            long = rng.random() < 0.5
            e = c[pos]
            risk = risk_pct[k] * e
            stop = e - risk if long else e + risk
            tgt = e + rew_pct[k] * e if long else e - rew_pct[k] * e
            ex = c[-1]
            for i in range(pos + 1, len(c)):
                if long:
                    if o[i] <= stop:
                        ex = o[i]
                        break  # noqa: E702
                    if l[i] <= stop:
                        ex = stop
                        break  # noqa: E702
                    if o[i] >= tgt:
                        ex = o[i]
                        break  # noqa: E702
                    if h[i] >= tgt:
                        ex = tgt
                        break  # noqa: E702
                else:
                    if o[i] >= stop:
                        ex = o[i]
                        break  # noqa: E702
                    if h[i] >= stop:
                        ex = stop
                        break  # noqa: E702
                    if o[i] <= tgt:
                        ex = o[i]
                        break  # noqa: E702
                    if l[i] <= tgt:
                        ex = tgt
                        break  # noqa: E702
                if mins[i] >= sq:
                    ex = c[i]
                    break
            g = (ex - e) if long else (e - ex)
            R.append((g - COST["bnf"]) / risk)
        R = pd.Series(R)
        eq = R.cumsum()
        rows.append(
            {
                "seed": s,
                "n": len(R),
                "win": 100 * float((R > 0).mean()),
                "PF": pf(R),
                "meanR": float(R.mean()),
                "netR": float(R.sum()),
                "maxDD_R": float((eq - eq.cummax()).min()),
            }
        )
    return rows


# ----------------------------------------------------------------------------- report


def daily_series(t: pd.DataFrame, strat: str, start, end) -> pd.Series:
    """Daily Rs P&L (option-equivalent) over every session, 0 on days without a trade."""
    sess = pd.Index(
        sorted(
            set(
                E.ra.read5(
                    "NIFTY_NSE_INDEX" if strat == "nifty" else "BANKNIFTY_NSE_INDEX"
                ).index.date
            )
        )
    )
    sess = sess[(sess >= start) & (sess <= end)]
    return (t.groupby("date").net.sum() * RS[strat]).reindex(sess, fill_value=0.0)


def max_dd_duration(eq: pd.Series) -> tuple[int, str, str]:
    peak = eq.cummax()
    under = eq < peak
    best, cur_start, best_span = 0, None, ("", "")
    for d, u in under.items():
        if u and cur_start is None:
            cur_start = d
        if not u and cur_start is not None:
            span = (pd.Timestamp(d) - pd.Timestamp(cur_start)).days
            if span > best:
                best, best_span = span, (str(cur_start), str(d))
            cur_start = None
    if cur_start is not None:
        span = (pd.Timestamp(eq.index[-1]) - pd.Timestamp(cur_start)).days
        if span > best:
            best, best_span = span, (str(cur_start), "still open")
    return best, *best_span


def longest_streak(x: pd.Series) -> int:
    best = cur = 0
    for v in x:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def regimes(t: pd.DataFrame, strat: str) -> dict:
    sym = "NIFTY_NSE_INDEX" if strat == "nifty" else "BANKNIFTY_NSE_INDEX"
    dc = E.ra.read5(sym)["close"]
    dc = dc.groupby(dc.index.date).last()
    ret60 = (dc / dc.shift(60) - 1).shift(1)  # trailing 60 sessions, known at the open
    vx = E.ra.read5("INDIAVIX_NSE_INDEX")["close"]
    vx = vx.groupby(vx.index.date).last().shift(1)
    lo_v, hi_v = vx.quantile([1 / 3, 2 / 3])
    x = t.copy()
    x["ret60"] = x.date.map(ret60)
    x["trend"] = np.where(
        x.ret60 > 0.05,
        "bull (60d > +5%)",
        np.where(x.ret60 < -0.05, "bear (60d < -5%)", "sideways"),
    )
    x["vixd"] = x.date.map(vx)
    x["vol"] = np.where(
        x.vixd < lo_v,
        f"low VIX (<{lo_v:.1f})",
        np.where(x.vixd > hi_v, f"high VIX (>{hi_v:.1f})", "mid VIX"),
    )
    out = {}
    for col in ("trend", "vol"):
        for k, g in x.groupby(col):
            out[f"{col}: {k}"] = {**summary(g), "Rs": round(float(g.net.sum() * RS[strat]))}
    return out


def monte_carlo(r: pd.Series, rs: pd.Series, n_sims=10000, seed=1) -> dict:
    rng = np.random.default_rng(seed)
    n = len(r)
    R, X = r.to_numpy(), rs.to_numpy()
    boot = rng.integers(0, n, size=(n_sims, n))
    pathR = np.cumsum(R[boot], axis=1)
    pathX = np.cumsum(X[boot], axis=1)
    ddR = (pathR - np.maximum.accumulate(pathR, axis=1)).min(axis=1)
    ddX = (pathX - np.maximum.accumulate(np.maximum(pathX, 0), axis=1)).min(axis=1)
    shuf = np.array([np.cumsum(rng.permutation(X)) for _ in range(2000)])
    ddS = (shuf - np.maximum.accumulate(np.maximum(shuf, 0), axis=1)).min(axis=1)
    out = {
        "sims": n_sims,
        "trades_per_sim": n,
        "pct_profitable": round(100 * float((pathR[:, -1] > 0).mean()), 2),
        "final_R_p5_p50_p95": [
            round(float(v), 1) for v in np.percentile(pathR[:, -1], [5, 50, 95])
        ],
        "maxDD_R_p50_p95_p99": [round(float(v), 1) for v in np.percentile(ddR, [50, 5, 1])],
        "maxDD_Rs_p50_p95_p99": [round(float(v)) for v in np.percentile(ddX, [50, 5, 1])],
        "shuffle_maxDD_Rs_p50_p95": [round(float(v)) for v in np.percentile(ddS, [50, 5])],
    }
    # risk of ruin: a drawdown from the start or any peak that reaches the capital
    out["ruin_pct_by_capital_Rs"] = {
        f"{c:,}": round(100 * float((ddX <= -c).mean()), 2)
        for c in (50_000, 100_000, 150_000, 200_000, 300_000, 500_000)
    }
    # band for the first 50 / 100 live trades (cumulative R)
    for k in (50, 100):
        b = np.cumsum(R[rng.integers(0, n, size=(n_sims, k))], axis=1)[:, -1]
        out[f"first_{k}_trades_cumR_p5_p50_p95"] = [
            round(float(v), 1) for v in np.percentile(b, [5, 50, 95])
        ]
        out[f"first_{k}_trades_P(loss)"] = round(100 * float((b < 0).mean()), 1)
    return out


def walk_forward(strat: str, tag: str = "") -> dict:
    p = OUT / f"{strat}{tag}_wf_runs.pkl"
    if not p.exists():
        return {}
    res = pd.read_pickle(p)
    first_year = 2019 if strat == "nifty" else 2019
    rows, oos_all, live_key = [], [], None
    for k in res:
        if strat == "nifty" and k == "slope=6 past=20.0 be=40":
            live_key = k
        if (
            strat == "bnf"
            and k == "extra_body=0.3 wide_stop=0.003 window=True stop_after_loss=True"
        ):
            live_key = k
    for y in range(first_year, 2027):
        a = pd.Timestamp(f"{y}-01-01").date()
        b = pd.Timestamp(f"{y}-12-31").date()
        is_scores = {}
        for k, t in res.items():
            s = t[t.date < a]
            if len(s) < 50:
                continue
            is_scores[k] = (float(s.R.sum()), s)
        best = max(is_scores, key=lambda k: is_scores[k][0])
        s_is = is_scores[best][1]
        s_oos = res[best][(res[best].date >= a) & (res[best].date <= b)]
        yrs_is = (pd.Timestamp(a) - pd.Timestamp(s_is.date.min())).days / 365.25
        yrs_oos = min(1.0, (pd.Timestamp(min(b, E.ra.END)) - pd.Timestamp(a)).days / 365.25)
        rows.append(
            {
                "oos_year": y,
                "picked": best,
                "IS_R_per_yr": round(s_is.R.sum() / yrs_is, 1),
                "IS_PF": round(pf(s_is.R), 2),
                "OOS_R_per_yr": round(s_oos.R.sum() / yrs_oos, 1),
                "OOS_PF": round(pf(s_oos.R), 2),
                "OOS_n": len(s_oos),
                "live_rule_OOS_R": round(
                    float(
                        res[live_key][(res[live_key].date >= a) & (res[live_key].date <= b)].R.sum()
                    ),
                    1,
                ),
            }
        )
        oos_all.append(s_oos)
    df = pd.DataFrame(rows)
    oos = pd.concat(oos_all)
    eff = df.OOS_R_per_yr.sum() / df.IS_R_per_yr.sum()
    # grid neighbourhood (plateau view) on the full sample
    grid = {k: summary(t) for k, t in res.items()}
    return {
        "steps": df.to_dict("records"),
        "WFE_OOS_over_IS": round(float(eff), 2),
        "stitched_OOS": summary(oos),
        "stitched_OOS_PF": round(pf(oos.R), 2),
        "grid_full_sample": grid,
        "grid_PF_range": [
            round(min(v["PF"] for v in grid.values()), 2),
            round(max(v["PF"] for v in grid.values()), 2),
        ],
        "grid_share_PF_gt_1.1": round(float(np.mean([v["PF"] > 1.1 for v in grid.values()])), 2),
    }


def report(strat: str, trades: pd.DataFrame | None = None, tag: str = "") -> dict:
    t = (
        trades.copy()
        if trades is not None
        else pd.read_pickle(HERE / f"{'nf' if strat == 'nifty' else 'bnf'}_live.pkl")
    )
    rs_mult = RS[strat]
    t["Rs"] = t.net * rs_mult
    start, end = t.date.min(), t.date.max()
    years = (pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25
    r = t.R
    n = len(t)
    w, lo = r[r > 0], r[r <= 0]
    winp, lossp = len(w) / n, len(lo) / n
    payoff = w.mean() / -lo.mean()
    cost_R = COST[strat] / t.risk  # per-lot cost over per-lot risk
    exp_R = winp * w.mean() - lossp * -lo.mean()
    be_wr = 1 / (1 + payoff)
    tstat = r.mean() / (r.std(ddof=1) / math.sqrt(n))
    daily = daily_series(t, strat, start, end)
    sharpe = daily.mean() / daily.std(ddof=1) * math.sqrt(252)
    down = math.sqrt((np.minimum(daily, 0) ** 2).mean())
    sortino = daily.mean() / down * math.sqrt(252)
    eq_rs = daily.cumsum()
    dd_rs = float((eq_rs - eq_rs.cummax()).min())
    mar = (daily.sum() / years) / -dd_rs
    dur_days, dur_from, dur_to = max_dd_duration(eq_rs)
    eqR = r.cumsum()
    # trades in the deepest R drawdown
    under, best_tr, cur = (eqR < eqR.cummax()).to_numpy(), 0, 0
    for u in under:
        cur = cur + 1 if u else 0
        best_tr = max(best_tr, cur)

    # 4 / 8: chronological 70/30 split
    cut = pd.Timestamp(start) + 0.7 * (pd.Timestamp(end) - pd.Timestamp(start))
    is_, oos = t[pd.to_datetime(t.date) < cut], t[pd.to_datetime(t.date) >= cut]
    split = {
        "cut_date": str(cut.date()),
        "IS": summary(is_),
        "OOS": summary(oos),
        "OOS_PF_over_IS_PF": round(pf(oos.R) / pf(is_.R), 2),
        "OOS_R_per_yr_over_IS_R_per_yr": round(
            (oos.R.sum() / ((pd.Timestamp(end) - cut).days / 365.25))
            / (is_.R.sum() / ((cut - pd.Timestamp(start)).days / 365.25)),
            2,
        ),
    }
    recent = {}
    for lab, a in (
        ("last 2y", pd.Timestamp(end) - pd.DateOffset(years=2)),
        ("last 1y", pd.Timestamp(end) - pd.DateOffset(years=1)),
    ):
        s = t[pd.to_datetime(t.date) > a]
        recent[lab] = {
            **summary(s),
            "t": round(float(s.R.mean() / (s.R.std(ddof=1) / math.sqrt(len(s)))), 2),
        }

    # 9: costs (cost is linear: net = gross - cost x lots)
    def at_cost(mult):
        net = t.gross - mult * COST[strat] * t.lots
        R = net / (t.risk * t.lots)
        return {
            "meanR": round(float(R.mean()), 4),
            "PF": round(pf(R), 3),
            "net_Rs": round(float(net.sum() * rs_mult)),
        }

    gross_per_lot = (t.gross / t.lots).mean()
    # break-even cost per lot in R terms: solve mean((gross/lots - c)/risk) = 0
    be_cost = float((t.gross / t.lots / t.risk).mean() / (1 / t.risk).mean())

    # 11: consistency
    yr = t.groupby(pd.to_datetime(t.date).dt.year).agg(
        n=("R", "size"), netR=("R", "sum"), Rs=("Rs", "sum")
    )
    yr["PF"] = t.groupby(pd.to_datetime(t.date).dt.year).R.apply(pf)
    q = t.groupby(pd.to_datetime(t.date).dt.to_period("Q")).R.sum()
    mo = t.groupby(pd.to_datetime(t.date).dt.to_period("M")).Rs.sum()
    srt = r.sort_values(ascending=False)
    conc = {
        "pct_years_profitable": round(100 * float((yr.netR > 0).mean()), 1),
        "pct_quarters_profitable": round(100 * float((q > 0).mean()), 1),
        "pct_months_profitable": round(100 * float((mo > 0).mean()), 1),
        "netR_without_top5": round(float(srt.iloc[5:].sum()), 1),
        "netR_without_top10": round(float(srt.iloc[10:].sum()), 1),
        "PF_without_top10": round(pf(srt.iloc[10:]), 2),
        "best_month": str(mo.idxmax()),
        "best_month_share_of_total": round(float(mo.max() / mo.sum()), 3),
        "years": {
            str(k): {
                "n": int(v.n),
                "netR": round(v.netR, 1),
                "Rs": round(v.Rs),
                "PF": round(v.PF, 2),
            }
            for k, v in yr.iterrows()
        },
    }

    # 13: streaks
    exp_streak = math.log(n) / -math.log(lossp)

    pj = OUT / f"{strat}{tag}_params.json"
    params = json.loads(pj.read_text()) if pj.exists() else {}
    rnd = (
        pd.read_csv(OUT / f"{strat}_random.csv") if (OUT / f"{strat}_random.csv").exists() else None
    )
    random_bm = None
    if rnd is not None:
        random_bm = {
            "runs": len(rnd),
            "random_trades_mean": round(float(rnd.n.mean())),
            "random_meanR_p50_p95_p99": [
                round(float(v), 4) for v in rnd.meanR.quantile([0.5, 0.95, 0.99])
            ],
            "random_PF_p50_p95": [round(float(v), 3) for v in rnd.PF.quantile([0.5, 0.95])],
            "strategy_meanR": round(float(r.mean()), 4),
            "pct_random_beaten_on_meanR": round(100 * float((rnd.meanR < r.mean()).mean()), 1),
            "pct_random_beaten_on_netR": round(100 * float((rnd.netR < r.sum()).mean()), 1),
        }

    return {
        "strategy": strat,
        "period": [str(start), str(end)],
        "years": round(years, 2),
        "rs_per_pt_per_lot_option_equiv": rs_mult,
        "cost_pts_per_lot": COST[strat],
        "1_sample": {
            "trades": n,
            "trades_per_year": round(n / years, 1),
            "trades_per_week": round(n / years / 52, 2),
        },
        "2_expectancy": {
            "win%": round(100 * winp, 1),
            "avg_win_R": round(float(w.mean()), 3),
            "avg_loss_R": round(float(-lo.mean()), 3),
            "expectancy_R": round(float(exp_R), 4),
            "avg_cost_R": round(float(cost_R.mean()), 4),
            "expectancy_over_cost": round(float(exp_R / cost_R.mean()), 2),
            "expectancy_Rs_per_trade": round(float(t.Rs.mean())),
            "median_risk_pts": round(float(t.risk.median()), 1),
        },
        "3_breakeven_wr": {
            "payoff": round(float(payoff), 3),
            "breakeven_win%": round(100 * be_wr, 1),
            "margin_pts": round(100 * (winp - be_wr), 1),
        },
        "4_profit_factor": {
            "all": round(pf(r), 3),
            "IS_70": round(pf(is_.R), 3),
            "OOS_30": round(pf(oos.R), 3),
        },
        "5_t_stat": {
            "t": round(float(tstat), 2),
            "mean_R": round(float(r.mean()), 4),
            "std_R": round(float(r.std(ddof=1)), 3),
            "recent": recent,
        },
        "6_risk_adjusted": {
            "sharpe_ann": round(float(sharpe), 2),
            "sortino_ann": round(float(sortino), 2),
            "net_Rs": round(float(daily.sum())),
            "Rs_per_year": round(float(daily.sum() / years)),
            "maxDD_Rs": round(dd_rs),
            "MAR": round(float(mar), 2),
        },
        "7_monte_carlo": monte_carlo(r, t.Rs),
        "8_oos": split,
        "8_walk_forward": walk_forward(strat, tag),
        "9_costs": {
            "x1": at_cost(1),
            "x2": at_cost(2),
            "x3": at_cost(3),
            "gross_pts_per_lot_per_trade": round(float(gross_per_lot), 2),
            "breakeven_cost_pts_per_lot": round(be_cost, 2),
            "breakeven_over_actual": round(be_cost / COST[strat], 2),
        },
        "10_params": params,
        "11_consistency": conc,
        "12_regimes": regimes(t, strat),
        "13_streaks": {
            "expected_worst_losing_streak": round(exp_streak, 1),
            "actual_worst_losing_streak": longest_streak(r <= 0),
            "longest_DD_calendar_days": dur_days,
            "longest_DD_from": dur_from,
            "longest_DD_to": dur_to,
            "longest_DD_trades": int(best_tr),
        },
        "14_random": random_bm,
    }


if __name__ == "__main__":
    phase = sys.argv[1]
    strats = sys.argv[2:] or ["nifty", "bnf"]
    for s in strats:
        if phase == "params":
            phase_params(s)
        elif phase == "wf":
            phase_wf(s)
        elif phase == "random":
            phase_random(s)
        elif phase == "report":
            rep = report(s)
            (OUT / f"{s}_report.json").write_text(json.dumps(rep, indent=1, default=str))
            print(
                json.dumps(
                    {k: v for k, v in rep.items() if k not in ("10_params", "8_walk_forward")},
                    indent=1,
                    default=str,
                )
            )
        print(f"{phase} {s} done", flush=True)
