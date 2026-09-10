"""NIFTY weekly short straddle, regime-switched, for the Python Strategy Host.

Sells an ATM NIFTY straddle on the nearest weekly expiry each Wednesday, holds to
Tuesday expiry, and manages the position two ways depending on the volatility
regime measured at entry:

Wednesday is enforced by ENTRY_WEEKDAY, not merely implied by MIN_DTE_TO_ENTER,
so a missed Wednesday skips the cycle rather than entering a day late.

    entry India VIX <  15  ->  delta hedge daily + stop at 2x credit
    entry India VIX >= 15  ->  stop at 2x credit only, no hedge

That switch is not tuned. It was pre-registered before validation and follows from
mechanism: daily delta rebalancing while short gamma buys every rally and sells
every drop, so it harvests calm and bleeds in chaos. Out-of-sample (2018-2026) the
switched variant returned +38.6 points per cycle, t = 2.73, p = 0.0066.

SAFETY: this script refuses to place an order unless OpenAlgo is in analyzer
(sandbox) mode. Flipping that requires editing I_UNDERSTAND_THIS_IS_LIVE by hand.
Analyzer mode is a GLOBAL instance flag, so it is re-checked every cycle.

Sizing is done against the REAL broker margin from /api/v1/margin, which has no
sandbox branch, because sandbox blocks only the option premium (leverage 1x) and
would otherwise permit a position no real account could fund.

Note on logging: the strategy host captures stdout to log/strategies/, so print()
is the logging channel here. That is the host contract and differs deliberately
from the repo-wide rule that application modules use utils.logging.
"""

from __future__ import annotations

import calendar
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from openalgo import api

# =============================================================================
# CONFIGURATION  (per-strategy env vars do not exist in the host - edit here)
# =============================================================================

I_UNDERSTAND_THIS_IS_LIVE = False   # must be True to run outside analyzer mode
DRY_RUN = False                     # resolve and print, place nothing

STRATEGY_TAG = "NIFTY_WEEKLY_SHORT_STRADDLE"
UNDERLYING = "NIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
FO_EXCHANGE = "NFO"
PRODUCT = "NRML"                    # weekly carry; MIS would be squared off daily

CAPITAL = 500_000                   # rupees allocated to this strategy
MAX_LOTS = 1                        # hard cap regardless of what margin allows
MARGIN_UTILISATION = 0.50           # use at most this share of CAPITAL as margin
MAX_LOSS = 75_000                   # kill switch on realised + unrealised

STOP_MULT = 2.0                     # exit when straddle value >= this x credit
VIX_REGIME_THRESHOLD = 15.0         # below -> delta hedge; at/above -> stop only
MIN_DTE_TO_ENTER = 5                # calendar days; keeps entry at cycle start

# Cycle start is Wednesday, the day after the Tuesday weekly expiry. The DTE
# floor alone does not pin that down: on a Thursday the next Tuesday is still 5
# days out, so entry would drift a day later and the cycle would be short one
# day of theta. Set to None to enter on any day the DTE floor allows.
#
# Consequence of a fixed day: a Wednesday lost to a holiday, an outage, or a
# late start past ENTRY_END is a skipped cycle, not a Thursday entry.
ENTRY_WEEKDAY = 2                   # Monday is 0, so 2 is Wednesday

ENTRY_START, ENTRY_END = "09:20", "10:00"
HEDGE_AT = "15:10"

# DO NOT let this position expire, and do not push EXIT_AT later.
#
# From 2026-08-03 the NIFTY index feed stops publishing continuously at 15:14
# and emits a single settlement print at 15:28-15:29. Measured across the days
# since: the move from the 15:14 price to that final print has sd 60.4 pts
# against 23.9 pts before the change -- a 6.4x jump in variance (F=6.38,
# p<0.00001). NIFTY futures show no such change, so this is specific to how the
# index close is now formed.
#
# The case for exiting is about the TAIL, not the average. Measured over the
# year of 1m data, buying back at 15:14 costs about 4 pts MORE than settling on
# average, because you pay residual time value and two legs of spread. What you
# buy for those 4 pts is the variance: the spread between the two routes has
# sd 22.9 pts before the change and 65.0 after, with the worst single session
# going from 80 pts to 198.
#
# A short straddle is short gamma, so a surprise in the settlement print is a
# loss whichever way it goes, and 198 pts is about six cycles of edge. Paying a
# small certain cost to remove a large uncertain one is the same logic as the
# 2x stop, so it is applied here for the same reason.
EXIT_AT = "15:15"
POLL_SECONDS = 60
ACT_EVERY_SECONDS = 300             # evaluate the stop every 5 minutes

