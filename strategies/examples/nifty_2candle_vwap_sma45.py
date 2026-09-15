"""NIFTY 50 two-candle VWAP + SMA(45) breakout, ATM weekly options.

Signals are read off NIFTY 50 SPOT on 5-minute candles; orders go to the ATM
option of the nearest weekly expiry - CE on a long signal, PE on a short. Every
stop, target and trail is measured in INDEX points, never in premium.

Entry (long; short is the mirror):
    1. Two consecutive candles lie completely above both VWAP and SMA(45) -
       each candle's low is above the higher of the two lines.
    2. The pair is the first of its run: the candle before candle 1 was not
       itself completely above both lines.
    3. Candle 2 closes above candle 1's close.
    Entry is a market order as soon as candle 2 closes, and never before
    NO_NEW_ENTRY_BEFORE. Over 2026-01-01 to 2026-09-09 the average trade by
    entry hour climbed monotonically from -7.58 index points at 09h to +9.89
    at 13h; skipping the first hour turned that year from -329.7 points to
    +541.5 and halved the drawdown.
    4. India VIX above VIX_MIN. Below 12 the edge is simply absent - see the
       table at VIX_MIN. This gate stands the strategy down entirely in a calm
       regime, which is what it did through September 2026.

Stop:
    Candle 1's low, less STOP_BUFFER points. If the higher line sits within
    LINE_PROXIMITY points of that level, the line is used instead. The lines
    are always below candle 1's low by construction, so this widens the stop
    rather than tightening it.

    Both values are deliberately wide. Profit comes from positions that survive
    to square-off (they averaged +62.7 index points over 2026-01-01 to
    2026-09-09, against -30.0 for a stop-out), so a tight stop kills the only
    exit that pays. Widening these from 2.0/8.0 cut the stop-out rate from
    51.8% to 46.2% and roughly doubled the year. Anything in the 5-12 range for
    STOP_BUFFER performs similarly; 10.0 is not a fitted optimum.

Management (2 lots):
    BOOK_AT_R  sell 1 lot, move the stop on the remaining lot to breakeven
    beyond     the remaining lot trails the nearer line, and is closed when a
               candle CLOSES back below it (above it, for a short)
    15:00      everything is squared off

NIFTY spot publishes no volume, so the VWAP is weighted by synthetic volume:
the summed traded shares of all 50 constituents on the same 5-minute grid,
applied to the INDEX typical price (H+L+C)/3. That reconstruction was checked
against TradingView on 2026-09-08 15:10 - 11,254,832 against a displayed
11.27M, a 0.13% difference.

SAFETY: this script refuses to place an order unless OpenAlgo is in analyzer
(sandbox) mode. Flipping that requires editing I_UNDERSTAND_THIS_IS_LIVE by
hand. Analyzer mode is a GLOBAL instance flag, so it is re-checked every cycle.

COST: rebuilding the synthetic volume needs one history call per constituent
per bar. OpenAlgo paces Angel's historical endpoint to roughly 2 requests a
second, so a refresh takes about 30 seconds. The entry order therefore lands
part-way into candle 3 rather than exactly at its open; backtested fills assume
that open, so expect some slippage against them.

Note on logging: the strategy host captures stdout to log/strategies/, so
print() is the logging channel here. That is the host contract and differs
deliberately from the repo-wide rule that application modules use utils.logging.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
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

I_UNDERSTAND_THIS_IS_LIVE = False   # must be True to run outside analyzer mode
DRY_RUN = False                     # resolve and log, place nothing

STRATEGY_TAG = "NIFTY_2CANDLE_VWAP_SMA45"
UNDERLYING = "NIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
EQUITY_EXCHANGE = "NSE"
FO_EXCHANGE = "NFO"
PRODUCT = "MIS"                     # intraday, squared off the same session

INTERVAL = "5m"
SMA_PERIOD = 45
LOOKBACK_DAYS = 10                  # history span, must warm up SMA(45)

LOTS = 2                            # 1 lot is booked at +BOOK_AT_R, 1 lot trails
BOOK_AT_R = 1.5                     # R multiple that books a lot and arms the trail
MAX_TRADES_PER_DAY = 2
DIRECTIONS = ("long", "short")      # sides to trade

# Regime filter: take no signal at or below this India VIX. Set to None to disable.
#
# Backtested 2026-01-01 to 2026-09-10, 171 sessions, VIX read on the signal bar
# itself so there is no lookahead. Splitting every unfiltered trade by the VIX at
# its entry gives a clean sign change at 12, not a fitted cliff:
#
#     VIX < 11   12 trades   avg  -0.87 pts   win 33%
#     11 - 12    34 trades   avg  -0.78       win 38%
#     12 - 13    27 trades   avg  +8.66       win 41%
#     13 - 15    45 trades   avg +12.14       win 58%
#     over 18    45 trades   avg +27.71       win 53%
#
# The 46 trades below 12 netted -36.9 points between them. Removing them lifts the
# average trade from +11.12 to +15.02, profit factor 1.59 -> 1.75, and cuts max
# drawdown 455 -> 349 points. The threshold is not sharp: anything from 11.5 to 13
# behaves similarly, so 12 is a point on a smooth curve rather than an optimum.
#
# Know what this does in a calm tape: through 2026 it removes trades only in
# January, February, August and September. In September 2026 it takes NONE - the
# strategy simply stands aside until volatility returns.
VIX_MIN = 12.0

MIN_CLEARANCE = 0.0                 # points a candle must clear both lines by
STOP_BUFFER = 10.0                  # stop sits this far beyond candle 1
LINE_PROXIMITY = 25.0               # swap to the line when it is this close

SESSION_OPEN = dtime(9, 15)
NO_NEW_ENTRY_BEFORE = dtime(10, 30)  # the first hour backtests strongly negative
NO_NEW_ENTRY_AFTER = dtime(14, 30)  # too close to square-off to be worth it
SQUARE_OFF = dtime(15, 0)
ABANDON_AFTER = dtime(15, 20)       # the exchange refuses MIS orders past 15:15

BAR_SECONDS = 300
BAR_DELAY = 10                      # wait this long after a bar closes
USE_WEBSOCKET_VOLUME = True         # False falls back to 51 history calls a bar
FEED_WARMUP = 6                     # seconds to collect the opening snapshot
FEED_SNAPSHOT_LAG = 1.0             # snapshot this long after a bar boundary
HISTORY_PACE = 0.55                 # seconds between history calls

IST = ZoneInfo("Asia/Kolkata")
STATE_DIR = Path("strategies") / "state"
STATE_FILE = STATE_DIR / "nifty_2candle_vwap_sma45.json"

# NIFTY 50 constituents, verified against the Angel symbol master on 2026-09-07.
# TMPV is Tata Motors after the passenger-vehicle demerger and rename; the old
# TATAMOTORS ticker no longer resolves. Re-verify at every NSE index review.
CONSTITUENTS = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO",
    "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY",
    "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT",
    "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
    "SUNPHARMA", "TATACONSUM", "TMPV", "TATASTEEL", "TCS",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
]

_shutdown = False
_PRIOR_INDEX: pd.DataFrame | None = None
_PRIOR_DAY: str | None = None
_FEED: VolumeFeed | None = None
_VOLUME_BY_BAR: dict = {}
_VOLUME_DAY: str | None = None


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

    The strategy host normally injects the key. When it starts the process
    before its own environment carries one, the variable is simply absent and
    the run used to die on its first line. Reading the same file the host is
    configured from turns that into a recoverable case.

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


def normalize_expiry(dashed: str) -> str:
    """'08-SEP-26' -> '08SEP26'."""
    return dashed.replace("-", "").upper()


# =============================================================================
# SAFETY RAILS
# =============================================================================

def assert_paper_mode(client) -> bool:
    """Analyzer mode is global to the instance, so this is checked every cycle."""
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
    analyze = bool((st.get("data") or {}).get("analyze_mode"))
    if analyze:
        return True
    if I_UNDERSTAND_THIS_IS_LIVE:
        log("WARNING: analyzer mode is OFF and live trading was acknowledged")
        return True
    log("analyzer mode is OFF and I_UNDERSTAND_THIS_IS_LIVE is False; not trading")
    return False


# =============================================================================
# MARKET DATA
# =============================================================================

def fetch_history(client, symbol: str, exchange: str, start: str, end: str,
                  retries: int = 3) -> pd.DataFrame:
    """Fetch one symbol's bars, pacing and retrying around Angel throttling.

    An exhausted retry surfaces as an empty frame or a no_data dict rather than
    an HTTP error, so both are treated as retryable.

    Returns:
        DataFrame indexed by timestamp, empty when unavailable.
    """
    for attempt in range(1, retries + 1):
        time.sleep(HISTORY_PACE)
        try:
            result = client.history(symbol=symbol, exchange=exchange,
                                    interval=INTERVAL, start_date=start,
                                    end_date=end)
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the run
            log(f"history {exchange}/{symbol} raised {exc!r}")
            result = None
        if isinstance(result, pd.DataFrame) and not result.empty:
            return result
        time.sleep(HISTORY_PACE * (2 ** attempt))
    log(f"history {exchange}/{symbol}: giving up after {retries} attempts")
    return pd.DataFrame()


# =============================================================================
# CONSTITUENT VOLUME FEED
# =============================================================================

class VolumeFeed:
    """Turns the WebSocket quote feed into per-bar constituent volume.

    Angel's quote mode carries ``volume_trade_for_the_day`` - the running total
    for the session - so a bar's volume is the difference between two snapshots
    taken at consecutive 5-minute boundaries. Differencing a cumulative counter
    is self-healing: a dropped tick or a reconnect cannot lose volume, because
    the next tick still carries the full running total. Summing
    ``last_traded_quantity`` instead would lose every tick that went missing.

    Snapshots are taken by an internal thread a second after each boundary, not
    by the strategy loop, so a bar's figure is not contaminated by trades from
    the bar that has just started.
    """

    def __init__(self, client, symbols: list[str]) -> None:
        self.client = client
        self.symbols = symbols
        self.started = False
        self._cumulative: dict[str, int] = {}
        self._baseline: dict[str, int] = {}
        self._baseline_at: datetime | None = None
        self._bars: dict[pd.Timestamp, tuple[float, int]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _on_tick(self, message) -> None:
        if not isinstance(message, dict):
            return
        symbol = message.get("symbol")
        payload = message.get("data") or {}
        volume = payload.get("volume")
        if symbol and volume is not None:
            with self._lock:
                self._cumulative[symbol] = int(volume)

    def start(self) -> bool:
        """Connect, subscribe, and begin snapshotting at bar boundaries.

        Safe to call again after a failed attempt: the host starts this
        strategy before the open, where the warm-up sees no quotes at all.

        Returns:
            True when the feed is live and a baseline has been captured.
        """
        if self.started:
            return True
        try:
            if not self.client.connect():
                log("websocket connect failed")
                return False
            instruments = [{"exchange": EQUITY_EXCHANGE, "symbol": s} for s in self.symbols]
            if not self.client.subscribe_quote(instruments, on_data_received=self._on_tick):
                log("quote subscription rejected")
                return False
        except Exception as exc:  # noqa: BLE001 - fall back to history on any failure
            log(f"websocket setup raised {exc!r}")
            return False

        time.sleep(FEED_WARMUP)
        with self._lock:
            self._baseline = dict(self._cumulative)
        if not self._baseline:
            log("no quotes arrived during warm-up; feed not up yet")
            return False
        self._baseline_at = datetime.now(IST)
        log(f"volume feed live: {len(self._baseline)}/{len(self.symbols)} symbols reporting")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.started = True
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            wait = BAR_SECONDS - (time.time() % BAR_SECONDS) + FEED_SNAPSHOT_LAG
            if self._stop.wait(wait):
                return
            try:
                self._close_bar()
            except Exception as exc:  # noqa: BLE001 - a bad snapshot must not kill the thread
                log(f"volume snapshot failed: {exc!r}")

    def _close_bar(self) -> None:
        """Difference the current snapshot against the previous boundary."""
        now = datetime.now(IST)
        boundary = now.replace(second=0, microsecond=0)
        boundary -= timedelta(minutes=boundary.minute % 5)
        bar_start = boundary - timedelta(seconds=BAR_SECONDS)

        # A bar that had already begun when the baseline was taken is only
        # partly covered by the difference, so it is left to the history
        # backfill rather than recorded at a fraction of its real volume.
        if self._baseline_at is not None and bar_start < self._baseline_at:
            with self._lock:
                self._baseline = dict(self._cumulative)
            return

        with self._lock:
            current = dict(self._cumulative)
        total = 0.0
        contributors = 0
        for symbol, value in current.items():
            base = self._baseline.get(symbol)
            if base is None:
                continue
            delta = value - base
            if delta <= 0:
                # Zero means the stock did not trade in the bar; negative means
                # the counter reset, which only happens across sessions.
                continue
            total += float(delta)
            contributors += 1
        self._baseline = current
        with self._lock:
            self._bars[bar_start] = (total, contributors)

    def bars(self) -> dict[pd.Timestamp, tuple[float, int]]:
        """Return the per-bar volumes captured so far.

        Returns:
            Mapping of bar start time to (volume, contributing symbols).
        """
        with self._lock:
            return dict(self._bars)

    def stop(self) -> None:
        """Unsubscribe and shut the snapshot thread down."""
        if not self.started:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            instruments = [{"exchange": EQUITY_EXCHANGE, "symbol": s} for s in self.symbols]
            self.client.unsubscribe_quote(instruments)
            self.client.disconnect()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            log(f"feed shutdown raised {exc!r}")


def backfill_volume(client, grid) -> tuple[pd.Series, pd.Series]:
    """Rebuild constituent volume for a whole grid from the history API.

    This is the slow path - one call per constituent, each paced server-side by
    ANGEL_HISTORY_MIN_INTERVAL. It runs once at startup to cover bars that
    closed before the feed was subscribed, and again only if a bar goes missing.

    Returns:
        Tuple of (volume series, contributor-count series) on the grid.
    """
    today = grid[0].date()
    volume = pd.Series(0.0, index=grid)
    contributors = pd.Series(0, index=grid)
    for symbol in CONSTITUENTS:
        bars = fetch_history(client, symbol, EQUITY_EXCHANGE, str(today), str(today))
        if bars.empty or "volume" not in bars.columns:
            continue
        if bars.index.tz is None:
            bars = bars.tz_localize(IST)
        series = bars["volume"].astype(float).reindex(grid)
        contributors += series.notna().astype(int)
        volume = volume.add(series.fillna(0.0), fill_value=0.0)
    return volume, contributors


def volume_for_grid(client, grid) -> tuple[pd.Series, pd.Series]:
    """Assemble per-bar volume, preferring the feed and backfilling any gaps.

    Returns:
        Tuple of (volume series, contributor-count series) on the grid.
    """
    global _VOLUME_BY_BAR, _VOLUME_DAY
    today = str(grid[0].date())
    if _VOLUME_DAY != today:
        _VOLUME_BY_BAR, _VOLUME_DAY = {}, today

    if _FEED is not None and _FEED.started:
        for bar_start, measured in _FEED.bars().items():
            _VOLUME_BY_BAR.setdefault(bar_start, measured)

    missing = [stamp for stamp in grid if stamp not in _VOLUME_BY_BAR]
    if missing:
        log(f"{len(missing)} bars without feed volume; backfilling from history "
            f"({len(CONSTITUENTS)} calls)")
        filled, counts = backfill_volume(client, grid)
        for stamp in grid:
            _VOLUME_BY_BAR[stamp] = (float(filled[stamp]), int(counts[stamp]))

    volume = pd.Series([_VOLUME_BY_BAR[s][0] for s in grid], index=grid, dtype=float)
    contributors = pd.Series([_VOLUME_BY_BAR[s][1] for s in grid], index=grid, dtype=int)
    return volume, contributors


def prior_index_bars(client, today) -> pd.DataFrame:
    """Return the index bars before today, fetched once per session.

    Only the index needs history older than today: the VWAP resets every
    session, so constituent volume before today is never used, while SMA(45)
    needs roughly four hours of prior closes. Those bars are immutable once the
    session has ended, so they are fetched once and reused all day.

    Returns:
        Bars from the lookback window up to yesterday, possibly empty.
    """
    global _PRIOR_INDEX, _PRIOR_DAY
    key = str(today)
    if _PRIOR_DAY != key:
        _PRIOR_INDEX, _PRIOR_DAY = None, key
    if _PRIOR_INDEX is None:
        _PRIOR_INDEX = fetch_history(
            client, UNDERLYING, INDEX_EXCHANGE,
            str(today - timedelta(days=LOOKBACK_DAYS)), str(today - timedelta(days=1)),
        )
        log(f"cached {len(_PRIOR_INDEX)} prior index bars for the session")
    return _PRIOR_INDEX


def drop_absurd(frame: pd.DataFrame) -> pd.DataFrame:
    """Remove bars whose range dwarfs the median.

    Angel occasionally emits a junk late-session index bar; on 2026-09-08 a
    15:20 bar spanned 452 points between two closes six points apart. Left in,
    it poisons the VWAP for the rest of the day and the SMA for days after.

    Returns:
        The frame without implausible bars.
    """
    span = frame["high"] - frame["low"]
    absurd = span > 10.0 * span.median()
    for stamp in frame.index[absurd]:
        log(f"dropping implausible bar {stamp}: range {float(span[stamp]):.2f}")
    return frame[~absurd]


def build_frame(client) -> pd.DataFrame:
    """Assemble today's bars with synthetic volume, VWAP and the SMA.

    Returns:
        Today's indicator frame, empty when the data could not be assembled.
    """
    today = datetime.now(IST).date()
    prior = prior_index_bars(client, today)
    index_today = fetch_history(client, UNDERLYING, INDEX_EXCHANGE,
                                str(today), str(today))
    if index_today.empty:
        log("no index bars for today")
        return pd.DataFrame()

    combined = pd.concat([prior, index_today]) if not prior.empty else index_today
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    combined = drop_absurd(combined)
    if combined.index.tz is None:
        combined = combined.tz_localize(IST)
    else:
        combined = combined.tz_convert(IST)

    # Drop the bar that is still forming. Angel labels a bar with the boundary
    # it OPENS on and serves it the moment that boundary passes, so the newest
    # row at BAR_DELAY seconds past the close is a candle a few seconds old,
    # not a completed one. Read as complete it moves the signal twice over:
    # `latest_signal` tests it as candle 2, and its close is revised on the
    # next cycle, so a pair that qualified stops qualifying.
    #
    # On 2026-09-10 both entries came from this. The 12:20 bar read 23464.10
    # against 12:15's 23462.50 and fired a long; it finally closed 23457.35
    # against 23462.10, which does not. Same for 13:45. Both stopped out.
    #
    # It also costs every cycle two minutes: the forming bar is one the volume
    # feed has correctly not recorded, so `volume_for_grid` finds it missing
    # and falls back to 50 history calls on every single bar.
    cutoff = datetime.now(IST) - timedelta(seconds=BAR_SECONDS)
    combined = combined[combined.index <= cutoff]
    if combined.empty:
        return pd.DataFrame()

    # SMA(45) spans prior sessions, so it is computed before today is sliced out.
    combined["sma"] = combined["close"].rolling(SMA_PERIOD).mean()
    is_today = [stamp.date() == today for stamp in combined.index]

    frame = combined[is_today].copy()
    if frame.empty:
        return pd.DataFrame()

    volume, contributors = volume_for_grid(client, frame.index)

    if volume.sum() <= 0:
        log("synthetic volume is zero; cannot compute VWAP")
        return pd.DataFrame()

    frame["volume"] = volume
    frame["contributors"] = contributors
    frame = frame[frame["contributors"] > 0]

    # One session, so the VWAP is a plain running total with no daily grouping.
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    frame["vwap"] = typical.mul(frame["volume"]).cumsum() / frame["volume"].cumsum()
    frame["upper"] = frame[["vwap", "sma"]].max(axis=1)
    frame["lower"] = frame[["vwap", "sma"]].min(axis=1)
    frame["above"] = frame["low"] > frame["upper"] + MIN_CLEARANCE
    frame["below"] = frame["high"] < frame["lower"] - MIN_CLEARANCE
    return frame.dropna(subset=["vwap", "sma"])


def read_vix(client) -> float | None:
    """India VIX right now, used as the regime gate on a fresh signal.

    Returns:
        The last traded VIX, or None when it cannot be read.
    """
    try:
        q = client.quotes(symbol="INDIAVIX", exchange=INDEX_EXCHANGE)
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"VIX quote raised {exc!r}")
        return None
    if not ok(q):
        log(f"VIX quote failed: {q}")
        return None
    d = q.get("data") or {}
    return float(d.get("ltp") or d.get("prev_close") or 0) or None


# =============================================================================
# SIGNAL
# =============================================================================

def latest_signal(frame: pd.DataFrame) -> dict | None:
    """Test the two most recently completed candles for a first-pair breakout.

    Returns:
        Signal dict, or None when the last pair does not qualify.
    """
    if len(frame) < 3:
        return None
    today = datetime.now(IST).date()
    second, first, before = frame.iloc[-1], frame.iloc[-2], frame.iloc[-3]
    if frame.index[-1].date() != today or frame.index[-2].date() != today:
        return None

    for direction, flag in (("long", "above"), ("short", "below")):
        if direction not in DIRECTIONS:
            continue
        if not (bool(first[flag]) and bool(second[flag])):
            continue
        # The run has to begin at candle 1, so the candle before it must not
        # already have been clear of both lines.
        if bool(before[flag]) and frame.index[-3].date() == today:
            continue
        if direction == "long" and second["close"] <= first["close"]:
            continue
        if direction == "short" and second["close"] >= first["close"]:
            continue
        return {
            "direction": direction,
            "c1_low": float(first["low"]),
            "c1_high": float(first["high"]),
            "line": float(first["upper"] if direction == "long" else first["lower"]),
            "signal_bar": frame.index[-1].isoformat(),
        }
    return None


def initial_stop(sig: dict) -> float:
    """Candle 1's extreme, or the nearby line when it sits close to it.

    Returns:
        Stop level in index points.
    """
    if sig["direction"] == "long":
        level = sig["c1_low"] - STOP_BUFFER
        if 0 <= level - sig["line"] <= LINE_PROXIMITY:
            return sig["line"] - STOP_BUFFER
        return level
    level = sig["c1_high"] + STOP_BUFFER
    if 0 <= sig["line"] - level <= LINE_PROXIMITY:
        return sig["line"] + STOP_BUFFER
    return level


# =============================================================================
# ORDERS
# =============================================================================

def resolve_atm(client, expiry: str, option_type: str) -> dict | None:
    """Resolve the ATM option for one side against real listed strikes.

    Returns:
        Dict with symbol and lotsize, or None when the lookup fails.
    """
    r = client.optionsymbol(underlying=UNDERLYING, exchange=INDEX_EXCHANGE,
                            expiry_date=expiry, offset="ATM",
                            option_type=option_type)
    if not ok(r):
        log(f"optionsymbol {option_type} failed: {r}")
        return None
    return {"symbol": r["symbol"], "lotsize": int(r["lotsize"])}


def nearest_weekly(client) -> str | None:
    """Return the nearest option expiry AFTER today, in compact form.

    Today's own expiry is skipped deliberately. Every stop, target and trail
    here is measured in INDEX points, but an expiry-day ATM option held to the
    15:00 square-off decays to almost nothing whatever the index does, so a
    0-DTE contract cannot express the move the backtest measured. On NIFTY that
    costs one week of extra time value roughly one day in five.

    The list is parsed and sorted rather than trusted in order, so an unsorted
    or stale response cannot hand back a contract that has already expired.

    Returns:
        Expiry such as ``22SEP26``, or None when the lookup fails or nothing
        listed expires after today.
    """
    e = client.expiry(symbol=UNDERLYING, exchange=FO_EXCHANGE,
                      instrumenttype="options")
    if not ok(e) or not e.get("data"):
        log(f"expiry lookup failed: {e}")
        return None
    today = datetime.now(IST).date()
    dated = []
    for dashed in e["data"]:
        try:
            dated.append((datetime.strptime(dashed, "%d-%b-%y").date(), dashed))
        except (TypeError, ValueError):
            log(f"ignoring unparseable expiry {dashed!r}")
    if not dated:
        log(f"no expiry could be parsed from {e['data']}")
        return None
    dated.sort()
    stale = [dashed for day, dashed in dated if day <= today]
    for day, dashed in dated:
        if day > today:
            if stale:
                log(f"skipping expiry {', '.join(stale)} (today or past); "
                    f"using {dashed}")
            return normalize_expiry(dashed)
    log(f"nothing expires after {today}; furthest listed is {dated[-1][1]}")
    return None


def send(client, symbol: str, action: str, quantity: int) -> bool:
    """Place one market order on the option leg.

    Returns:
        True when the order was accepted.
    """
    if DRY_RUN:
        log(f"DRY_RUN {action} {quantity} {symbol}")
        return True
    r = client.placeorder(strategy=STRATEGY_TAG, symbol=symbol,
                          action=action, exchange=FO_EXCHANGE,
                          price_type="MARKET", product=PRODUCT,
                          quantity=quantity)
    if not ok(r):
        log(f"order REJECTED {action} {quantity} {symbol}: {r}")
        return False
    log(f"order OK {action} {quantity} {symbol} -> {r.get('orderid')}")
    return True


def net_quantity(client, symbol: str) -> int | None:
    """Net open quantity the broker reports for one option leg.

    Returns:
        The quantity, 0 when the book carries no open lot, or None when the
        book cannot be read at all.
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


