"""BANKNIFTY two-candle VWAP + SMA(45) breakdown on 3-minute candles. LIVE.

TRADES REAL ORDERS. The signal is read off BANKNIFTY spot (NSE_INDEX); the
order goes to the nearest monthly NFO option ITM_POINTS (200) in the money.
The rule is short-only, so in practice that leg is always a PE. BANKNIFTY has
listed monthly expiries only since November 2024.

SAFETY. assert_live_allowed runs at startup: it reads the instance's analyzer
(sandbox) flag. In analyzer mode OpenAlgo simulates the orders. Outside it, a
real order is placed only because I_UNDERSTAND_THIS_IS_LIVE is True - set that
False to refuse them. DRY_RUN=True resolves and logs every order without
sending one. LIVE_BOOK names the single book that goes live; the other stays
paper for comparison, so the two books never double a real position. A resting
stop-limit is parked at the broker on every live entry, sized off the option's
own Greeks (delta and gamma map the index stop to a premium, widened by theta
to square-off and BROKER_STOP_IV_POINTS of vega). It is a disaster backstop
BEHIND the in-process stop, not a tighter second stop: the in-process stop and
target still exit normally, and the resting stop only fires if the process dies
or the index gaps. It is cancelled before every exit so it can never
double-sell, and an orphaned one is pulled when the broker reports the position
already flat. Set BROKER_STOP = False to rely on the in-process poll alone.

SHORT-ONLY, ATR stop and a 100-point target since 2026-10-04. The long side
was removed and the exit rebuilt after a 478-session study (2024-10-01 ..
2026-09-30): the raw rule won 24.5% of its trades, and the change below lifts
that to 62.4% with a higher profit factor. See EVIDENCE at the end.

Bars: 3-minute candles built from 1-minute history, anchored at 09:15, exactly
as the backtest builds them. VWAP is the day's index typical price weighted by
the summed volume of the six largest banks (the index has none). SMA(45) and
ATR(14) run over the same 3m bars across sessions, so prior sessions are
fetched once a day.

Rule (short; the long mirror is computed but not traded - see DIRECTIONS):
    1. Two consecutive completed candles lie COMPLETELY below both VWAP and
       SMA(45) (high below the lower of the two lines), the candle before them
       did not, and candle 2 closes below candle 1's close.
    2. Entry: at the next bar's open - here, the index price when candle 2 has
       just completed - between NO_NEW_ENTRY_BEFORE (10:15) and
       NO_NEW_ENTRY_AFTER (14:30).
    3. Stop: ATR_STOP_MULT (3) times ATR(14) of the 3m bars beyond the entry,
       so the stop breathes with volatility instead of pinning candle 1. The
       candle-1 stop is kept only as a fallback while the ATR has not warmed.
    4. Target: TARGET_PTS (100) index points from the entry, banked the moment
       the index trades there. No trail.
    5. At most MAX_TRADES_PER_DAY (2), one position at a time per book.

Two books run side by side on the same bars; LIVE_BOOK places the real order
and the other is paper:
    plain     the rule above
    breadth   the rule above, taken only when the 14 NIFTY Bank stocks are
              one-sided by BREADTH_MIN (2x): advancers at least twice decliners
              or the reverse, in EITHER direction ("a decisive day"), read
              against each stock's previous close when the signal fires.

EVIDENCE - rebuilt 2026-10-04 from cached 1m bars (478 sessions, 2024-10-01 ..
2026-09-30), 4 pts cost a trade, 1 lot of 30, stops and targets filled AT their
level, a bar that touches both scored as a stop:

    book                 2025     2026   last 100    full 2025-26
    raw rule (both dir)  23.8%    25.6%    24.8%       24.5%   (PF 1.16)
    short + ATR3 + 100   59.6%    66.0%    61.7%       62.4%   (PF 1.28)

    full: 319 trades, win 62.4%, +3,995 index pts (Rs 119,863 a lot), PF 1.28,
    maxDD -1,109 pts against -2,598 for the raw rule. By year: 2025 +1,631
    (PF 1.21), 2026 +2,364 (PF 1.38). Exit mix: 184 target, 95 stop, 40
    square-off.

    Why short-only: the long-side mirror, with this same exit, won ~52% of its
    trades but still lost money in every window (2025 -1,632, 2026 -3,595,
    full -5,226 pts; PF 0.75) - its winners are smaller than its losers. The
    breakout has paid on the downside, not the upside, so the long side is
    disabled rather than kept as a drag.

    The ATR stop x fixed target region is broad - ATR 2.5-3.5 x 75-130 pts all
    give 54-69% win and PF 1.1-1.4 - so 3.0 and 100 are a point on a plateau,
    not a fitted optimum. Reproduce: backtesting/banknifty-2candle/research/.

THE REASON THIS IS LOGGED HEAVILY is cost: the edge is real but modest - about
12 index points a trade net of the 4-point cost, roughly what monthly-option
spreads and slippage may take. Every exit logs the option premium in and out,
and every stop exit the index price actually seen. Reproduce:
backtesting/banknifty-2candle/research/ (harness.py + exp_atr.py).

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
ATR_PERIOD = 14                     # Wilder ATR on the 3m bars, sizes the stop
ATR_STOP_MULT = 3.0                 # stop sits this many ATRs beyond the entry
TARGET_PTS = 100.0                  # index-point target, banked on touch
DIRECTIONS = ("short",)             # long side removed, see EVIDENCE in the docstring

# LIVE TRADING
I_UNDERSTAND_THIS_IS_LIVE = True    # False refuses real orders outside analyzer mode
DRY_RUN = False                     # True resolves and logs the orders, places nothing
LIVE_BOOK = "plain"                 # which book sends real orders; the other stays paper
PRODUCT = "MIS"                     # intraday, squared off the same session
LOTS = 1                            # lots sent per live entry
BROKER_STOP = True                  # park a resting SL at the broker as a backstop
BROKER_STOP_IV_POINTS = 2.0         # IV points of vega headroom on that stop
BROKER_STOP_LIMIT_SLIP = 0.05       # its limit sits this far below the trigger
TICK = 0.05                         # NFO option tick
EXIT_RETRY_SECONDS = 20             # minimum gap between retries of a rejected exit

PRIOR_DAYS = 6                      # calendar days of 1m history for the SMA/ATR warm-up
STOP_BUFFER_PCT = 0.0002            # fallback stop beyond candle 1 while ATR warms
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
    if prior is not None and not prior.empty:
        combined = pd.concat([prior[["high", "low", "close"]], bars[["high", "low", "close"]]])
    else:
        combined = bars[["high", "low", "close"]]
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    closes = combined["close"]
    bars["sma"] = closes.rolling(SMA_PERIOD).mean().reindex(bars.index)
    prev_close = closes.shift(1)
    tr = pd.concat([
        combined["high"] - combined["low"],
        (combined["high"] - prev_close).abs(),
        (combined["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    bars["atr"] = tr.ewm(alpha=1.0 / ATR_PERIOD, adjust=False).mean().reindex(bars.index)
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
# LIVE ORDERS
# =============================================================================

def assert_live_allowed(client) -> bool:
    """Read the instance's analyzer flag and decide whether trading is allowed.

    Analyzer (sandbox) mode is a global instance flag. Outside it a real order
    needs I_UNDERSTAND_THIS_IS_LIVE, which the user sets by hand.

    Returns:
        True when orders may be placed (real, or simulated by the sandbox).
    """
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
        return True
    if I_UNDERSTAND_THIS_IS_LIVE:
        log("WARNING: analyzer mode is OFF and live trading was acknowledged")
        return True
    log("analyzer mode is OFF and I_UNDERSTAND_THIS_IS_LIVE is False; not trading")
    return False


def send(client, symbol: str, action: str, quantity: int) -> bool:
    """Place one market order on the option leg.

    Returns:
        True when the order was accepted (or DRY_RUN swallowed it).
    """
    if DRY_RUN:
        log(f"DRY_RUN {action} {quantity} {symbol}")
        return True
    try:
        r = client.placeorder(strategy=STRATEGY_TAG, symbol=symbol, action=action,
                              exchange=FO_EXCHANGE, price_type="MARKET",
                              product=PRODUCT, quantity=quantity)
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"order RAISED {action} {quantity} {symbol}: {exc!r}")
        return False
    if not ok(r):
        log(f"order REJECTED {action} {quantity} {symbol}: {r}")
        return False
    log(f"order OK {action} {quantity} {symbol} -> {r.get('orderid')}")
    return True


def net_quantity(client, symbol: str) -> int | None:
    """Net open quantity the broker reports for one option leg.

    Returns:
        The quantity, 0 when nothing is open, or None when the book cannot be
        read.
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
        if row.get("symbol") == symbol and row.get("exchange") == FO_EXCHANGE:
            try:
                return int(float(row.get("quantity") or 0))
            except (TypeError, ValueError):
                log(f"unreadable quantity on {symbol}: {row.get('quantity')!r}")
                return None
    return 0