IST = ZoneInfo("Asia/Kolkata")
STATE_DIR = Path("strategies") / "state"
STATE_FILE = STATE_DIR / "nifty_weekly_short_straddle.json"

_shutdown = False


# =============================================================================
# PLUMBING
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


def now_hm() -> str:
    return f"{datetime.now(IST):%H:%M}"


def normalize_expiry(dashed: str) -> str:
    """'08-SEP-26' -> '08SEP26'. The expiry API and every other options API
    disagree on this format; everything downstream wants the compact form."""
    return dashed.replace("-", "").upper()


def parse_expiry(compact: str) -> datetime:
    return datetime.strptime(compact, "%d%b%y").replace(tzinfo=IST)


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
        log("WARNING: running against LIVE trading, as explicitly configured")
        return True
    log("REFUSING TO TRADE: OpenAlgo is in live mode and "
        "I_UNDERSTAND_THIS_IS_LIVE is False")
    return False


def kill_switch_tripped(client) -> bool:
    f = client.funds()
    if not ok(f):
        log(f"cannot read funds: {f}")
        return False
    d = f["data"]
    pnl = float(d.get("m2mrealized", 0) or 0) + float(d.get("m2munrealized", 0) or 0)
    if pnl <= -abs(MAX_LOSS):
        log(f"KILL SWITCH: P&L {pnl:,.0f} breached limit {-abs(MAX_LOSS):,.0f}")
        return True
    return False


# =============================================================================
# MARKET LOOKUPS
# =============================================================================

def nearest_weekly(client) -> str | None:
    e = client.expiry(symbol=UNDERLYING, exchange=FO_EXCHANGE, instrumenttype="options")
    if not ok(e) or not e.get("data"):
        log(f"expiry lookup failed: {e}")
        return None
    return normalize_expiry(e["data"][0])


def resolve_atm(client, expiry: str) -> dict | None:
    """Both legs off one underlying snapshot, using real DB strikes.

    strike_int is deliberately omitted so the ATM is the nearest ACTUAL listed
    strike rather than an arithmetic guess that may not exist.
    """
    legs = {}
    for opt in ("CE", "PE"):
        r = client.optionsymbol(underlying=UNDERLYING, exchange=INDEX_EXCHANGE,
                                expiry_date=expiry, offset="ATM", option_type=opt)
        if not ok(r):
            log(f"optionsymbol {opt} failed: {r}")
            return None
        legs[opt] = r
        time.sleep(0.2)
    return {
        "ce": legs["CE"]["symbol"],
        "pe": legs["PE"]["symbol"],
        "lotsize": int(legs["CE"]["lotsize"]),
        "freeze_qty": int(legs["CE"].get("freeze_qty") or 0),
        "spot": float(legs["CE"].get("underlying_ltp") or 0),
    }


def read_vix(client) -> float | None:
    q = client.quotes(symbol="INDIAVIX", exchange=INDEX_EXCHANGE)
    if not ok(q):
        log(f"VIX quote failed: {q}")
        return None
    d = q["data"]
    return float(d.get("ltp") or d.get("prev_close") or 0) or None


def leg_prices(client, ce: str, pe: str) -> tuple[float, float] | None:
    r = client.multiquotes(symbols=[{"symbol": ce, "exchange": FO_EXCHANGE},
                                    {"symbol": pe, "exchange": FO_EXCHANGE}])
    prices = {}
    for row in (r or {}).get("results", []):
        d = row.get("data") or {}
        px = d.get("ltp") or d.get("prev_close")
        if px:
            prices[row["symbol"]] = float(px)
    if ce not in prices or pe not in prices:
        log(f"could not price both legs: {r}")
        return None
    return prices[ce], prices[pe]


def near_month_future(client) -> str | None:
    e = client.expiry(symbol=UNDERLYING, exchange=FO_EXCHANGE, instrumenttype="futures")
    if not ok(e) or not e.get("data"):
        log(f"futures expiry lookup failed: {e}")
        return None
    return f"{UNDERLYING}{normalize_expiry(e['data'][0])}FUT"


# =============================================================================
# SIZING
# =============================================================================

