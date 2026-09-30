"""BANKNIFTY two-candle VWAP + SMA(45) breakout on 3-minute candles. PAPER ONLY.

This script never places an order: there is no order call anywhere in it. It
reads BANKNIFTY spot (NSE_INDEX), finds the signals, and logs a paper record
with the real premium of the nearest monthly NFO option ITM_POINTS (200) in the
money - CE below ATM, PE above - at entry and exit. BANKNIFTY has listed
monthly expiries only since November 2024.

Bars: 3-minute candles built from 1-minute history, anchored at 09:15, exactly
as the backtest builds them. VWAP is the day's index typical price weighted by
the summed volume of the six largest banks (the index has none). SMA(45) runs
over 3m closes across sessions, so prior sessions are fetched once a day.

Rule (long; short is the mirror):
    1. Two consecutive completed candles lie COMPLETELY above both VWAP and
       SMA(45) (low above the higher of the two lines), the candle before them
       did not, and candle 2 closes above candle 1's close.
    2. Entry: at the next bar's open - here, the index price when candle 2 has
       just completed - between NO_NEW_ENTRY_BEFORE (10:15) and
       NO_NEW_ENTRY_AFTER (14:30).
    3. Stop: candle 1's low less STOP_BUFFER_PCT (0.02%) of price.
    4. No target and no trail: held to the stop or SQUARE_OFF (15:00).
    5. At most MAX_TRADES_PER_DAY (2), one position at a time per book.

Two paper books run side by side on the same bars:
    plain     the rule above
    breadth   the rule above, taken only when the 14 NIFTY Bank stocks are
              one-sided by BREADTH_MIN (2x): advancers at least twice decliners
              or the reverse, in EITHER direction ("a decisive day"), read
              against each stock's previous close when the signal fires.

Backtest, BANKNIFTY 1m -> 3m, 2021-01..2026-09-30, 4 pts cost a trade, 1 lot
of 30, stops filled AT the stop. Chosen on 2021-23, validated on 2024-25,
2026 untouched until the end:

                 trades  2021-23 PF  2024-25 PF  2026 PF  gross/trade  PF @8 pts
    plain         1,981     1.19        1.06       1.30     +10.9        1.06
    breadth       1,343     1.27        1.11       1.32     +13.6        1.12

    plain by year (Rs): 144,465 / 50,667 / 46,045 / 13,608 / 40,810 / 2026 117,301
    breadth by year:    179,420 / 13,425 / 45,798 / 80,770 / -19,764 / 2026 87,097

    Breadth in the trade's direction (the NIFTY live rule) HURT here: 1.5x
    gave PF 1.00 / 0.96 / 0.99. Only the either-direction test helps.

THE REASON THIS DRY RUN EXISTS is cost: the edge is only ~11-14 index points
a trade, about what monthly-option spreads and slippage may take. Every exit
logs the option premium in and out, and every stop exit the index price
actually seen. Reproduce: Claude session c8d3fa98 scratchpad bnf_lowtf.py /
bnf3m_breadth.py.

Note on logging: the strategy host captures stdout to log/strategies/, so
print() is the logging channel here. That is the host contract and differs
deliberately from the repo-wide rule that application modules use utils.logging.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from openalgo import api

# =============================================================================
# CONFIGURATION  (per-strategy env vars do not exist in the host - edit here)
# =============================================================================

STRATEGY_TAG = "BANKNIFTY_2CANDLE_3M"
UNDERLYING = "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
EQUITY_EXCHANGE = "NSE"
FO_EXCHANGE = "NFO"

BAR_MINUTES = 3
SMA_PERIOD = 45
PRIOR_DAYS = 6                      # calendar days of 1m history for the SMA warm-up
STOP_BUFFER_PCT = 0.0002            # stop sits this far beyond candle 1
MAX_TRADES_PER_DAY = 2
COST_PTS = 4.0                      # assumed round-trip cost in the backtest
ITM_POINTS = 200                    # paper strike this far in the money

BREADTH_MIN = 2.0                   # one-sided either way
BREADTH_MIN_STOCKS = 12             # skip the signal if fewer quotes are readable
BOOKS = ("plain", "breadth")

# The six largest NIFTY Bank constituents, whose summed volume weights VWAP.
VOLUME_CONSTITUENTS = ["AXISBANK", "HDFCBANK", "ICICIBANK", "INDUSINDBK", "KOTAKBANK", "SBIN"]
# All 14 NIFTY Bank constituents (YESBANK and UNIONBANK joined 2025-12-31), for breadth.
BREADTH_CONSTITUENTS = VOLUME_CONSTITUENTS + [
    "BANKBARODA", "AUBANK", "FEDERALBNK", "IDFCFIRSTB", "PNB", "CANBK", "YESBANK", "UNIONBANK",
]

SESSION_OPEN = dtime(9, 15)
NO_NEW_ENTRY_BEFORE = dtime(10, 15)
NO_NEW_ENTRY_AFTER = dtime(14, 30)
SQUARE_OFF = dtime(15, 0)

BAR_SECONDS = BAR_MINUTES * 60
BAR_DELAY = 8                       # wait this long after a bar closes
STOP_POLL_SECONDS = 5               # index polling while a position is open
HISTORY_PACE = 0.4                  # seconds between history calls

IST = ZoneInfo("Asia/Kolkata")
STATE_DIR = Path("strategies") / "state"
STATE_FILE = STATE_DIR / "banknifty_2candle_3m.json"

_shutdown = False
_prior: dict = {}                   # {"date": today, "frame": prior sessions' 3m bars}


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


# =============================================================================
# MARKET DATA
# =============================================================================

def history_1m(client, symbol: str, exchange: str, start: str, end: str,
               retries: int = 3) -> pd.DataFrame:
    """1m bars in IST, de-duplicated and sorted; empty when unavailable."""
    for attempt in range(1, retries + 1):
        try:
            result = client.history(symbol=symbol, exchange=exchange, interval="1m",
                                    start_date=start, end_date=end)
        except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
            log(f"history raised {exc!r} for {symbol}")
            result = None
        if isinstance(result, pd.DataFrame) and not result.empty:
            frame = result[~result.index.duplicated(keep="last")].sort_index()
            frame = frame.tz_localize(IST) if frame.index.tz is None else frame.tz_convert(IST)
            return frame[(frame.index.time >= SESSION_OPEN) & (frame.index.time <= dtime(15, 29))]
        time.sleep(HISTORY_PACE * (2 ** attempt))
    log(f"history {exchange}/{symbol}: no 1m bars after {retries} attempts")
    return pd.DataFrame()


def to_3m(frame: pd.DataFrame, columns: dict) -> pd.DataFrame:
    """Resample 1m bars to BAR_MINUTES bars anchored at 09:15, session by session."""
    parts = []
    for day, g in frame.groupby(frame.index.date):
        origin = pd.Timestamp(datetime.combine(day, SESSION_OPEN), tz=IST)
        parts.append(g.resample(f"{BAR_MINUTES}min", origin=origin, label="left").agg(columns)
                     .dropna(subset=[next(iter(columns))]))
    return pd.concat(parts) if parts else pd.DataFrame()


OHLC = {"open": "first", "high": "max", "low": "min", "close": "last"}


def completed(frame: pd.DataFrame) -> pd.DataFrame:
    """Only bars whose full BAR_MINUTES have elapsed."""
    return frame[frame.index + timedelta(minutes=BAR_MINUTES) <= datetime.now(IST)]


def fetch_prior(client) -> pd.DataFrame | None:
    """Prior sessions' 3m index bars for the SMA warm-up, fetched once a day."""
    today = datetime.now(IST).date()
    if _prior.get("date") == today:
        return _prior["frame"]
    bars = history_1m(client, UNDERLYING, INDEX_EXCHANGE,
                      str(today - timedelta(days=PRIOR_DAYS)), str(today - timedelta(days=1)))
    if bars.empty:
        return None
    frame = to_3m(bars[bars.index.date < today], OHLC)
    _prior.update(date=today, frame=frame)
    log(f"cached {len(frame)} prior 3m bars over {len(set(frame.index.date))} sessions for SMA({SMA_PERIOD})")
    return frame


