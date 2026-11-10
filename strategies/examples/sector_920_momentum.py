"""Sector-heatmap 09:20 momentum, traded with the stock's ATM option. LIVE from 2026-11-09 (book 1.5R).

From 2026-11-04 the trade is one lot of the picked stock's ATM option instead
of the stock: a CE for a long, a PE for a short. Every entry and exit decision
is still read off the STOCK's own price, never the option's: the option only
carries the position. TRADE_LIVE = False (the default) resolves the option,
logs the order it would send and tracks the option premium on paper; no
order is ever placed. With TRADE_LIVE = True the LIVE_BOOK sends real orders.

Order of work each day (the user's, 2026-11-03):
    1. Sector: at SNAPSHOT_TIME (09:20) read every Sector Heatmap stock (the
       NSE sectoral-index lists in data/sector_constituents.json) and NIFTY.
       % change = last price vs previous close, as on the heatmap. NIFTY
       decides the side: up -> LONG plan, down -> SHORT plan. Sectors are
       ranked by the average % change of their stocks. LONG: the greenest
       sector (it must be green). SHORT: the reddest (it must be red).
    2. Stock: in that sector, the biggest mover in NIFTY's direction whose move
       is at most MAX_MOVE_PCT (4%) AND that has listed NFO stock options.
       A stock beyond 4% or without options is skipped and the next taken.
       It must be green for a long, red for a short.
    3. Lot size: read from the option contract itself (every stock differs,
       e.g. BRITANNIA 125, WIPRO 3000), so one lot is always one exchange lot.
    4. Option: at ENTRY_TIME (09:21) the ATM strike (the listed strike nearest
       the stock's price), nearest expiry AFTER today, is bought for LOTS (1)
       lot: CE for a long, PE for a short.
    5. Levels, on the STOCK price at entry: stop STOP_PCT (1%) against the
       trade; target the book's % with it. Psychological levels: if a multiple
       of 100 lies between entry and target, the target sits PSY_BUFFER_PCT
       (0.05%) short of it.
    6. Exit: the option lot is sold when the STOCK hits the target or the stop,
       or at SQUARE_OFF (15:15). From 2026-11-03 to 11-10 a 10:10 time limit
       closed unfinished trades early; the user removed it on 2026-11-10 so a
       trade is left to reach its target or stop. One trade a day.

Two books run on the same pick, differing only in the target:
    1R     target 1.0%  (the rule as stated)
    1.5R   target 1.5%
LIVE_BOOK names the single book that would place real orders; the other is
always paper, so the two never double a real position.

Backtest of the STOCK version, 5m bars 2021-01..2026-09-30, today's sector
lists, 0.05% cost a trade, entry at the 09:20 bar's open (win % / PF):

                     2021-23        2024-25        2026           all
    1R    all       48% / 0.81     48% / 0.80     53% / 0.87     49% / 0.81
    1.5R  all       40% / 0.76     43% / 0.91     49% / 0.94     42% / 0.83

October 2026 replay (stock version): 1R 55% / 0.88, 1.5R 55% / 1.18. The
option version has not been backtested: the F&O-only stock list changes some
picks, and premium, spread and decay change every result.

No resting stop is parked at the broker: the in-process stop on the stock
price is the stop, and MIS square-off at the broker is the backstop if the
process dies with a live lot open.

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
OPTION_EXCHANGE = "NFO"
INDEX_SYMBOL, INDEX_EXCHANGE = "NIFTY", "NSE_INDEX"

SNAPSHOT_TIME = dtime(9, 20)
ENTRY_TIME = dtime(9, 21)
LATEST_ENTRY = dtime(9, 25)         # a late start after this skips the day
SQUARE_OFF = dtime(15, 15)         # MIS square-off; was a 10:10 time limit 2026-11-03..11-10

MAX_MOVE_PCT = 4.0
STOP_PCT = 1.0
BOOKS = {"1R": 1.0, "1.5R": 1.5}    # book name -> target % of the stock's entry price
PSY_STEP = 100                      # psychological levels: multiples of this
PSY_BUFFER_PCT = 0.05
MIN_SECTOR_COVER = 0.5              # a sector needs this share of its stocks quoted
COST_PCT = 0.05                     # stock-version backtest cost, for the % figures

# Option execution (from 2026-11-04)
TRADE_LIVE = True                   # live from 2026-11-09; False = dry run, no order is ever sent
LIVE_BOOK = "1.5R"                  # the one book that trades when TRADE_LIVE is True (1.5R from 2026-11-03)
LOTS = 1                            # lots per live entry; quantity = LOTS x contract lot size
PRODUCT = "MIS"                     # intraday; broker end-of-day square-off is the backstop
ORDER_POLL_TRIES = 10               # 1 s reads a market order is given to settle
ORDER_POLL_SECONDS = 1.0
EXIT_RETRY_SECONDS = 20             # gap between retries of an exit that did not fill
POSITION_CHECK_SECONDS = 30         # how often an open live lot is checked for a hand exit
MANUAL_EXIT_RECHECK_SECONDS = 2.0   # pause before trusting a flat position book

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


def quote(client, symbol: str, exchange: str) -> dict | None:
    """The quote's data block, or None when it cannot be read."""
    try:
        q = client.quotes(symbol=symbol, exchange=exchange)
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"quote raised {exc!r} for {symbol}")
        return None
    if not ok(q):
        return None
    return q.get("data") or {}


