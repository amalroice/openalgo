"""Is the edge real? Six tests on the live NIFTY 2-candle and BANKNIFTY CPR rules.

1. Permutation (target shuffling): inside every session the 5m bars after the
   first are shuffled (each bar keeps its own gap, high, low and close relative
   to its open), so every day keeps its open, close and volatility but loses its
   intraday order. The live rules are re-run on each shuffled history. Also read:
   the random-entry runs from battery.py (same exits, random entry timing).
2. PBO via CSCV (Bailey, Borwein, Lopez de Prado, Zhu): the 48-variant grid from
   battery.py, daily P&L split into 16 blocks, all 12,870 half/half splits.
3. Deflated Sharpe Ratio (Bailey, Lopez de Prado), for the 48-variant grid and
   for larger trial counts standing in for the whole research history.
4. White's Reality Check and Hansen's SPA against "no trade", stationary
   bootstrap over the 48 variants' daily P&L.
5. Walk-forward with rolling re-optimisation: pick the best grid variant on the
   trailing 3 years (daily Sharpe), trade it the next 6 months, roll.
6. Noise: Gaussian noise on every OHLC price, sd a fraction of the day's
   median 5m bar range; the live rules re-run on each noisy history.

Run:  uv run python backtesting/robustness/edge_tests.py [runs|report|all]
Output: backtesting/robustness/out/edge_*.csv / edge_report.json / edge_report.txt
"""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import time as dtime
from itertools import combinations
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import kurtosis, norm, skew

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engines as E  # noqa: E402

OUT = HERE / "out"
OUT.mkdir(exist_ok=True)

N_PERM = 200
NOISE_LEVELS = (0.05, 0.10, 0.20, 0.30)
NOISE_SEEDS = 15
LIVE_KEY = {
    "nifty": "slope=6 past=20.0 be=40",
    "bnf": "extra_body=0.3 wide_stop=0.003 window=True stop_after_loss=True",
}

# ============================================================================ price perturbation

_ORIG: dict = {}


def _init_worker(strat: str = "both"):
    """Load only what the strategy needs (memory: one worker holds one 10-year history)."""
    if strat in ("nifty", "both"):
        fr, _ = E._nf_base()
        _ORIG["nf"] = fr[["open", "high", "low", "close", "volume", "vix"]].copy()
        _ORIG["nf_fr"] = E._NF["fr"]
    if strat in ("bnf", "both"):
        b = E.ra.read5("BANKNIFTY_NSE_INDEX")[["open", "high", "low", "close"]]
        b = b[
            (b.index.dayofweek < 5)
            & (b.index.time >= dtime(9, 15))
            & (b.index.time <= dtime(15, 25))
        ]
        _ORIG["bnf"] = b.copy()
        _ORIG["vix"] = E.ra.read5("INDIAVIX_NSE_INDEX")["close"]
        E._bnf_base()
        _ORIG["bnf_pre"] = E._BNF["pre"]


def _day_bounds(index) -> list[tuple[int, int]]:
    d = np.asarray(index.date)
    cut = np.flatnonzero(d[1:] != d[:-1]) + 1
    starts = np.r_[0, cut]
    ends = np.r_[cut, len(d)]
    return list(zip(starts, ends, strict=True))


def permute_days(df: pd.DataFrame, rng: np.random.Generator, carry=("volume",)) -> pd.DataFrame:
    """Shuffle each session's bars after the first; day open and close are preserved."""
    o, h, lo, c = (df[k].to_numpy(float).copy() for k in ("open", "high", "low", "close"))
    extra = {k: df[k].to_numpy().copy() for k in carry if k in df}
    for s, e in _day_bounds(df.index):
        n = e - s
        if n < 3:
            continue
        sl = slice(s + 1, e)
        ro = np.log(o[sl] / c[s : e - 1])
        rh, rl, rc = np.log(h[sl] / o[sl]), np.log(lo[sl] / o[sl]), np.log(c[sl] / o[sl])
        p = rng.permutation(n - 1)
        ro, rh, rl, rc = ro[p], rh[p], rl[p], rc[p]
        cl = c[s] * np.exp(np.cumsum(ro + rc))
        prev = np.r_[c[s], cl[:-1]]
        op = prev * np.exp(ro)
        o[sl], h[sl], lo[sl], c[sl] = op, op * np.exp(rh), op * np.exp(rl), cl
        for k in extra:
            extra[k][sl] = extra[k][sl][p]
    out = df.copy()
    out["open"], out["high"], out["low"], out["close"] = o, h, lo, c
    for k, v in extra.items():
        out[k] = v
    return out