# =============================================================================
# BROKER-SIDE RESTING STOP
# =============================================================================

def round_tick(value: float) -> float:
    """Round down to the exchange tick, cleaning float residue."""
    return round(max(TICK, int(round(value / TICK, 6)) * TICK), 2)


def option_stop_price(client, symbol: str, index_entry: float,
                      index_stop: float) -> float | None:
    """Premium level matching the index stop, widened into a disaster stop.

    The index move is mapped to the option with delta and gamma from the
    broker's Greeks, then the level is pushed further out by theta to
    square-off and by BROKER_STOP_IV_POINTS of vega, so ordinary decay and IV
    noise do not fire a stop no index move justified. This is a backstop
    behind the in-process stop, not a second, tighter one.

    Returns:
        Trigger rounded to the tick, or None when it cannot be trusted - the
        caller then places no resting stop and says so.
    """
    try:
        r = client.optiongreeks(symbol=symbol, exchange=FO_EXCHANGE)
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"greeks raised {exc!r}; no broker stop for {symbol}")
        return None
    if not ok(r):
        log(f"greeks failed for {symbol}: {r}")
        return None
    body = r.get("data") if isinstance(r.get("data"), dict) else r
    greeks = body.get("greeks") or {}
    try:
        delta = float(greeks["delta"])
        gamma = float(greeks["gamma"])
        theta = float(greeks["theta"])
        vega = float(greeks["vega"])
        entry_premium = float(body["option_price"])
    except (KeyError, TypeError, ValueError):
        log(f"greeks unreadable for {symbol}: {body!r}")
        return None
    if delta == 0.0:
        log(f"zero delta for {symbol}; refusing to size a stop off it")
        return None
    if entry_premium <= 0:
        log(f"non-positive premium {entry_premium} for {symbol}")
        return None
    move = index_stop - index_entry
    mirror = entry_premium + delta * move + 0.5 * gamma * move ** 2
    now = datetime.now(IST)
    close = now.replace(hour=SQUARE_OFF.hour, minute=SQUARE_OFF.minute,
                        second=0, microsecond=0)
    hours = max((close - now).total_seconds() / 3600.0, 0.0)
    level = mirror - abs(theta) * hours / 24.0 - abs(vega) * BROKER_STOP_IV_POINTS
    if level <= 0 or level >= entry_premium:
        log(f"mapped stop {level:.2f} is not below the {entry_premium:.2f} entry "
            f"premium; no broker stop for {symbol}")
        return None
    trigger = round_tick(level)
    log(f"broker stop for {symbol}: index {index_entry:.2f} -> {index_stop:.2f} "
        f"({move:+.2f}) maps to {mirror:.2f} on delta {delta:+.4f} gamma "
        f"{gamma:.6f}; widened to trigger {trigger:.2f}")
    return trigger


