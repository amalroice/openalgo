"""SENSEX range-cluster breakout, 5-minute candles. PAPER ONLY.

This script never places an order: there is no order call anywhere in it. It
reads SENSEX spot (BSE_INDEX), finds the signals, and logs a paper record with
the ATM weekly BFO option's real premium at entry and exit.

Rule (long; short is the mirror):
    1. Cluster: at least MIN_CANDLES consecutive completed candles that all fit
       inside one box whose height (highest high - lowest low) is at most
       BOX_PCT of price. The cluster keeps growing while new candles still
       fit; when one does not, the oldest candles drop off until the rest fit.
    2. Signal: the first candle that CLOSES above the box (short: below it).
    3. Entry: at the next bar's open - here, the index price when the signal
       candle has just completed.
    4. Stop: the opposite side of the box.
    5. Trail: the stop sits TRAIL_DIST_PCT of the entry behind the best price,
       moved in TRAIL_STEP_PCT steps, never looser than the box stop.
    6. Square-off at SQUARE_OFF. Entries NO_NEW_ENTRY_BEFORE..NO_NEW_ENTRY_AFTER,
       at most MAX_TRADES_PER_DAY, one position at a time. After an exit a new
       cluster may only start on the bar after the exit bar.
    No trend filter: day-open and SMA(45) filters did not beat no filter at
    this box width.

Backtest, SENSEX 5m 2016-10..2026-09-25, stops filled AT the stop price, index
points per trade:

    box 0.25%, 4 candles, trail   3,524 trades  gross +18.9/trade
        net at cost 10  +31,472  PF 1.13   9 of 11 years positive
        net at cost 15  +13,852  PF 1.05
        net at cost 20   -3,768  PF 0.99

The edge is thin: breakeven cost is about 19 points a trade, and the backtest
assumes every stop fills exactly at the box edge. THE REASON THIS DRY RUN
EXISTS is to measure that: every stop exit logs the index price actually seen
when the stop was crossed, and the gap to the stop level ("slippage"). If the
average slippage plus real option cost is near 19 points, there is no edge.
Reproduce: Claude session 687c73c7 scratchpad robust.py / cluster_trend.py.

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

STRATEGY_TAG = "SENSEX_CLUSTER_BREAKOUT"
UNDERLYING = "SENSEX"
INDEX_EXCHANGE = "BSE_INDEX"
FO_EXCHANGE = "BFO"
INTERVAL = "5m"

MIN_CANDLES = 4                     # cluster length before a breakout counts
BOX_PCT = 0.0025                    # box height as a fraction of price (0.25%)
TRAIL_DIST_PCT = 0.005              # stop distance behind the best price
TRAIL_STEP_PCT = 0.001              # the stop moves once per step of this size
MAX_TRADES_PER_DAY = 2
COST_PTS = 10.0                     # assumed round-trip cost in the backtest

SESSION_OPEN = dtime(9, 15)
NO_NEW_ENTRY_BEFORE = dtime(9, 30)
NO_NEW_ENTRY_AFTER = dtime(14, 30)
SQUARE_OFF = dtime(15, 0)

BAR_SECONDS = 300
BAR_DELAY = 10                      # wait this long after a bar closes
STOP_POLL_SECONDS = 5               # index polling while a position is open
HISTORY_PACE = 0.55                 # seconds between history retries

IST = ZoneInfo("Asia/Kolkata")
STATE_DIR = Path("strategies") / "state"
STATE_FILE = STATE_DIR / "sensex_cluster_breakout.json"

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


# =============================================================================
# MARKET DATA
# =============================================================================

def fetch_today(client, retries: int = 3) -> pd.DataFrame:
    """Today's completed SENSEX 5m bars.

    Angel labels a bar with the boundary it OPENS on and serves it as soon as
    that boundary passes, so the newest row is still forming and is dropped.

    Returns:
        Completed bars indexed by IST timestamp, empty when unavailable.
    """
    today = str(datetime.now(IST).date())
    frame = pd.DataFrame()
    for attempt in range(1, retries + 1):
        try:
            result = client.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE,
                                    interval=INTERVAL, start_date=today, end_date=today)
        except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
            log(f"history raised {exc!r}")
            result = None
        if isinstance(result, pd.DataFrame) and not result.empty:
            frame = result
            break
        time.sleep(HISTORY_PACE * (2 ** attempt))
    if frame.empty:
        log(f"history {INDEX_EXCHANGE}/{UNDERLYING}: no bars after {retries} attempts")
        return frame
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame = frame.tz_localize(IST) if frame.index.tz is None else frame.tz_convert(IST)
    cutoff = datetime.now(IST) - timedelta(seconds=BAR_SECONDS)
    frame = frame[frame.index <= cutoff]
    # Angel occasionally emits a junk bar whose range dwarfs the rest; one
    # such bar would swallow every cluster after it.
    if len(frame) >= 5:
        span = frame["high"] - frame["low"]
        absurd = span > 10.0 * span.median()
        for stamp in frame.index[absurd]:
            log(f"dropping implausible bar {stamp:%H:%M}: range {float(span[stamp]):.2f}")
        frame = frame[~absurd]
    return frame


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


def nearest_weekly(client) -> str | None:
    """Nearest expiry AFTER today, compact form such as 01OCT26.

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


