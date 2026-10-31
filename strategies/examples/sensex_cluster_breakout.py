"""SENSEX range-cluster breakout, 5-minute candles.

DRY RUN from 2026-11-02 (TRADE_LIVE = False: paper only, no order is sent).
Live 2026-10-03..10-30: with TRADE_LIVE the script sends real orders - a market
BUY of LIVE_LOTS of the weekly BFO option ITM_POINTS (100) in the money - CE
below ATM, PE above; ATM until 2026-09-29. Once the option BUY fills, the
script reads that contract's broker Greeks, maps the initial index stop to an
option premium trigger, and immediately parks a broker-side SL-M backstop.
Market SELL exits handle index polls or bars touching the stop, the trail,
and the 15:00 square-off. Every exit cancels the backstop first, so a backstop
firing mid-exit is detected instead of sold twice. If Greeks are unavailable
or the broker does not confirm the stop, the filled option is immediately
flattened; an unsettled flatten stays in emergency-exit retries. TRADE_LIVE =
False returns every path to the old paper record: premiums quoted, no order
ever sent. Orders follow the platform's Analyze (sandbox) versus Live mode,
so the same script can be dry run on the sandbox before real money is at risk.
Exit failures are retried, never assumed flat.

From 2026-11-02 each signal trades a lock lot and a runner lot ("two locks",
see LOCK_AT_ATR): the lock lot is floored at entry + 1 ATR once the move
reaches 2 ATR, the runner at entry + 1 ATR once it reaches 4 ATR, ATR being
the ATR(14) at entry; each lot exits on its own stop. On a gap day (open at
least GAP_SIZE_PCT from the previous close) both lots are doubled.

There is no target: winners ride to the square-off on the trail, losers are
cut at the tighter box-edge or 2.5xATR initial index stop. Every exit logs
index slippage against the stop and, for live positions, the option fill
against the premium quoted at entry.

The broker trigger projects from the actual option fill using delta and gamma
for the index move to the stop, subtracts theta decay through square-off and a
2-vol-point vega buffer, then rounds down to the BFO 0.05 tick. The ATR cap
only tightens the box-edge stop: long max(box low, entry - 2.5 x ATR(14));
short min(box high, entry + 2.5 x ATR(14)).

Rule (long; short is the mirror):
    1. Cluster: at least MIN_CANDLES consecutive completed candles that all fit
       inside one box whose height (highest high - lowest low) is at most
       BOX_PCT of price. The cluster keeps growing while new candles still
       fit; when one does not, the oldest candles drop off until the rest fit.
    2. Signal: the first candle that CLOSES above the box (short: below it).
    3. Entry: at the next bar's open - here, the index price when the signal
       candle has just completed.
    4. Initial index stop: the tighter of the opposite box edge and
       MAX_INITIAL_RISK_ATR (2.5) x ATR(14) from entry. The live option stop
       is mapped from that index level using the bought option's Greeks.
    5. Trail: the stop sits TRAIL_DIST_PCT of the entry behind the best price,
       moved in TRAIL_STEP_PCT steps, never loosening the initial stop.
       Breakeven: OFF (BREAKEVEN_AT_PTS = None). When set, once the best
       move reaches that many index points the stop is never worse than the
       entry. Every level from 40 to 160 lost points over 10y.
    6. Square-off at SQUARE_OFF. Entries NO_NEW_ENTRY_BEFORE..NO_NEW_ENTRY_AFTER,
       at most MAX_TRADES_PER_DAY, one position at a time. After an exit a new
       cluster may only start on the bar after the exit bar.

Active-day filter (added 2026-09-27), read when a signal fires:
    India VIX above VIX_MIN (13), AND NIFTY 50
    breadth one-sided by BREADTH_MIN
    (2x): advancers at least twice decliners, or decliners at least twice
    advancers, in EITHER direction - a short breakout on a strongly up day
    qualifies too. It is a "decisive day" test, not a trend-direction test.
    An unreadable VIX or breadth skips the signal.

    Breadth was switched off for part of 2026-09-29 and back on the same
    evening: over 2016-10..2026-09-25 VIX > 13 alone netted about the same
    (41,258 vs 42,044 pts) from 853 more trades, with maxDD -3,863 vs -2,029
    and OOS PF 1.28 vs 1.45.

One book runs every session: boxatr applies the active-day filter and
requires the box to be at most BOX_ATR_MAX (2) x ATR(14) at the signal bar.
The filtered-only comparison and unfiltered plain book have been removed.

boxatr skips breakouts from boxes that are wide for the current volatility.
Over 2016-10..2026-09-25 against filtered: 1,324 vs 1,768 trades, net 36,297
vs 42,044, PF 1.39 vs 1.35, OOS PF 1.50 vs 1.45 (1.37 vs 1.32 at a 20 pt
cost), maxDD -2,330 vs -2,029, 11/11 years. Every cap from 1.75 to 3.0 gave
PF 1.36-1.39, so 2.0 is not a spike. It is here to see whether fewer, better
trades win once real stop slippage is paid. ATR is Wilder's (EWM alpha 1/14)
over the true range, carried across days, warmed on ATR_WARMUP_DAYS prior
sessions fetched once a day, as the backtest computes it.

2026-10-04: BOX_ATR_MAX 2.0 -> 3.0 and TRAIL_STEP_PCT 0.1% -> 0.2%, replayed
on the 650-session gated window (2024-01-08..2026-09-15, 10 pts a trade):

                  trades  win%   net pts    avg     PF   PF@20
    old (2.0/0.1%)   292  44.2    20,254   69.4   1.72   1.59
    new (3.0/0.2%)   323  48.3    30,760   95.2   1.96   1.81

    By year (new): 2024 51.8% PF 2.29, 2025 40.8% PF 1.52, 2026 51.8% PF
    1.99 - the newest year, which the change was not fitted on, is the best
    of the three. The cap is a plateau: 2.25/2.5/2.75/3.0/3.25/3.5 gave PF
    1.77/1.85/1.87/1.93/1.84/1.85, and 0.2% is the trail step the NIFTY book
    has run all along. Profit targets were tried and dropped - R and fixed
    targets lifted win rate to 47-51% but cut PF to 1.29-1.55 by capping the
    rides to square-off that carry this book. Reproduce:
    backtesting/sensex_cluster/research/ (harness.py + exp_refine.py).

A vix14 book (same filter, VIX floor 14) ran 2026-09-28..29 and was dropped at
the user's call: the VIX floor is 13 only.

Backtest, SENSEX 5m 2016-10..2026-09-25, stops filled AT the stop price, index
points per trade. The filter was chosen on 2016-10..2022-12 only and checked
once on 2023-01..2026-09 (out of sample):

                      2016-22 PF   2023-26 trades  PF    PF@20 cost  years +
    plain                1.12           1,317      1.13     1.02      9/11
    filtered             1.29             502      1.45     1.32     11/11

    filtered, full 10y: 1,768 trades, +33.8 gross a trade (breakeven cost
    ~34 points, plain ~19), maxDD -2,029 pts vs -5,450.

The filtered book sits out low-VIX spells: Aug-Sep 2026 (VIX 10-12.6) gave
it 2 trades in 8 weeks against 54 for plain. Breadth in the backtest uses
today's NIFTY 50 list for every year, which flatters the early years.

THE REASON THIS STRATEGY WAS PAPER FIRST is the stop fill: the backtest
fills every stop exactly at the stop level. Every stop exit - paper or live -
logs the index price actually seen when the stop was crossed and the gap
("slippage"), and live also logs the option fill against the premium quoted
at the signal. Reproduce: Claude session 687c73c7 scratchpad cluster_edge.py /
edge_gates.py / edge_oos.py.

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
TRAIL_STEP_PCT = 0.002              # the stop moves once per step of this size
MAX_TRADES_PER_DAY = 2
BREAKEVEN_AT_PTS = None             # was 120 on 2026-09-29 only; costs points at every level tested
# "Two locks" plus gap sizing, user's request 2026-10-31. Every lot rides the
# trail; on top, each lot's stop gets a floor scaled by the ATR(14) read at
# entry (the volatility the trade was taken in, not fixed points as on NIFTY):
#   lock lot:   once the best move reaches LOCK_AT_ATR (2) x ATR, its stop is
#               never worse than entry + LOCK_ATR (1) x ATR
#   runner lot: once the best move reaches RUNNER_LOCK_AT_ATR (4) x ATR, its
#               stop is never worse than entry + RUNNER_LOCK_ATR (1) x ATR
# Each lot exits on its own stop; the SL-M backstop always covers exactly the
# lots still open. Entries stop at 13:30 (NO_NEW_ENTRY_AFTER): late
# breakouts were mostly losers. On a gap day, today's open at least
# GAP_SIZE_PCT (0.4%) from the previous session's close either way, every
# lot count is multiplied by GAP_LOT_MULTIPLIER (2): 4 lots instead of 2.
#
# Replayed 2016-10..2026-10-30 on these rules (stops filled at the level,
# 10 pts a lot, index points; win = the whole trade > 0):
#
#                              trades  win%   PF    net pts  pts/lot  maxDD   worst day
#   old: 2 lots, no lock        1,711  41.8  1.34   +81,554    23.8   -4,766     -948
#   two locks, 2 lots           1,548  46.6  1.35   +69,136    22.3   -4,175     -948
#   two locks + gap sizing      1,548  46.6  1.39  +108,304    24.7   -5,973   -1,881
#
# Win rate about 5 points higher in every year 2017-2026; 2017 and 2018 lose
# (the old rules lost only 2018). October 2026: +2,311 pts vs +1,524 old.
# Gap sizing is a modest edge (+5-12% per lot in every test window) and
# doubles the worst day. Locks at 1.5-2.5 ATR all behave alike (a plateau).
# Revert: LOCK_AT_ATR = RUNNER_LOCK_AT_ATR = None, GAP_LOT_MULTIPLIER = 1,
# NO_NEW_ENTRY_AFTER 14:30. Reproduce: session e5b5848a scratchpad sxw.py /
# sxw2.py / sizing.py / tenyr.py.
LOCK_AT_ATR = 2.0                   # best move, in entry ATRs, that arms the lock lot's floor
LOCK_ATR = 1.0                      # the lock lot's floor, entry ATRs in profit
RUNNER_LOTS = 1                     # of LIVE_LOTS, these use the runner's lock
RUNNER_LOCK_AT_ATR = 4.0            # best move, in entry ATRs, that arms the runner's floor
RUNNER_LOCK_ATR = 1.0               # the runner's floor, entry ATRs in profit
GAP_SIZE_PCT = 0.004                # |open / previous close - 1| at or above this is a gap day
GAP_LOT_MULTIPLIER = 2              # lots multiplied by this on a gap day; 1 switches sizing off
COST_PTS = 10.0                     # assumed round-trip cost in the backtest

# Live orders. TRADE_LIVE False makes every path paper-only again. Orders
# follow the platform's Analyze (sandbox) versus Live mode, so this same code
# can be dry run on the sandbox before real money is at risk.
TRADE_LIVE = False                  # dry run from 2026-11-02 (user, 2026-10-31); True sends real orders
LIVE_LOTS = 2                       # lock + runner; 1 until 2026-10-30; x GAP_LOT_MULTIPLIER on a gap day
LIVE_PRODUCT = "MIS"                # intraday; broker end-of-day square-off is the backstop
ORDER_POLL_TRIES = 10               # 1s reads a market order is given to fill
ORDER_POLL_SECONDS = 1
CANCEL_CONFIRM_TRIES = 6            # reads that a cancelled backstop is really gone
CANCEL_CONFIRM_SECONDS = 0.5
MANUAL_EXIT_RECHECK_SECONDS = 2.0   # pause before trusting a flat position book
SQUARE_OFF_RETRY_SECONDS = 20       # exit retry cadence once past SQUARE_OFF
MAX_INITIAL_RISK_ATR = 2.5          # cap initial index risk, never widen past the box edge
SLM_IV_BUFFER_POINTS = 2.0          # Greek-mapped backstop headroom for IV movement
SLM_TICK = 0.05                     # BFO option price tick

# Active-day filter for the gated books. VIX_MIN sits on the 13-14 plateau
# of the in-sample sweep, BREADTH_MIN on the 2.0-2.5 plateau. None switches
# the breadth part off so VIX alone gates (tried 2026-09-29, backtests worse).
VIX_MIN = 13.0
BREADTH_MIN = 2.0
# VIX floor per gated book; every book not listed here is ungated.
BOOK_VIX_MIN = {"boxatr": VIX_MIN}
# Box height cap in ATR(14) multiples, per book; books not listed have none.
BOOK_BOX_ATR_MAX = {"boxatr": 3.0}
ATR_PERIOD = 14
ATR_WARMUP_DAYS = 10                # calendar days of prior bars for the ATR
BREADTH_MIN_STOCKS = 40             # skip the signal if fewer quotes are readable
BOOKS = ("boxatr",)
ITM_POINTS = 100                    # strike this far in the money; was ATM until 2026-09-29

SESSION_OPEN = dtime(9, 15)
NO_NEW_ENTRY_BEFORE = dtime(9, 30)
NO_NEW_ENTRY_AFTER = dtime(13, 30)  # 14:30 until 2026-10-31
SQUARE_OFF = dtime(15, 0)

BAR_SECONDS = 300
BAR_DELAY = 10                      # wait this long after a bar closes
STOP_POLL_SECONDS = 5               # index polling while a position is open
HISTORY_PACE = 0.55                 # seconds between history retries

IST = ZoneInfo("Asia/Kolkata")
STATE_DIR = Path("strategies") / "state"
STATE_FILE = STATE_DIR / "sensex_cluster_breakout.json"

# NIFTY 50, for the breadth filter only - the same list as the NIFTY 2-candle
# strategy's CONSTITUENTS, verified against the Angel symbol master 2026-09-07.
BREADTH_CONSTITUENTS = [
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
_prior_bars: dict = {}              # {"date": today, "frame": prior sessions' bars}


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
    # such bar would swallow every cluster after it. The opening bar is exempt:
    # a gap or opening rush is genuinely wide, and once the midday bars go quiet
    # it would otherwise clear 10x the median and be thrown away.
    if len(frame) >= 5:
        span = frame["high"] - frame["low"]
        opening = frame.index.time == SESSION_OPEN
        absurd = (span > 10.0 * span.median()) & ~opening
        for stamp in frame.index[absurd]:
            log(f"dropping implausible bar {stamp:%H:%M}: range {float(span[stamp]):.2f}")
        frame = frame[~absurd]
    return frame


def fetch_prior(client) -> pd.DataFrame | None:
    """Completed 5m bars of the sessions before today, fetched once a day.

    Returns:
        Bars from ATR_WARMUP_DAYS calendar days back to yesterday, or None
        when the fetch fails (retried on the next call).
    """
    today = datetime.now(IST).date()
    if _prior_bars.get("date") == today:
        return _prior_bars["frame"]
    try:
        frame = client.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval=INTERVAL,
                               start_date=str(today - timedelta(days=ATR_WARMUP_DAYS)),
                               end_date=str(today - timedelta(days=1)))
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"prior bars raised {exc!r}")
        return None
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        log(f"prior bars unavailable: {frame if not isinstance(frame, pd.DataFrame) else 'empty'}")
        return None
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame = frame.tz_localize(IST) if frame.index.tz is None else frame.tz_convert(IST)
    frame = frame[(frame.index.date < today) & (frame.index.time >= SESSION_OPEN)
                  & (frame.index.time <= dtime(15, 25))]
    _prior_bars.update(date=today, frame=frame)
    log(f"cached {len(frame)} prior bars over {len(set(frame.index.date))} sessions for the ATR")
    return frame


def atr_now(prior: pd.DataFrame, frame: pd.DataFrame) -> float | None:
    """Wilder ATR(ATR_PERIOD) at the last completed bar, across days."""
    bars = pd.concat([prior, frame]).sort_index()
    bars = bars[~bars.index.duplicated(keep="last")]
    if len(bars) < 3 * ATR_PERIOD:
        return None
    prev = bars["close"].shift(1)
    tr = pd.concat([bars["high"] - bars["low"], (bars["high"] - prev).abs(),
                    (bars["low"] - prev).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1 / ATR_PERIOD, adjust=False).mean().iloc[-1])


def open_gap_pct(prior: pd.DataFrame | None, frame: pd.DataFrame) -> float | None:
    """Today's opening gap: |first bar open / previous session's last close - 1|.

    Returns:
        The gap as a fraction, or None when either price is unavailable.
    """
    if prior is None or prior.empty or frame.empty:
        return None
    prev_close = float(prior["close"].iloc[-1])
    today_open = float(frame["open"].iloc[0])
    if not (math.isfinite(prev_close) and math.isfinite(today_open)) or prev_close <= 0:
        return None
    return abs(today_open / prev_close - 1.0)


def lot_multiplier(gap: float | None) -> int:
    """GAP_LOT_MULTIPLIER on a gap day, else 1; an unreadable gap sizes normally."""
    if gap is not None and gap >= GAP_SIZE_PCT:
        return max(1, int(GAP_LOT_MULTIPLIER))
    return 1


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
    """Last traded premium of one option leg, for the entry and exit record."""
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


def resolve_leg(client, option_type: str) -> dict | None:
    """Option ITM_POINTS in the money, nearest weekly expiry after today.

    ATM (listed strike nearest spot) comes from optionsymbol; the strike then
    moves ITM_POINTS into the money, down for a CE and up for a PE. Points, not
    an ITMn offset, because ITMn counts listed strikes. The target contract
    must be listed, or the leg is left unresolved - paper logs the signal and
    enters without a premium, live stays flat.
    """
    expiry = nearest_weekly(client)
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
    log(f"strike {strike:g}: ATM {atm:g} (spot {r.get('underlying_ltp')}) "
        f"moved {ITM_POINTS} pts ITM")
    return {"symbol": symbol, "lotsize": int(r["lotsize"])}


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


def initial_index_stop(entry: float, box_low: float, box_high: float,
                       atr: float, long: bool) -> float:
    """Cap index risk at MAX_INITIAL_RISK_ATR without widening the box stop."""
    if not math.isfinite(atr) or atr <= 0:
        raise ValueError("ATR must be finite and positive to calculate the initial stop")
    max_risk = MAX_INITIAL_RISK_ATR * atr
    if long:
        return max(box_low, entry - max_risk)
    return min(box_high, entry + max_risk)


def trail_level(entry: float, mfe: float, long: bool) -> float | None:
    """Trailing stop for a best excursion of mfe points, in whole steps."""
    steps = int(mfe // (TRAIL_STEP_PCT * entry))
    if steps < 1:
        return None
    offset = steps * TRAIL_STEP_PCT * entry - TRAIL_DIST_PCT * entry
    return entry + offset if long else entry - offset


# =============================================================================
# ACTIVE-DAY FILTER
# =============================================================================

def read_vix(client) -> float | None:
    """India VIX right now, or None when it cannot be read."""
    try:
        q = client.quotes(symbol="INDIAVIX", exchange="NSE_INDEX")
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"VIX quote raised {exc!r}")
        return None
    if not ok(q):
        log(f"VIX quote failed: {q}")
        return None
    d = q.get("data") or {}
    return float(d.get("ltp") or d.get("prev_close") or 0) or None


def read_breadth(client) -> tuple[int, int] | None:
    """NIFTY 50 advancers and decliners against each stock's previous close.

    Returns:
        (advancers, decliners), or None when fewer than BREADTH_MIN_STOCKS
        constituents return a usable quote.
    """
    symbols = [{"symbol": s, "exchange": "NSE"} for s in BREADTH_CONSTITUENTS]
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


def active_day(vix: float | None, breadth: tuple[int, int] | None,
               vix_min: float = VIX_MIN) -> tuple[bool, str]:
    """A gated book's filter, as backtested.

    Returns:
        (passes, a reading to log).
    """
    if vix is None:
        return False, "VIX unreadable"
    if BREADTH_MIN is None:
        return vix > vix_min, f"VIX {vix:.2f} (needs > {vix_min:g}), breadth filter off"
    if breadth is None:
        return False, f"VIX {vix:.2f}, breadth unreadable"
    adv, dec = breadth
    ratio = max(adv, dec) / max(min(adv, dec), 1)
    reading = f"VIX {vix:.2f} (needs > {vix_min:g}), NIFTY 50 {adv} up / {dec} down = {ratio:.2f}x (needs {BREADTH_MIN:g}x)"
    return vix > vix_min and ratio >= BREADTH_MIN, reading


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
    else:
        # Retire removed comparison books on restart so they cannot keep running.
        state["books"] = {name: state["books"][name]
                          for name in BOOKS if name in state["books"]}
    # A book added mid-day (restart after an upgrade) joins with a clean slate.
    for name in BOOKS:
        state["books"].setdefault(
            name, {"trades": 0, "position": None, "cluster_from": None, "closed": []})
    return state


# =============================================================================
# LIVE ORDERS
# =============================================================================


def wait_order(client, orderid: str) -> tuple[str, float | None]:
    """Poll one order toward a terminal state; a timeout cancels it first.

    Returns (status, average fill): "complete" carries the traded price
    (None when the tradebook could not price it, so the caller falls back to
    a quote), "rejected"/"cancelled" carry None, and anything else means the
    order could not be settled - the caller must not assume the position
    exists (entry) or is gone (exit).
    """
    status = "open"
    for attempt in range(ORDER_POLL_TRIES):
        if attempt:
            time.sleep(ORDER_POLL_SECONDS)
        r = client.orderstatus(order_id=orderid, strategy=STRATEGY_TAG)
        if not ok(r):
            continue
        data = r.get("data") or {}
        status = str(data.get("order_status", "")).lower() or status
        if status == "complete":
            return status, float(data.get("average_price") or 0) or None
        if status in ("rejected", "cancelled"):
            return status, None
    client.cancelorder(order_id=orderid, strategy=STRATEGY_TAG)
    r = client.orderstatus(order_id=orderid, strategy=STRATEGY_TAG)
    if ok(r):
        data = r.get("data") or {}
        status = str(data.get("order_status", "")).lower() or status
        if status == "complete":
            return status, float(data.get("average_price") or 0) or None
    return status, None


def greek_mapped_slm_trigger(premium_in: float, delta: float, gamma: float,
                             theta: float, vega: float, index_move: float,
                             hours_to_square_off: float) -> float | None:
    """Project the option stop premium from broker Greeks and round to BFO ticks."""
    try:
        values = tuple(float(value) for value in (
            premium_in, delta, gamma, theta, vega, index_move, hours_to_square_off,
        ))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    premium_in, delta, gamma, theta, vega, index_move, hours_to_square_off = values
    if premium_in <= 0 or delta == 0 or hours_to_square_off < 0:
        return None
    projected = premium_in + delta * index_move + 0.5 * gamma * index_move**2
    theta_buffer = abs(theta) * hours_to_square_off / 24.0
    vega_buffer = abs(vega) * SLM_IV_BUFFER_POINTS
    level = projected - theta_buffer - vega_buffer
    if level <= 0 or level >= premium_in:
        return None
    trigger = round(math.floor(level / SLM_TICK) * SLM_TICK, 2)
    return trigger if trigger >= SLM_TICK else None


def slm_trigger(client, symbol: str, premium_in: float,
                index_entry: float, index_stop: float) -> float | None:
    """Fetch the filled option's Greeks and map the index stop to its premium."""
    try:
        response = client.optiongreeks(symbol=symbol, exchange=FO_EXCHANGE)
    except Exception as exc:  # noqa: BLE001 - entry handling will flatten on failure
        log(f"option Greeks raised {exc!r}; no broker SL-M for {symbol}")
        return None
    if not ok(response):
        log(f"option Greeks failed for {symbol}: {response}")
        return None
    body = response.get("data") if isinstance(response.get("data"), dict) else response
    greeks = body.get("greeks") or {}
    try:
        delta, gamma = float(greeks["delta"]), float(greeks["gamma"])
        theta, vega = float(greeks["theta"]), float(greeks["vega"])
    except (KeyError, TypeError, ValueError):
        log(f"option Greeks unreadable for {symbol}: {body!r}")
        return None
    now = datetime.now(IST)
    close = now.replace(hour=SQUARE_OFF.hour, minute=SQUARE_OFF.minute, second=0, microsecond=0)
    hours = max((close - now).total_seconds() / 3600.0, 0.0)
    move = index_stop - index_entry
    trigger = greek_mapped_slm_trigger(premium_in, delta, gamma, theta, vega, move, hours)
    if trigger is None:
        log(f"Greek-mapped stop invalid for {symbol}; no broker stop")
        return None
    log(f"Greek-mapped stop {symbol}: index {index_entry:.2f}->{index_stop:.2f}, "
        f"delta {delta:+.4f}, gamma {gamma:.6f}, theta {theta:.4f}, vega {vega:.4f}; "
        f"SL-M trigger {trigger:.2f}")
    return trigger