def place_broker_stop(client, symbol: str, quantity: int, trigger: float) -> str | None:
    """Park a stop-limit SELL at the broker to close the long option leg.

    A limit rather than SL-M: NSE restricts market-type stops in F&O, and a
    rejected SL-M would leave the position silently unprotected. The limit sits
    BROKER_STOP_LIMIT_SLIP below the trigger so it still fills on a fast move.

    Returns:
        The broker order id, or None when it was not accepted.
    """
    limit = round_tick(trigger * (1.0 - BROKER_STOP_LIMIT_SLIP))
    if DRY_RUN:
        log(f"DRY_RUN broker stop: SELL {quantity} {symbol} trigger {trigger:.2f} "
            f"limit {limit:.2f}")
        return "DRY"
    try:
        r = client.placeorder(
            strategy=STRATEGY_TAG, symbol=symbol, action="SELL",
            exchange=FO_EXCHANGE, price_type="SL", product=PRODUCT,
            quantity=quantity, price=limit, trigger_price=trigger,
        )
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"broker stop raised {exc!r} for {symbol}")
        return None
    if not ok(r):
        log(f"broker stop REJECTED for {symbol} at trigger {trigger:.2f} "
            f"limit {limit:.2f}: {r}")
        return None
    order_id = r.get("orderid")
    log(f"broker stop resting: {quantity} {symbol} trigger {trigger:.2f} "
        f"limit {limit:.2f} -> {order_id}")
    return order_id


