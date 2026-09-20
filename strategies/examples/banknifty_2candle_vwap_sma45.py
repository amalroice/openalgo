"""NIFTY Bank two-candle VWAP + SMA(45) breakout, ATM monthly options.

A parallel paper test of the NIFTY 50 strategy (nifty_2candle_vwap_sma45.py)
with the same rules applied to NIFTY BANK. It shares no state, tag or schedule
with that script and changes nothing in it. The differences:

    - Signals are read off NIFTY BANK spot on 5-minute candles. Orders go to the
      ATM option of the nearest expiry; BANKNIFTY lists monthly expiries only,
      so that is the current month contract - CE on a long, PE on a short.
    - STOP_BUFFER and LINE_PROXIMITY are the NIFTY values scaled by the index
      level ratio (56,606.55 / 23,398.10 = 2.42 on 2026-09-11), so the stop sits
      the same relative distance from candle 1.
    - No India VIX filter, and entries run 11:00 to 14:30 rather than 10:30 to
      14:30. Both come from BANKNIFTY backtests; see the notes at the settings.
    - A stretch rule: while India VIX is 14 or below, a signal is skipped when its
      entry sits more than 0.4% of price off the day's low (buy) or high (sell) so
      far.
    - Synthetic volume comes from the six largest NIFTY Bank constituents only.

Every stop, target and trail is measured in INDEX points, never in premium.

Entry (long; short is the mirror):
    1. Two consecutive candles lie completely above both VWAP and SMA(45) -
       each candle's low is above the higher of the two lines.
    2. The pair is the first of its run: the candle before candle 1 was not
       itself completely above both lines.
    3. Candle 2 closes above candle 1's HIGH (a short mirrors it: candle 2
       closes below candle 1's LOW). Clearing candle 1's whole range, not just
       its close, is what makes this a breakout.
    Entry is a market order as soon as candle 2 closes, and never before
    NO_NEW_ENTRY_BEFORE. On NIFTY over 2026-01-01 to 2026-09-09 the average trade by
    entry hour climbed monotonically from -7.58 index points at 09h to +9.89
    at 13h; skipping the first hour turned that year from -329.7 points to
    +541.5 and halved the drawdown.
    4. No India VIX filter in this copy (the NIFTY version requires VIX above
       12). VIX_MIN and VIX_MAX are still honoured and are both off.
    5. Stretch rule (this copy only): while India VIX is inside the STRETCH_VIX_LOW /
       STRETCH_VIX_HIGH band (now 14 and below), a buy's entry must be within
       STRETCH_MAX_PCT of the day's low so far, and a sell's within it of the day's
       high so far.

Stop:
    Candle 1's low, less STOP_BUFFER points. If the higher line sits within
    LINE_PROXIMITY points of that level, the line is used instead. The lines
    are always below candle 1's low by construction, so this widens the stop
    rather than tightening it.

    Both values are deliberately wide. Profit comes from positions that survive
    to square-off (on NIFTY they averaged +62.7 index points over 2026-01-01 to
    2026-09-09, against -30.0 for a stop-out), so a tight stop kills the only
    exit that pays. Widening these from 2.0/8.0 cut the stop-out rate from
    51.8% to 46.2% and roughly doubled the year. Anything in the 5-12 range for
    STOP_BUFFER performs similarly on NIFTY; 10.0 is not a fitted optimum, and
    the 25.0 used here is that value scaled to the BANKNIFTY level.

Management (2 lots, at most MAX_TRADES_PER_DAY entries a session):
    trail      both lots stay in. The stop sits TRAIL_DIST_PCT of the entry
               behind the best price, moved in TRAIL_STEP_PCT steps, and never
               below the candle stop. No partial booking, no line exit.
    15:00      everything is squared off

Index spot publishes no volume, so the VWAP is weighted by synthetic volume:
the summed traded shares of the six largest constituents on the same 5-minute
grid, applied to the INDEX typical price (H+L+C)/3. The eight smaller banks are
left out on purpose: summing shares would let low-priced, heavily traded names
such as YESBANK and PNB dominate the weighting. The method was checked against
TradingView for NIFTY 50 only and has not been calibrated for NIFTY Bank.

SAFETY: this script refuses to place an order unless OpenAlgo is in analyzer
(sandbox) mode. Flipping that requires editing I_UNDERSTAND_THIS_IS_LIVE by
hand. Analyzer mode is a GLOBAL instance flag, so it is re-checked every cycle.

COST: rebuilding the synthetic volume needs one history call per constituent
per bar. OpenAlgo paces Angel's historical endpoint to roughly 2 requests a
second, so a refresh takes about 5 seconds. The entry order therefore lands
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

I_UNDERSTAND_THIS_IS_LIVE = True    # must be True to run outside analyzer mode
DRY_RUN = False                     # resolve and log, place nothing

STRATEGY_TAG = "BANKNIFTY_2CANDLE_VWAP_SMA45"
UNDERLYING = "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
EQUITY_EXCHANGE = "NSE"
FO_EXCHANGE = "NFO"
PRODUCT = "MIS"                     # intraday, squared off the same session

INTERVAL = "5m"
SMA_PERIOD = 45
LOOKBACK_DAYS = 10                  # history span, must warm up SMA(45)

LOTS = 1                            # cut from 2 on 2026-09-20 for the first live week

# Percent trailing stop, replacing 1-lot booking at +1.5R and the line trail on
# 2026-09-18. Replayed 2018-01-01..2026-09-16 on this script's own entries, net
# of 4 pts a trade, closes filled at the bar close:
#
#                        last 2y   last 5y   2018-01..2021-09     full    maxDD
#     book 1.5R + line    +6,371    +9,386        +4,400        +13,786   -3,301
#     trail 0.5% / 0.1%   +8,905   +11,591        +4,707        +16,298   -4,140
#
# Better in all four windows and in 6 of 9 years (worse in 2020-2022), at the
# cost of a deeper drawdown and a 44% -> 36% win rate. The step matters: with
# the line exit kept, 0.5%/0.2% was worse than the old rule in 2018-2021 and
# 0.4%/0.2% no better overall.
# Figures are index points - the trail holds longer, and option decay over that
# extra time is not in them.
TRAIL_DIST_PCT = 0.005              # stop distance behind the best price
TRAIL_STEP_PCT = 0.001              # the stop moves once per step of this size

# TESTED AND REJECTED, 2026-09-20: protecting profit earlier. Median favourable
# move here is 80.7 pts and the stop first lifts above entry around 270:
#
#                           last 2y  last 5y  2018-01..2021-09   full   maxDD
#     none (this setting)    +5,594   +5,591       +4,032      +9,907  -2,366
#     breakeven at +30 pts   +4,775   +6,579       +1,800      +8,695  -1,505
#     breakeven at +70 pts   +4,395   +4,640       +4,855      +9,730  -1,865
#     breakeven at +100 pts  +4,545   +4,508       +4,013      +8,804  -1,895
#
# Closer here than on NIFTY, where every variant simply lost: +30 buys 36% less
# drawdown for 12% less profit, better risk-adjusted at 5.78 against 4.19
# return over max drawdown. Rejected because it more than halves the
# out-of-sample window, +4,032 -> +1,800, which is the window worth trusting.
# Note 30 pts is proportionally far tighter here than on NIFTY.
# Reproduce: backtests/banknifty_percent_trail_replay.py, replay(lock_pts=30).

# Raised from 2 on 2026-09-16. The third trade of the day is systematically better
# than the average one: with the window left at 11:00, going 2 -> 3 adds 77 trades
# over 2018-2026 for +1,621 points (+13%) and 38 trades over 2018-01..2021-09 for
# +1,054 (+31%), and it is the rare change that improves the OUT-OF-SAMPLE window -
# PF 1.10 -> 1.12 and max drawdown -3,662 -> -3,301 there. Better in all four
# windows tested. A 4th slot is noise: it adds 7 trades in 2,133 sessions and is
# slightly worse in three of the four. Note this does nothing for days that take no
# trade at all - it only frees a slot on days that already traded twice.
MAX_TRADES_PER_DAY = 3
DIRECTIONS = ("long", "short")      # sides to trade

# Resting stop parked at the broker on entry, so a dead machine or a dropped
# link cannot leave the option unprotected. The in-process trail above is still
# what normally exits; this only catches the session nobody is watching.
#
# The index stop is converted to a premium with delta AND gamma. Checked against
# a full Black-76 revaluation, delta alone is off by 3.20 points at a 60-point
# adverse move and 12.54 at 120; adding the gamma term cuts that to 0.03 and
# 0.40. Delta alone is not good enough.
#
# The level is then pushed DOWN, because premium falls for reasons the index
# has not moved: theta to square-off is worth about 4.3 points on a 2-day ATM
# option, and 2 points of IV is worth about 13.7 - against a total stop
# distance of roughly 26. An unwidened mirror of the index stop would be fired
# by ordinary IV noise on trades that were never in trouble. Widening is what
# makes this a disaster stop rather than a second, worse trail.
BROKER_STOP = True                  # False reverts to the in-process stop alone
BROKER_STOP_IV_POINTS = 2.0         # IV points of vega headroom
BROKER_STOP_LIMIT_SLIP = 0.05       # limit sits this far below the trigger
TICK = 0.05                         # NFO option tick

# Regime filter: off in this copy. On BANKNIFTY (six-bank volume) no VIX band beat
# taking every signal in total, over 2018-2026 or over the last two years. Set
# VIX_MIN to take signals only above a level, VIX_MAX only below one.
VIX_MIN = None
VIX_MAX = None

# Stretch rule. While India VIX is above STRETCH_VIX_LOW and up to STRETCH_VIX_HIGH
# (None removes that bound), skip a buy whose entry is more than STRETCH_MAX_PCT of
# the entry price above the day's low so far (a sell: below the day's high so far).
# An unreadable VIX applies the check. Backtest, six-bank volume, entries
# 11:00-14:30, 4-point cost, 2018-01..2026-09, net index points per lot:
#   no rule                          +8,620  max drawdown -4,930
#   0.4% at VIX above 12, up to 14  +11,194  max drawdown -3,984
#   0.4% at VIX 14 and below        +11,860  max drawdown -3,662  (this setting)
# The 14-and-below band was picked after seeing those results, and its extra gain
# comes almost entirely from 2021-2026, when VIX was often under 12. 0.3% on the
# same band did worse than no rule, so do not tighten the limit without re-testing.
# Set STRETCH_MAX_PCT to None to disable the rule.
STRETCH_VIX_LOW = None
STRETCH_VIX_HIGH = 14.0
STRETCH_MAX_PCT = 0.004             # 0.4% of the entry price

MIN_CLEARANCE = 0.0                 # points a candle must clear both lines by
STOP_BUFFER = 25.0                  # NIFTY's 10.0 scaled by the 2.42 level ratio
LINE_PROXIMITY = 60.0               # NIFTY's 25.0 scaled by the 2.42 level ratio

# TESTED AND REJECTED, 2026-09-20: rescaling these by volatility instead of by
# index level. The 2.42 above is a PRICE ratio, but BANKNIFTY also ranges 1.49%
# a day against NIFTY's 1.07% (2,410 sessions, 2016-2026), so in POINTS its
# average day range is about 3.1x NIFTY's, not 2.42x. That argues for a buffer
# near 31. Swept with proximity held at this file's 2.4 ratio to the buffer:
#
#     buffer  prox  last 2y  last 5y  2018-01..2021-09   full    maxDD  ret/DD
#         15    36   +5,478   +5,733       +3,271       +9,352  -2,159   4.33
#         25    60   +5,594   +5,591       +4,032       +9,907  -2,366   4.19
#         31    74   +5,591   +5,698       +3,626       +9,654  -2,428   3.98
#         40    96   +4,805   +5,385       +4,864      +10,561  -2,077   5.09
#         60   144   +4,712   +5,713       +4,930      +10,911  -2,292   4.76
#
# 31 is worse than 25 on profit, on drawdown and on return over drawdown, so
# the volatility argument simply does not hold. Wider still (40-60) does beat
# 25 on the full window, but every point of that gain comes from 2018-2021
# while the last two years fall +5,594 -> +4,712 - a regime artifact, not an
# edge. The surface is noisy enough to swamp the effect in any case: 20 gives
# +8,777 against 25's +9,907, a 1,130-point swing for five points of buffer.
# Win rate does climb cleanly with the buffer, 36.2% -> 42.5%. It just does not
# pay, which is the STOP_BUFFER lesson from the other direction.
# Reproduce: backtests/banknifty_percent_trail_replay.py,
# replay(stop_buffer=..., line_proximity=...).

# Entry window 11:00-14:30. On BANKNIFTY with six-bank volume, no VIX filter and a
# 4-point cost per trade, 2021-09..2026-09 netted +7,258 index points per lot
# against +3,550 for 10:30-14:30. But 11:00 was picked from that same period:
# over 2018-01..2021-09 it made +1,362 while an unrestricted 09:30 start made
# +2,781. Treat it as a hypothesis under paper test, not a proven edge.
SESSION_OPEN = dtime(9, 15)
NO_NEW_ENTRY_BEFORE = dtime(11, 0)   # BANKNIFTY paper test; see the note above
NO_NEW_ENTRY_AFTER = dtime(14, 30)  # too close to square-off to be worth it
SQUARE_OFF = dtime(15, 0)
ABANDON_AFTER = dtime(15, 20)       # the exchange refuses MIS orders past 15:15

BAR_SECONDS = 300
BAR_DELAY = 10                      # wait this long after a bar closes
USE_WEBSOCKET_VOLUME = True         # False falls back to 7 history calls a bar
FEED_WARMUP = 6                     # seconds to collect the opening snapshot
FEED_SNAPSHOT_LAG = 1.0             # snapshot this long after a bar boundary
HISTORY_PACE = 0.55                 # seconds between history calls

IST = ZoneInfo("Asia/Kolkata")
STATE_DIR = Path("strategies") / "state"
STATE_FILE = STATE_DIR / "banknifty_2candle_vwap_sma45.json"

# The six largest NIFTY Bank constituents. The index has 14 since YESBANK and
# UNIONBANK joined on 2025-12-31, but only these six feed the synthetic volume.
# Verified against the Angel symbol master on 2026-09-11.
CONSTITUENTS = [
    "AXISBANK", "HDFCBANK", "ICICIBANK", "INDUSINDBK", "KOTAKBANK", "SBIN",
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
        # Candle 2 must close clear of candle 1's RANGE, not merely its close:
        # above the high to buy a CE, below the low to buy a PE. Until
        # 2026-09-20 this compared closes, which was not the intended rule.
        # Measured on BANKNIFTY by backtests/banknifty_percent_trail_replay.py
        # (which reproduces the trail table above to within ~4%), net of 4 pts
        # a trade:
        #
        #                       last 2y  last 5y  2018-01..2021-09   full    maxDD
        #     close beyond close  +9,371  +11,634       +5,073     +16,966  -4,217
        #     close beyond range  +5,594   +5,591       +4,032      +9,907  -2,366
        #
        # NOTE this is the OPPOSITE of NIFTY, where the same correction nearly
        # trebled the full window. Here it costs 42% of the total points - but
        # not because the trades are worse: the average trade is unchanged at
        # +10.36 -> +10.35, on 1,637 trades cut to 957. Same edge, 41% fewer
        # entries. Win rate rises 35.7% -> 37.9% and drawdown nearly halves, so
        # return over max drawdown improves 4.02 -> 4.19. Fewer, better-behaved
        # trades for meaningfully less absolute profit.
        if direction == "long" and second["close"] <= first["high"]:
            continue
        if direction == "short" and second["close"] >= first["low"]:
            continue
        return {
            "direction": direction,
            "c1_low": float(first["low"]),
            "c1_high": float(first["high"]),
            "line": float(first["upper"] if direction == "long" else first["lower"]),
            "signal_bar": frame.index[-1].isoformat(),
        }
    return None


def trail_level(entry: float, mfe: float, long: bool) -> float | None:
    """Trailing-stop level for a trade whose best excursion is mfe points.

    The stop sits TRAIL_DIST_PCT of the entry price behind the best price, and
    that best price is counted only in whole TRAIL_STEP_PCT steps, so the stop
    moves in jumps rather than tick by tick.

    Returns:
        The trailing level, or None before the first full step.
    """
    steps = int(mfe // (TRAIL_STEP_PCT * entry))
    if steps < 1:
        return None
    offset = steps * TRAIL_STEP_PCT * entry - TRAIL_DIST_PCT * entry
    return entry + offset if long else entry - offset


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


def entry_stretch(sig: dict, frame: pd.DataFrame) -> float:
    """How far the entry sits off the day's extreme so far, as a share of its price.

    Returns:
        (entry - day low) / entry for a long, (day high - entry) / entry for a
        short, over today's completed candles up to and including candle 2.
    """
    spot = float(frame["close"].iloc[-1])
    if sig["direction"] == "long":
        return (spot - float(frame["low"].min())) / spot
    return (float(frame["high"].max()) - spot) / spot


def stretch_allowed(stretch: float, vix: float | None) -> bool:
    """Apply the stretch rule for the current India VIX.

    Returns:
        True when the rule is off, VIX is outside the rule's band, or the entry
        is within STRETCH_MAX_PCT. An unreadable VIX (None) gets the check.
    """
    if STRETCH_MAX_PCT is None:
        return True
    if vix is not None:
        above_low = STRETCH_VIX_LOW is None or vix > STRETCH_VIX_LOW
        below_high = STRETCH_VIX_HIGH is None or vix <= STRETCH_VIX_HIGH
        if not (above_low and below_high):
            return True
    return stretch <= STRETCH_MAX_PCT


def stretch_band() -> str:
    """Describe the VIX band the stretch rule applies in, for log lines.

    Returns:
        Text such as "VIX 14.0 or below".
    """
    parts = []
    if STRETCH_VIX_LOW is not None:
        parts.append(f"above {STRETCH_VIX_LOW}")
    if STRETCH_VIX_HIGH is not None:
        parts.append(f"{STRETCH_VIX_HIGH} or below")
    return "VIX " + " and ".join(parts) if parts else "every VIX level"


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


def nearest_expiry(client) -> str | None:
    """Return the nearest option expiry AFTER today, in compact form.

    BANKNIFTY has listed monthly expiries only since November 2024, so this is
    normally the current month contract, and the next month on expiry day.

    Today's own expiry is skipped deliberately. Every stop, target and trail
    here is measured in INDEX points, but an expiry-day ATM option held to the
    15:00 square-off decays to almost nothing whatever the index does, so a
    0-DTE contract cannot express the move the backtest measured.

    The list is parsed and sorted rather than trusted in order, so an unsorted
    or stale response cannot hand back a contract that has already expired.

    Returns:
        Expiry such as ``29SEP26``, or None when the lookup fails or nothing
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
        # The lot is gone but the resting stop is not: left alone it becomes a
        # live SELL with nothing behind it, and if the premium later touches
        # the trigger it opens a NAKED SHORT. Pull it before dropping the
        # position, not after.
        drop_resting_stop(client, position, "the position was closed elsewhere")
        state["position"] = None
        return True
    log(f"broker still reports {qty} open {position['symbol']}; keeping the "
        f"position and retrying the exit next bar")
    return False