def flatten_unprotected_entry(client, symbol: str, quantity: int, buy_orderid: str) -> bool:
    """Immediately flatten a filled option if its broker-side stop is not confirmed."""
    log(f"no confirmed broker stop; flattening {symbol} x{quantity}")
    how, _premium = live_close(client, {"symbol": symbol, "qty": quantity,
                                       "buy_orderid": buy_orderid, "slm_orderid": None})
    return how in ("closed", "already-flat")


def live_entry(client, leg: dict, spot: float, stop: float, lots: int = LIVE_LOTS) -> dict | None:
    """Market-buy `lots` lots of the leg, then park the broker-side backstop.

    Returns the fills and order ids for the position dict, or None when
    nothing filled: the signal is dropped rather than retried, exactly as the
    paper book drops a signal it cannot price.
    """
    qty = int(leg["lotsize"]) * int(lots)
    try:
        r = client.placeorder(strategy=STRATEGY_TAG, symbol=leg["symbol"], action="BUY",
                              exchange=FO_EXCHANGE, price_type="MARKET",
                              product=LIVE_PRODUCT, quantity=qty)
    except Exception as exc:  # noqa: BLE001 - a transport blip must not kill the day
        log(f"LIVE entry raised {exc!r} for {leg['symbol']} x{qty}")
        return None
    if not ok(r):
        log(f"LIVE entry rejected: {leg['symbol']} x{qty}: {r}")
        return None
    orderid = str(r.get("orderid") or "")
    try:
        status, fill = wait_order(client, orderid)
    except Exception as exc:  # noqa: BLE001 - never leave an order unwatched
        log(f"LIVE entry {orderid} unsettled ({exc!r}); cancelling blind, dropping the signal")
        try:
            client.cancelorder(order_id=orderid, strategy=STRATEGY_TAG)
        except Exception as cancel_exc:  # noqa: BLE001
            log(f"cancel of {orderid} failed: {cancel_exc!r}")
        return None
    if status != "complete":
        log(f"LIVE entry {orderid} ended '{status}'; dropping the signal, staying flat")
        return None
    if not fill:
        fill = option_ltp(client, leg["symbol"])
    filled_spot = index_ltp(client) or spot
    long = stop < spot
    if (filled_spot <= stop) if long else (filled_spot >= stop):
        trigger = None
    else:
        trigger = slm_trigger(client, leg["symbol"], fill, filled_spot, stop) if fill else None
    slm_id = None
    shown = fill if fill else "unknown"
    if trigger is None:
        flattened = flatten_unprotected_entry(client, leg["symbol"], qty, orderid)
        if flattened:
            return None
        log(f"CRITICAL: unprotected fill {leg['symbol']} remains open; retrying exit on each poll")
        return {"buy_orderid": orderid, "slm_orderid": None, "slm_trigger": None,
                "premium_in": fill, "qty": qty, "emergency_exit": True}
    try:
        sr = client.placeorder(strategy=STRATEGY_TAG, symbol=leg["symbol"], action="SELL",
                               exchange=FO_EXCHANGE, price_type="SL-M",
                               product=LIVE_PRODUCT, quantity=qty,
                               trigger_price=f"{trigger:.2f}")
    except Exception as exc:  # noqa: BLE001 - entry is filled; flatten if stop placement fails
        log(f"LIVE entry {orderid}: SL-M raised {exc!r} for {leg['symbol']}")
        sr = None
    if not ok(sr):
        log(f"LIVE entry {orderid}: broker did not confirm SL-M for {leg['symbol']}: {sr}")
        if flatten_unprotected_entry(client, leg["symbol"], qty, orderid):
            return None
        log(f"CRITICAL: unprotected fill {leg['symbol']} remains open; retrying exit on each poll")
        return {"buy_orderid": orderid, "slm_orderid": None, "slm_trigger": trigger,
                "premium_in": fill, "qty": qty, "emergency_exit": True}
    slm_id = str(sr.get("orderid") or "") if ok(sr) else ""
    if not slm_id:
        log(f"LIVE entry {orderid}: broker did not return an SL-M order id for {leg['symbol']}")
        if flatten_unprotected_entry(client, leg["symbol"], qty, orderid):
            return None
        return {"buy_orderid": orderid, "slm_orderid": None, "slm_trigger": trigger,
                "premium_in": fill, "qty": qty, "emergency_exit": True}
    log(f"LIVE entry {orderid}: filled {shown} x{qty}, Greeks-mapped SL-M "
        f"{trigger:.2f} (order {slm_id})")
    return {"buy_orderid": orderid, "slm_orderid": slm_id,
            "slm_trigger": trigger, "premium_in": fill, "qty": qty}


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