def abandon_if_flat(client, state: dict) -> bool:
    """Drop the tracked position when the broker says it is already closed.

    An exit order is rejected for two very different reasons. Either the order
    itself failed and retrying next bar is right, or the position is simply no
    longer there - squared off by the sandbox or the broker on their own
    schedule, which also refuse fresh MIS orders after 15:15 IST. Retrying that
    second case never succeeds, and on 2026-09-15 it left the strategy posting
    the same rejected SELL every five minutes for ninety minutes after the
    position had in fact been closed at 15:39.

    The position book is the tiebreaker: it is what the broker actually holds,
    where the state file is only what this process last believed.

    Returns:
        True when the position was cleared, False when the broker still
        reports an open lot or the book could not be read.
    """
    position = state.get("position")
    if not position:
        return True
    qty = net_quantity(client, position["symbol"])
    if qty is None:
        return False
    if qty == 0:
        log(f"broker reports no open {position['symbol']}; it was closed "
            f"elsewhere, so the local position is stale - clearing it")
        state["position"] = None
        return True
    log(f"broker still reports {qty} open {position['symbol']}; keeping the "
        f"position and retrying the exit next bar")
    return False


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
    """Reset the per-session counters when the date rolls over.

    Returns:
        State dict valid for today.
    """
    if state.get("date") != today:
        state = {"date": today, "trades": 0, "position": None}
    return state


