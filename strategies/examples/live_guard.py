"""Live-trading stop rule shared by the live strategies (user's rule, 2026-11-17).

Once a strategy has REVIEW_AT (30) closed LIVE trades since 2026-11-09 and their
profit factor (rupees won / rupees lost, option premium before charges) is
MAX_STOP_PF (1.0) or less, the strategy stops sending real orders and runs on
paper from its next entry on.

Losing-streak rule (user's rule, 2026-11-20): a strategy listed in
MAX_LOSS_STREAK stops live trading once that many of its most recent closed
live trades are all losses. BANKNIFTY CPR is set to 4: its longest losing run
in the last two years of backtest was 3, and it had 2 live losses in a row
(11-12, 11-20) when the rule was added. The stop is latched like the PF rule.

Each strategy calls live_blocked() just before a new entry, while it is flat,
so a trade that is already open is always exited the way it was entered.

The trades are read from the strategy logs by the tracker's parser
(backtesting/live_tracker/export_trades.py), the same numbers the Live
Strategy Tracker shows. A stop is latched in strategies/state/live_guard.json
so it holds even if old logs are deleted; only removing that strategy's entry
from the file turns live trading back on, and that is the user's decision.

If the check itself fails, live orders are refused for that entry (paper
instead) and the reason is logged: the rule must never be skipped silently.

Tracked twin: strategies/examples/live_guard.py.
"""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REVIEW_AT = 30          # closed live trades before the rule can stop a strategy
MAX_STOP_PF = 1.0       # profit factor at or below this, after REVIEW_AT trades, stops live trading
MAX_LOSS_STREAK = {     # tracker name -> live losses in a row that stop live trading
    "BankNifty CPR": 4,
}

IST = ZoneInfo("Asia/Kolkata")
REPO = Path(__file__).resolve().parents[2]
EXPORT = REPO / "backtesting" / "live_tracker" / "export_trades.py"
LATCH = REPO / "strategies" / "state" / "live_guard.json"


def _parser():
    spec = importlib.util.spec_from_file_location("live_tracker_export", EXPORT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {EXPORT}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def live_stats(strategy: str) -> dict:
    """Closed live trades, rupees won and lost, and profit factor for one strategy.

    Args:
        strategy: the tracker's name for it, e.g. "NIFTY 2-candle".

    Returns:
        {"trades", "won", "lost", "net", "pf", "streak"}; pf is None when nothing was
        lost; streak is the live losses in a row at the end of the list.
    """
    rows = [r for r in _parser().collect()
            if r["strategy"] == strategy and r["mode"] == "Live" and r["pnl_rs"] is not None]
    rows.sort(key=lambda r: (r["date"], r["entry_time"] or ""))
    streak = 0
    for r in reversed(rows):
        if r["pnl_rs"] >= 0:
            break
        streak += 1
    won = sum(r["pnl_rs"] for r in rows if r["pnl_rs"] > 0)
    lost = -sum(r["pnl_rs"] for r in rows if r["pnl_rs"] < 0)
    return {"trades": len(rows), "won": round(won, 2), "lost": round(lost, 2),
            "net": round(won - lost, 2), "pf": round(won / lost, 3) if lost > 0 else None,
            "streak": streak}


def _read_latch() -> dict:
    if not LATCH.exists():
        return {}
    return json.loads(LATCH.read_text(encoding="utf-8"))


def _write_latch(latch: dict) -> None:
    LATCH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LATCH.with_suffix(".tmp")
    tmp.write_text(json.dumps(latch, indent=2), encoding="utf-8")
    os.replace(tmp, LATCH)


def live_blocked(strategy: str, log=print) -> bool:
    """True when this strategy must not send real orders for its next entry.

    Args:
        strategy: the tracker's name for it.
        log: the strategy's own log function.
    """
    try:
        latch = _read_latch()
        if strategy in latch:
            s = latch[strategy]
            why = s.get("reason") or f"{s.get('trades')} live trades at PF {s.get('pf')}"
            log(f"LIVE GUARD: {strategy} was stopped on {s.get('stopped_at')} ({why}); "
                f"paper only (remove it from {LATCH.name} to trade live again)")
            return True
        st = live_stats(strategy)
        pf_text = "no losses" if st["pf"] is None else f"{st['pf']:.2f}"
        if st["trades"] >= REVIEW_AT and st["pf"] is not None and st["pf"] <= MAX_STOP_PF:
            latch[strategy] = {"stopped_at": datetime.now(IST).isoformat(timespec="seconds"),
                               "trades": st["trades"], "pf": st["pf"], "net_rs": st["net"]}
            _write_latch(latch)
            log(f"LIVE GUARD: {strategy} STOPPED - {st['trades']} live trades, profit factor {pf_text} "
                f"(needs > {MAX_STOP_PF:g}), net Rs {st['net']:+,.0f}; no more real orders, paper from now")
            return True
        cap = MAX_LOSS_STREAK.get(strategy)
        if cap is not None and st["streak"] >= cap:
            reason = f"{st['streak']} live losses in a row (limit {cap})"
            latch[strategy] = {"stopped_at": datetime.now(IST).isoformat(timespec="seconds"),
                               "trades": st["trades"], "pf": st["pf"], "net_rs": st["net"],
                               "reason": reason}
            _write_latch(latch)
            log(f"LIVE GUARD: {strategy} STOPPED - {reason}, net Rs {st['net']:+,.0f}; "
                f"no more real orders, paper from now")
            return True
        streak_text = f", losses in a row {st['streak']}/{cap}" if cap is not None else ""
        log(f"live guard: {strategy} {st['trades']}/{REVIEW_AT} live trades, PF {pf_text}, "
            f"net Rs {st['net']:+,.0f}{streak_text}; live allowed")
        return False
    except Exception as exc:  # noqa: BLE001 - the rule must fail safe, never skip
        log(f"LIVE GUARD: check failed ({exc!r}); refusing real orders for this entry, paper instead")
        return True
