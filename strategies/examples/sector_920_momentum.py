"""Sector-heatmap 09:20 momentum, NSE stocks, intraday. PAPER ONLY.

This script never places an order: there is no order call anywhere in it. It
reads live quotes, picks one stock a day exactly as the rule below says, and
logs a paper trade with entry, stop, target and exit.

Rule (the user's, 2026-09-30):
    1. At SNAPSHOT_TIME (09:20) read every Sector Heatmap stock (the NSE
       sectoral-index lists in data/sector_constituents.json) and NIFTY.
       % change = last price vs previous close, as on the heatmap.
    2. NIFTY decides the side: up -> LONG plan, down -> SHORT plan.
    3. Sectors are ranked by the average % change of their stocks, as on the
       heatmap. LONG: the greenest sector (it must be green). SHORT: the
       reddest sector (it must be red).
    4. In that sector take the biggest mover in NIFTY's direction whose move
       is at most MAX_MOVE_PCT (4%) - anything beyond 4% is skipped and the
       next one taken. It must be green for a long, red for a short.
    5. Enter at ENTRY_TIME (09:21). Stop STOP_PCT (1%) of the entry price
       against the trade; target TARGET_PCT of the entry price with it.
    6. Psychological levels: if a multiple of 100 lies between entry and
       target, the target sits PSY_BUFFER_PCT (0.05%) short of it (below it
       for a long, above it for a short).
    7. Exit at the target, the stop, or SQUARE_OFF (15:15). One trade a day.

Two paper books run on the same pick, differing only in the target:
    1R     target 1.0%  (the rule as stated)
    1.5R   target 1.5%

Backtest, 5m bars 2021-01..2026-09-30, today's sector lists for every year,
0.05% cost a trade, entry at the 09:20 bar's open (win % / profit factor):

                     2021-23        2024-25        2026           all
    1R    all       48% / 0.81     48% / 0.80     53% / 0.87     49% / 0.81
          longs     47% / 0.77     47% / 0.77     50% / 0.79     47% / 0.77
          shorts    50% / 0.87     49% / 0.84     56% / 0.96     51% / 0.87
    1.5R  all       40% / 0.76     43% / 0.91     49% / 0.94     42% / 0.83

The backtest loses in every period; the user chose to run it as a dry run
anyway to see it live. Reproduce: Claude session c8d3fa98 scratchpad
sector920.py / rr.py.

Note on logging: the strategy host captures stdout to log/strategies/, so
print() is the logging channel here. That is the host contract and differs
deliberately from the repo-wide rule that application modules use utils.logging.
"""

from __future__ import annotations

import json
import math
import os
import signal
import sys
import time
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from openalgo import api

# =============================================================================
# CONFIGURATION  (per-strategy env vars do not exist in the host - edit here)
# =============================================================================

STRATEGY_TAG = "SECTOR_920_MOMENTUM"
STOCK_EXCHANGE = "NSE"
INDEX_SYMBOL, INDEX_EXCHANGE = "NIFTY", "NSE_INDEX"

SNAPSHOT_TIME = dtime(9, 20)
ENTRY_TIME = dtime(9, 21)
LATEST_ENTRY = dtime(9, 25)         # a late start after this skips the day
SQUARE_OFF = dtime(15, 15)

MAX_MOVE_PCT = 4.0
STOP_PCT = 1.0
BOOKS = {"1R": 1.0, "1.5R": 1.5}    # book name -> target % of entry price
PSY_STEP = 100                      # psychological levels: multiples of this
PSY_BUFFER_PCT = 0.05
MIN_SECTOR_COVER = 0.5              # a sector needs this share of its stocks quoted
COST_PCT = 0.05                     # assumed round-trip cost in the backtest
NOTIONAL = 100_000                  # rupees per paper trade, for the Rs figures

POLL_SECONDS = 5
IST = ZoneInfo("Asia/Kolkata")
REPO = Path(__file__).resolve().parents[2]
SECTOR_FILE = REPO / "data" / "sector_constituents.json"
STATE_DIR = Path("strategies") / "state"
STATE_FILE = STATE_DIR / "sector_920_momentum.json"

_shutdown = False


# =============================================================================
# HELPERS
# =============================================================================

def log(msg: str) -> None:
    print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S IST}] {msg}", flush=True)


def _on_signal(signum, _frame):
    global _shutdown
    _shutdown = True
    log(f"signal {signum} received, shutting down")


def _api_key_from_env_file() -> str | None:
    """Read OPENALGO_API_KEY straight from the project .env.

    Returns:
        The key, or None when no reachable .env carries one.
    """
    seen = set()
    candidates = [Path.cwd() / ".env"]
    candidates += [parent / ".env" for parent in Path(__file__).resolve().parents]
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() != "OPENALGO_API_KEY":
                continue
            value = value.strip().strip("'\"").strip()
            if value:
                return value
    return None