# =============================================================================
# MANAGEMENT
# =============================================================================

def manage(client, state: dict, frame: pd.DataFrame) -> dict:
    """Advance stops on the open position and close it when a rule fires.

    Returns:
        The updated state dict.
    """
    position = state.get("position")
    if not position:
        return state

    bar = frame.iloc[-1]
    long = position["direction"] == "long"
    close = float(bar["close"])
    stop = float(position["stop"])
    entry = float(position["entry"])
    risk = float(position["risk"])
    lot = int(position["lotsize"])

    excursion = (float(bar["high"]) - entry) if long else (entry - float(bar["low"]))
    position["mfe"] = max(float(position.get("mfe", 0.0)), excursion)

    breached = (float(bar["low"]) <= stop) if long else (float(bar["high"]) >= stop)
    if breached:
        qty = lot * position["lots_open"]
        if send(client, position["symbol"], "SELL", qty):
            log(f"STOP hit at {stop:.2f} on the index; closed {qty}")
            state["position"] = None
        else:
            abandon_if_flat(client, state)
        return state

    # +BOOK_AT_R: book one lot and move the survivor to breakeven.
    if not position["booked"] and position["mfe"] >= BOOK_AT_R * risk:
        if send(client, position["symbol"], "SELL", lot):
            position["lots_open"] -= 1
            position["booked"] = True
            position["stop"] = entry
            log(f"+{BOOK_AT_R}R reached (mfe {position['mfe']:.2f} pts); booked "
                f"1 lot, stop to breakeven {entry:.2f}")
        elif abandon_if_flat(client, state):
            return state
        if position["lots_open"] <= 0:
            state["position"] = None
            return state

    # The runner trails the nearer line and dies on a close back through it.
    if position["booked"]:
        line = float(bar["upper"]) if long else float(bar["lower"])
        position["stop"] = max(position["stop"], line) if long else min(
            position["stop"], line)
        lost = close < line if long else close > line
        if lost:
            qty = lot * position["lots_open"]
            if send(client, position["symbol"], "SELL", qty):
                log(f"close {close:.2f} back through the line {line:.2f}; "
                    f"closed the runner ({qty})")
                state["position"] = None
            else:
                abandon_if_flat(client, state)
            return state

    state["position"] = position
    return state