def size_position(client, atm: dict) -> int:
    """Lots, sized on REAL broker margin.

    /api/v1/margin has no sandbox branch, so this returns true SPAN+exposure even
    in analyzer mode. Sandbox's own funds() would block only the premium at 1x
    leverage and permit roughly ten times the real position.
    """
    qty = atm["lotsize"]
    positions = [
        {"symbol": atm["ce"], "exchange": FO_EXCHANGE, "action": "SELL",
         "product": PRODUCT, "pricetype": "MARKET", "quantity": qty},
        {"symbol": atm["pe"], "exchange": FO_EXCHANGE, "action": "SELL",
         "product": PRODUCT, "pricetype": "MARKET", "quantity": qty},
    ]
    m = client.margin(positions=positions)
    if not ok(m):
        log(f"margin lookup failed, refusing to size blind: {m}")
        return 0
    per_lot = float(m["data"].get("total_margin_required") or 0)
    if per_lot <= 0:
        log(f"margin returned {per_lot}, refusing to size blind")
        return 0
    budget = CAPITAL * MARGIN_UTILISATION
    lots = int(budget // per_lot)
    log(f"margin per lot Rs {per_lot:,.0f} (real broker SPAN+exposure) | "
        f"budget Rs {budget:,.0f} -> {lots} lots, capped at {MAX_LOTS}")
    return max(0, min(lots, MAX_LOTS))


# =============================================================================
# STATE
# =============================================================================

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as exc:
            log(f"state file unreadable ({exc}), treating as flat")
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


def clear_state() -> None:
    if STATE_FILE.exists():
        STATE_FILE.unlink()


def position_map(client) -> dict | None:
    """One position-book fetch, keyed by symbol, for this strategy's product.

    Returns:
        Symbol to signed quantity, or None when the book could not be read.
        None means UNKNOWN, never flat: a failed fetch that read as an empty
        book would let `reconcile` forget a live straddle and `exit_legs`
        report every leg already closed while both are still short.
    """
    try:
        pb = client.positionbook()
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"positionbook raised {exc!r}")
        return None
    if not ok(pb):
        log(f"positionbook unavailable: {pb}")
        return None
    return {p["symbol"]: int(float(p.get("quantity") or 0))
            for p in pb.get("data", [])
            if p.get("product") == PRODUCT and p.get("symbol")}


def live_qty(client, symbol: str, book: dict | None = None) -> int | None:
    """Signed net quantity for one symbol. Pass `book` to avoid a refetch.

    Returns:
        The quantity, or None when the position book could not be read.
    """
    if book is None:
        book = position_map(client)
    if book is None:
        return None
    return book.get(symbol, 0)


def reconcile(client, state: dict) -> dict:
    """Positions are the source of truth. The state file only carries what
    positions cannot tell us: the entry credit and the entry VIX."""
    if not state:
        return {}
    book = position_map(client)
    if book is None:
        log("position book unreadable; keeping the recorded position untouched")
        return state
    ce_q, pe_q = book.get(state["ce"], 0), book.get(state["pe"], 0)
    if ce_q == 0 and pe_q == 0:
        log("state file describes a position that no longer exists, clearing")
        clear_state()
        return {}
    if ce_q != pe_q:
        log(f"WARNING: legs are unbalanced (CE {ce_q}, PE {pe_q}). "
            "Taking no automated action - resolve by hand.")
        state["unbalanced"] = True
    state["ce_qty"], state["pe_qty"] = ce_q, pe_q
    return state


# =============================================================================
# ACTIONS
# =============================================================================