def stop_order_state(client, order_id: str) -> str:
    """Classify a resting stop: 'open', 'filled', 'gone' or 'unknown'."""
    if not order_id or order_id == "DRY":
        return "gone"
    try:
        r = client.orderstatus(order_id=order_id, strategy=STRATEGY_TAG)
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"orderstatus raised {exc!r} for {order_id}")
        return "unknown"
    if not ok(r):
        log(f"orderstatus failed for {order_id}: {r}")
        return "unknown"
    data = r.get("data") or r
    raw = str(data.get("order_status") or data.get("status") or "").lower()
    if "complete" in raw or "filled" in raw or "executed" in raw:
        return "filled"
    if "cancel" in raw or "reject" in raw:
        return "gone"
    if raw:
        return "open"
    return "unknown"


def _cancel_stop(client, order_id: str) -> bool:
    """Cancel a resting broker stop. True when it is gone."""
    if not order_id or order_id == "DRY":
        return True
    try:
        return ok(client.cancelorder(order_id=order_id, strategy=STRATEGY_TAG))
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"cancelorder raised {exc!r} for {order_id}")
        return False


def release_broker_stop(client, name: str, book: dict) -> str:
    """Cancel this position's resting stop before a market exit.

    Returns:
        'clear' (nothing rests), 'filled' (the stop already closed the
        position) or 'blocked' (the stop may still be live - do not sell).
    """
    pos = book.get("position")
    if not pos:
        return "clear"
    order_id = pos.get("stop_order_id")
    if not order_id:
        return "clear"
    status = stop_order_state(client, order_id)
    if status == "filled":
        pos.pop("stop_order_id", None)
        return "filled"
    if status == "gone":
        pos.pop("stop_order_id", None)
        return "clear"
    if status == "open":
        if _cancel_stop(client, order_id):
            pos.pop("stop_order_id", None)
            log(f"[{name}] cancelled resting stop {order_id}")
            return "clear"
        status = stop_order_state(client, order_id)
        if status == "filled":
            pos.pop("stop_order_id", None)
            return "filled"
        if status == "gone":
            pos.pop("stop_order_id", None)
            return "clear"
    log(f"[{name}] cannot confirm resting stop {order_id} is gone (status "
        f"{status}); holding off the exit to avoid a double sell")
    return "blocked"


def drop_resting_stop(client, position: dict, why: str) -> None:
    """Cancel an orphaned stop whose position is already gone.

    A stop that outlives its position is a live SELL with nothing behind it,
    which on a fill would open a naked short, so a failure here is shouted
    rather than swallowed.
    """
    order_id = position.pop("stop_order_id", None)
    if not order_id:
        return
    if _cancel_stop(client, order_id):
        log(f"cancelled orphaned resting stop {order_id}: {why}")
        return
    status = stop_order_state(client, order_id)
    if status in ("filled", "gone"):
        log(f"orphaned stop {order_id} is already {status}; nothing rests")
        return
    log(f"COULD NOT CANCEL resting stop {order_id} ({why}). It may be live with "
        f"no position behind it - CANCEL IT BY HAND AT THE BROKER")