def add_noise(df: pd.DataFrame, rng: np.random.Generator, frac: float) -> pd.DataFrame:
    """Gaussian noise on each OHLC price, sd = frac x that day's median bar range."""
    rngs = (df["high"] - df["low"]).to_numpy(float)
    sd = np.empty(len(df))
    for s, e in _day_bounds(df.index):
        sd[s:e] = np.median(rngs[s:e])
    x = {
        k: df[k].to_numpy(float) + rng.normal(0.0, 1.0, len(df)) * frac * sd
        for k in ("open", "high", "low", "close")
    }
    hi = np.maximum.reduce([x["open"], x["high"], x["low"], x["close"]])
    lo = np.minimum.reduce([x["open"], x["high"], x["low"], x["close"]])
    out = df.copy()
    out["open"], out["high"], out["low"], out["close"] = x["open"], hi, lo, x["close"]
    return out


def nf_frame(base: pd.DataFrame) -> pd.DataFrame:
    """Rebuild the NIFTY signal frame (SMA45, synthetic-volume VWAP, clearance flags)."""
    m = E._nf_module({})
    fr = base.copy()
    fr["sma"] = fr["close"].rolling(45).mean()
    sess = pd.Index(fr.index.date)
    typ = (fr["high"] + fr["low"] + fr["close"]) / 3.0
    fr["vwap"] = (typ * fr["volume"]).groupby(sess).cumsum().values / fr["volume"].groupby(
        sess
    ).cumsum().values
    fr["upper"] = fr[["vwap", "sma"]].max(axis=1)
    fr["lower"] = fr[["vwap", "sma"]].min(axis=1)
    fr["above"] = fr["low"] > fr["upper"] + m.MIN_CLEARANCE
    fr["below"] = fr["high"] < fr["lower"] - m.MIN_CLEARANCE
    return fr.dropna(subset=["vwap", "sma"])


def bnf_pre(b: pd.DataFrame) -> list:
    """Rebuild the BANKNIFTY per-day inputs (previous-day H/L/C for the CPR levels)."""
    g0 = b.groupby(b.index.date)
    d = pd.DataFrame(
        {"high": g0.high.max(), "low": g0.low.min(), "close": g0.close.last(), "n": g0.size()}
    )
    d = d[d.n >= 40]
    groups = dict(list(g0))
    vix = _ORIG["vix"].reindex(b.index).ffill(limit=12)
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
    return pre


def stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"n": 0, "win": np.nan, "PF": np.nan, "net_pts": 0.0, "meanR": np.nan}
    w, lo = t.net[t.net > 0].sum(), -t.net[t.net < 0].sum()
    return {
        "n": len(t),
        "win": 100 * float((t.net > 0).mean()),
        "PF": float(w / lo) if lo > 0 else np.inf,
        "net_pts": float(t.net.sum()),
        "meanR": float(t.R.mean()),
    }