def ltp(client, symbol: str, exchange: str = STOCK_EXCHANGE) -> float | None:
    d = quote(client, symbol, exchange)
    if d is None:
        return None
    try:
        return float(d.get("ltp") or 0) or None
    except (TypeError, ValueError):
        return None


# =============================================================================
# OPTIONS
# =============================================================================

def option_expiry(client, symbol: str) -> str | None:
    """Nearest listed option expiry AFTER today for a stock, compact (24NOV26).

    Returns:
        None when the stock has no listed NFO options (not in F&O) or the
        lookup fails. Today's expiry is skipped: a 0-DTE option decays to
        nothing over a day's hold whatever the stock does.
    """
    try:
        e = client.expiry(symbol=symbol, exchange=OPTION_EXCHANGE, instrumenttype="options")
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"expiry lookup raised {exc!r} for {symbol}")
        return None
    if not ok(e) or not e.get("data"):
        return None
    today = datetime.now(IST).date()
    dated = []
    for dashed in e["data"]:
        try:
            dated.append((datetime.strptime(dashed, "%d-%b-%y").date(), dashed))
        except (TypeError, ValueError):
            continue
    for day, dashed in sorted(dated):
        if day > today:
            return dashed.replace("-", "").upper()
    return None


def resolve_atm(client, symbol: str, expiry: str, option_type: str) -> dict | None:
    """ATM option on the stock: the listed strike nearest its price right now.

    Returns:
        {"symbol", "lotsize", "spot"} or None. The lot size is the contract's
        own, read from the symbol master.
    """
    try:
        r = client.optionsymbol(underlying=symbol, exchange=STOCK_EXCHANGE,
                                expiry_date=expiry, offset="ATM", option_type=option_type)
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"optionsymbol raised {exc!r} for {symbol} {option_type}")
        return None
    if not ok(r):
        log(f"optionsymbol {symbol} {expiry} ATM {option_type} failed: {r}")
        return None
    try:
        lotsize = int(r["lotsize"])
    except (KeyError, TypeError, ValueError):
        log(f"no lot size for {r.get('symbol')}: {r}")
        return None
    if lotsize <= 0:
        log(f"bad lot size {lotsize} for {r.get('symbol')}")
        return None
    return {"symbol": r["symbol"], "lotsize": lotsize, "spot": r.get("underlying_ltp")}


# =============================================================================
# SELECTION
# =============================================================================

def pick(client, sectors: dict[str, list[str]]) -> dict | None:
    """The 09:20 decision: side from NIFTY, then sector, then an F&O stock.

    Returns:
        {"side", "sector", "sector_pct", "symbol", "stock_pct", "nifty_pct",
        "expiry"} or None when there is no trade today.
    """
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
        if not side * p > 0:
            break
        expiry = option_expiry(client, s)
        if expiry is None:
            log(f"skip {s} {p:+.2f}%: no listed NFO options")
            continue
        return {"side": "long" if side == 1 else "short", "sector": sector, "sector_pct": score,
                "symbol": s, "stock_pct": p, "nifty_pct": nifty, "expiry": expiry}
    log(f"no {sector} F&O stock moved 0-{MAX_MOVE_PCT:g}% in NIFTY's direction; no trade today")
    return None


def levels(entry: float, long: bool, target_pct: float) -> tuple[float, float, str]:
    """Stop and target on the STOCK price for one book, with the psychological-level rule."""
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
# LIVE ORDERS
# =============================================================================