def fetch_today(client, prior: pd.DataFrame) -> pd.DataFrame:
    """Today's completed 3m bars with VWAP and SMA(45); empty when unavailable.

    VWAP needs all six banks; if any is missing the frame is returned without
    a usable VWAP and the caller skips signals.
    """
    today = str(datetime.now(IST).date())
    idx = history_1m(client, UNDERLYING, INDEX_EXCHANGE, today, today)
    if idx.empty:
        return idx
    bars = completed(to_3m(idx, OHLC))
    if bars.empty:
        return bars
    volume = pd.Series(0.0, index=bars.index)
    readable = 0
    for name in VOLUME_CONSTITUENTS:
        time.sleep(HISTORY_PACE)
        b = history_1m(client, name, EQUITY_EXCHANGE, today, today, retries=2)
        if b.empty or "volume" not in b:
            continue
        readable += 1
        v = to_3m(b[["volume"]], {"volume": "sum"})["volume"]
        volume = volume.add(v.reindex(bars.index).fillna(0.0), fill_value=0.0)
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    cum = volume.cumsum()
    bars = bars.copy()
    bars["vwap"] = (typical * volume).cumsum() / cum.where(cum > 0)
    bars.attrs["banks"] = readable
    closes = pd.concat([prior["close"], bars["close"]]) if prior is not None else bars["close"]
    bars["sma"] = closes.rolling(SMA_PERIOD).mean().reindex(bars.index)
    return bars


