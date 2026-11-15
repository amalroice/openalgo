"""Live trade journal: the first 40 live trades of each strategy from Monday 2026-11-16.

Reads the strategy host logs (log/strategies/) and rebuilds the journal from scratch
on every run, so it is safe to run any number of times. A trade is one option
position from flat to flat, priced from the "order COMPLETE BUY/SELL" fills.

Manual exits ("closed outside the strategy") have no fill in the log. The BANKNIFTY
script logs an estimate (the option LTP when it noticed); NIFTY logs nothing. Put the
real broker price in out/live_journal_overrides.csv (columns: symbol, entry_time,
exit_price) and run again; until then the trade shows as "price needed".

Each strategy is compared with its backtest: for the same number of trades, the
5th..95th percentile of win% and PF over 10,000 random draws of backtest trades.

NIFTY and BANKNIFTY also get:
  - Same-day backtest: the backtest engine (engines.py, today's script rules) replayed on
    exactly the days the strategy ran live, trade by trade against the live trades:
    matched / live only (backtest had no such trade) / backtest only (live missed it).
    Live and backtest agree -> results measure the edge; they differ -> fix execution.
    Missing 5m bars for finished live days are fetched first (fetch_day.py, needs the
    OpenAlgo server; each data file is backed up before it is changed).
  - Stop checks in R: live R = index points captured / initial index risk, from the
    logged index entry and stop, the index price at each exit (5m data) and the same
    cost as the backtest (3 pts a lot NIFTY, 5 BANKNIFTY). Checked against the 10y
    backtest: cum R after 20 trades (review / worry), drawdown, losing streak.

Run:  uv run python backtesting/robustness/live_journal.py
      JOURNAL_START=20261109 ... to try it on earlier days (default 20261116)
Out:  backtesting/robustness/out/live_journal.csv, out/live_vs_backtest.csv
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LOGS = ROOT / "log" / "strategies"
OUT = HERE / "out"
START = os.getenv("JOURNAL_START", "20261116")
TARGET = 40

STRATS = {
    "nifty_2candle_vwap_sma45_20260908211324": ("NIFTY 2-candle", HERE / "nf_live.pkl"),
    "banknifty_cpr_price_action_20261106220000_20261107011742": (
        "BANKNIFTY CPR",
        HERE / "bnf_live.pkl",
    ),
    "sector_920_momentum_20260930231437": ("Sector 920", None),
}
# index file, lot size, backtest cost in points a lot, engine name
INDEX_META = {
    "NIFTY 2-candle": ("NIFTY_NSE_INDEX", 65, 3.0, "nifty"),
    "BANKNIFTY CPR": ("BANKNIFTY_NSE_INDEX", 30, 5.0, "bnf"),
}
DATA = ROOT / "workspace" / "indicators" / "data" / "long2016"
SIG_NF = re.compile(
    r"^\[(\S+ \S+) IST\] (LONG|SHORT) signal: index ([\d.]+), stop ([\d.]+) \(risk ([\d.]+) pts\), buying \d+ (\S+)"
)
SIG_BNF = re.compile(
    r"^\[(\S+ \S+) IST\] LIVE (LONG|SHORT) (\S+) \((\S+) tier.*?index ([\d.]+).*?stop ([\d.]+) \(risk ([\d.]+)\).*?; (\S+) x\d+"
)

FILL = re.compile(r"^\[(\S+ \S+) IST\] order COMPLETE (BUY|SELL) (\d+) (\S+) -> \S+ at ([\d.]+)")
MANUAL = re.compile(r"^\[(\S+ \S+) IST\] (\S+) was closed outside the strategy")
PARTIAL = re.compile(
    r"^\[(\S+ \S+) IST\] (\S+) partly closed outside the strategy: broker holds (\d+) of (\d+)"
)
STOPFILL = re.compile(r"^\[(\S+ \S+) IST\] resting stop \S+ filled on (\S+);")
RESTING = re.compile(r"broker stop resting: \d+ (\S+) trigger ([\d.]+)")
EST = re.compile(r"option (?:\S+ )?([\d.]+) -> ([\d.]+)")


def _left(p: dict) -> int:
    return p["qty"] - p["sold"] - sum(u["qty"] for u in p["unpriced"])


def parse(prefix: str) -> list[dict]:
    """Option positions flat to flat. Exits without a logged fill go to p['unpriced']
    as {qty, est, kind}: a manual exit (est = the script's logged option price, if any)
    or a broker-stop fill (est = that stop's trigger)."""
    files = sorted(
        f for f in LOGS.glob(f"{prefix}_*_IST.log") if f.name[len(prefix) + 1 :][:8] >= START
    )
    trades, open_pos, trigger, pending = [], {}, {}, {}
    for f in files:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, ln in enumerate(lines):
            sg = SIG_NF.match(ln)
            if sg:
                info = {
                    "dir": sg.group(2).lower(),
                    "idx_entry": float(sg.group(3)),
                    "idx_stop": float(sg.group(4)),
                    "risk_pts": float(sg.group(5)),
                }
                sym = sg.group(6)
            else:
                sg = SIG_BNF.match(ln)
                if sg:
                    info = {
                        "dir": sg.group(2).lower(),
                        "idx_entry": float(sg.group(5)),
                        "idx_stop": float(sg.group(6)),
                        "risk_pts": float(sg.group(7)),
                        "setup": sg.group(3),
                        "tier": sg.group(4),
                    }
                    sym = sg.group(8)
            if sg:
                if sym in open_pos and "risk_pts" not in open_pos[sym]:
                    open_pos[sym].update(info)
                else:
                    pending[sym] = info
                continue
            r = RESTING.search(ln)
            if r:
                trigger[r.group(1)] = float(r.group(2))
            m = FILL.match(ln)
            if m:
                ts, side, qty, sym, px = (
                    m.group(1),
                    m.group(2),
                    int(m.group(3)),
                    m.group(4),
                    float(m.group(5)),
                )
                p = open_pos.get(sym)
                if side == "BUY":
                    if p is None:
                        p = open_pos[sym] = {
                            "symbol": sym,
                            "entry_time": ts,
                            "qty": 0,
                            "cost": 0.0,
                            "sold": 0,
                            "proceeds": 0.0,
                            "unpriced": [],
                            "exits": [],
                        }
                        p.update(pending.pop(sym, {}))
                    p["qty"] += qty
                    p["cost"] += qty * px
                elif p is not None:
                    p["sold"] += qty
                    p["proceeds"] += qty * px
                    p["exits"].append((ts, qty))
                    p["exit_time"] = ts
                    if _left(p) <= 0:
                        trades.append(open_pos.pop(sym))
                continue
            m = PARTIAL.match(ln)
            if m and m.group(2) in open_pos:
                p = open_pos[m.group(2)]
                gone = int(m.group(4)) - int(m.group(3))
                if gone > 0:
                    p["unpriced"].append({"qty": gone, "est": np.nan, "kind": "manual"})
                    p["exits"].append((m.group(1), gone))
                continue
            m = MANUAL.match(ln)
            if m and m.group(2) in open_pos:
                p = open_pos.pop(m.group(2))
                p["exit_time"] = m.group(1)
                est = next((EST.search(x) for x in lines[i + 1 : i + 6] if EST.search(x)), None)
                p["exits"].append((m.group(1), _left(p)))
                p["unpriced"].append(
                    {
                        "qty": _left(p),
                        "est": float(est.group(2)) if est else np.nan,
                        "kind": "manual",
                    }
                )
                trades.append(p)
                continue
            m = STOPFILL.match(ln)
            if m and m.group(2) in open_pos:
                p = open_pos.pop(m.group(2))
                p["exit_time"] = m.group(1)
                p["exits"].append((m.group(1), _left(p)))
                p["unpriced"].append(
                    {"qty": _left(p), "est": trigger.get(m.group(2), np.nan), "kind": "broker stop"}
                )
                trades.append(p)
    return trades


def band(pkl: Path, n: int, sims: int = 10000) -> dict:
    net = pd.read_pickle(pkl)["net"].to_numpy(float)
    rng = np.random.default_rng(3)
    s = net[rng.integers(0, len(net), size=(sims, n))]
    win = (s > 0).mean(axis=1) * 100
    gw = np.where(s > 0, s, 0).sum(axis=1)
    gl = -np.where(s < 0, s, 0).sum(axis=1)
    pf = np.where(gl > 0, gw / np.where(gl > 0, gl, 1), 99.0)
    return {"win": np.percentile(win, [5, 50, 95]), "pf": np.percentile(pf, [5, 50, 95])}


def _ts(text: str) -> pd.Timestamp:
    return pd.Timestamp(text).tz_localize("Asia/Kolkata")


def ensure_data(days: list) -> list:
    """Fetch 5m bars for finished live days missing from the data. Returns days still missing."""
    last = pd.read_parquet(DATA / "NIFTY_NSE_INDEX.parquet").index.max().date()
    now = datetime.now()
    todo = [
        d
        for d in days
        if d > last and (d < now.date() or now.hour * 60 + now.minute >= 15 * 60 + 35)
    ]
    if not todo:
        return [d for d in days if d > last]
    try:
        import fetch_day
    except Exception as exc:  # noqa: BLE001
        print(f"  cannot load fetch_day ({exc!r}); same-day backtest skips {len(todo)} day(s)")
        return [d for d in days if d > last]
    for d in todo:
        print(f"  fetching 5m bars for {d} ...", flush=True)
        try:
            fetch_day.fetch(str(d), quiet=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  fetch {d} failed: {exc!r}")
    last = pd.read_parquet(DATA / "NIFTY_NSE_INDEX.parquet").index.max().date()
    return [d for d in days if d > last]


def index_at(bars: pd.DataFrame, ts: pd.Timestamp) -> float:
    """Index price at a moment: close of the last 5m bar finished by then (30 s grace)."""
    ends = bars.index + pd.Timedelta(minutes=5)
    k = ends.searchsorted(ts + pd.Timedelta(seconds=30), side="right") - 1
    return float(bars["close"].iloc[k]) if k >= 0 else np.nan


def live_r(p: dict, bars: pd.DataFrame, lot: int, cost: float) -> float:
    """Index R of a live trade, net of the backtest's cost, all lots together."""
    if "risk_pts" not in p or not p["exits"] or p["risk_pts"] <= 0:
        return np.nan
    sgn = 1.0 if p["dir"] == "long" else -1.0
    pts = 0.0
    for ts, qty in p["exits"]:
        px = index_at(bars, _ts(ts))
        if np.isnan(px):
            return np.nan
        pts += sgn * (px - p["idx_entry"]) * qty
    lots = p["qty"] / lot
    return (pts / lot - cost * lots) / (p["risk_pts"] * lots)


def same_day(name: str, trades: list[dict], days: list) -> pd.DataFrame:
    """Backtest replay of the live days, matched trade by trade with the live trades."""
    import engines

    stem, lot, cost, eng = INDEX_META[name]
    if not days:
        return pd.DataFrame()
    s, e = min(days), max(days)
    bt = engines.nifty(start=s, end=e) if eng == "nifty" else engines.bnf(start=s, end=e)
    bt = bt[bt.date.isin(set(days))] if len(bt) else bt
    bars = pd.read_parquet(DATA / f"{stem}.parquet").sort_index()
    bars.index = bars.index.tz_convert("Asia/Kolkata")
    rows, used = [], set()
    for k, p in enumerate(trades, 1):
        t = _ts(p["entry_time"])
        r = live_r(p, bars, lot, cost)
        hit = None
        for j, b in bt.iterrows() if len(bt) else []:
            if j in used or b["date"] != t.date() or b["dir"] != p.get("dir"):
                continue
            gap = (t - (b["entry_time"] + pd.Timedelta(minutes=5))).total_seconds() / 60
            if -2 <= gap <= 10:
                hit = j
                break
        row = {
            "strategy": name,
            "trade": k,
            "date": t.date(),
            "live_entry": p["entry_time"],
            "dir": p.get("dir", "?"),
            "live_R": round(r, 2) if np.isfinite(r) else np.nan,
        }
        if hit is not None:
            used.add(hit)
            b = bt.loc[hit]
            row.update(status="matched", bt_entry=str(b["entry_time"])[:16], bt_R=round(b["R"], 2))
        else:
            row.update(status="live only", bt_entry="", bt_R=np.nan)
        rows.append(row)
    for j, b in bt.iterrows() if len(bt) else []:
        if j not in used:
            rows.append(
                {
                    "strategy": name,
                    "trade": None,
                    "date": b["date"],
                    "live_entry": "",
                    "dir": b["dir"],
                    "live_R": np.nan,
                    "status": "backtest only",
                    "bt_entry": str(b["entry_time"])[:16],
                    "bt_R": round(b["R"], 2),
                }
            )
    return pd.DataFrame(rows)


def thresholds(pkl: Path, sims: int = 20000) -> dict:
    """Stop levels in R from the 10y backtest and its last 2 years."""
    df = pd.read_pickle(pkl)
    rng = np.random.default_rng(5)
    out = {}
    for lab, R in (
        ("10y", df["R"].to_numpy(float)),
        (
            "2y",
            df[pd.to_datetime(df.date) >= pd.Timestamp(df.date.max()) - pd.DateOffset(years=2)][
                "R"
            ].to_numpy(float),
        ),
    ):
        s = R[rng.integers(0, len(R), size=(sims, TARGET))]
        eq = np.cumsum(s, axis=1)
        dd = (np.maximum.accumulate(eq, axis=1) - eq).max(axis=1)
        streak = np.zeros(sims, int)
        cur = np.zeros(sims, int)
        for c in range(TARGET):
            cur = np.where(s[:, c] <= 0, cur + 1, 0)
            streak = np.maximum(streak, cur)
        out[lab] = {
            "cum20_p5": np.percentile(eq[:, 19], 5),
            "dd_p95": np.percentile(dd, 95),
            "streak_p95": np.percentile(streak, 95),
        }
    return {
        "review20": out["2y"]["cum20_p5"],
        "worry20": out["10y"]["cum20_p5"],
        "dd": out["10y"]["dd_p95"],
        "streak": out["10y"]["streak_p95"],
    }


def stop_check(name: str, R: np.ndarray, th: dict) -> list[str]:
    R = R[np.isfinite(R)]
    if not len(R):
        return ["    stop checks: no live R yet"]
    eq = np.cumsum(R)
    dd = float((np.maximum.accumulate(np.r_[0.0, eq]) - np.r_[0.0, eq]).max())
    cur = 0
    for v in R[::-1]:
        if v > 0:
            break
        cur += 1
    n = len(R)
    lines = [
        f"    live R: {n} trades, total {eq[-1]:+.1f}R, avg {R.mean():+.2f}R, "
        f"max drawdown {dd:.1f}R, current losing streak {cur}"
    ]
    flags = []
    if dd > th["dd"]:
        flags.append(f"STOP: drawdown {dd:.1f}R is past the 10y 95th percentile {th['dd']:.1f}R")
    if cur > th["streak"]:
        flags.append(f"CHECK: {cur} losses in a row, more than the usual worst {th['streak']:.0f}")
    if n >= 20:
        c20 = float(eq[19])
        if c20 < th["worry20"]:
            flags.append(
                f"WORRY: {c20:+.1f}R after 20 trades, below the 10y 5th percentile {th['worry20']:+.1f}R"
            )
        elif c20 < th["review20"]:
            flags.append(
                f"REVIEW: {c20:+.1f}R after 20 trades, below the last-2y 5th percentile {th['review20']:+.1f}R"
            )
    lines.append(
        f"    limits: drawdown {th['dd']:.1f}R, streak {th['streak']:.0f}, after 20 trades review "
        f"< {th['review20']:+.1f}R / worry < {th['worry20']:+.1f}R -> "
        + ("; ".join(flags) if flags else "all within limits")
    )
    return lines


def main():
    ov_path = OUT / "live_journal_overrides.csv"
    if not ov_path.exists():
        pd.DataFrame(columns=["symbol", "entry_time", "exit_price"]).to_csv(ov_path, index=False)
    ov = pd.read_csv(ov_path)
    ov = {(r.symbol, str(r.entry_time)): float(r.exit_price) for r in ov.itertuples()}
    rows, parsed = [], {}
    for prefix, (name, _pkl) in STRATS.items():
        parsed[name] = parse(prefix)[:TARGET]
        for k, p in enumerate(parsed[name], 1):
            proceeds, notes = p["proceeds"], []
            real = ov.get((p["symbol"], p["entry_time"]))
            for u in p["unpriced"]:
                if real is not None:
                    proceeds += u["qty"] * real
                    notes.append(f"{u['kind']} exit, price from overrides")
                elif not np.isnan(u["est"]):
                    proceeds += u["qty"] * u["est"]
                    notes.append(
                        f"{u['kind']} exit {u['qty']} at ESTIMATED {u['est']:.2f} - check broker"
                    )
                else:
                    proceeds = np.nan
                    notes.append(f"{u['kind']} exit {u['qty']} - PRICE NEEDED in overrides")
            pnl = proceeds - p["cost"]
            rows.append(
                {
                    "strategy": name,
                    "trade": k,
                    "entry_time": p["entry_time"],
                    "exit_time": p.get("exit_time", ""),
                    "symbol": p["symbol"],
                    "side": "long" if p["symbol"].endswith("CE") else "short",
                    "qty": p["qty"],
                    "buy_avg": round(p["cost"] / p["qty"], 2),
                    "sell_avg": round(proceeds / p["qty"], 2) if not np.isnan(pnl) else np.nan,
                    "pnl_rs": round(pnl, 2) if not np.isnan(pnl) else np.nan,
                    "note": "; ".join(notes),
                }
            )
    df = pd.DataFrame(
        rows,
        columns=[
            "strategy",
            "trade",
            "entry_time",
            "exit_time",
            "symbol",
            "side",
            "qty",
            "buy_avg",
            "sell_avg",
            "pnl_rs",
            "note",
        ],
    )
    df.to_csv(OUT / "live_journal.csv", index=False)

    print(
        f"Live journal from {START[:4]}-{START[4:6]}-{START[6:]} (first {TARGET} trades each), "
        "P&L in Rs from option fills, before brokerage and taxes\n"
    )
    for _prefix, (name, pkl) in STRATS.items():
        g = df[df.strategy == name]
        known = g.pnl_rs.dropna()
        n = len(known)
        line = f"{name}: {len(g)}/{TARGET} trades"
        if n:
            w = 100 * (known > 0).mean()
            gl = -known[known < 0].sum()
            pf = known[known > 0].sum() / gl if gl > 0 else float("inf")
            line += f" | win {w:.0f}% | PF {pf:.2f} | net Rs {known.sum():+,.0f}"
            if pkl is not None and n >= 5:
                b = band(pkl, n)
                ok = w >= b["win"][0] and pf >= b["pf"][0]
                line += (
                    f"\n    backtest range for {n} trades: win {b['win'][0]:.0f}-{b['win'][2]:.0f}% "
                    f"(typical {b['win'][1]:.0f}), PF {b['pf'][0]:.2f}-{b['pf'][2]:.2f} "
                    f"(typical {b['pf'][1]:.2f}) -> {'normal' if ok else 'BELOW the normal range'}"
                )
        need = g[g.note.str.contains("NEEDED|ESTIMATED", na=False)]
        if len(need):
            line += (
                f"\n    {len(need)} manual exit(s) need the real broker price: see the note column"
            )
        print(line)

    # same-day backtest and stop checks (NIFTY, BANKNIFTY)
    print("\nSame-day backtest and stop checks (index R, net of backtest cost)")
    cmp_frames = []
    for prefix, (name, pkl) in STRATS.items():
        if name not in INDEX_META:
            continue
        files = sorted(LOGS.glob(f"{prefix}_*_IST.log"))
        days = sorted(
            {
                datetime.strptime(f.name[len(prefix) + 1 :][:8], "%Y%m%d").date()
                for f in files
                if f.name[len(prefix) + 1 :][:8] >= START
            }
        )
        trades = parsed[name]
        if len(trades) >= TARGET:  # stop at the 40th trade's day
            last = _ts(trades[-1]["entry_time"]).date()
            days = [d for d in days if d <= last]
        missing = ensure_data(days)
        ok_days = [d for d in days if d not in missing]
        print(
            f"\n{name}: {len(days)} live day(s)"
            + (f", no 5m data yet for {', '.join(map(str, missing))}" if missing else "")
        )
        cmp = same_day(name, [t for t in trades if _ts(t["entry_time"]).date() in ok_days], ok_days)
        if len(cmp):
            cmp_frames.append(cmp)
            n_m = (cmp.status == "matched").sum()
            n_l = (cmp.status == "live only").sum()
            n_b = (cmp.status == "backtest only").sum()
            print(f"    matched {n_m}, live only {n_l}, backtest only (missed live) {n_b}")
            m = cmp[cmp.status == "matched"]
            if len(m):
                diff = (m.live_R - m.bt_R).dropna()
                print(
                    f"    matched trades: live {m.live_R.sum():+.1f}R vs backtest {m.bt_R.sum():+.1f}R"
                    + (f" (avg gap {diff.mean():+.2f}R a trade)" if len(diff) else "")
                )
            for r in cmp.itertuples():
                if r.status != "matched" or (
                    np.isfinite(r.live_R) and abs(r.live_R - r.bt_R) >= 0.5
                ):
                    print(
                        f"      {r.date} {r.dir:<5} {r.status:<13} live {r.live_entry[11:16] or '-':>5} "
                        f"R {r.live_R if np.isfinite(r.live_R) else '-'} | backtest "
                        f"{r.bt_entry[11:16] or '-':>5} R {r.bt_R if np.isfinite(r.bt_R) else '-'}"
                    )
            print(
                f"    backtest on the same days: {cmp.bt_R.sum():+.1f}R over {cmp.bt_R.notna().sum()} trades"
            )
        R = np.array([r for r in (cmp.live_R if len(cmp) else []) if r is not None], float)
        for ln in stop_check(name, R, thresholds(pkl)):
            print(ln)
    if cmp_frames:
        pd.concat(cmp_frames).to_csv(OUT / "live_vs_backtest.csv", index=False)
    print(f"\nJournal: {OUT / 'live_journal.csv'}")
    if cmp_frames:
        print(f"Same-day comparison: {OUT / 'live_vs_backtest.csv'}")


if __name__ == "__main__":
    main()