def live_allowed(client) -> bool:
    """Whether real (or analyzer-simulated) orders may be sent today.

    TRADE_LIVE is the user's switch. The analyzer flag is read only to say
    in the log whether the orders are real or sandbox-simulated.
    """
    if not TRADE_LIVE:
        return False
    st = None
    for attempt in range(1, 4):
        try:
            st = client.analyzerstatus()
        except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
            log(f"analyzer status raised {exc!r} (attempt {attempt}/3)")
            st = None
        if ok(st):
            break
        if attempt < 3:
            time.sleep(2)
    if not ok(st):
        log(f"cannot read analyzer status, refusing to trade: {st}")
        return False
    if bool((st.get("data") or {}).get("analyze_mode")):
        log("analyzer mode is ON: orders are simulated by the sandbox")
    else:
        log("WARNING: analyzer mode is OFF and TRADE_LIVE is True: REAL ORDERS")
    return True


def order_status(client, order_id: str) -> tuple[str, float | None]:
    """Read one order: (lower-case status, or "" when unreadable; average price)."""
    try:
        r = client.orderstatus(order_id=order_id, strategy=STRATEGY_TAG)
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"orderstatus raised {exc!r} for {order_id}")
        return "", None
    if not ok(r):
        return "", None
    data = r.get("data") or {}
    status = str(data.get("order_status") or data.get("status") or "").lower()
    try:
        price = float(data.get("average_price") or 0) or None
    except (TypeError, ValueError):
        price = None
    return status, price


def settle_order(client, order_id: str) -> tuple[str, float | None]:
    """Wait for a market order to finish.

    Returns:
        ("complete", average price or None), ("rejected", None),
        ("cancelled", None) or ("unknown", None). An order still working
        after ORDER_POLL_TRIES reads is cancelled first, so a late fill
        cannot surprise a retry.
    """
    def terminal(status: str, price: float | None) -> tuple[str, float | None] | None:
        if "complete" in status:
            return "complete", price
        if "reject" in status:
            return "rejected", None
        if "cancel" in status:
            return "cancelled", None
        return None

    for attempt in range(ORDER_POLL_TRIES):
        if attempt:
            time.sleep(ORDER_POLL_SECONDS)
        done = terminal(*order_status(client, order_id))
        if done:
            return done
    try:
        client.cancelorder(order_id=order_id, strategy=STRATEGY_TAG)
    except Exception as exc:  # noqa: BLE001 - the read below decides
        log(f"cancel of unsettled order {order_id} raised {exc!r}")
    return terminal(*order_status(client, order_id)) or ("unknown", None)


def market_order(client, symbol: str, action: str, quantity: int) -> tuple[str, float | None]:
    """Place one market order on the option and read it to a final state.

    Returns:
        (status, average fill) as settle_order returns them. Only "complete"
        means the lot changed hands; "unknown" means the caller must ask the
        position book before believing either way.
    """
    try:
        r = client.placeorder(strategy=STRATEGY_TAG, symbol=symbol, action=action,
                              exchange=OPTION_EXCHANGE, price_type="MARKET",
                              product=PRODUCT, quantity=quantity)
    except Exception as exc:  # noqa: BLE001 - the broker may still have taken it
        log(f"order RAISED {action} {quantity} {symbol}: {exc!r}")
        return "unknown", None
    if not ok(r):
        log(f"order REJECTED {action} {quantity} {symbol}: {r}")
        return "rejected", None
    order_id = str(r.get("orderid") or "")
    if not order_id:
        log(f"order {action} {quantity} {symbol} returned no order id: {r}")
        return "unknown", None
    status, price = settle_order(client, order_id)
    fill = f" at {price:.2f}" if price else ""
    log(f"order {status.upper()} {action} {quantity} {symbol} -> {order_id}{fill}")
    return status, price


def net_quantity(client, symbol: str) -> int | None:
    """Net open quantity the broker reports for one option.

    Returns:
        The quantity, 0 when nothing is open, or None when the book cannot be read.
    """
    try:
        r = client.positionbook()
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"positionbook raised {exc!r}")
        return None
    if not ok(r):
        log(f"positionbook failed: {r}")
        return None
    for row in r.get("data") or []:
        if row.get("symbol") == symbol and row.get("exchange") == OPTION_EXCHANGE:
            try:
                return int(float(row.get("quantity") or 0))
            except (TypeError, ValueError):
                log(f"unreadable quantity on {symbol}: {row.get('quantity')!r}")
                return None
    return 0


def buy_live(client, option: str, quantity: int) -> tuple[bool, float | None]:
    """Buy the live lot. Returns (holding, average fill or None)."""
    status, fill = market_order(client, option, "BUY", quantity)
    if status == "complete":
        return True, fill
    if status == "unknown":
        held = net_quantity(client, option)
        if held is not None and held > 0:
            log(f"buy state unknown but the position book shows {held} {option}; holding it")
            return True, None
        if held is None:
            log(f"buy state unknown and the position book is unreadable; treating {option} as held")
            return True, None
    return False, None


