"""Build the live strategy tracker's trade list from the strategy host logs.

Reads log/strategies/*.log for the four running strategies, from START_DATE on,
and writes trades.json as {"rows": [...], "meta": [...]} for the tracker page.

Each row is one closed trade (one option position opened and fully closed).
P&L is the option premium change x quantity, in rupees, before brokerage and
taxes. Where the log does not record an exit fill (a broker-side stop fill or a
hand exit), the exit price comes from estimates.json and the row's price_note
says "estimated".

Run:  uv run python backtesting/live_tracker/export_trades.py
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
LOG_DIR = REPO / "log" / "strategies"
OUT = HERE / "trades.json"
ESTIMATES = HERE / "estimates.json"   # {"YYYY-MM-DD|SYMBOL": {"price": x, "how": "..."}}
START_DATE = "2026-11-09"             # first day any strategy traded live

STRATEGIES = {
    "nifty_2candle_vwap_sma45": "NIFTY 2-candle",
    "banknifty_cpr_price_action": "BankNifty CPR",
    "sector_920_momentum": "Sector 920",
    "sensex_cluster_breakout": "Sensex cluster",
}

TS = r"\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}):\d{2} IST\]"
RS = r"Rs ([+-]?[\d,]+)"


def rupees(text: str) -> float:
    return float(text.replace(",", ""))


def log_files(prefix: str) -> list[tuple[str, Path]]:
    """(trade date, path) for each of the strategy's logs on or after START_DATE."""
    out = []
    for p in sorted(LOG_DIR.glob(f"{prefix}_*.log")):
        m = re.search(r"_(\d{8})_\d{6}_IST\.log$", p.name)
        if not m:
            continue
        d = m.group(1)
        day = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        if day >= START_DATE:
            out.append((day, p))
    return out


def lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def nifty(day: str, path: Path, estimates: dict) -> list[dict]:
    """Pair BUY and SELL fills per option until the position is flat."""
    rows = []
    open_pos: dict[str, dict] = {}
    for ln in lines(path):
        ts = re.match(TS, ln)
        if not ts:
            continue
        hhmm = ts.group(2)
        m = re.search(r"order COMPLETE (BUY|SELL) (\d+) (\S+) -> \S+ at ([\d.]+)", ln)
        if m:
            act, qty, sym, px = m.group(1), int(m.group(2)), m.group(3), float(m.group(4))
            if act == "BUY":
                side = "Long" if sym.endswith("CE") else "Short"
                pos = open_pos.setdefault(sym, {"qty": 0, "cost": 0.0, "proceeds": 0.0, "bought": 0,
                                                "entry_time": hhmm, "side": side, "reasons": [],
                                                "estimated": False})
                pos["qty"] += qty
                pos["bought"] += qty
                pos["cost"] += qty * px
            elif sym in open_pos:
                pos = open_pos[sym]
                pos["qty"] -= qty
                pos["proceeds"] += qty * px
                pos["exit_time"] = hhmm
            continue
        reason = None
        if "STOP hit" in ln:
            reason = "stop"
        elif "lock stop hit" in ln:
            reason = "lock"
        elif "RUNNER EMA EXIT" in ln:
            reason = "runner EMA"
        elif "square-off" in ln.lower() and "closed" in ln:
            reason = "square-off"
        if reason:
            for pos in open_pos.values():
                pos["reasons"].append(reason)
        m = re.search(r"resting stop \S+ filled on ([A-Z0-9.]+)", ln) or \
            re.search(r"cancelled resting stop \S+: the position was closed by (hand)", ln) or \
            re.search(r"([A-Z0-9.]+) was closed outside the strategy", ln)
        if m:
            sym = m.group(1)
            if sym == "hand":
                sym = next((s for s, p in open_pos.items() if p["qty"] > 0), None)
            if sym in open_pos and open_pos[sym]["qty"] > 0:
                pos = open_pos[sym]
                est = estimates.get(f"{day}|{sym}")
                why = "closed outside"
                pos["reasons"].append(why)
                if est:
                    pos["proceeds"] += pos["qty"] * est["price"]
                    pos["estimated"] = True
                    pos["qty"] = 0
                    pos["exit_time"] = hhmm
        for sym in [s for s, p in open_pos.items() if p["qty"] == 0 and p["bought"] > 0]:
            p = open_pos.pop(sym)
            rows.append({
                "date": day, "strategy": "NIFTY 2-candle", "mode": "Live", "side": p["side"],
                "instrument": sym, "entry_time": p["entry_time"], "exit_time": p.get("exit_time", ""),
                "qty": p["bought"], "entry_px": round(p["cost"] / p["bought"], 2),
                "exit_px": round(p["proceeds"] / p["bought"], 2),
                "pnl_rs": round(p["proceeds"] - p["cost"], 2),
                "exit_reason": " + ".join(dict.fromkeys(p["reasons"])) or "exit",
                "price_note": "estimated" if p["estimated"] else "logged",
            })
    for sym, p in open_pos.items():
        if p["qty"] > 0:
            rows.append({
                "date": day, "strategy": "NIFTY 2-candle", "mode": "Live", "side": p["side"],
                "instrument": sym, "entry_time": p["entry_time"], "exit_time": "", "qty": p["bought"],
                "entry_px": round(p["cost"] / p["bought"], 2), "exit_px": None, "pnl_rs": None,
                "exit_reason": "exit price not logged", "price_note": "missing",
            })
    return rows