def resolve_atm(client, option_type: str) -> dict | None:
    """ATM option of the nearest weekly expiry after today."""
    expiry = nearest_weekly(client)
    if not expiry:
        return None
    r = client.optionsymbol(underlying=UNDERLYING, exchange=INDEX_EXCHANGE,
                            expiry_date=expiry, offset="ATM", option_type=option_type)
    if not ok(r):
        log(f"optionsymbol {option_type} failed: {r}")
        return None
    return {"symbol": r["symbol"], "lotsize": int(r["lotsize"])}


# =============================================================================
# SIGNAL
# =============================================================================

def scan(frame: pd.DataFrame, start: int) -> tuple[dict | None, dict | None]:
    """Walk today's bars from `start` exactly as the backtest does.

    Args:
        frame: today's completed bars.
        start: first bar index a cluster may begin on.

    Returns:
        (signal on the LAST completed bar or None, the cluster that bar was
        tested against or None). Signals on earlier bars are ignored, as the
        backtest ignores a breakout it could not take.
    """
    hi = frame["high"].to_numpy(float)
    lo = frame["low"].to_numpy(float)
    cl = frame["close"].to_numpy(float)
    n = len(cl)
    s = start
    for i in range(start, n):
        if i - s >= MIN_CANDLES:
            box_hi, box_lo = hi[s:i].max(), lo[s:i].min()
            cluster = {"candles": i - s, "high": float(box_hi), "low": float(box_lo),
                       "from": frame.index[s].strftime("%H:%M")}
            if i == n - 1:
                if cl[i] > box_hi:
                    return {"direction": "long", **cluster}, cluster
                if cl[i] < box_lo:
                    return {"direction": "short", **cluster}, cluster
        limit = BOX_PCT * cl[i]
        while s < i and hi[s:i + 1].max() - lo[s:i + 1].min() > limit:
            s += 1
    if n - s >= 1 and n > start:
        tail = {"candles": n - s, "high": float(hi[s:n].max()), "low": float(lo[s:n].min()),
                "from": frame.index[s].strftime("%H:%M")}
        return None, tail
    return None, None