def sell_live(client, book: dict) -> tuple[bool, float | None]:
    """Sell the live lot. Returns (flat, average fill or None).

    A refused or unknown sell is checked against the position book: if the
    lot is already gone (hand exit, broker square-off) the book is flat and
    no second sell is sent, which would open a short option.
    """
    status, fill = market_order(client, book["option"], "SELL", book["quantity"])
    if status == "complete":
        return True, fill
    held = net_quantity(client, book["option"])
    if held == 0:
        log(f"sell was {status}, but the position book shows {book['option']} flat; treating it as closed")
        return True, None
    return False, None


def closed_outside(client, book: dict) -> bool:
    """True when an open live lot is no longer in the position book (hand exit)."""
    if net_quantity(client, book["option"]) != 0:
        return False
    time.sleep(MANUAL_EXIT_RECHECK_SECONDS)
    return net_quantity(client, book["option"]) == 0


# =============================================================================
# BOOKS
# =============================================================================

def finish_book(client, name: str, book: dict, price: float, why: str, opt_fill: float | None) -> None:
    """Record a closed book: stock move and the option premium in and out."""
    long = book["side"] == "long"
    pct = ((price / book["entry"] - 1) if long else (1 - price / book["entry"])) * 100
    premium_out = opt_fill or ltp(client, book["option"], OPTION_EXCHANGE)
    book.update(exit=price, why=why, pct=round(pct, 3), premium_out=premium_out,
                closed=datetime.now(IST).isoformat(), exit_pending=None)
    opt = ""
    if book.get("premium_in") and premium_out:
        pts = premium_out - book["premium_in"]
        book["option_rs"] = round(pts * book["quantity"], 2)
        opt = (f"; option {book['option']} {book['premium_in']:.2f} -> {premium_out:.2f} "
               f"({pts:+.2f} x {book['quantity']} = Rs {pts * book['quantity']:+,.0f})")
    tag = "LIVE" if book.get("live") else "PAPER"
    log(f"[{name}] {tag} EXIT {why}: {book['side']} {book['symbol']} {book['entry']:.2f} -> {price:.2f}, "
        f"stock {pct:+.2f}%{opt}")


def close_book(client, name: str, book: dict, price: float, why: str) -> None:
    """Exit one book. A live book closes only once its sell is confirmed."""
    if not book.get("live"):
        finish_book(client, name, book, price, why, None)
        return
    last = book.get("last_exit_try") or 0
    if book.get("exit_pending") and time.time() - last < EXIT_RETRY_SECONDS:
        return
    book["exit_pending"] = book.get("exit_pending") or why
    book["last_exit_try"] = time.time()
    flat, fill = sell_live(client, book)
    if flat:
        finish_book(client, name, book, price, book["exit_pending"], fill)
    else:
        log(f"[{name}] LIVE exit ({book['exit_pending']}) not filled; retrying in {EXIT_RETRY_SECONDS}s")


def manage(client, state: dict) -> None:
    """Poll the STOCK until every book is closed; exits are read off its price."""
    books = state["books"]
    symbol = state["pick"]["symbol"]
    last_price = None
    last_position_check = time.time()
    while not _shutdown and any(b.get("exit") is None for b in books.values()):
        now = datetime.now(IST).time()
        price = ltp(client, symbol)
        if price is not None:
            last_price = price
        elif now >= SQUARE_OFF and last_price is not None:
            price = last_price          # square-off must not wait on a quote
        if price is None:
            time.sleep(POLL_SECONDS)
            continue
        check_hand_exit = time.time() - last_position_check >= POSITION_CHECK_SECONDS
        if check_hand_exit:
            last_position_check = time.time()
        for name, b in books.items():
            if b.get("exit") is not None:
                continue
            long = b["side"] == "long"
            if b.get("live") and not b.get("exit_pending") and check_hand_exit and closed_outside(client, b):
                finish_book(client, name, b, price, "closed outside", None)
            elif b.get("exit_pending"):
                close_book(client, name, b, price, b["exit_pending"])
            elif now >= SQUARE_OFF:
                close_book(client, name, b, price, "square-off")
            elif (price <= b["stop"]) if long else (price >= b["stop"]):
                close_book(client, name, b, price, "stop")
            elif (price >= b["target"]) if long else (price <= b["target"]):
                close_book(client, name, b, price, "target")
        save_state(state)
        time.sleep(POLL_SECONDS)