def read_order(client, orderid: str) -> tuple[str, float | None]:
    """One order's (lower-case status, or "" when unreadable; average price)."""
    try:
        r = client.orderstatus(order_id=orderid, strategy=STRATEGY_TAG)
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"orderstatus raised {exc!r} for {orderid}")
        return "", None
    if not ok(r):
        return "", None
    data = r.get("data") or {}
    status = str(data.get("order_status", "")).lower()
    try:
        price = float(data.get("average_price") or 0) or None
    except (TypeError, ValueError):
        price = None
    return status, price


def release_backstop(client, slm: str) -> tuple[str, float | None]:
    """Cancel the SL-M backstop and confirm it is really gone before any sell.

    An accepted cancel is a request, not a result, so the order is read back.
    Returns ("gone", None), ("filled", fill) when the backstop fired first,
    or ("live", None) when it may still fire - the caller must not sell.
    A backstop that never becomes readable after an accepted cancel is
    trusted gone, as before this check existed.
    """
    try:
        cancelled = ok(client.cancelorder(order_id=slm, strategy=STRATEGY_TAG))
    except Exception as exc:  # noqa: BLE001 - the reads below decide
        log(f"cancel of SL-M {slm} raised {exc!r}")
        cancelled = False
    seen = ""
    for attempt in range(CANCEL_CONFIRM_TRIES):
        if attempt:
            time.sleep(CANCEL_CONFIRM_SECONDS)
        status, fill = read_order(client, slm)
        if "complete" in status:
            return "filled", fill
        if "cancel" in status or "reject" in status:
            return "gone", None
        if status:
            seen = status
            if not cancelled:
                break               # refused cancel on a live order: no point waiting
    if cancelled and not seen:
        return "gone", None
    log(f"SL-M {slm} would not cancel ({seen or 'unknown'}); "
        "refusing to sell twice, the next poll retries")
    return "live", None