def build_client() -> api:
    key = os.getenv("OPENALGO_API_KEY")
    if not key:
        key = _api_key_from_env_file()
        if key:
            log("OPENALGO_API_KEY was not injected by the host; read it from .env")
    if not key:
        log("FATAL: OPENALGO_API_KEY is neither in the environment nor in any .env")
        sys.exit(1)
    return api(api_key=key, host=os.getenv("HOST_SERVER") or
               os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000"))


def ok(resp) -> bool:
    return isinstance(resp, dict) and resp.get("status") == "success"


def sleep_until(when: dtime) -> None:
    while not _shutdown and datetime.now(IST).time() < when:
        time.sleep(1)


# =============================================================================
# MARKET DATA
# =============================================================================

def load_sectors() -> dict[str, list[str]]:
    """Sector name -> constituent symbols, from the heatmap's list."""
    raw = json.loads(SECTOR_FILE.read_text(encoding="utf-8"))
    log(f"sector lists from NSE, generated {raw.get('generated_at')}")
    return {s["name"]: [x["symbol"] for x in s["stocks"]] for s in raw["sectors"]}


def changes(client, symbols: list[tuple[str, str]]) -> dict[str, float]:
    """% change from the previous close for each (symbol, exchange); unreadable ones are left out."""
    try:
        q = client.multiquotes(symbols=[{"symbol": s, "exchange": e} for s, e in symbols])
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"multiquotes raised {exc!r}")
        return {}
    if not ok(q):
        log(f"multiquotes failed: {str(q)[:200]}")
        return {}
    out = {}
    for item in q.get("results") or []:
        d = item.get("data") or {}
        try:
            ltp, prev = float(d.get("ltp") or 0), float(d.get("prev_close") or 0)
        except (TypeError, ValueError):
            continue
        if ltp > 0 and prev > 0:
            out[item.get("symbol")] = (ltp / prev - 1) * 100
    return out


def ltp(client, symbol: str, exchange: str = STOCK_EXCHANGE) -> float | None:
    try:
        q = client.quotes(symbol=symbol, exchange=exchange)
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"quote raised {exc!r} for {symbol}")
        return None
    if not ok(q):
        return None
    return float((q.get("data") or {}).get("ltp") or 0) or None


# =============================================================================
# SELECTION
# =============================================================================

def pick(client, sectors: dict[str, list[str]]) -> dict | None:
    """The 09:20 decision: side from NIFTY, sector, then stock. None = no trade today."""
    stocks = sorted({s for members in sectors.values() for s in members})
    nifty = changes(client, [(INDEX_SYMBOL, INDEX_EXCHANGE)]).get(INDEX_SYMBOL)
    pct = changes(client, [(s, STOCK_EXCHANGE) for s in stocks])
    if nifty is None:
        log("NIFTY unreadable; no trade today")
        return None
    log(f"snapshot: NIFTY {nifty:+.2f}%, {len(pct)}/{len(stocks)} stocks quoted")
    if nifty == 0:
        log("NIFTY flat; no trade today")
        return None
    side = 1 if nifty > 0 else -1

    scores = {}
    for name, members in sectors.items():
        got = [pct[s] for s in members if s in pct]
        if members and len(got) >= max(1, int(len(members) * MIN_SECTOR_COVER)):
            scores[name] = sum(got) / len(got)
    if not scores:
        log("no sector has enough quotes; no trade today")
        return None
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=(side == 1))
    log("sectors " + ("greenest" if side == 1 else "reddest") + " first: " +
        ", ".join(f"{n.replace('Nifty ', '')} {v:+.2f}%" for n, v in ranked[:6]))
    sector, score = ranked[0]
    if not side * score > 0:
        log(f"{sector} is not on NIFTY's side ({score:+.2f}%); no trade today")
        return None

    moves = sorted(((pct[s], s) for s in sectors[sector] if s in pct), reverse=(side == 1))
    log(f"{sector} stocks: " + ", ".join(f"{s} {p:+.2f}%" for p, s in moves[:8]))
    for p, s in moves:
        if side * p > MAX_MOVE_PCT:
            log(f"skip {s} {p:+.2f}%: beyond {MAX_MOVE_PCT:g}%")
            continue
        if side * p > 0:
            return {"side": "long" if side == 1 else "short", "sector": sector, "sector_pct": score,
                    "symbol": s, "stock_pct": p, "nifty_pct": nifty}
        break
    log(f"no {sector} stock moved 0-{MAX_MOVE_PCT:g}% in NIFTY's direction; no trade today")
    return None


def levels(entry: float, long: bool, target_pct: float) -> tuple[float, float, str]:
    """Stop and target for one book, with the psychological-level rule."""
    stop = entry * (1 - STOP_PCT / 100) if long else entry * (1 + STOP_PCT / 100)
    target = entry * (1 + target_pct / 100) if long else entry * (1 - target_pct / 100)
    note = ""
    if long:
        level = math.floor(entry / PSY_STEP) * PSY_STEP + PSY_STEP
        if level < target:
            target, note = max(level - entry * PSY_BUFFER_PCT / 100, entry * 1.0001), f" (cut below {level:g})"
    else:
        level = math.ceil(entry / PSY_STEP) * PSY_STEP - PSY_STEP
        if level > target:
            target, note = min(level + entry * PSY_BUFFER_PCT / 100, entry * 0.9999), f" (cut above {level:g})"
    return round(stop, 2), round(target, 2), note