def banknifty(day: str, path: Path) -> list[dict]:
    rows = []
    entry_qty = entry_time = entry_sym = None
    for ln in lines(path):
        ts = re.match(TS, ln)
        if not ts:
            continue
        m = re.search(r"order COMPLETE BUY (\d+) (\S+) -> \S+ at", ln)
        if m:
            entry_qty, entry_sym, entry_time = int(m.group(1)), m.group(2), ts.group(2)
        m = re.search(r"(LIVE|PAPER) EXIT ([^:]+): \S+ \(\w+\) (long|short) .*?option ([\d.]+) -> ([\d.]+) .*?" + RS, ln)
        if m:
            rows.append({
                "date": day, "strategy": "BankNifty CPR", "mode": m.group(1).title(),
                "side": m.group(3).title(), "instrument": entry_sym or "BANKNIFTY option",
                "entry_time": entry_time or "", "exit_time": ts.group(2), "qty": entry_qty,
                "entry_px": float(m.group(4)), "exit_px": float(m.group(5)),
                "pnl_rs": rupees(m.group(6)), "exit_reason": m.group(2).strip(), "price_note": "logged",
            })
            entry_qty = entry_time = entry_sym = None
    return rows


def sector(day: str, path: Path) -> list[dict]:
    """The 1.5R book only: it is the one that trades live (or would)."""
    rows = []
    entry = {}
    for ln in lines(path):
        ts = re.match(TS, ln)
        if not ts:
            continue
        m = re.search(r"\[1\.5R\] (LIVE|PAPER) (LONG|SHORT) (\S+) at [\d.]+ via BUY (\d+) (\S+) @ ([\d.]+)", ln)
        if m:
            entry = {"mode": m.group(1).title(), "side": m.group(2).title(), "qty": int(m.group(4)),
                     "instrument": m.group(5), "entry_px": float(m.group(6)), "entry_time": ts.group(2)}
            continue
        m = re.search(r"\[1\.5R\] (LIVE|PAPER) EXIT ([^:]+):.*?option \S+ ([\d.]+) -> ([\d.]+) .*?" + RS, ln)
        if m and entry:
            rows.append({
                "date": day, "strategy": "Sector 920", "mode": m.group(1).title(), "side": entry["side"],
                "instrument": entry["instrument"], "entry_time": entry["entry_time"],
                "exit_time": ts.group(2), "qty": entry["qty"], "entry_px": float(m.group(3)),
                "exit_px": float(m.group(4)), "pnl_rs": rupees(m.group(5)),
                "exit_reason": m.group(2).strip(), "price_note": "logged",
            })
            entry = {}
    return rows