def drop_backstop(client, pos: dict, why: str) -> None:
    """Cancel a backstop whose position is already gone; shout if it will not go."""
    slm = pos.get("slm_orderid")
    if not slm:
        return
    how, _fill = release_backstop(client, slm)
    pos["slm_orderid"] = None
    if how == "live":
        log(f"COULD NOT CANCEL SL-M {slm} ({why}). It may be live with no position "
            f"behind it - CANCEL IT BY HAND AT THE BROKER")
    else:
        log(f"SL-M {slm} is {how}: {why}")


def live_close(client, pos: dict) -> tuple[str, float | None]:
    """Cancel the SL-M backstop, then market-sell the full quantity.

    Returns (how, premium_out): "closed" - we sold it; "already-flat" - the
    backstop fired first and its fill is the exit; "failed" - nothing
    settled, so the caller keeps the position and retries. A sell is never
    sent while the backstop could still fire.
    """
    slm = pos.get("slm_orderid")
    if slm:
        how, fill = release_backstop(client, slm)
        if how == "filled":
            pos["slm_orderid"] = None
            return "already-flat", fill or option_ltp(client, pos["symbol"])
        if how == "live":
            return "failed", None
        pos["slm_orderid"] = None
    qty = int(pos.get("qty") or 0)
    status, fill = "unsent", None
    try:
        r = client.placeorder(strategy=STRATEGY_TAG, symbol=pos["symbol"], action="SELL",
                              exchange=FO_EXCHANGE, price_type="MARKET",
                              product=LIVE_PRODUCT, quantity=qty)
    except Exception as exc:  # noqa: BLE001 - treated as unsettled below
        log(f"LIVE sell raised {exc!r} for {pos['symbol']} x{qty}")
        r = None
    if r is not None and not ok(r):
        log(f"LIVE sell rejected: {pos['symbol']} x{qty}: {r}")
    elif r is not None:
        orderid = str(r.get("orderid") or "")
        try:
            status, fill = wait_order(client, orderid)
        except Exception as exc:  # noqa: BLE001 - never leave a sell running unwatched
            log(f"LIVE sell {orderid} unsettled ({exc!r}); cancelling blind")
            try:
                client.cancelorder(order_id=orderid, strategy=STRATEGY_TAG)
            except Exception as cancel_exc:  # noqa: BLE001
                log(f"cancel of {orderid} failed: {cancel_exc!r}")
        if status == "complete":
            return "closed", fill or option_ltp(client, pos["symbol"])
        log(f"LIVE sell {orderid} ended '{status}'")
    # A refused sell means either the order failed (retry) or there is
    # nothing left to sell - closed by hand or by the broker, which then
    # rejects every SELL as a fresh short (2026-10-05, three times). The
    # position book is the tiebreaker.
    held = net_quantity(client, pos["symbol"])
    if held is not None and held <= 0:
        log(f"broker holds {held} {pos['symbol']}; it was closed outside the strategy")
        return "outside", option_ltp(client, pos["symbol"])
    log(f"the position stays for a retry (broker holds {held if held is not None else 'unknown'})")
    return "failed", None