# =============================================================================
# STATE
# =============================================================================

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (OSError, ValueError) as exc:
            log(f"state unreadable ({exc}), starting clean")
    return {}


def save_state(state: dict) -> None:
    """Persist state, but never let a disk problem end the session."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str))
        os.replace(tmp, STATE_FILE)
    except (OSError, TypeError, ValueError) as exc:
        log(f"could not persist state ({exc}); carrying on in memory")


# =============================================================================
# PAPER TRADE
# =============================================================================

def close_book(name: str, book: dict, price: float, why: str) -> None:
    long = book["side"] == "long"
    pct = ((price / book["entry"] - 1) if long else (1 - price / book["entry"])) * 100
    book.update(exit=price, why=why, pct=round(pct, 3), closed=datetime.now(IST).isoformat())
    net = pct - COST_PCT
    log(f"[{name}] PAPER EXIT {why}: {book['side']} {book['symbol']} {book['entry']:.2f} -> {price:.2f}, "
        f"{pct:+.2f}% gross, {net:+.2f}% after {COST_PCT:g}% cost = Rs {net / 100 * NOTIONAL:+,.0f} "
        f"on Rs {NOTIONAL:,}")


def manage(client, state: dict) -> None:
    """Poll the stock until every book is closed or it is square-off time."""
    books = state["books"]
    symbol = state["pick"]["symbol"]
    while not _shutdown and any(b.get("exit") is None for b in books.values()):
        now = datetime.now(IST).time()
        price = ltp(client, symbol)
        if price is None:
            time.sleep(POLL_SECONDS)
            continue
        for name, b in books.items():
            if b.get("exit") is not None:
                continue
            long = b["side"] == "long"
            if now >= SQUARE_OFF:
                close_book(name, b, price, "square-off")
            elif (price <= b["stop"]) if long else (price >= b["stop"]):
                close_book(name, b, price, "stop")
            elif (price >= b["target"]) if long else (price <= b["target"]):
                close_book(name, b, price, "target")
        save_state(state)
        time.sleep(POLL_SECONDS)


def main() -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    client = build_client()
    log(f"{STRATEGY_TAG} starting, PAPER ONLY - no order is ever sent: snapshot {SNAPSHOT_TIME:%H:%M}, "
        f"entry {ENTRY_TIME:%H:%M}, stop {STOP_PCT:g}%, books " +
        ", ".join(f"{n} target {t:g}%" for n, t in BOOKS.items()) +
        f", max move {MAX_MOVE_PCT:g}%, square-off {SQUARE_OFF:%H:%M}")

    today = str(datetime.now(IST).date())
    state = load_state()
    if state.get("date") != today:
        state = {"date": today}
    try:
        if "pick" in state and state["pick"] is None:
            log("today's 09:20 check already found no trade; nothing to do")
            return 0
        if not state.get("books"):
            if datetime.now(IST).time() > LATEST_ENTRY:
                log(f"started after {LATEST_ENTRY:%H:%M} with no trade on record; nothing to do today")
                return 0
            sectors = load_sectors()
            sleep_until(SNAPSHOT_TIME)
            if _shutdown:
                return 0
            choice = pick(client, sectors)
            state["pick"] = choice
            save_state(state)
            if choice is None:
                return 0
            log(f"PICK: {choice['side'].upper()} {choice['symbol']} ({choice['stock_pct']:+.2f}%) from "
                f"{choice['sector']} ({choice['sector_pct']:+.2f}%), NIFTY {choice['nifty_pct']:+.2f}%")
            sleep_until(ENTRY_TIME)
            entry = None
            while not _shutdown and entry is None and datetime.now(IST).time() <= LATEST_ENTRY:
                entry = ltp(client, choice["symbol"])
                if entry is None:
                    time.sleep(2)
            if entry is None:
                log(f"no price for {choice['symbol']} by {LATEST_ENTRY:%H:%M}; no trade today")
                return 0
            long = choice["side"] == "long"
            state["books"] = {}
            for name, target_pct in BOOKS.items():
                stop, target, note = levels(entry, long, target_pct)
                state["books"][name] = {"side": choice["side"], "symbol": choice["symbol"], "entry": entry,
                                        "stop": stop, "target": target, "exit": None,
                                        "opened": datetime.now(IST).isoformat()}
                log(f"[{name}] PAPER {choice['side'].upper()} {choice['symbol']} at {entry:.2f}: stop {stop:.2f}, "
                    f"target {target:.2f}{note}")
            save_state(state)
        else:
            log(f"resuming {state['pick']['side']} {state['pick']['symbol']} from saved state")
        manage(client, state)
        closed = [b for b in state["books"].values() if b.get("exit") is not None]
        for name, b in state["books"].items():
            if b.get("pct") is not None:
                log(f"[{name}] day summary: {b['why']}, {b['pct'] - COST_PCT:+.2f}% after cost")
        if len(closed) == len(state["books"]):
            log("all books closed; done for the day")
    finally:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