def square_off(client, state: dict) -> dict:
    """Flatten whatever is open at the square-off time.

    Returns:
        The updated state dict.
    """
    position = state.get("position")
    if not position:
        return state
    qty = int(position["lotsize"]) * int(position["lots_open"])
    if send(client, position["symbol"], "SELL", qty):
        log(f"square-off: closed {qty} {position['symbol']}")
        state["position"] = None
    else:
        abandon_if_flat(client, state)
    return state


# =============================================================================
# MAIN
# =============================================================================

def cycle(client, state: dict) -> dict:
    """Run one bar's worth of work.

    Returns:
        The updated state dict.
    """
    now = datetime.now(IST)
    state = fresh_day(state, str(now.date()))

    # Flattening must not depend on the data pipeline: a history hiccup after
    # SQUARE_OFF would otherwise leave the position open past the session.
    if now.time() >= SQUARE_OFF:
        return square_off(client, state)

    frame = build_frame(client)
    if frame.empty:
        log("no usable bars for today yet")
        return state

    last = frame.index[-1]
    log(f"bar {last:%H:%M} close {float(frame['close'].iloc[-1]):.2f} "
        f"vwap {float(frame['vwap'].iloc[-1]):.2f} "
        f"sma {float(frame['sma'].iloc[-1]):.2f}")

    state = manage(client, state, frame)

    if state.get("position"):
        return state
    if state.get("trades", 0) >= MAX_TRADES_PER_DAY:
        return state
    if now.time() >= NO_NEW_ENTRY_AFTER:
        return state
    if now.time() < NO_NEW_ENTRY_BEFORE:
        return state

    sig = latest_signal(frame)
    if not sig:
        return state

    if VIX_MIN is not None:
        vix = read_vix(client)
        if vix is None:
            # The whole point of the gate is to know the regime before committing,
            # so an unreadable VIX skips the signal rather than trading blind.
            log("VIX unreadable; skipping this signal rather than guessing the regime")
            return state
        if vix <= VIX_MIN:
            log(f"{sig['direction']} signal skipped: India VIX {vix:.2f} is at or "
                f"below VIX_MIN {VIX_MIN}")
            return state
        log(f"India VIX {vix:.2f} clears VIX_MIN {VIX_MIN}")

    stop = initial_stop(sig)
    spot = float(frame["close"].iloc[-1])
    risk = (spot - stop) if sig["direction"] == "long" else (stop - spot)
    if risk <= 0:
        log(f"{sig['direction']} signal ignored: non-positive risk")
        return state

    expiry = nearest_weekly(client)
    if not expiry:
        return state
    leg = resolve_atm(client, expiry, "CE" if sig["direction"] == "long" else "PE")
    if not leg:
        return state

    quantity = leg["lotsize"] * LOTS
    log(f"{sig['direction'].upper()} signal: index {spot:.2f}, stop {stop:.2f} "
        f"(risk {risk:.2f} pts), buying {quantity} {leg['symbol']}")
    if not send(client, leg["symbol"], "BUY", quantity):
        return state

    state["trades"] = state.get("trades", 0) + 1
    state["position"] = {
        "direction": sig["direction"], "symbol": leg["symbol"],
        "lotsize": leg["lotsize"], "lots_open": LOTS,
        "entry": spot, "stop": stop, "risk": risk,
        "mfe": 0.0, "booked": False, "opened": now.isoformat(),
    }
    return state