def enter(client, state: dict) -> dict:
    today = datetime.now(IST)
    if ENTRY_WEEKDAY is not None and today.weekday() != ENTRY_WEEKDAY:
        log(f"today is {calendar.day_name[today.weekday()]}, not the "
            f"{calendar.day_name[ENTRY_WEEKDAY]} entry day; not entering")
        return state

    expiry = nearest_weekly(client)
    if not expiry:
        return state
    dte = (parse_expiry(expiry).date() - today.date()).days
    if dte < MIN_DTE_TO_ENTER:
        log(f"nearest weekly {expiry} is {dte} days out, below "
            f"MIN_DTE_TO_ENTER={MIN_DTE_TO_ENTER}; waiting for the next cycle")
        return state

    atm = resolve_atm(client, expiry)
    if not atm:
        return state
    vix = read_vix(client)
    if vix is None:
        return state

    regime = "calm" if vix < VIX_REGIME_THRESHOLD else "stressed"
    lots = size_position(client, atm)
    if lots < 1:
        log("sized to zero lots, not entering")
        return state
    qty = lots * atm["lotsize"]
    split = atm["freeze_qty"] or 0

    log(f"ENTRY {expiry} dte={dte} spot={atm['spot']:.1f} VIX={vix:.2f} "
        f"regime={regime} lots={lots} qty={qty}")
    log(f"  CE {atm['ce']}   PE {atm['pe']}")

    if DRY_RUN:
        log("DRY_RUN: no order placed")
        return state

    legs = [{"offset": "ATM", "option_type": opt, "action": "SELL",
             "quantity": qty, "product": PRODUCT, "splitsize": split}
            for opt in ("CE", "PE")]
    resp = client.optionsmultiorder(strategy=STRATEGY_TAG, underlying=UNDERLYING,
                                    exchange=INDEX_EXCHANGE, expiry_date=expiry,
                                    legs=legs)
    log(f"  order response: {resp}")

    # The envelope says success even when an individual leg failed.
    results = (resp or {}).get("results", [])
    failed = [r for r in results if r.get("status") != "success"]
    if not ok(resp) or failed or len(results) != 2:
        log(f"ENTRY INCOMPLETE - {len(failed)} leg(s) failed. Not recording state; "
            "check the position book by hand.")
        return state

    time.sleep(2)
    prices = leg_prices(client, atm["ce"], atm["pe"])
    credit = sum(prices) if prices else 0.0
    state = {"expiry": expiry, "ce": atm["ce"], "pe": atm["pe"],
             "lotsize": atm["lotsize"], "lots": lots, "qty": qty,
             "freeze_qty": split, "entry_vix": vix, "regime": regime,
             "credit": credit, "entered": datetime.now(IST).isoformat(),
             "hedged_on": None}
    save_state(state)
    log(f"  entered. credit={credit:.2f} pts, stop at {credit * STOP_MULT:.2f}")
    return state


def exit_legs(client, state: dict, reason: str) -> dict:
    log(f"EXIT ({reason}) {state['ce']} / {state['pe']}")
    if DRY_RUN:
        log("DRY_RUN: no order placed")
        return state

    split = state.get("freeze_qty") or 0
    book = position_map(client)
    if book is None:
        log("  position book unreadable; refusing to exit blind, retrying next cycle")
        return state
    for key in ("ce", "pe"):
        symbol = state[key]
        qty = abs(book.get(symbol, 0))
        if qty == 0:
            log(f"  {symbol}: already flat")
            continue
        # Chunk at the exchange freeze limit; a stop must be able to flatten any
        # size, so this never inherits the entry cap.
        chunks = []
        remaining = qty
        step = split if split else qty
        while remaining > 0:
            take = min(step, remaining)
            chunks.append(take)
            remaining -= take
        for c in chunks:
            r = client.placeorder(strategy=STRATEGY_TAG, symbol=symbol, action="BUY",
                                  exchange=FO_EXCHANGE, price_type="MARKET",
                                  product=PRODUCT, quantity=str(c))
            log(f"  BUY {symbol} x{c}: {r.get('status')} {r.get('orderid', '')}")
            time.sleep(0.3)

    flatten_hedge(client, state)
    clear_state()
    return {}


def flatten_hedge(client, state: dict) -> None:
    fut = state.get("fut")
    if not fut:
        return
    q = live_qty(client, fut)
    if q is None:
        log(f"  cannot read the hedge position for {fut}; leaving it in place")
        return
    if q == 0:
        return
    log(f"  flattening hedge {fut} ({q})")
    if DRY_RUN:
        return
    client.placesmartorder(strategy=STRATEGY_TAG, symbol=fut, exchange=FO_EXCHANGE,
                           action="SELL" if q > 0 else "BUY", product=PRODUCT,
                           quantity=str(abs(q)), position_size="0")