# =============================================================================
# POSITIONS  (one per book)
# =============================================================================

def initial_legs(stop: float, multiplier: int = 1) -> dict:
    """The lock and runner legs of a fresh position, both on the entry stop.

    Args:
        stop: The initial index stop.
        multiplier: Lot multiplier (GAP_LOT_MULTIPLIER on a gap day).
    """
    m = max(1, int(multiplier))
    runner = max(0, min(RUNNER_LOTS, LIVE_LOTS))
    legs = {}
    if LIVE_LOTS - runner > 0:
        legs["lock"] = {"lots": (LIVE_LOTS - runner) * m, "stop": stop}
    if runner > 0:
        legs["runner"] = {"lots": runner * m, "stop": stop}
    return legs


def position_legs(pos: dict) -> dict:
    """The position's legs; a position saved before the split is one lock leg."""
    if not pos.get("legs"):
        pos["legs"] = {"lock": {"lots": int(pos.get("lots") or 1), "stop": float(pos["stop"])}}
    return pos["legs"]


def sync_position(pos: dict) -> None:
    """Total the open lots and set the position stop to the loosest leg's.

    The SL-M backstop is sized off pos["stop"], so it must be the stop of the
    leg with the most room; a backstop tighter than a leg's own stop would
    sell that leg early.
    """
    legs = position_legs(pos)
    long = pos["direction"] == "long"
    pos["lots"] = sum(int(leg["lots"]) for leg in legs.values())
    if pos.get("buy_orderid"):
        pos["qty"] = pos["lots"] * int(pos["lotsize"])
    stops = [float(leg["stop"]) for leg in legs.values()]
    if stops:
        pos["stop"] = min(stops) if long else max(stops)


def leg_hits(pos: dict, low: float, high: float) -> list[str]:
    """Legs whose stop the range [low, high] reached, or whose exit is pending."""
    long = pos["direction"] == "long"
    return [n for n, leg in position_legs(pos).items()
            if leg.get("exit_pending")
            or ((low <= float(leg["stop"])) if long else (high >= float(leg["stop"])))]


def advance_legs(name: str, pos: dict) -> None:
    """Trail every leg, then apply each leg's ATR lock floor; stops only tighten.

    The floors scale with pos["atr"], the ATR(14) at entry. A position saved
    without it (opened before the ATR locks) gets the trail only.
    """
    long = pos["direction"] == "long"
    entry = float(pos["entry"])
    level = trail_level(entry, pos["mfe"], long)
    atr = pos.get("atr")
    atr = float(atr) if atr is not None and math.isfinite(float(atr)) and float(atr) > 0 else None
    for leg_name, leg in position_legs(pos).items():
        stop = float(leg["stop"])
        new = stop
        if level is not None:
            new = max(new, level) if long else min(new, level)
        arm_at, lock = ((RUNNER_LOCK_AT_ATR, RUNNER_LOCK_ATR) if leg_name == "runner"
                        else (LOCK_AT_ATR, LOCK_ATR))
        if atr is not None and arm_at is not None and pos["mfe"] >= arm_at * atr:
            floor = entry + lock * atr if long else entry - lock * atr
            new = max(new, floor) if long else min(new, floor)
        if BREAKEVEN_AT_PTS is not None and pos["mfe"] >= BREAKEVEN_AT_PTS:
            new = max(new, entry) if long else min(new, entry)
        if new != stop:
            log(f"[{name}] {leg_name} stop: best +{pos['mfe']:.2f} pts, {stop:.2f} -> {new:.2f}")
            leg["stop"] = new
    sync_position(pos)