def resting_stop_at_broker(client, symbol: str) -> str | None:
    """Order id of a stop SELL already resting on this leg, if there is one.

    Checked before a retry so an entry call that timed out AFTER the broker
    took the order cannot lead to a second resting SELL.
    """
    try:
        r = client.orderbook()
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"orderbook raised {exc!r}")
        return None
    if not ok(r):
        log(f"orderbook failed: {r}")
        return None
    data = r.get("data") or {}
    rows = data.get("orders") if isinstance(data, dict) else data
    for row in rows or []:
        status = str(row.get("order_status") or "").lower()
        if (row.get("symbol") == symbol and row.get("exchange") == FO_EXCHANGE
                and str(row.get("action") or "").upper() == "SELL"
                and str(row.get("pricetype") or "").upper() in ("SL", "SL-M")
                and ("trigger" in status or status == "open")):
            return str(row.get("orderid"))
    return None


def retry_broker_stop(client, name: str, book: dict, spot: float) -> None:
    """Park a resting stop on a live position that is still without one."""
    pos = book.get("position")
    if not pos or not pos.get("live") or not BROKER_STOP:
        return
    symbol = pos["symbol"]
    existing = resting_stop_at_broker(client, symbol)
    if existing:
        pos["stop_order_id"] = existing
        log(f"[{name}] found resting stop {existing} on {symbol}; adopting it")
        return
    stop = float(pos["stop"])
    long = pos["direction"] == "long"
    if (spot <= stop) if long else (spot >= stop):
        return
    trigger = option_stop_price(client, symbol, spot, stop)
    order_id = (place_broker_stop(client, symbol, int(pos["qty"]), trigger)
                if trigger is not None else None)
    if order_id:
        pos["stop_order_id"] = order_id
        log(f"[{name}] broker stop parked on retry for {symbol}")
    else:
        log(f"[{name}] STILL NO BROKER STOP on {symbol}; retrying next bar")


def abandon_if_flat(client, name: str, book: dict) -> bool:
    """Clear a live position the broker no longer holds.

    A rejected exit means either the exit order failed (a retry is right) or
    the position is already gone - squared off by the sandbox or the broker,
    which also refuse fresh MIS orders after 15:15 IST. Retrying that second
    case never succeeds, so the position book is the tiebreaker.

    Returns:
        True when the position was cleared.
    """
    pos = book.get("position")
    if not pos or name != LIVE_BOOK or not pos.get("live"):
        return False
    qty = net_quantity(client, pos["symbol"])
    if qty is None:
        return False
    if qty == 0:
        log(f"[{name}] broker reports no open {pos['symbol']}; it was closed "
            f"elsewhere - clearing the local position")
        drop_resting_stop(client, pos, "the position was closed elsewhere")
        book["position"] = None
        return True
    log(f"[{name}] broker still reports {qty} open {pos['symbol']}; keeping the position")
    return False


# =============================================================================
# POSITIONS  (one per book)
# =============================================================================

def paper_exit(client, name: str, book: dict, price: float, why: str) -> None:
    """Record a closed position: log the index and option result.

    The order itself is placed by close_position; this only keeps the books.
    """
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
    tag = "LIVE" if pos.get("live") else "PAPER"
    log(f"[{name}] {tag} EXIT {why}: {pos['direction']} {pos['entry']:.2f} -> {price:.2f}, "
        f"index {gross:+.2f} pts gross, {gross - COST_PTS:+.2f} after {COST_PTS:.0f} cost{slip}{opt}")
    book["closed"].append({"direction": pos["direction"], "entry": pos["entry"], "exit": price,
                           "why": why, "gross": round(gross, 2), "stop": pos["stop"],
                           "premium_in": pos.get("premium_in"), "premium_out": premium,
                           "opened": pos["opened"], "closed": datetime.now(IST).isoformat()})
    # The backtest looks for the next signal on bars that START after the exit.
    epoch = int(datetime.now(IST).timestamp())
    book["free_from"] = datetime.fromtimestamp(epoch - epoch % BAR_SECONDS, IST).isoformat()
    book["position"] = None