def _job(args) -> dict:
    strat, kind, level, seed = args
    rng = np.random.default_rng(seed * 1000 + int(level * 100))
    if strat == "nifty":
        base = _ORIG["nf"]
        if kind == "perm":
            base = permute_days(base, rng)
        elif kind == "noise":
            base = add_noise(base, rng, level)
        E._NF["fr"] = nf_frame(base)
        t = E.nifty()
        E._NF["fr"] = _ORIG["nf_fr"]
    else:
        base = _ORIG["bnf"]
        if kind == "perm":
            base = permute_days(base, rng, carry=())
        elif kind == "noise":
            base = add_noise(base, rng, level)
        E._BNF["pre"] = bnf_pre(base)
        t = E.bnf()
        E._BNF["pre"] = _ORIG["bnf_pre"]
    return {"strat": strat, "kind": kind, "level": level, "seed": seed, **stats(t)}


def phase_runs(workers: int = 2):
    """One strategy at a time; every finished run is appended at once, done runs are skipped."""
    path = OUT / "edge_runs.csv"
    done = set()
    if path.exists():
        prev = pd.read_csv(path)
        done = {(r.strat, r.kind, round(float(r.level), 2), int(r.seed)) for r in prev.itertuples()}
    for s in ("nifty", "bnf"):
        jobs = [(s, "rebuild", 0.0, 0)]  # check: rebuilt frame == live result
        jobs += [(s, "perm", 0.0, k) for k in range(N_PERM)]
        jobs += [(s, "noise", lv, k) for lv in NOISE_LEVELS for k in range(NOISE_SEEDS)]
        jobs = [j for j in jobs if (j[0], j[1], round(j[2], 2), j[3]) not in done]
        if not jobs:
            continue
        t0 = time.time()

        def save(i, r, s=s, jobs=jobs, t0=t0):
            pd.DataFrame([r]).to_csv(path, mode="a", header=not path.exists(), index=False)
            if i % 10 == 0 or i == len(jobs):
                print(f"  {s}: {i}/{len(jobs)} runs, {time.time() - t0:.0f}s", flush=True)

        if workers == 1:  # in this process: no extra worker process at all
            _ORIG.clear()
            _init_worker(s)
            for i, j in enumerate(jobs, 1):
                save(i, _job(j))
            _ORIG.clear()
            continue
        with Pool(workers, initializer=_init_worker, initargs=(s,)) as p:
            for i, r in enumerate(p.imap_unordered(_job, jobs, chunksize=1), 1):
                save(i, r)


# ============================================================================ grid-based tests


def daily_matrix(strat: str) -> pd.DataFrame:
    """Daily net index points per grid variant, every session (0 on no-trade days)."""
    res = pd.read_pickle(OUT / f"{strat}_wf_runs.pkl")
    sym = "NIFTY_NSE_INDEX" if strat == "nifty" else "BANKNIFTY_NSE_INDEX"
    sess = pd.Index(sorted(set(E.ra.read5(sym).index.date)))
    first = min(t.date.min() for t in res.values())
    sess = sess[(sess >= first) & (sess <= E.ra.END)]
    return pd.DataFrame(
        {k: t.groupby("date").net.sum().reindex(sess, fill_value=0.0) for k, t in res.items()}
    )


def sharpe(x: np.ndarray, axis=0):
    sd = x.std(axis=axis, ddof=1)
    return np.where(sd > 0, x.mean(axis=axis) / np.where(sd > 0, sd, 1), 0.0)