def sell_market(client, symbol: str, qty: int) -> tuple[str, float | None]:
    """Market-sell qty and read the order to a final state."""
    try:
        r = client.placeorder(strategy=STRATEGY_TAG, symbol=symbol, action="SELL",
                              exchange=FO_EXCHANGE, price_type="MARKET",
                              product=LIVE_PRODUCT, quantity=qty)
    except Exception as exc:  # noqa: BLE001 - treated as unsettled
        log(f"LIVE sell raised {exc!r} for {symbol} x{qty}")
        return "unknown", None
    if not ok(r):
        log(f"LIVE sell rejected: {symbol} x{qty}: {r}")
        return "rejected", None
    orderid = str(r.get("orderid") or "")
    try:
        return wait_order(client, orderid)
    except Exception as exc:  # noqa: BLE001 - never leave a sell running unwatched
        log(f"LIVE sell {orderid} unsettled ({exc!r}); cancelling blind")
        try:
            client.cancelorder(order_id=orderid, strategy=STRATEGY_TAG)
        except Exception as cancel_exc:  # noqa: BLE001
            log(f"cancel of {orderid} failed: {cancel_exc!r}")
        return "unknown", None


def park_backstop(client, pos: dict) -> bool:
    """Greek-map a fresh SL-M for exactly the lots still open; True when it rests."""
    spot = index_ltp(client)
    premium = option_ltp(client, pos["symbol"])
    long = pos["direction"] == "long"
    if not spot or not premium:
        log(f"backstop re-park: no quote for {pos['symbol']}")
        return False
    if (spot <= pos["stop"]) if long else (spot >= pos["stop"]):
        log("backstop re-park: index already beyond the stop")
        return False
    trigger = slm_trigger(client, pos["symbol"], premium, spot, float(pos["stop"]))
    if trigger is None:
        return False
    try:
        sr = client.placeorder(strategy=STRATEGY_TAG, symbol=pos["symbol"], action="SELL",
                               exchange=FO_EXCHANGE, price_type="SL-M",
                               product=LIVE_PRODUCT, quantity=int(pos["qty"]),
                               trigger_price=f"{trigger:.2f}")
    except Exception as exc:  # noqa: BLE001
        log(f"backstop re-park raised {exc!r}")
        return False
    if not ok(sr) or not sr.get("orderid"):
        log(f"backstop re-park refused: {sr}")
        return False
    pos["slm_orderid"] = str(sr["orderid"])
    pos["slm_trigger"] = trigger
    log(f"SL-M backstop re-parked: {pos['qty']} {pos['symbol']} trigger {trigger:.2f} "
        f"(order {pos['slm_orderid']})")
    return True


def close_legs(client, name: str, book: dict, names: list[str], price: float, why: str,
               bar_start: str) -> bool:
    """Close some legs; the whole position when every open leg is named.

    Live, the backstop covers every open lot, so it is cleared first, the
    legs are sold, and a fresh backstop is parked for the lots left. If that
    re-park fails the rest is flattened, as an unprotected entry is.

    Returns:
        True when the named legs are closed.
    """
    pos = book["position"]
    legs = position_legs(pos)
    if set(names) >= set(legs):
        return close_position(client, name, book, price, why, bar_start)
    lots = sum(int(legs[n]["lots"]) for n in names)
    live = bool(pos.get("buy_orderid"))
    premium = None
    if live:
        slm = pos.get("slm_orderid")
        if slm:
            how, fill = release_backstop(client, slm)
            if how == "filled":
                pos["slm_orderid"] = None
                return close_position(client, name, book, price, "backstop", bar_start,
                                      settled=("already-flat", fill or option_ltp(client, pos["symbol"])))
            if how == "live":
                for n in names:
                    legs[n]["exit_pending"] = True
                return False
            pos["slm_orderid"] = None
        status, fill = sell_market(client, pos["symbol"], lots * int(pos["lotsize"]))
        if status != "complete":
            held = net_quantity(client, pos["symbol"])
            if held is not None and held <= 0:
                return close_position(client, name, book, price, why, bar_start,
                                      settled=("outside", option_ltp(client, pos["symbol"])))
            for n in names:
                legs[n]["exit_pending"] = True
            log(f"[{name}] LIVE {'+'.join(names)} exit ({why}) {status}; retrying")
            if not park_backstop(client, pos):
                pos["emergency_exit"] = True
            return False
        premium = fill or option_ltp(client, pos["symbol"])
    elif pos.get("symbol"):
        premium = option_ltp(client, pos["symbol"])
    long = pos["direction"] == "long"
    gross = (price - pos["entry"]) if long else (pos["entry"] - price)
    opt = ""
    if premium is not None and pos.get("premium_in") is not None:
        opt_pts = premium - pos["premium_in"]
        opt = (f"; option {pos['premium_in']:.2f} -> {premium:.2f} "
               f"({opt_pts:+.2f}, Rs {opt_pts * pos['lotsize']:+,.0f} per lot)")
    label = "LIVE" if live else "PAPER"
    log(f"[{name}] {label} EXIT {'+'.join(names)} {why} ({lots} lot): {pos['direction']} "
        f"{pos['entry']:.2f} -> {price:.2f}, index {gross:+.2f} pts gross{opt}")
    book["closed"].append({"direction": pos["direction"], "entry": pos["entry"],
                           "exit": price, "why": f"{why}:{'+'.join(names)}",
                           "gross": round(gross, 2), "lots": lots,
                           "stop": min(float(legs[n]["stop"]) for n in names) if long
                           else max(float(legs[n]["stop"]) for n in names),
                           "premium_in": pos.get("premium_in"), "premium_out": premium,
                           "opened": pos["opened"], "closed": datetime.now(IST).isoformat()})
    for n in names:
        del legs[n]
    sync_position(pos)
    if live and not park_backstop(client, pos):
        log(f"[{name}] CRITICAL: no backstop for the {pos['lots']} lot(s) left; flattening them")
        pos["emergency_exit"] = True
    return True


def close_position(client, name: str, book: dict, price: float, why: str,
                   bar_start: str, settled: tuple[str, float | None] | None = None) -> bool:
    """Close the book's position and log index and option results.

    Live, the SL-M backstop is cancelled BEFORE the market sell - never the
    other way round - so a backstop caught mid-fire reads as "already flat"
    instead of being sold twice. Returns False when a live exit could not be
    settled, leaving the position in place for the next poll to retry.
    """
    pos = book["position"]
    live = bool(pos.get("buy_orderid"))
    note = ""
    premium = None
    if live:
        how, premium = settled if settled is not None else live_close(client, pos)
        if how == "failed":
            log(f"[{name}] LIVE exit ({why}) unsettled; keeping the position for a retry")
            return False
        if how == "already-flat":
            note = "; SL-M backstop fired first"
        if how == "outside":
            note = "; closed outside the strategy, the broker was already flat"
    else:
        premium = option_ltp(client, pos["symbol"]) if pos.get("symbol") else None
    long = pos["direction"] == "long"
    gross = (price - pos["entry"]) if long else (pos["entry"] - price)
    opt = ""
    if premium is not None and pos.get("premium_in") is not None:
        opt_pts = premium - pos["premium_in"]
        opt = (f"; option {pos['premium_in']:.2f} -> {premium:.2f} "
               f"({opt_pts:+.2f}, Rs {opt_pts * pos['lotsize']:+,.0f} per lot)")
    slip = ""
    if why == "stop":
        gap = (pos["stop"] - price) if long else (price - pos["stop"])
        slip = f"; stop {pos['stop']:.2f}, seen {price:.2f}, slippage {gap:+.2f} pts"
    label = "LIVE" if live else "PAPER"
    log(f"[{name}] {label} EXIT {why}: {pos['direction']} {pos['entry']:.2f} -> {price:.2f}, "
        f"index {gross:+.2f} pts gross, {gross - COST_PTS:+.2f} after {COST_PTS:.0f} cost"
        f"{slip}{opt}{note}")
    book["closed"].append({"direction": pos["direction"], "entry": pos["entry"],
                           "exit": price, "why": why, "gross": round(gross, 2),
                           "lots": int(pos.get("lots") or 1),
                           "stop": pos["stop"], "premium_in": pos.get("premium_in"),
                           "premium_out": premium, "opened": pos["opened"],
                           "closed": datetime.now(IST).isoformat()})
    # The backtest restarts the cluster on the bar after the exit bar.
    book["cluster_from"] = bar_start
    book["position"] = None
    return True