def open_books(client, state: dict, choice: dict) -> bool:
    """09:21: read the stock, resolve its ATM option and lot size, open the books."""
    entry = None
    while not _shutdown and entry is None and datetime.now(IST).time() <= LATEST_ENTRY:
        entry = ltp(client, choice["symbol"])
        if entry is None:
            time.sleep(2)
    if entry is None:
        log(f"no price for {choice['symbol']} by {LATEST_ENTRY:%H:%M}; no trade today")
        return False
    long = choice["side"] == "long"
    option_type = "CE" if long else "PE"
    leg = resolve_atm(client, choice["symbol"], choice["expiry"], option_type)
    if leg is None:
        log(f"no ATM {option_type} for {choice['symbol']} {choice['expiry']}; no trade today")
        return False
    quantity = leg["lotsize"] * LOTS
    oq = quote(client, leg["symbol"], OPTION_EXCHANGE) or {}
    try:
        premium = float(oq.get("ltp") or 0) or None
        bid, ask = float(oq.get("bid") or 0), float(oq.get("ask") or 0)
    except (TypeError, ValueError):
        premium, bid, ask = None, 0.0, 0.0
    spread = f", bid {bid:.2f} / ask {ask:.2f}" if bid and ask else ""
    log(f"option: {leg['symbol']} (ATM {option_type}, stock {entry:.2f}), lot size {leg['lotsize']}, "
        f"{LOTS} lot = qty {quantity}, premium {premium if premium else 'unread'}{spread}")

    live = LIVE_BOOK in BOOKS and live_allowed(client)
    live_fill = None
    if live:
        held, live_fill = buy_live(client, leg["symbol"], quantity)
        if not held:
            log(f"[{LIVE_BOOK}] live BUY did not fill; the book runs on paper today")
            live = False
    elif TRADE_LIVE:
        log("live trading refused; every book is paper today")
    else:
        log(f"DRY RUN: would BUY {quantity} {leg['symbol']} ({OPTION_EXCHANGE}, {PRODUCT}, MARKET) "
            f"for book {LIVE_BOOK}; no order sent")

    state["books"] = {}
    for name, target_pct in BOOKS.items():
        stop, target, note = levels(entry, long, target_pct)
        is_live = live and name == LIVE_BOOK
        premium_in = (live_fill or premium) if is_live else premium
        state["books"][name] = {"side": choice["side"], "symbol": choice["symbol"], "entry": entry,
                                "stop": stop, "target": target, "exit": None,
                                "option": leg["symbol"], "lotsize": leg["lotsize"], "quantity": quantity,
                                "premium_in": premium_in, "live": is_live, "exit_pending": None,
                                "opened": datetime.now(IST).isoformat()}
        tag = "LIVE" if is_live else "PAPER"
        prem = f" @ {premium_in:.2f}" if premium_in else ""
        log(f"[{name}] {tag} {choice['side'].upper()} {choice['symbol']} at {entry:.2f} via BUY "
            f"{quantity} {leg['symbol']}{prem}: stock stop {stop:.2f}, stock target {target:.2f}{note}")
    save_state(state)
    return True


def main() -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    client = build_client()
    mode = f"LIVE book {LIVE_BOOK}" if TRADE_LIVE else "DRY RUN - no order is ever sent"
    log(f"{STRATEGY_TAG} starting, {mode}: snapshot {SNAPSHOT_TIME:%H:%M}, entry {ENTRY_TIME:%H:%M} "
        f"with {LOTS} lot of the stock's ATM option (CE long / PE short, nearest expiry after today), "
        f"exits on the stock price: stop {STOP_PCT:g}%, books " +
        ", ".join(f"{n} target {t:g}%" for n, t in BOOKS.items()) +
        f", max move {MAX_MOVE_PCT:g}%, F&O stocks only, square-off {SQUARE_OFF:%H:%M}")

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
                f"{choice['sector']} ({choice['sector_pct']:+.2f}%), NIFTY {choice['nifty_pct']:+.2f}%, "
                f"option expiry {choice['expiry']}")
            sleep_until(ENTRY_TIME)
            if _shutdown or not open_books(client, state, choice):
                return 0
        else:
            log(f"resuming {state['pick']['side']} {state['pick']['symbol']} from saved state")
        manage(client, state)
        for name, b in state["books"].items():
            if b.get("pct") is not None:
                opt = f", option Rs {b['option_rs']:+,.0f}" if b.get("option_rs") is not None else ""
                log(f"[{name}] day summary: {b['why']}, stock {b['pct']:+.2f}% "
                    f"({b['pct'] - COST_PCT:+.2f}% after cost){opt}")
        if all(b.get("exit") is not None for b in state["books"].values()):
            log("all books closed; done for the day")
    finally:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