def read_breadth(client) -> tuple[int, int] | None:
    """NIFTY Bank advancers and decliners against each stock's previous close."""
    symbols = [{"symbol": s, "exchange": EQUITY_EXCHANGE} for s in BREADTH_CONSTITUENTS]
    try:
        q = client.multiquotes(symbols=symbols)
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"breadth multiquotes raised {exc!r}")
        return None
    if not ok(q):
        log(f"breadth multiquotes failed: {q}")
        return None
    advancers = decliners = readable = 0
    for item in q.get("results") or []:
        d = item.get("data") or {}
        try:
            ltp = float(d.get("ltp") or 0)
            prev = float(d.get("prev_close") or 0)
        except (TypeError, ValueError):
            continue
        if ltp <= 0 or prev <= 0:
            continue
        readable += 1
        if ltp > prev:
            advancers += 1
        elif ltp < prev:
            decliners += 1
    if readable < BREADTH_MIN_STOCKS:
        log(f"breadth: only {readable}/{len(BREADTH_CONSTITUENTS)} quotes readable")
        return None
    return advancers, decliners


def index_ltp(client) -> float | None:
    """Last traded index price, or None when the quote cannot be read."""
    try:
        q = client.quotes(symbol=UNDERLYING, exchange=INDEX_EXCHANGE)
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"index quote raised {exc!r}")
        return None
    if not ok(q):
        return None
    return float((q.get("data") or {}).get("ltp") or 0) or None


def option_ltp(client, symbol: str) -> float | None:
    """Last traded premium of one option leg, for the paper record."""
    try:
        q = client.quotes(symbol=symbol, exchange=FO_EXCHANGE)
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"quote raised {exc!r} for {symbol}")
        return None
    if not ok(q):
        return None
    return float((q.get("data") or {}).get("ltp") or 0) or None


def nearest_expiry(client) -> str | None:
    """Nearest expiry AFTER today, compact form such as 28OCT26.

    Today's expiry is skipped: a 0-DTE option held for hours decays to nothing
    whatever the index does, so it cannot express the move being measured.
    """
    e = client.expiry(symbol=UNDERLYING, exchange=FO_EXCHANGE, instrumenttype="options")
    if not ok(e) or not e.get("data"):
        log(f"expiry lookup failed: {e}")
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
    log("no listed expiry after today")
    return None


def resolve_leg(client, option_type: str) -> dict | None:
    """Option ITM_POINTS in the money, nearest expiry after today.

    ATM (listed strike nearest spot) comes from optionsymbol; the strike then
    moves ITM_POINTS into the money, down for a CE and up for a PE. The target
    contract must be listed, or the paper leg is left unresolved.
    """
    expiry = nearest_expiry(client)
    if not expiry:
        return None
    r = client.optionsymbol(underlying=UNDERLYING, exchange=INDEX_EXCHANGE,
                            expiry_date=expiry, offset="ATM", option_type=option_type)
    if not ok(r):
        log(f"optionsymbol {option_type} failed: {r}")
        return None
    prefix = f"{UNDERLYING}{expiry}"
    atm_symbol = r["symbol"]
    try:
        atm = float(atm_symbol[len(prefix):-2])
    except ValueError:
        log(f"cannot read the strike from {atm_symbol}")
        return None
    strike = atm - ITM_POINTS if option_type == "CE" else atm + ITM_POINTS
    symbol = f"{prefix}{strike:g}{option_type}"
    s = client.symbol(symbol=symbol, exchange=FO_EXCHANGE)
    if not ok(s):
        log(f"{symbol} ({ITM_POINTS} pts ITM of {atm_symbol}) is not listed: {s}")
        return None
    log(f"strike {strike:g}: ATM {atm:g} (spot {r.get('underlying_ltp')}) moved {ITM_POINTS} pts ITM")
    return {"symbol": symbol, "lotsize": int(r["lotsize"])}