def next_bar_start(stamp: datetime) -> str:
    """Start of the bar after the one containing `stamp`."""
    epoch = int(stamp.timestamp())
    start = datetime.fromtimestamp(epoch - epoch % BAR_SECONDS, IST) + timedelta(seconds=BAR_SECONDS)
    return start.isoformat()


def reconcile_live(client, name: str, book: dict) -> bool:
    """Book out a live position the broker no longer holds.

    The backstop may have fired between polls, or the lot may have been sold
    by hand at the broker. Either way the process must stop managing it, and
    a backstop left behind a hand exit must be pulled before it can sell a
    naked short. A flat book is read twice before it is believed.

    Returns:
        True when the position was booked out.
    """
    pos = book.get("position")
    if not pos or not pos.get("buy_orderid"):
        return False
    stamp = next_bar_start(datetime.now(IST))
    slm = pos.get("slm_orderid")
    if slm:
        status, fill = read_order(client, slm)
        if "complete" in status:
            pos["slm_orderid"] = None
            log(f"[{name}] SL-M backstop {slm} filled at the broker")
            return close_position(client, name, book, index_ltp(client) or pos["stop"],
                                  "backstop", stamp,
                                  settled=("already-flat", fill or option_ltp(client, pos["symbol"])))
    held = net_quantity(client, pos["symbol"])
    if held is None or held > 0:
        return False
    time.sleep(MANUAL_EXIT_RECHECK_SECONDS)
    held = net_quantity(client, pos["symbol"])
    if held is None or held > 0:
        return False
    if held < 0:
        log(f"[{name}] broker reports {held} {pos['symbol']} - NET SHORT, not the long "
            f"option this strategy bought. CHECK THE BROKER POSITION BY HAND")
    log(f"[{name}] {pos['symbol']} was closed outside the strategy (manual exit?)")
    drop_backstop(client, pos, "the position was closed outside the strategy")
    return close_position(client, name, book, index_ltp(client) or pos["entry"],
                          "closed-outside", stamp,
                          settled=("outside", option_ltp(client, pos["symbol"])))


def manage(client, name: str, book: dict, frame: pd.DataFrame) -> None:
    """Catch stops, retry an unprotected emergency exit, then ratchet the trail."""
    pos = book.get("position")
    if not pos:
        return
    if pos.get("emergency_exit"):
        close_position(client, name, book, index_ltp(client) or pos["entry"],
                       "unprotected-entry", next_bar_start(datetime.now(IST)))
        return
    if reconcile_live(client, name, book):
        return
    long = pos["direction"] == "long"
    since = frame[frame.index >= pd.Timestamp(pos["entry_bar"])]
    if since.empty:
        return
    last = since.iloc[-1]
    hits = leg_hits(pos, float(last["low"]), float(last["high"]))
    if hits:
        price = index_ltp(client) or pos["stop"]
        log(f"[{name}] bar {since.index[-1]:%H:%M} traded through the "
            f"{'+'.join(hits)} stop between polls")
        if not close_legs(client, name, book, hits, price, "stop", next_bar_start(since.index[-1])):
            return
        pos = book.get("position")
        if not pos:
            return
    best = (float(since["high"].max()) - pos["entry"]) if long else (pos["entry"] - float(since["low"].min()))
    pos["mfe"] = max(pos.get("mfe", 0.0), best)
    advance_legs(name, pos)


def open_books(state: dict) -> list[tuple[str, dict]]:
    return [(n, b) for n, b in state["books"].items() if b.get("position")]


def watch_stop(client, state: dict, deadline: float) -> None:
    """Poll index stops; the broker SL-M remains the live outage backstop."""
    while not _shutdown and time.monotonic() + STOP_POLL_SECONDS < deadline:
        if not open_books(state) or datetime.now(IST).time() >= SQUARE_OFF:
            return
        time.sleep(STOP_POLL_SECONDS)
        price = index_ltp(client)
        if price is None:
            continue
        for name, book in open_books(state):
            pos = book["position"]
            if pos.get("emergency_exit"):
                close_position(client, name, book, price, "unprotected-entry",
                               next_bar_start(datetime.now(IST)))
                save_state(state)
                continue
            hits = leg_hits(pos, price, price)
            if hits:
                close_legs(client, name, book, hits, price, "stop", next_bar_start(datetime.now(IST)))
                save_state(state)


# =============================================================================
# MAIN
# =============================================================================

def cluster_text(cluster: dict | None) -> str:
    if not cluster:
        return "no cluster"
    return (f"cluster {cluster['candles']} candles from {cluster['from']}, box "
            f"{cluster['low']:.2f}-{cluster['high']:.2f} ({cluster['high'] - cluster['low']:.2f} pts)")