def close_position(client, name: str, book: dict, price: float, why: str) -> bool:
    """Close one book: send the real exit for the live book, then record it.

    Returns:
        True when the position is closed (or was already flat), False when the
        exit was rejected and the position is kept for a later retry.
    """
    pos = book.get("position")
    if not pos:
        return True
    if name == LIVE_BOOK and pos.get("live"):
        # the resting stop may have already closed the position at the broker
        if pos.get("stop_order_id") and stop_order_state(client, pos["stop_order_id"]) == "filled":
            log(f"[{name}] resting stop filled; the position is already closed")
            pos.pop("stop_order_id", None)
            paper_exit(client, name, book, price, "broker-stop")
            return True
        last = pos.get("exit_try_at")
        now_mono = time.monotonic()
        if last is not None and now_mono - last < EXIT_RETRY_SECONDS:
            return False
        pos["exit_try_at"] = now_mono
        outcome = release_broker_stop(client, name, book)
        if outcome == "filled":
            log(f"[{name}] the resting stop filled during the exit; position closed")
            paper_exit(client, name, book, price, "broker-stop")
            return True
        if outcome == "blocked":
            return False
        if not send(client, pos["symbol"], "SELL", pos["qty"]):
            if abandon_if_flat(client, name, book):
                return True
            log(f"[{name}] exit ({why}) order rejected; keeping the position, "
                f"retrying after {EXIT_RETRY_SECONDS}s")
            return False
    paper_exit(client, name, book, price, why)
    return True


def open_books(state: dict) -> list[tuple[str, dict]]:
    return [(n, b) for n, b in state["books"].items() if b.get("position")]