def pbo_cscv(M: pd.DataFrame, S: int = 16) -> dict:
    X = M.to_numpy(float)
    T = (len(X) // S) * S
    blocks = np.array_split(np.arange(T), S)
    logits, deg, live_rank = [], [], []
    live = list(M.columns).index(LIVE_KEY_CUR[0])
    for comb in combinations(range(S), S // 2):
        tr = np.concatenate([blocks[i] for i in comb])
        te = np.concatenate([blocks[i] for i in range(S) if i not in comb])
        sr_is, sr_oos = sharpe(X[tr]), sharpe(X[te])
        best = int(np.argmax(sr_is))
        rank = (sr_oos < sr_oos[best]).sum() + 0.5 * ((sr_oos == sr_oos[best]).sum() - 1)
        w = (rank + 1) / (len(sr_oos) + 1)
        logits.append(math.log(w / (1 - w)))
        deg.append((sr_is[best], sr_oos[best]))
        live_rank.append((sr_oos < sr_oos[live]).sum() / (len(sr_oos) - 1))
    logits = np.array(logits)
    d = np.array(deg)
    slope = np.polyfit(d[:, 0], d[:, 1], 1)[0]
    return {
        "splits": len(logits),
        "PBO": round(float((logits <= 0).mean()), 3),
        "median_logit": round(float(np.median(logits)), 2),
        "best_IS_mean_OOS_sharpe_ann": round(float(d[:, 1].mean() * math.sqrt(252)), 2),
        "best_IS_P(OOS_sharpe<0)": round(float((d[:, 1] < 0).mean()), 3),
        "IS_to_OOS_degradation_slope": round(float(slope), 2),
        "live_rule_median_OOS_percentile": round(float(np.median(live_rank)), 2),
    }


def dsr(M: pd.DataFrame) -> dict:
    x = M[LIVE_KEY_CUR[0]].to_numpy(float)
    T = len(x)
    sr = float(x.mean() / x.std(ddof=1))
    g3, g4 = float(skew(x)), float(kurtosis(x, fisher=False))
    sig = math.sqrt((1 - g3 * sr + (g4 - 1) / 4 * sr * sr) / (T - 1))
    all_sr = sharpe(M.to_numpy(float))
    v = float(np.var(all_sr, ddof=1))
    gamma = 0.5772156649

    def sr0(n):
        return math.sqrt(v) * (
            (1 - gamma) * norm.ppf(1 - 1 / n) + gamma * norm.ppf(1 - 1 / (n * math.e))
        )

    out = {
        "days": T,
        "sharpe_daily": round(sr, 4),
        "sharpe_ann": round(sr * math.sqrt(252), 2),
        "skew": round(g3, 2),
        "kurtosis": round(g4, 1),
        "PSR_vs_0": round(float(norm.cdf(sr / sig)), 4),
        "trial_sharpe_sd_ann": round(math.sqrt(v) * math.sqrt(252), 3),
    }
    for n in (48, 200, 500, 1000, 5000):
        out[f"DSR_N={n}"] = round(float(norm.cdf((sr - sr0(n)) / sig)), 4)
    # wider trial spread: the research tried whole different strategies, not 48 neighbours
    for spread_ann in (0.5, 1.0):
        vv = (spread_ann / math.sqrt(252)) ** 2
        for n in (100, 500):
            s0 = math.sqrt(vv) * (
                (1 - gamma) * norm.ppf(1 - 1 / n) + gamma * norm.ppf(1 - 1 / (n * math.e))
            )
            out[f"DSR_N={n}_trialSD={spread_ann}"] = round(float(norm.cdf((sr - s0) / sig)), 4)
    return out


def stationary_idx(T: int, B: int, p: float, rng) -> np.ndarray:
    idx = np.empty((B, T), dtype=np.int64)
    for b in range(B):
        i = rng.integers(T)
        for t in range(T):
            if t > 0 and rng.random() >= p:
                i = (i + 1) % T
            elif t > 0:
                i = rng.integers(T)
            idx[b, t] = i
    return idx


def rc_spa(M: pd.DataFrame, B: int = 2000, block: float = 10.0) -> dict:
    X = M.to_numpy(float)
    T, K = X.shape
    rng = np.random.default_rng(11)
    idx = stationary_idx(T, B, 1 / block, rng)
    dbar = X.mean(axis=0)
    boot = np.stack([X[ix].mean(axis=0) for ix in idx])  # B x K
    om = np.sqrt(T) * boot.std(axis=0, ddof=1)
    om = np.where(om > 0, om, 1e-12)
    V = np.sqrt(T) * dbar.max()
    Vb = np.sqrt(T) * (boot - dbar).max(axis=1)
    p_rc = float((Vb >= V).mean())
    tk = np.sqrt(T) * dbar / om
    Tspa = max(tk.max(), 0.0)
    # Hansen (2005): Z* = dbar* - dbar + mu; lower <= consistent <= upper (upper = mu 0)
    poor = tk <= -math.sqrt(2 * math.log(math.log(T)))
    mu_c = np.where(poor, dbar, 0.0)
    mu_l = np.minimum(dbar, 0.0)
    out = {
        "variants": K,
        "days": T,
        "bootstrap": B,
        "mean_block_days": block,
        "best_variant": M.columns[int(dbar.argmax())],
        "RC_p": round(p_rc, 4),
    }
    for lab, mu in (
        ("SPA_consistent_p", mu_c),
        ("SPA_lower_p", mu_l),
        ("SPA_upper_p", np.zeros(K)),
    ):
        Tb = np.maximum((np.sqrt(T) * (boot - dbar + mu) / om).max(axis=1), 0.0)
        out[lab] = round(float((Tb >= Tspa).mean()), 4)
    live = list(M.columns).index(LIVE_KEY_CUR[0])
    out["live_alone_bootstrap_p"] = round(
        float(((boot[:, live] - dbar[live]) >= dbar[live]).mean()), 4
    )
    return out


def walk_forward(strat: str, M: pd.DataFrame, train_y=3, test_m=6) -> dict:
    res = pd.read_pickle(OUT / f"{strat}_wf_runs.pkl")
    d0 = pd.Timestamp(M.index.min())
    first_ok = d0 + pd.DateOffset(years=train_y)
    start = pd.Timestamp(year=first_ok.year, month=1, day=1)
    while start < first_ok:
        start += pd.DateOffset(months=6)  # first 1 Jan / 1 Jul with 3 full years behind it
    end = pd.Timestamp(E.ra.END)
    steps, picked_tr, live_tr = [], [], []
    a = start
    while a <= end:
        b = a + pd.DateOffset(months=test_m) - pd.Timedelta(days=1)
        tr0 = a - pd.DateOffset(years=train_y)
        ism = M[(M.index >= tr0.date()) & (M.index < a.date())]
        sr = pd.Series(sharpe(ism.to_numpy(float)), index=M.columns)
        best = sr.idxmax()
        t = res[best]
        oos = t[(t.date >= a.date()) & (t.date <= b.date())]
        isn = t[(t.date >= tr0.date()) & (t.date < a.date())]
        lv = res[LIVE_KEY_CUR[0]]
        lvo = lv[(lv.date >= a.date()) & (lv.date <= b.date())]
        steps.append(
            {
                "test": f"{a.date()}..{min(b, end).date()}",
                "picked": best,
                "IS_PF": round(stats(isn)["PF"], 2),
                "IS_pts_per_6m": round(isn.net.sum() / (2 * train_y)),
                "OOS_n": len(oos),
                "OOS_PF": round(stats(oos)["PF"], 2),
                "OOS_pts": round(oos.net.sum()),
                "live_OOS_pts": round(lvo.net.sum()),
            }
        )
        picked_tr.append(oos)
        live_tr.append(lvo)
        a = a + pd.DateOffset(months=test_m)
    df = pd.DataFrame(steps)
    so, sl = pd.concat(picked_tr), pd.concat(live_tr)
    return {
        "train_years": train_y,
        "test_months": test_m,
        "steps": df.to_dict("records"),
        "stitched_OOS": {k: round(v, 2) for k, v in stats(so).items()},
        "live_rule_same_windows": {k: round(v, 2) for k, v in stats(sl).items()},
        "WF_efficiency_OOS_over_IS": round(float(df.OOS_pts.sum() / df.IS_pts_per_6m.sum()), 2),
        "positive_windows": f"{int((df.OOS_pts > 0).sum())}/{len(df)}",
        "times_live_rule_picked": int((df.picked == LIVE_KEY_CUR[0]).sum()),
    }


LIVE_KEY_CUR = [""]


# ============================================================================ report


def report(grid_only: bool = False):
    runs = None if grid_only else pd.read_csv(OUT / "edge_runs.csv")
    rep = {}
    for strat in ("nifty", "bnf"):
        LIVE_KEY_CUR[0] = LIVE_KEY[strat]
        live = stats(pd.read_pickle(HERE / f"{'nf' if strat == 'nifty' else 'bnf'}_live.pkl"))
        out = {"live": {k: round(v, 3) for k, v in live.items()}}
        rnd = pd.read_csv(OUT / f"{strat}_random.csv")
        out["1b_random_entry_timing"] = {
            "runs": len(rnd),
            "random_meanR_p50_p95": [round(float(v), 3) for v in rnd.meanR.quantile([0.5, 0.95])],
            "live_meanR": round(live["meanR"], 3),
            "real_beats_pct": round(100 * float((rnd.meanR < live["meanR"]).mean()), 1),
            "p_value": round((1 + int((rnd.meanR >= live["meanR"]).sum())) / (len(rnd) + 1), 4),
        }
        M = daily_matrix(strat)
        out["2_PBO_CSCV"] = pbo_cscv(M)
        out["3_deflated_sharpe"] = dsr(M)
        out["4_reality_check_SPA"] = rc_spa(M)
        out["5_walk_forward"] = walk_forward(strat, M)
        if grid_only:
            rep[strat] = out
            continue
        r = runs[runs.strat == strat]
        reb = r[r.kind == "rebuild"].iloc[0].to_dict()
        perm = r[r.kind == "perm"]
        out["rebuilt_frame_check"] = {k: reb[k] for k in ("n", "win", "PF", "net_pts")}
        ref = reb  # compare shuffles with the rebuilt (same-pipeline) result
        out["1_permutation"] = {
            "runs": len(perm),
            "perm_net_pts_p5_p50_p95": [
                round(float(v)) for v in perm.net_pts.quantile([0.05, 0.5, 0.95])
            ],
            "perm_PF_p5_p50_p95": [round(float(v), 2) for v in perm.PF.quantile([0.05, 0.5, 0.95])],
            "perm_trades_median": int(perm.n.median()),
            "real_beats_pct_net": round(100 * float((perm.net_pts < ref["net_pts"]).mean()), 1),
            "real_beats_pct_PF": round(100 * float((perm.PF < ref["PF"]).mean()), 1),
            "p_value_net": round(
                (1 + int((perm.net_pts >= ref["net_pts"]).sum())) / (len(perm) + 1), 4
            ),
            "p_value_PF": round((1 + int((perm.PF >= ref["PF"]).sum())) / (len(perm) + 1), 4),
        }
        nz = []
        for lv in NOISE_LEVELS:
            g = r[(r.kind == "noise") & (r.level == lv)]
            nz.append(
                {
                    "noise_sd_x_bar_range": lv,
                    "runs": len(g),
                    "n_med": int(g.n.median()),
                    "PF_p5_p50_p95": [round(float(v), 2) for v in g.PF.quantile([0.05, 0.5, 0.95])],
                    "win_med": round(float(g.win.median()), 1),
                    "net_pts_med": round(float(g.net_pts.median())),
                    "net_kept_pct": round(100 * float(g.net_pts.median() / ref["net_pts"]), 1),
                    "share_PF_gt_1": round(float((g.PF > 1).mean()), 2),
                }
            )
        out["6_noise"] = nz
        rep[strat] = out
    name = "edge_report_grid.json" if grid_only else "edge_report.json"
    (OUT / name).write_text(json.dumps(rep, indent=1, default=str))
    print(json.dumps(rep, indent=1, default=str))


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what == "grid":
        report(grid_only=True)
    if what in ("runs", "all"):
        phase_runs(workers=int(sys.argv[2]) if len(sys.argv) > 2 else 2)
    if what in ("report", "all"):
        report()