# =============================================================================
# BROKER-SIDE STOP
# =============================================================================

def round_tick(value: float) -> float:
    """Round down to the exchange tick, so the level never drifts tighter.

    Rounded to paise as well: ``int(41.70 / 0.05) * 0.05`` lands on
    41.650000000000006, and a price carrying that much float residue is a
    field the broker can reject outright.
    """
    return round(max(TICK, int(round(value / TICK, 6)) * TICK), 2)


def option_stop_price(client, symbol: str, index_entry: float,
                      index_stop: float) -> float | None:
    """Premium level matching the index stop, widened into a disaster stop.

    See the BROKER_STOP notes in CONFIGURATION for why delta alone is not
    enough and why the level is deliberately pushed below the mirror. The
    premium comes from the same Greeks call as the sensitivities, so the two
    always describe one snapshot of one option.

    Returns:
        Trigger price rounded to the tick, or None when it cannot be trusted -
        in which case no resting stop is placed and the caller says so loudly.
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
    mirror = entry_premium + delta * move + 0.5 * gamma * move**2

    now = datetime.now(IST)
    close = now.replace(hour=SQUARE_OFF.hour, minute=SQUARE_OFF.minute,
                        second=0, microsecond=0)
    hours = max((close - now).total_seconds() / 3600.0, 0.0)
    theta_buffer = abs(theta) * hours / 24.0
    vega_buffer = abs(vega) * BROKER_STOP_IV_POINTS

    level = mirror - theta_buffer - vega_buffer
    if level <= 0 or level >= entry_premium:
        log(f"stop level {level:.2f} is not below the {entry_premium:.2f} entry "
            f"premium; no broker stop for {symbol}")
        return None

    trigger = round_tick(level)
    log(f"broker stop for {symbol}: index {index_entry:.2f} -> {index_stop:.2f} "
        f"({move:+.2f}) maps to {mirror:.2f} on delta {delta:+.4f} gamma "
        f"{gamma:.6f}; less {theta_buffer:.2f} theta over {hours:.1f}h and "
        f"{vega_buffer:.2f} for {BROKER_STOP_IV_POINTS:.0f} IV points "
        f"-> trigger {trigger:.2f}")
    return trigger


def place_broker_stop(client, symbol: str, quantity: int,
                      trigger: float) -> str | None:
    """Park a stop-limit SELL at the broker.

    A limit rather than SL-M: NSE restricts market-type stops in F&O, and a
    rejected SL-M would leave the position silently unprotected. The limit sits
    BROKER_STOP_LIMIT_SLIP below the trigger so it still fills when the move is
    quick - a stop that cannot fill protects nothing.

    Returns:
        The broker order id, or None when the order was not accepted.
    """
    limit = round_tick(trigger * (1.0 - BROKER_STOP_LIMIT_SLIP))
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


def drop_resting_stop(client, position: dict, why: str) -> None:
    """Cancel a resting stop whose position is already gone.

    Unlike ``release_broker_stop`` this cannot decline to act: the caller has
    already established there is no lot left to protect, so the only question
    is whether the cancel lands. A stop that outlives its position is a live
    SELL with nothing behind it, which is why a failure here is shouted rather
    than swallowed - it needs a human at the broker terminal.
    """
    order_id = position.pop("stop_order_id", None)
    if not order_id:
        return
    try:
        if ok(client.cancelorder(order_id=order_id, strategy=STRATEGY_TAG)):
            log(f"cancelled resting stop {order_id}: {why}")
            return
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"cancel of orphaned stop {order_id} raised {exc!r}")
    status = stop_order_state(client, order_id)
    if status in ("filled", "gone"):
        log(f"orphaned stop {order_id} is already {status}; nothing rests")
        return
    log(f"COULD NOT CANCEL resting stop {order_id} ({why}). It may still be "
        f"live with no position behind it, and filling it would open a naked "
        f"short. CANCEL IT BY HAND AT THE BROKER")


def release_broker_stop(client, state: dict) -> str:
    """Clear the resting stop before this process sends its own exit.

    This is the guard against the one failure that matters: the resting stop
    filling at the same time as a market exit, which does not flatten the
    position but sells it twice and leaves a NAKED SHORT option. The exit is
    only allowed once the stop is known to be gone.

    Returns:
        'clear'   - nothing is resting; the caller may send its exit.
        'filled'  - the stop already did the job; the position has been cleared.
        'blocked' - the stop may still fill; the caller must NOT send an exit.
    """
    position = state.get("position") or {}
    order_id = position.get("stop_order_id")
    if not order_id:
        return "clear"

    try:
        r = client.cancelorder(order_id=order_id, strategy=STRATEGY_TAG)
        cancelled = ok(r)
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"cancel of stop {order_id} raised {exc!r}")
        cancelled = False

    if cancelled:
        log(f"cancelled resting stop {order_id}")
        position.pop("stop_order_id", None)
        return "clear"

    # A cancel fails both when the order is already gone and when the broker
    # simply could not be reached; those need opposite responses, so ask.
    status = stop_order_state(client, order_id)
    if status == "filled":
        log(f"resting stop {order_id} had already filled; the position is "
            f"closed at the broker, clearing it locally")
        state["position"] = None
        return "filled"
    if status == "gone":
        position.pop("stop_order_id", None)
        return "clear"
    log(f"cannot confirm resting stop {order_id} is gone (status {status}); "
        f"holding off the exit this bar rather than risking a double sell")
    return "blocked"


def stop_fired(client, state: dict) -> bool:
    """True when the resting stop closed the position while we were away."""
    position = state.get("position") or {}
    order_id = position.get("stop_order_id")
    if not order_id:
        return False
    if stop_order_state(client, order_id) != "filled":
        return False
    log(f"resting stop {order_id} filled on {position.get('symbol')}; the "
        f"broker closed this position, clearing local state")
    state["position"] = None
    return True


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

    # The resting stop may have closed this position between bars - or while
    # this process was not running at all, which is the whole point of it.
    if BROKER_STOP and stop_fired(client, state):
        return state

    bar = frame.iloc[-1]
    long = position["direction"] == "long"
    stop = float(position["stop"])
    entry = float(position["entry"])
    lot = int(position["lotsize"])

    excursion = (float(bar["high"]) - entry) if long else (entry - float(bar["low"]))
    position["mfe"] = max(float(position.get("mfe", 0.0)), excursion)

    breached = (float(bar["low"]) <= stop) if long else (float(bar["high"]) >= stop)
    if breached:
        outcome = release_broker_stop(client, state) if BROKER_STOP else "clear"
        if outcome == "filled":
            return state
        if outcome == "blocked":
            # Selling now could double up on a stop that is still live, so the
            # index stop simply waits a bar. It is already breached; another
            # five minutes of exposure beats an accidental naked short.
            return state
        qty = lot * position["lots_open"]
        if send(client, position["symbol"], "SELL", qty):
            log(f"STOP hit at {stop:.2f} on the index; closed {qty}")
            state["position"] = None
        else:
            abandon_if_flat(client, state)
        return state

    # Ratchet the stop behind the best price. It only ever moves in the trade's
    # favour, and is checked against the NEXT bar, as the backtest does.
    level = trail_level(entry, position["mfe"], long)
    if level is not None:
        moved = max(stop, level) if long else min(stop, level)
        if moved != stop:
            position["stop"] = moved
            log(f"trail: best +{position['mfe']:.2f} pts, stop {stop:.2f} -> {moved:.2f}")

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
    if BROKER_STOP:
        outcome = release_broker_stop(client, state)
        if outcome == "filled":
            return state
        if outcome == "blocked":
            log("square-off deferred: the resting stop could not be confirmed "
                "gone. CHECK THE BROKER POSITION BY HAND")
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

    if VIX_MIN is not None or VIX_MAX is not None:
        vix = read_vix(client)
        if vix is None:
            # The whole point of the gate is to know the regime before committing,
            # so an unreadable VIX skips the signal rather than trading blind.
            log("VIX unreadable; skipping this signal rather than guessing the regime")
            return state
        if VIX_MIN is not None and vix <= VIX_MIN:
            log(f"{sig['direction']} signal skipped: India VIX {vix:.2f} is at or "
                f"below VIX_MIN {VIX_MIN}")
            return state
        if VIX_MAX is not None and vix >= VIX_MAX:
            log(f"{sig['direction']} signal skipped: India VIX {vix:.2f} is at or "
                f"above VIX_MAX {VIX_MAX}")
            return state
        log(f"India VIX {vix:.2f} is inside the gate (min {VIX_MIN}, max {VIX_MAX})")

    if STRETCH_MAX_PCT is not None:
        stretch = entry_stretch(sig, frame)
        if stretch > STRETCH_MAX_PCT:
            # Only a stretched entry depends on the VIX band, so the quote is read here.
            stretch_vix = read_vix(client)
            if not stretch_allowed(stretch, stretch_vix):
                regime = "unreadable" if stretch_vix is None else f"{stretch_vix:.2f}"
                side = "low" if sig["direction"] == "long" else "high"
                log(f"{sig['direction']} signal skipped: entry is {stretch:.2%} off the day's "
                    f"{side} with India VIX {regime}, over the {STRETCH_MAX_PCT:.1%} limit")
                return state
            log(f"entry is {stretch:.2%} off the day's extreme, but India VIX {stretch_vix:.2f} "
                f"is outside the rule's band ({stretch_band()}); allowed")

    stop = initial_stop(sig)
    spot = float(frame["close"].iloc[-1])
    risk = (spot - stop) if sig["direction"] == "long" else (stop - spot)
    if risk <= 0:
        log(f"{sig['direction']} signal ignored: non-positive risk")
        return state

    expiry = nearest_expiry(client)
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
        "mfe": 0.0, "opened": now.isoformat(),
    }

    # Park the disaster stop now, while the position is fresh and the Greeks
    # describe the option we actually hold. A failure here is logged loudly but
    # does not unwind the trade: the in-process stop still runs, and exiting a
    # good entry because a Greeks call timed out would be the worse trade.
    if BROKER_STOP:
        trigger = option_stop_price(client, leg["symbol"], spot, stop)
        order_id = (place_broker_stop(client, leg["symbol"], quantity, trigger)
                    if trigger is not None else None)
        if order_id:
            state["position"]["stop_order_id"] = order_id
            state["position"]["stop_trigger"] = trigger
        else:
            log("NO BROKER STOP on this position. It is protected only while "
                "this process is alive.")
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
    log(f"{STRATEGY_TAG} starting: {LOTS} lots, stop trails {TRAIL_DIST_PCT:.1%} behind "
        f"the best price in {TRAIL_STEP_PCT:.1%} steps, square-off {SQUARE_OFF:%H:%M}")

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
                    # This process is about to exit while a lot is still open.
                    # The broker squares MIS off on its own shortly, and once
                    # it does, a stop still resting would be a naked short
                    # waiting on the 15:20-15:30 tail. Take it down now: the
                    # auto square-off is better protection than an order
                    # nobody is left to reconcile.
                    drop_resting_stop(client, state["position"],
                                      "abandoning the position past "
                                      f"{ABANDON_AFTER:%H:%M}")
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