def rehedge(client, state: dict) -> dict:
    """Neutralise net delta with near-month futures. Calm regime only."""
    if state.get("regime") != "calm":
        return state
    today = datetime.now(IST).date().isoformat()
    if state.get("hedged_on") == today:
        return state

    deltas = {}
    for key in ("ce", "pe"):
        g = client.optiongreeks(symbol=state[key], exchange=FO_EXCHANGE,
                                underlying_symbol=UNDERLYING,
                                underlying_exchange=INDEX_EXCHANGE)
        if not ok(g) or "greeks" not in g:
            log(f"greeks failed for {state[key]}: {g}")
            return state
        # optiongreeks returns greeks at the top level, not nested under "data",
        # unlike funds/margin/positionbook. Its spot_price is the per-expiry
        # synthetic future, which is the right forward for a Black-76 delta.
        deltas[key] = float(g["greeks"]["delta"])
        time.sleep(2.5)   # greeks endpoint is rate limited to 30/min

    qty = state["qty"]
    net_delta = -(deltas["ce"] * qty + deltas["pe"] * qty)   # we are short both
    fut = state.get("fut") or near_month_future(client)
    if not fut:
        return state
    state["fut"] = fut

    fut_lot = state["lotsize"]
    target = int(round(-net_delta / fut_lot)) * fut_lot
    current = live_qty(client, fut)
    if current is None:
        log("  cannot read the futures position; skipping this hedge")
        return state
    log(f"HEDGE deltaCE={deltas['ce']:+.3f} deltaPE={deltas['pe']:+.3f} "
        f"net={net_delta:+.1f} -> target {target} (currently {current})")

    if target != current and not DRY_RUN:
        diff = target - current
        r = client.placesmartorder(strategy=STRATEGY_TAG, symbol=fut,
                                   exchange=FO_EXCHANGE,
                                   action="BUY" if diff > 0 else "SELL",
                                   product=PRODUCT, quantity=str(abs(diff)),
                                   position_size=str(target))
        log(f"  hedge order: {r.get('status')} {r.get('orderid', '')}")

    state["hedged_on"] = today
    save_state(state)
    return state


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    log("=" * 62)
    log(f"{STRATEGY_TAG} starting")
    log(f"  stop {STOP_MULT}x credit | VIX regime threshold {VIX_REGIME_THRESHOLD}")
    log(f"  capital Rs {CAPITAL:,} | max {MAX_LOTS} lot(s) | kill switch Rs {MAX_LOSS:,}")
    log(f"  DRY_RUN={DRY_RUN}  LIVE_OVERRIDE={I_UNDERSTAND_THIS_IS_LIVE}")
    log("=" * 62)

    client = build_client()
    if not assert_paper_mode(client):
        sys.exit(1)

    try:
        state = reconcile(client, load_state())
    except Exception as exc:  # noqa: BLE001 - a startup blip must not kill the run
        log(f"startup reconcile failed ({exc!r}); using the state file as recorded")
        state = load_state()
    if state:
        log(f"resumed cycle: {state['ce']} / {state['pe']} "
            f"credit={state.get('credit', 0):.2f} regime={state.get('regime')}")
    else:
        log("starting flat")

    halted = False
    last_act = 0.0

    while not _shutdown:
        try:
            if time.time() - last_act < ACT_EVERY_SECONDS:
                time.sleep(POLL_SECONDS)
                continue
            last_act = time.time()

            if not assert_paper_mode(client):
                break
            if halted:
                time.sleep(POLL_SECONDS)
                continue
            if kill_switch_tripped(client):
                if state:
                    state = exit_legs(client, state, "kill switch")
                # Halt only once actually flat. An exit that could not be sent
                # must be retried, not sealed behind the halt flag.
                if not state:
                    halted = True
                continue

            hm = now_hm()
            state = reconcile(client, state)

            if not state:
                if ENTRY_START <= hm <= ENTRY_END:
                    state = enter(client, state)
                time.sleep(POLL_SECONDS)
                continue

            if state.get("unbalanced"):
                time.sleep(POLL_SECONDS)
                continue

            prices = leg_prices(client, state["ce"], state["pe"])
            if prices:
                value = sum(prices)
                credit = float(state.get("credit") or 0)
                stop = credit * STOP_MULT
                log(f"mark: straddle {value:.2f} vs credit {credit:.2f} "
                    f"(stop {stop:.2f})  P&L {credit - value:+.2f} pts")
                if credit > 0 and value >= stop:
                    state = exit_legs(client, state, f"stop {STOP_MULT}x")
                    continue

            expiry_day = parse_expiry(state["expiry"]).date() == datetime.now(IST).date()
            if expiry_day and hm >= EXIT_AT:
                state = exit_legs(client, state, "expiry")
                continue

            if hm >= HEDGE_AT and not expiry_day:
                state = rehedge(client, state)

        except KeyboardInterrupt:
            break
        except Exception as exc:
            log(f"ERROR: {type(exc).__name__}: {exc}")

        time.sleep(POLL_SECONDS)

    log(f"{STRATEGY_TAG} stopped")


if __name__ == "__main__":
    main()