def trail_level(entry: float, mfe: float, long: bool) -> float | None:
    """Trailing stop for a best excursion of mfe points, in whole steps."""
    steps = int(mfe // (TRAIL_STEP_PCT * entry))
    if steps < 1:
        return None
    offset = steps * TRAIL_STEP_PCT * entry - TRAIL_DIST_PCT * entry
    return entry + offset if long else entry - offset


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
    if state.get("date") != today:
        state = {"date": today, "trades": 0, "position": None,
                 "cluster_from": None, "closed": []}
    return state


# =============================================================================
# PAPER POSITION
# =============================================================================

def paper_exit(client, state: dict, price: float, why: str, bar_start: str) -> dict:
    """Close the paper position and log index and option results."""
    pos = state["position"]
    long = pos["direction"] == "long"
    gross = (price - pos["entry"]) if long else (pos["entry"] - price)
    premium = option_ltp(client, pos["symbol"])
    opt = ""
    if premium is not None and pos.get("premium_in") is not None:
        opt_pts = premium - pos["premium_in"]
        opt = (f"; option {pos['premium_in']:.2f} -> {premium:.2f} "
               f"({opt_pts:+.2f}, Rs {opt_pts * pos['lotsize']:+,.0f} per lot)")
    slip = ""
    if why == "stop":
        gap = (pos["stop"] - price) if long else (price - pos["stop"])
        slip = f"; stop {pos['stop']:.2f}, seen {price:.2f}, slippage {gap:+.2f} pts"
    log(f"PAPER EXIT {why}: {pos['direction']} {pos['entry']:.2f} -> {price:.2f}, "
        f"index {gross:+.2f} pts gross, {gross - COST_PTS:+.2f} after {COST_PTS:.0f} cost"
        f"{slip}{opt}")
    state["closed"].append({"direction": pos["direction"], "entry": pos["entry"],
                            "exit": price, "why": why, "gross": round(gross, 2),
                            "stop": pos["stop"], "premium_in": pos.get("premium_in"),
                            "premium_out": premium, "opened": pos["opened"],
                            "closed": datetime.now(IST).isoformat()})
    # The backtest restarts the cluster on the bar after the exit bar.
    state["cluster_from"] = bar_start
    state["position"] = None
    return state


def current_bar_start(now: datetime) -> str:
    epoch = int(now.timestamp())
    start = datetime.fromtimestamp(epoch - epoch % BAR_SECONDS, IST) + timedelta(seconds=BAR_SECONDS)
    return start.isoformat()


def manage(client, state: dict, frame: pd.DataFrame) -> dict:
    """Catch a stop the poll missed, then ratchet the trail on completed bars."""
    pos = state.get("position")
    if not pos:
        return state
    long = pos["direction"] == "long"
    since = frame[frame.index >= pd.Timestamp(pos["entry_bar"])]
    if since.empty:
        return state
    last = since.iloc[-1]
    touched = float(last["low"]) <= pos["stop"] if long else float(last["high"]) >= pos["stop"]
    if touched:
        price = index_ltp(client) or pos["stop"]
        log(f"bar {since.index[-1]:%H:%M} traded through the stop between polls")
        return paper_exit(client, state, price, "stop", current_bar_start(since.index[-1]))
    best = (float(since["high"].max()) - pos["entry"]) if long else (pos["entry"] - float(since["low"].min()))
    pos["mfe"] = max(pos.get("mfe", 0.0), best)
    level = trail_level(pos["entry"], pos["mfe"], long)
    if level is not None:
        moved = max(pos["stop"], level) if long else min(pos["stop"], level)
        if moved != pos["stop"]:
            log(f"trail: best +{pos['mfe']:.2f} pts, stop {pos['stop']:.2f} -> {moved:.2f}")
            pos["stop"] = moved
    return state


def watch_stop(client, state: dict, deadline: float) -> None:
    """Poll the index between bars and paper-exit when the stop trades."""
    while not _shutdown and time.monotonic() + STOP_POLL_SECONDS < deadline:
        pos = state.get("position")
        if not pos or datetime.now(IST).time() >= SQUARE_OFF:
            return
        time.sleep(STOP_POLL_SECONDS)
        price = index_ltp(client)
        if price is None:
            continue
        long = pos["direction"] == "long"
        if (price <= pos["stop"]) if long else (price >= pos["stop"]):
            paper_exit(client, state, price, "stop", current_bar_start(datetime.now(IST)))
            save_state(state)
            return


# =============================================================================
# MAIN
# =============================================================================

def cycle(client, state: dict) -> dict:
    now = datetime.now(IST)
    state = fresh_day(state, str(now.date()))

    if now.time() >= SQUARE_OFF:
        if state.get("position"):
            price = index_ltp(client)
            if price is not None:
                state = paper_exit(client, state, price, "square-off", now.isoformat())
        return state

    frame = fetch_today(client)
    if frame.empty:
        log("no usable bars for today yet")
        return state

    state = manage(client, state, frame)
    if state.get("position"):
        return state

    start = 0
    if state.get("cluster_from"):
        start = int((frame.index < pd.Timestamp(state["cluster_from"])).sum())
    sig, cluster = scan(frame, start)
    bar = frame.index[-1]
    box = (f"cluster {cluster['candles']} candles from {cluster['from']}, box "
           f"{cluster['low']:.2f}-{cluster['high']:.2f} ({cluster['high'] - cluster['low']:.2f} pts)"
           if cluster else "no cluster")
    log(f"bar {bar:%H:%M} close {float(frame['close'].iloc[-1]):.2f}; {box}")
    if not sig:
        return state

    entry_time = (bar + timedelta(seconds=BAR_SECONDS)).time()
    if not (NO_NEW_ENTRY_BEFORE <= entry_time <= NO_NEW_ENTRY_AFTER):
        log(f"{sig['direction']} breakout outside the entry window; not taken")
        return state
    if state.get("trades", 0) >= MAX_TRADES_PER_DAY:
        log(f"{sig['direction']} breakout skipped: {MAX_TRADES_PER_DAY} trades already today")
        return state

    long = sig["direction"] == "long"
    spot = index_ltp(client) or float(frame["close"].iloc[-1])
    stop = sig["low"] if long else sig["high"]
    risk = (spot - stop) if long else (stop - spot)
    if risk <= 0:
        log(f"{sig['direction']} breakout ignored: index {spot:.2f} already back past the stop")
        return state
    leg = resolve_atm(client, "CE" if long else "PE")
    premium = option_ltp(client, leg["symbol"]) if leg else None
    shown = f"{premium:.2f}" if premium is not None else "unknown"
    log(f"PAPER {sig['direction'].upper()}: close {float(frame['close'].iloc[-1]):.2f} broke the "
        f"{sig['candles']}-candle box {sig['low']:.2f}-{sig['high']:.2f}; entry {spot:.2f}, "
        f"stop {stop:.2f} (risk {risk:.2f} pts); "
        f"{leg['symbol'] if leg else 'option unresolved'} premium {shown}")
    state["trades"] = state.get("trades", 0) + 1
    state["position"] = {
        "direction": sig["direction"], "entry": spot, "stop": stop, "risk": risk,
        "mfe": 0.0, "opened": now.isoformat(),
        "entry_bar": (bar + timedelta(seconds=BAR_SECONDS)).isoformat(),
        "symbol": leg["symbol"] if leg else None,
        "lotsize": leg["lotsize"] if leg else 0, "premium_in": premium,
    }
    return state


def sleep_to_next_bar(client=None, state: dict | None = None) -> None:
    now = datetime.now(IST)
    epoch = int(now.timestamp())
    wait = BAR_SECONDS - (epoch % BAR_SECONDS) + BAR_DELAY
    deadline = time.monotonic() + wait
    if client is not None and state is not None and state.get("position"):
        watch_stop(client, state, deadline)
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def summary(state: dict) -> None:
    closed = state.get("closed") or []
    if not closed:
        log("day summary: no paper trades")
        return
    gross = sum(t["gross"] for t in closed)
    stops = [t for t in closed if t["why"] == "stop"]
    slips = [((t["stop"] - t["exit"]) if t["direction"] == "long" else (t["exit"] - t["stop"]))
             for t in stops]
    avg_slip = f", average stop slippage {sum(slips) / len(slips):+.2f} pts" if slips else ""
    log(f"day summary: {len(closed)} paper trades, index {gross:+.2f} pts gross, "
        f"{gross - COST_PTS * len(closed):+.2f} after cost{avg_slip}")


def main() -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    client = build_client()
    log(f"{STRATEGY_TAG} starting, PAPER ONLY - no order is ever sent: "
        f">= {MIN_CANDLES} candles in a {BOX_PCT:.2%} box, stop at the far side, "
        f"trail {TRAIL_DIST_PCT:.1%} in {TRAIL_STEP_PCT:.1%} steps, entries "
        f"{NO_NEW_ENTRY_BEFORE:%H:%M}-{NO_NEW_ENTRY_AFTER:%H:%M}, max {MAX_TRADES_PER_DAY}, "
        f"square-off {SQUARE_OFF:%H:%M}")

    state = load_state()
    try:
        while not _shutdown:
            now = datetime.now(IST)
            if now.time() < SESSION_OPEN:
                sleep_to_next_bar()
                continue
            if now.time() >= SQUARE_OFF and not state.get("position"):
                state = fresh_day(state, str(now.date()))
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