def manage(client, name: str, book: dict, bars: pd.DataFrame) -> None:
    """Reconcile the resting stop, then catch a stop/target the poll missed."""
    pos = book.get("position")
    if not pos:
        return
    long = pos["direction"] == "long"
    # the resting stop may have closed the position on its own between bars
    if name == LIVE_BOOK and pos.get("live") and pos.get("stop_order_id"):
        if stop_order_state(client, pos["stop_order_id"]) == "filled":
            price = index_ltp(client) or pos["stop"]
            log(f"[{name}] resting stop {pos['stop_order_id']} filled at the broker")
            pos.pop("stop_order_id", None)
            paper_exit(client, name, book, price, "broker-stop")
            return
    since = bars[bars.index >= pd.Timestamp(pos["entry_bar"])]
    if since.empty:
        return
    last = since.iloc[-1]
    stop_hit = (float(last["low"]) <= pos["stop"]) if long else (float(last["high"]) >= pos["stop"])
    target = pos.get("target")
    target_hit = target is not None and (
        (float(last["high"]) >= target) if long else (float(last["low"]) <= target))
    if stop_hit:
        price = index_ltp(client) or pos["stop"]
        log(f"[{name}] bar {since.index[-1]:%H:%M} traded through the stop between polls")
        close_position(client, name, book, price, "stop")
    elif target_hit:
        price = index_ltp(client) or target
        log(f"[{name}] bar {since.index[-1]:%H:%M} traded through the target between polls")
        close_position(client, name, book, price, "target")
    elif name == LIVE_BOOK and pos.get("live") and BROKER_STOP and not pos.get("stop_order_id"):
        retry_broker_stop(client, name, book, float(last["close"]))


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
            stop_hit = (price <= pos["stop"]) if long else (price >= pos["stop"])
            target = pos.get("target")
            target_hit = target is not None and (
                (price >= target) if long else (price <= target))
            if stop_hit:
                close_position(client, name, book, price, "stop")
                save_state(state)
            elif target_hit:
                close_position(client, name, book, price, "target")
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
                    close_position(client, name, book, price, "square-off")
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
            tgt = p.get("target")
            tgt_text = f", target {tgt:.2f}" if tgt is not None else ""
            parts.append(f"{name}: in {p['direction']} from {p['entry']:.2f}, "
                         f"stop {p['stop']:.2f}{tgt_text}")
            continue
        if not sig:
            parts.append(f"{name}: flat")
            continue
        side = sig["direction"]
        if side not in DIRECTIONS:
            parts.append(f"{name}: {side} pair skipped, only {','.join(DIRECTIONS)} trades")
            continue
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
        atr = float(last["atr"]) if ("atr" in last and pd.notna(last["atr"])) else float("nan")
        if atr > 0:
            stop = spot + ATR_STOP_MULT * atr if long else spot - ATR_STOP_MULT * atr
        else:
            stop = sig["stop"]      # candle-1 fallback until the ATR has warmed up
        target = spot + TARGET_PTS if long else spot - TARGET_PTS
        risk = abs(spot - stop)
        if risk <= 0:
            parts.append(f"{name}: {side} pair ignored, index {spot:.2f} sits on the stop")
            continue
        leg = resolve_leg(client, "CE" if long else "PE")
        live = name == LIVE_BOOK
        if live and leg is None:
            parts.append(f"{name}: {side} pair skipped, option leg unresolved (live)")
            continue
        qty = (leg["lotsize"] * LOTS) if (leg and live) else 0
        premium = option_ltp(client, leg["symbol"]) if leg else None
        shown = f"{premium:.2f}" if premium is not None else "unknown"
        atr_text = f"{atr:.2f}" if atr > 0 else "n/a"
        if live and not send(client, leg["symbol"], "BUY", qty):
            parts.append(f"{name}: {side} entry order rejected, not taken")
            continue
        stop_order_id = None
        if live and BROKER_STOP:
            trigger = option_stop_price(client, leg["symbol"], spot, stop)
            stop_order_id = (place_broker_stop(client, leg["symbol"], qty, trigger)
                             if trigger is not None else None)
            if not stop_order_id:
                log(f"[{name}] NO BROKER STOP yet on {leg['symbol']}; retried next bar")
        log(f"[{name}] {'LIVE' if live else 'PAPER'} {side.upper()}: candles "
            f"{sig['c1'].name:%H:%M} and {sig['c2'].name:%H:%M} clear of VWAP and SMA{SMA_PERIOD}; "
            f"entry {spot:.2f}, stop {stop:.2f} ({ATR_STOP_MULT:g} x ATR {atr_text}, "
            f"risk {risk:.2f} pts), target {target:.2f}, held to {SQUARE_OFF:%H:%M}; "
            f"{leg['symbol'] if leg else 'option unresolved'} x{qty} premium {shown}")
        book["trades"] += 1
        book["position"] = {
            "direction": side, "entry": spot, "stop": stop, "target": target, "risk": risk,
            "opened": now.isoformat(), "entry_bar": (bar + timedelta(minutes=BAR_MINUTES)).isoformat(),
            "symbol": leg["symbol"] if leg else None, "qty": qty,
            "lotsize": leg["lotsize"] if leg else 0, "live": live, "premium_in": premium,
            "stop_order_id": stop_order_id,
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
            log(f"[{name}] day summary: no trades")
            continue
        gross = sum(t["gross"] for t in closed)
        log(f"[{name}] day summary: {len(closed)} trades, index {gross:+.2f} pts gross, "
            f"{gross - COST_PTS * len(closed):+.2f} after cost")


def main() -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    client = build_client()
    if not assert_live_allowed(client):
        log("live trading is not permitted by the analyzer setting; exiting")
        return 1
    mode = ("DRY_RUN, no orders" if DRY_RUN
            else f"LIVE, the {LIVE_BOOK} book places real orders")
    log(f"{STRATEGY_TAG} starting, {mode}: {BAR_MINUTES}m two-candle VWAP+SMA{SMA_PERIOD}, "
        f"directions {','.join(DIRECTIONS)}, stop {ATR_STOP_MULT:g} x ATR{ATR_PERIOD}, "
        f"target {TARGET_PTS:g} pts, entries {NO_NEW_ENTRY_BEFORE:%H:%M}-{NO_NEW_ENTRY_AFTER:%H:%M}, "
        f"max {MAX_TRADES_PER_DAY} a day, square-off {SQUARE_OFF:%H:%M}; books: plain, breadth "
        f"({len(BREADTH_CONSTITUENTS)} banks >= {BREADTH_MIN:g}x either way); option {ITM_POINTS} pts ITM; "
        f"broker stop {'ON' if BROKER_STOP else 'OFF'}")

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