def sensex(day: str, path: Path) -> list[dict]:
    """Rs is logged per lot; an exit line closes its share of the entry's lots."""
    rows = []
    trade = None
    for ln in lines(path):
        ts = re.match(TS, ln)
        if not ts:
            continue
        m = re.search(r"\[boxatr\] (LIVE signal|PAPER) (LONG|SHORT): .*?; (SENSEX\S+) premium ([\d.]+|unknown); "
                      r"(\d+) lots", ln)
        if m:
            trade = {"mode": "Live" if m.group(1).startswith("LIVE") else "Paper", "side": m.group(2).title(),
                     "instrument": m.group(3),
                     "entry_px": float(m.group(4)) if m.group(4) != "unknown" else None,
                     "lots": int(m.group(5)), "left": int(m.group(5)), "entry_time": ts.group(2), "exits": []}
            continue
        if not trade:
            continue
        # A partial exit names its lots, "EXIT lock stop (1 lot): ..."; a full exit closes what is left.
        m = re.search(r"\[boxatr\] (LIVE|PAPER) EXIT ([^:(]+?)(?: \((\d+) lot\))?:.*?option ([\d.]+) -> ([\d.]+) "
                      r".*?" + RS + " per lot", ln)
        if m:
            lots = int(m.group(3)) if m.group(3) else trade["left"]
            lots = min(lots, trade["left"])
            trade["left"] -= lots
            if trade["entry_px"] is None:
                trade["entry_px"] = float(m.group(4))
            trade["exits"].append((ts.group(2), m.group(2).strip(), float(m.group(5)), rupees(m.group(6)), lots))
        if trade["exits"] and (trade["left"] <= 0 or "day summary" in ln):
            pnl = sum(e[3] * e[4] for e in trade["exits"])
            rows.append({
                "date": day, "strategy": "Sensex cluster", "mode": trade["mode"], "side": trade["side"],
                "instrument": trade["instrument"], "entry_time": trade["entry_time"],
                "exit_time": trade["exits"][-1][0], "qty": None, "entry_px": trade["entry_px"],
                "exit_px": round(sum(e[2] * e[4] for e in trade["exits"]) / max(1, sum(e[4] for e in trade["exits"])), 2),
                "pnl_rs": round(pnl, 2),
                "exit_reason": " + ".join(dict.fromkeys(e[1] for e in trade["exits"])),
                "price_note": "logged",
            })
            trade = None
    return rows


def collect() -> list[dict]:
    """Every closed trade from the logs, oldest first (also used by strategies/scripts/live_guard.py)."""
    estimates = json.loads(ESTIMATES.read_text()) if ESTIMATES.exists() else {}
    rows = []
    for prefix in STRATEGIES:
        for day, path in log_files(prefix):
            if prefix.startswith("nifty"):
                rows += nifty(day, path, estimates)
            elif prefix.startswith("banknifty"):
                rows += banknifty(day, path)
            elif prefix.startswith("sector"):
                rows += sector(day, path)
            else:
                rows += sensex(day, path)
    rows.sort(key=lambda r: (r["date"], r["entry_time"]))
    for i, r in enumerate(rows, 1):
        r["trade_id"] = i
        r["result"] = "" if r["pnl_rs"] is None else ("Win" if r["pnl_rs"] > 0 else "Loss")
    return rows


def main() -> None:
    estimates = json.loads(ESTIMATES.read_text()) if ESTIMATES.exists() else {}
    rows = collect()
    by = defaultdict(int)
    for r in rows:
        by[r["strategy"]] += 1
    meta = [
        {"label": "export_trades.py", "value": Path(__file__).read_text(encoding="utf-8"),
         "type": "code", "lang": "python"},
        {"label": "Input", "value": f"log/strategies/*.log from {START_DATE}"},
        {"label": "Estimated exits", "value": json.dumps(estimates, indent=1)},
    ]
    OUT.write_text(json.dumps({"rows": rows, "meta": meta}, indent=1), encoding="utf-8")
    print(f"{len(rows)} trades -> {OUT}: " + ", ".join(f"{k} {v}" for k, v in by.items()))


if __name__ == "__main__":
    main()