# =============================================================================
# SIGNAL
# =============================================================================

def two_candle(bars: pd.DataFrame) -> dict | None:
    """First qualifying pair ending on the LAST completed bar, as backtested.

    Returns:
        {"direction", "stop", "c1", "c2"} or None.
    """
    # Candle 1 is never the session's first bar (the backtest skips that, and
    # entries start at 10:15), so the candle before the pair is always today's.
    if len(bars) < 3:
        return None
    before, c1, c2 = bars.iloc[-3], bars.iloc[-2], bars.iloc[-1]

    def lines(bar):
        v, s = bar["vwap"], bar["sma"]
        if pd.isna(v) or pd.isna(s):
            return None
        return max(v, s), min(v, s)

    l1, l2, lb = lines(c1), lines(c2), lines(before)
    if l1 is None or l2 is None:
        return None
    above = lambda bar, ln: ln is not None and float(bar["low"]) > ln[0]    # noqa: E731
    below = lambda bar, ln: ln is not None and float(bar["high"]) < ln[1]   # noqa: E731
    buffer = STOP_BUFFER_PCT * float(c2["close"])
    if above(c1, l1) and above(c2, l2) and not above(before, lb) and float(c2["close"]) > float(c1["close"]):
        return {"direction": "long", "stop": float(c1["low"]) - buffer, "c1": c1, "c2": c2}
    if below(c1, l1) and below(c2, l2) and not below(before, lb) and float(c2["close"]) < float(c1["close"]):
        return {"direction": "short", "stop": float(c1["high"]) + buffer, "c1": c1, "c2": c2}
    return None


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


def fresh_day(state: dict, today: str) -> dict:
    if state.get("date") != today or "books" not in state:
        state = {"date": today, "books": {}}
    for name in BOOKS:
        state["books"].setdefault(name, {"trades": 0, "position": None, "closed": [], "free_from": None})
    return state


# =============================================================================
# PAPER POSITIONS  (one per book)
# =============================================================================

def paper_exit(client, name: str, book: dict, price: float, why: str) -> None:
    """Close one book's paper position and log index and option results."""
    pos = book["position"]
    long = pos["direction"] == "long"
    gross = (price - pos["entry"]) if long else (pos["entry"] - price)
    premium = option_ltp(client, pos["symbol"]) if pos.get("symbol") else None
    opt = ""
    if premium is not None and pos.get("premium_in") is not None:
        opt_pts = premium - pos["premium_in"]
        opt = (f"; option {pos['premium_in']:.2f} -> {premium:.2f} "
               f"({opt_pts:+.2f}, Rs {opt_pts * pos['lotsize']:+,.0f} per lot)")
    slip = ""
    if why == "stop":
        gap = (pos["stop"] - price) if long else (price - pos["stop"])
        slip = f"; stop {pos['stop']:.2f}, seen {price:.2f}, slippage {gap:+.2f} pts"
    log(f"[{name}] PAPER EXIT {why}: {pos['direction']} {pos['entry']:.2f} -> {price:.2f}, "
        f"index {gross:+.2f} pts gross, {gross - COST_PTS:+.2f} after {COST_PTS:.0f} cost{slip}{opt}")
    book["closed"].append({"direction": pos["direction"], "entry": pos["entry"], "exit": price,
                           "why": why, "gross": round(gross, 2), "stop": pos["stop"],
                           "premium_in": pos.get("premium_in"), "premium_out": premium,
                           "opened": pos["opened"], "closed": datetime.now(IST).isoformat()})
    # The backtest looks for the next signal on bars that START after the exit.
    epoch = int(datetime.now(IST).timestamp())
    book["free_from"] = datetime.fromtimestamp(epoch - epoch % BAR_SECONDS, IST).isoformat()
    book["position"] = None