def ensure_feed() -> None:
    """Bring the volume feed up, retrying on later bars until it takes.

    The host starts this strategy at 09:00, so the first warm-up lands in the
    pre-open silence and no quote ever arrives. Giving up there would push
    every bar of the session onto the 50-call history path, which Angel rate
    limits, so the attempt is repeated until the feed is live.
    """
    if _FEED is None or _FEED.started:
        return
    if datetime.now(IST).time() < SESSION_OPEN:
        return
    if _FEED.start():
        return
    log("volume feed still not up; using the history API for this bar")


def sleep_to_next_bar() -> None:
    """Block until shortly after the next 5-minute candle closes."""
    now = datetime.now(IST)
    epoch = int(now.timestamp())
    wait = BAR_SECONDS - (epoch % BAR_SECONDS) + BAR_DELAY
    time.sleep(wait)


def main() -> int:
    """Run the strategy until the session ends or a signal stops it.

    Returns:
        Process exit code.
    """
    global _FEED

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    client = build_client()
    log(f"{STRATEGY_TAG} starting: {LOTS} lots, 1 booked at +{BOOK_AT_R}R, runner trails "
        f"the lines, square-off {SQUARE_OFF:%H:%M}")

    if USE_WEBSOCKET_VOLUME:
        _FEED = VolumeFeed(build_client(), CONSTITUENTS)
    else:
        log("websocket volume disabled by config; using the history API")

    state = load_state()
    try:
        while not _shutdown:
            now = datetime.now(IST)
            if now.time() < SESSION_OPEN:
                sleep_to_next_bar()
                continue
            if now.time() >= SQUARE_OFF and not state.get("position"):
                log("past square-off with nothing open; done for the day")
                return 0
            if now.time() >= ABANDON_AFTER and state.get("position"):
                # Past this point the exchange refuses MIS orders outright, so
                # another exit attempt cannot succeed however many bars it is
                # given. Reconcile once, then stop either way rather than
                # looping until the host happens to kill the process.
                if not abandon_if_flat(client, state):
                    log(f"still tracking {state['position']['symbol']} past "
                        f"{ABANDON_AFTER:%H:%M} with exits refused; stopping. "
                        f"CHECK THE BROKER POSITION BY HAND")
                    state["position"] = None
                save_state(state)
                return 0
            if not assert_paper_mode(client):
                time.sleep(60)
                continue
            ensure_feed()
            try:
                state = cycle(client, state)
            except Exception as exc:  # noqa: BLE001 - one bad bar must not kill the day
                log(f"cycle failed: {exc!r}")
            save_state(state)
            sleep_to_next_bar()
    finally:
        save_state(state)
        if _FEED is not None:
            _FEED.stop()

    log("shut down cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