def box_atr_text(box: float, atr: float | None, cap: float) -> str:
    """Box height as an ATR multiple against a book's cap, for the log."""
    if atr is None:
        return "ATR unreadable"
    verdict = "within" if box <= cap * atr else "over"
    return f"{box / atr:.2f}x ATR {atr:.2f}, {verdict} {cap:g}x"


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
                    close_position(client, name, book, price, "square-off", now.isoformat())
        return state

    frame = fetch_today(client)
    if frame.empty:
        log("no usable bars for today yet")
        return state
    bar = frame.index[-1]
    close = float(frame["close"].iloc[-1])
    entry_time = (bar + timedelta(seconds=BAR_SECONDS)).time()

    for name, book in books.items():
        manage(client, name, book, frame)

    # Shared per cycle, so every book sees the same quotes. ATR is read every
    # bar (prior sessions fetched once a day) so the log shows it even on days
    # no breakout passes the active-day gate.
    gate_quotes: dict = {}
    if BOOK_BOX_ATR_MAX:
        prior = fetch_prior(client)
        gate_quotes["atr"] = atr_now(prior, frame) if prior is not None else None
        gate_quotes["gap"] = open_gap_pct(prior, frame)
    legs: dict = {}
    parts = []
    for name, book in books.items():
        if book.get("position"):
            p = book["position"]
            parts.append(f"{name}: in {p['direction']} from {p['entry']:.2f}, stop {p['stop']:.2f}")
            continue
        start = 0
        if book.get("cluster_from"):
            start = int((frame.index < pd.Timestamp(book["cluster_from"])).sum())
        sig, cluster = scan(frame, start)
        text = cluster_text(cluster)
        if cluster and name in BOOK_BOX_ATR_MAX:
            text += (f", {box_atr_text(cluster['high'] - cluster['low'], gate_quotes['atr'], BOOK_BOX_ATR_MAX[name])}")
        parts.append(f"{name}: {text}")
        if not sig:
            continue
        side = sig["direction"]
        if not (NO_NEW_ENTRY_BEFORE <= entry_time <= NO_NEW_ENTRY_AFTER):
            log(f"[{name}] {side} breakout outside the entry window; not taken")
            continue
        if book.get("trades", 0) >= MAX_TRADES_PER_DAY:
            log(f"[{name}] {side} breakout skipped: {MAX_TRADES_PER_DAY} trades already today")
            continue
        if name in BOOK_VIX_MIN:
            if "v" not in gate_quotes:
                breadth = read_breadth(client) if BREADTH_MIN is not None else None
                gate_quotes["v"] = (read_vix(client), breadth)
            passes, reading = active_day(*gate_quotes["v"], BOOK_VIX_MIN[name])
            if not passes:
                would = ""
                if name in BOOK_BOX_ATR_MAX:
                    would = (f"; box {sig['high'] - sig['low']:.2f} pts would be "
                             f"{box_atr_text(sig['high'] - sig['low'], gate_quotes['atr'], BOOK_BOX_ATR_MAX[name])}")
                log(f"[{name}] {side} breakout skipped, not an active day: {reading}{would}")
                continue
            log(f"[{name}] active day: {reading}")
        atr = gate_quotes.get("atr")
        if atr is None or not math.isfinite(float(atr)) or atr <= 0:
            log(f"[{name}] {side} breakout skipped: ATR unreadable, initial stop cannot be calculated")
            continue
        if name in BOOK_BOX_ATR_MAX:
            box = sig["high"] - sig["low"]
            cap = BOOK_BOX_ATR_MAX[name]
            if atr is None:
                log(f"[{name}] {side} breakout skipped: ATR unreadable")
                continue
            if box > cap * atr:
                log(f"[{name}] {side} breakout skipped: box {box:.2f} pts is {box_atr_text(box, atr, cap)}")
                continue
            log(f"[{name}] box {box:.2f} pts is {box_atr_text(box, atr, cap)}")

        long = side == "long"
        if "spot" not in legs:
            legs["spot"] = index_ltp(client) or close
        spot = legs["spot"]
        stop = initial_index_stop(spot, sig["low"], sig["high"], atr, long)
        risk = (spot - stop) if long else (stop - spot)
        if risk <= 0:
            log(f"[{name}] {side} breakout ignored: index {spot:.2f} already back past the stop")
            continue
        opt_type = "CE" if long else "PE"
        if opt_type not in legs:
            leg = resolve_leg(client, opt_type)
            legs[opt_type] = (leg, option_ltp(client, leg["symbol"]) if leg else None)
        leg, premium = legs[opt_type]
        shown = f"{premium:.2f}" if premium is not None else "unknown"
        head = "LIVE signal" if TRADE_LIVE else "PAPER"
        gap = gate_quotes.get("gap")
        mult = lot_multiplier(gap)
        pos_legs = initial_legs(stop, mult)
        lots = sum(int(x["lots"]) for x in pos_legs.values())
        gap_text = f"gap {gap:.2%}" if gap is not None else "gap unreadable"
        size_text = f"{lots} lots ({gap_text}{', gap day x' + str(mult) if mult > 1 else ''})"
        lock_text = ", ".join(
            f"{n} floor +{lk * atr:.2f} once +{at * atr:.2f}"
            for n, (at, lk) in (("lock", (LOCK_AT_ATR, LOCK_ATR)), ("runner", (RUNNER_LOCK_AT_ATR, RUNNER_LOCK_ATR)))
            if n in pos_legs and at is not None)
        log(f"[{name}] {head} {side.upper()}: close {close:.2f} broke the {sig['candles']}-candle box "
            f"{sig['low']:.2f}-{sig['high']:.2f}; entry {spot:.2f}, stop {stop:.2f} "
            f"(risk {risk:.2f} pts); {leg['symbol'] if leg else 'option unresolved'} premium {shown}; "
            f"{size_text}; ATR {atr:.2f}: {lock_text or 'no locks'}")
        position = {
            "direction": side, "entry": spot, "stop": stop, "risk": risk,
            "mfe": 0.0, "opened": now.isoformat(),
            "entry_bar": (bar + timedelta(seconds=BAR_SECONDS)).isoformat(),
            "symbol": leg["symbol"] if leg else None,
            "lotsize": leg["lotsize"] if leg else 0, "premium_in": premium,
            "legs": pos_legs, "lots": lots, "atr": float(atr), "gap": gap,
        }
        if TRADE_LIVE:
            if not leg:
                log(f"[{name}] live entry skipped: option unresolved; staying flat")
                continue
            fills = live_entry(client, leg, spot, stop, lots)
            if fills is None:
                continue            # rejected or unsettled: no position, no trade counted
            position.update(fills)
        book["trades"] = book.get("trades", 0) + 1
        book["position"] = position

    atr_part = ""
    if BOOK_BOX_ATR_MAX:
        atr = gate_quotes["atr"]
        atr_part = f" ATR{ATR_PERIOD} {atr:.2f}" if atr is not None else f" ATR{ATR_PERIOD} unreadable"
    log(f"bar {bar:%H:%M} close {close:.2f}{atr_part} | " + " | ".join(parts))
    return state


def sleep_to_next_bar(client=None, state: dict | None = None) -> None:
    now = datetime.now(IST)
    epoch = int(now.timestamp())
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
        gross = sum(t["gross"] * int(t.get("lots") or 1) for t in closed)
        lots = sum(int(t.get("lots") or 1) for t in closed)
        stops = [t for t in closed if t["why"] == "stop"]
        slips = [((t["stop"] - t["exit"]) if t["direction"] == "long" else (t["exit"] - t["stop"]))
                 for t in stops]
        avg_slip = f", average stop slippage {sum(slips) / len(slips):+.2f} pts" if slips else ""
        log(f"[{name}] day summary: {len(closed)} paper trades, index {gross:+.2f} pts gross, "
            f"{gross - COST_PTS * lots:+.2f} after cost ({lots} lot exits){avg_slip}")


def main() -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    client = build_client()
    be_text = (f"breakeven at +{BREAKEVEN_AT_PTS:g} pts" if BREAKEVEN_AT_PTS is not None
               else "breakeven off")
    mode = (f"LIVE: {LIVE_LOTS} lot (x{GAP_LOT_MULTIPLIER} on a {GAP_SIZE_PCT:.1%} gap day) "
            f"{LIVE_PRODUCT} market orders with a broker SL-M "
            "backstop, exit failures retried (Analyze mode routes them to the sandbox); "
            "TRADE_LIVE = False is paper") if TRADE_LIVE else "PAPER ONLY - no order is ever sent"
    log(f"{STRATEGY_TAG} starting, {mode}: "
        f">= {MIN_CANDLES} candles in a {BOX_PCT:.2%} box, stop at the tighter box edge or "
        f"{MAX_INITIAL_RISK_ATR:g}x ATR{ATR_PERIOD}, "
        f"trail {TRAIL_DIST_PCT:.1%} in {TRAIL_STEP_PCT:.1%} steps, {be_text}, entries "
        f"{NO_NEW_ENTRY_BEFORE:%H:%M}-{NO_NEW_ENTRY_AFTER:%H:%M}, max {MAX_TRADES_PER_DAY}, "
        f"square-off {SQUARE_OFF:%H:%M}")
    breadth_rule = (f" and NIFTY 50 breadth one-sided >= {BREADTH_MIN:g}x"
                    if BREADTH_MIN is not None else ", breadth filter off")
    log(f"book: boxatr (VIX > {VIX_MIN:g}{breadth_rule}, box <= "
        f"{BOOK_BOX_ATR_MAX['boxatr']:g}x ATR{ATR_PERIOD}); "
        f"option {ITM_POINTS} pts ITM; locks: lock lot +{LOCK_ATR} ATR once +{LOCK_AT_ATR} ATR, "
        f"runner +{RUNNER_LOCK_ATR} ATR once +{RUNNER_LOCK_AT_ATR} ATR")

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
            if datetime.now(IST).time() >= SQUARE_OFF and open_books(state):
                # A refused square-off is retried every few seconds, not every bar.
                time.sleep(SQUARE_OFF_RETRY_SECONDS)
                continue
            sleep_to_next_bar(client, state)
    finally:
        save_state(state)

    log("shut down cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