def open_books(state: dict) -> list[tuple[str, dict]]:
    return [(n, b) for n, b in state["books"].items() if b.get("position")]


def manage(client, name: str, book: dict, bars: pd.DataFrame) -> None:
    """Catch a stop the poll missed on the last completed bar."""
    pos = book.get("position")
    if not pos:
        return
    long = pos["direction"] == "long"
    since = bars[bars.index >= pd.Timestamp(pos["entry_bar"])]
    if since.empty:
        return
    last = since.iloc[-1]
    if (float(last["low"]) <= pos["stop"]) if long else (float(last["high"]) >= pos["stop"]):
        price = index_ltp(client) or pos["stop"]
        log(f"[{name}] bar {since.index[-1]:%H:%M} traded through the stop between polls")
        paper_exit(client, name, book, price, "stop")


def watch_stop(client, state: dict, deadline: float) -> None:
    """Poll the index between bars and paper-exit any book whose stop trades."""
    while not _shutdown and time.monotonic() + STOP_POLL_SECONDS < deadline:
        if not open_books(state) or datetime.now(IST).time() >= SQUARE_OFF:
            return
        time.sleep(STOP_POLL_SECONDS)
        price = index_ltp(client)
        if price is None:
            continue
        for name, book in open_books(state):
            pos = book["position"]
            long = pos["direction"] == "long"
            if (price <= pos["stop"]) if long else (price >= pos["stop"]):
                paper_exit(client, name, book, price, "stop")
                save_state(state)


# =============================================================================
# MAIN
# =============================================================================

def cycle(client, state: dict) -> dict:
    now = datetime.now(IST)
    state = fresh_day(state, str(now.date()))
    books = state["books"]

    if now.time() >= SQUARE_OFF:
        open_now = open_books(state)
        if open_now:
            price = index_ltp(client)
            if price is not None:
                for name, book in open_now:
                    paper_exit(client, name, book, price, "square-off")
        return state

    prior = fetch_prior(client)
    if prior is None:
        log("prior sessions unavailable; SMA cannot warm up, retrying next bar")
        return state
    bars = fetch_today(client, prior)
    if bars.empty:
        log("no completed 3m bars for today yet")
        return state
    for name, book in books.items():
        manage(client, name, book, bars)

    bar = bars.index[-1]
    last = bars.iloc[-1]
    banks = bars.attrs.get("banks", 0)
    vwap_text = f"{last['vwap']:.2f}" if pd.notna(last["vwap"]) else "n/a"
    sma_text = f"{last['sma']:.2f}" if pd.notna(last["sma"]) else "n/a"
    head = (f"bar {bar:%H:%M} close {float(last['close']):.2f} VWAP {vwap_text} "
            f"({banks}/{len(VOLUME_CONSTITUENTS)} banks) SMA{SMA_PERIOD} {sma_text}")
    entry_time = (bar + timedelta(minutes=BAR_MINUTES)).time()

    sig = two_candle(bars) if banks == len(VOLUME_CONSTITUENTS) else None
    parts, breadth = [], None
    for name, book in books.items():
        if book.get("position"):
            p = book["position"]
            parts.append(f"{name}: in {p['direction']} from {p['entry']:.2f}, stop {p['stop']:.2f}")
            continue
        if not sig:
            parts.append(f"{name}: flat")
            continue
        side = sig["direction"]
        if book.get("free_from") and sig["c1"].name < pd.Timestamp(book["free_from"]):
            parts.append(f"{name}: {side} pair began before the last exit; not taken")
            continue
        if not (NO_NEW_ENTRY_BEFORE <= entry_time <= NO_NEW_ENTRY_AFTER):
            parts.append(f"{name}: {side} pair outside the entry window")
            continue
        if book["trades"] >= MAX_TRADES_PER_DAY:
            parts.append(f"{name}: {side} pair skipped, {MAX_TRADES_PER_DAY} trades already today")
            continue
        if name == "breadth":
            if breadth is None:
                breadth = read_breadth(client) or (None, None)
            adv, dec = breadth
            if adv is None:
                parts.append(f"{name}: {side} pair skipped, breadth unreadable")
                continue
            ratio = max(adv, dec) / max(min(adv, dec), 1)
            reading = f"NIFTY Bank {adv} up / {dec} down = {ratio:.2f}x (needs {BREADTH_MIN:g}x either way)"
            if ratio < BREADTH_MIN:
                log(f"[{name}] {side} pair skipped, not a decisive day: {reading}")
                parts.append(f"{name}: skipped on breadth")
                continue
            log(f"[{name}] decisive day: {reading}")
        long = side == "long"
        spot = index_ltp(client) or float(last["close"])
        risk = (spot - sig["stop"]) if long else (sig["stop"] - spot)
        if risk <= 0:
            parts.append(f"{name}: {side} pair ignored, index {spot:.2f} already past the stop")
            continue
        leg = resolve_leg(client, "CE" if long else "PE")
        premium = option_ltp(client, leg["symbol"]) if leg else None
        shown = f"{premium:.2f}" if premium is not None else "unknown"
        log(f"[{name}] PAPER {side.upper()}: candles {sig['c1'].name:%H:%M} and {sig['c2'].name:%H:%M} "
            f"clear of VWAP and SMA{SMA_PERIOD}; entry {spot:.2f}, stop {sig['stop']:.2f} "
            f"(risk {risk:.2f} pts), held to {SQUARE_OFF:%H:%M}; "
            f"{leg['symbol'] if leg else 'option unresolved'} premium {shown}")
        book["trades"] += 1
        book["position"] = {
            "direction": side, "entry": spot, "stop": sig["stop"], "risk": risk,
            "opened": now.isoformat(), "entry_bar": (bar + timedelta(minutes=BAR_MINUTES)).isoformat(),
            "symbol": leg["symbol"] if leg else None,
            "lotsize": leg["lotsize"] if leg else 0, "premium_in": premium,
        }
        parts.append(f"{name}: entered {side}")
    if banks < len(VOLUME_CONSTITUENTS):
        parts.append(f"VWAP from only {banks} banks - no signals this bar")
    log(f"{head} | " + " | ".join(parts))
    return state


def sleep_to_next_bar(client=None, state: dict | None = None) -> None:
    now = datetime.now(IST)
    epoch = int(now.timestamp())
    # 3m bars are anchored at 09:15, which is itself on a 3-minute boundary
    wait = BAR_SECONDS - (epoch % BAR_SECONDS) + BAR_DELAY
    deadline = time.monotonic() + wait
    if client is not None and state is not None and state.get("books") and open_books(state):
        watch_stop(client, state, deadline)
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def summary(state: dict) -> None:
    for name, book in state.get("books", {}).items():
        closed = book.get("closed") or []
        if not closed:
            log(f"[{name}] day summary: no paper trades")
            continue
        gross = sum(t["gross"] for t in closed)
        log(f"[{name}] day summary: {len(closed)} paper trades, index {gross:+.2f} pts gross, "
            f"{gross - COST_PTS * len(closed):+.2f} after cost")


def main() -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    client = build_client()
    log(f"{STRATEGY_TAG} starting, PAPER ONLY - no order is ever sent: {BAR_MINUTES}m two-candle "
        f"VWAP+SMA{SMA_PERIOD}, stop {STOP_BUFFER_PCT:.2%} beyond candle 1, no target, entries "
        f"{NO_NEW_ENTRY_BEFORE:%H:%M}-{NO_NEW_ENTRY_AFTER:%H:%M}, max {MAX_TRADES_PER_DAY} a day, "
        f"square-off {SQUARE_OFF:%H:%M}; books: plain, breadth ({len(BREADTH_CONSTITUENTS)} banks "
        f">= {BREADTH_MIN:g}x either way); option {ITM_POINTS} pts ITM")

    state = load_state()
    try:
        while not _shutdown:
            now = datetime.now(IST)
            if now.time() < SESSION_OPEN:
                sleep_to_next_bar()
                continue
            state = fresh_day(state, str(now.date()))
            if now.time() >= SQUARE_OFF and not open_books(state):
                summary(state)
                log("past square-off with nothing open; done for the day")
                return 0
            try:
                state = cycle(client, state)
            except Exception as exc:  # noqa: BLE001 - one bad bar must not kill the day
                log(f"cycle failed: {exc!r}")
            save_state(state)
            sleep_to_next_bar(client, state)
    finally:
        save_state(state)

    log("shut down cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
