"""BANKNIFTY CPR price action on 5-minute candles, 200-pt ITM monthly options. LIVE from 2026-11-11.

LIVE from 2026-11-11 (DRY_RUN = False, user 2026-11-09): REAL ORDERS are sent.
With DRY_RUN = True every order is only resolved and logged. Signals are read off
BANKNIFTY spot (NSE_INDEX); the order goes to the nearest monthly NFO option
ITM_POINTS (200) in the money - a CE for a long, a PE for a short. Every stop
and target is in INDEX points.

LEVELS, from the previous session's high H, low L and close C:
    P  = (H + L + C) / 3      BC = (H + L) / 2      TC = 2P - BC   (TC >= BC)
    R1 = 2P - L   S1 = 2P - H   R2 = P + (H - L)   S2 = P - (H - L)
    R3 = R1 + (H - L)           S3 = S1 - (H - L)
The ladder is S3 S2 S1 [CPR] R1 R2 R3; the CPR rung is the BC..TC band.

DAY ZONE, from the 09:15 open: inside (within the CPR band +- 0.15% of P),
CPRR1 (above it, up to R1), S1CPR (below it, down to S1), R1R2, S1S2,
aboveR2, belowS2.

SIGNALS (user's rules 2026-11-06, tuned the same day; see EVIDENCE):
    rej   RESISTANCE at a rung, approached from below: a candle whose high
          reaches the rung (within TOUCH), closes back below it after a close
          below it, upper wick >= WICK of its range, close in its bottom
          (1 - CLOSE_HALF). Confirmed when the NEXT candle closes below the
          signal candle's low -> SHORT at that close. Stop: signal high +
          STOP_BUF. Target: the next rung down (R1 -> TC).
    sup   SUPPORT, the mirror -> LONG, target the next rung up (S1 -> BC).
    bo    SUCCESSFUL BREAKOUT: a candle closes BO_MARGIN beyond a rung with a
          body >= BO_BODY of its range in the breakout direction, after a close
          on the near side. Within RETEST_BARS candles price returns to the
          rung (within TOUCH) without a close back through it -> enter at that
          close. Stop: the retest extreme or the rung, whichever is further,
          + BO_BUF. Target: the next rung beyond (R1 -> R2).
    A trade is skipped if reward < MIN_RR (BO_RR for breakouts) x risk, or
    risk > MAX_RISK of price.

TIERS, checked in this order on every bar (one position at a time):
    strict  the original rules: R1 rejection, S1 support, R1 breakout, S1
            breakdown, tight filters, only on CPRR1 / S1CPR days.
    wide    the same family on more rungs with looser filters, any day:
            rej R1 S2, sup S1 R2, bo up S1 R1, bo down R1 S1.
    extra   the wide setups looser still, entries to 14:55, only when the
            entry candle's body is <= 30% of its range (see EXTRA TIER).
    Each setup trades at most once a day per tier. At most MAX_TRADES_PER_DAY.
    Entries on candles starting NO_NEW_ENTRY_BEFORE..NO_NEW_ENTRY_AFTER, none
    in NO_ENTRY_WINDOWS (13:00-13:55), none after a stop has been hit today,
    none at all on days whose zone is in SKIP_ZONES (S1S2, aboveR2, belowS2).
    Exits: stop or target on the index (polled every few seconds, and caught
    on the bar if a poll missed it); everything closed at SQUARE_OFF.

The signal engine (replay) is a pure function over today's completed candles;
the backtest imports it from THIS file, so live and backtest cannot drift. Each
bar the whole day is replayed and an entry is taken only when the replay enters
on the bar that just closed and the strategy is flat.

SAFETY: same scaffolding as banknifty_2candle_3m: a resting broker stop-limit
backstop sized off the option's Greeks, cancelled before every exit; a manual
exit at the broker is noticed and booked out; I_UNDERSTAND_THIS_IS_LIVE gates
real orders outside analyzer mode.

EVIDENCE: this file's replay() over 5m BANKNIFTY 2016-01-04..2026-11-06,
index points, 5 pts cost a trade, stops/targets filled at their level,
tuned on 2016-2022 and checked on 2023+:

                     trades  days/wk  win    PF    net pts  maxDD
    all                1279     2.09  52.6%  1.26  +18,484  -2,384
    2016-2022 (tuned)   886     2.20  51.5%  1.24  +10,504
    2023+ (unseen)      393     1.92  55.2%  1.31   +7,980  -1,693
    strict tier         152           52.0%  1.82   +5,935
    wide tier          1127           52.7%  1.20  +12,549

Every year positive except 2022 (-137) and 2025 (+52, flat). At 10 pts of
cost a trade PF is 1.17, at 15 pts 1.07: the wide tier is thin, so slippage
matters. The strict tier alone (0.4 days/wk) was PF 1.93 with every year
positive; the wide tier exists only to trade 2+ days a week (user, 2026-11-06).
Tested and dropped: every setup at the CPR band itself (support, rejection,
breakouts; PF 0.35-0.9), support/resistance at the CPR on gap days (the
user's original gap rule; PF 0.3-0.9), breakouts without a retest (fail on
2023+), S2/S3 support, R2 rejection. All numbers in this docstring come from
running this file's replay() day by day over 5m BANKNIFTY history.

STOP PLACEMENT, tested 2026-11-06 on this engine (wide tier varied; strict
tier insensitive). The signal-candle stop with a 0.3% buffer (~150 pts) is
kept; its neighbours 0.25%/0.35% land within 0.02 PF, so it is not a spike:

                                 days/wk  win    PF    2023+ PF  cost10 PF
    candle + 0.3% (kept)          2.09   52.6%  1.26   1.31      1.17
    candle + 0.1%                 2.76   48.5%  1.16   1.08      1.05
    candle + 0.5%                 1.07   49.4%  1.32   1.43      1.22
    beyond the rung + 0.2%        2.02   51.2%  1.23   1.25      1.13
    0.3 x prior day range         2.32   54.1%  1.17   1.12      1.06
    breakeven at 50% to target    2.09   38.8%  1.22   1.20      1.10

Tighter stops add trades but get run on noise; wider ones fail the reward/
risk check and cut the trade count below 2 days a week. Moving to breakeven
cuts the win rate from 53% to 39% for no gain. stop_mode / stop_rng / be_at
stay in Tier for the record; the defaults are the kept rule.

REVISION 2026-11-07 (good vs bad month review). The one difference between
winning and losing months that held in BOTH 2016-2022 and 2023+ was the entry
hour: 13:00-13:55 entries lost in each (PF 0.75 / 0.70), on every setup.
Volatility, CPR width, gap and trend splits flipped sign between the halves
and were not used. Changes: no entries 13:00-13:55; no new entry after a stop
the same day (cuts maxDD); no trading on days opening S1S2 / aboveR2 /
belowS2 (SKIP_ZONES: PF <= 1.0 in both halves; levels far from the open
price stop working). The user accepted < 2 days/wk for quality. Tried and
dropped: entries to 14:55 and wick 0.25 (only kept frequency, lost in 2026),
time stops, month loss caps and pauses after losing streaks (trades after
losses did BETTER, so pausing cuts the recoveries), volatility filters.

                     trades  days/wk  win    PF    net pts  maxDD   cost10 PF
    before (above)     1279     2.09  52.6%  1.26  +18,484  -2,384  1.17
    revised             916     1.62  55.8%  1.47  +21,930  -1,724  1.36
      2016-2022         630           54.9%  1.41  +11,930
      2023+             286           57.7%  1.56  +10,000
No losing year 2016-2026. Positive months 84 of 130 (was 82): losing months
remain part of this strategy, mostly small. 2026 to Nov 6: PF 2.05, +4,429,
one losing month (Aug, -2). Tables above this note describe the pre-revision
engine.

EXTRA TIER (2026-11-07). Looser copies of the wide setups (touch 0.8%, wick
0.2, min_rr 0.4, entries to 14:55) lose on their own (PF ~0.9). They are
taken only when the ENTRY candle's body in the trade direction is <= 30% of
its range: a big candle means the entry is far from the level (chasing).
Body limit vs result is smooth (0.2: PF 1.51 / 1.75 d/wk, 0.3: 1.48 / 1.83,
0.5: 1.36 / 2.03, none: 1.25 / 2.26), so 0.3 is not a spike. The same check
on the strict/wide tiers does not separate winners. Adding R2/S2 rejection or
CPR-band setups to the extra tier, even with the check, lowered PF.

                     trades  days/wk  win    PF    net pts  maxDD
    without extra       916     1.63  55.8%  1.47  +21,930  -1,724
    with extra         1047     1.83  56%    1.48  +24,631  -1,428
      2016-2022         715           55%    1.39  +12,649
      2023+             332           59%    1.63  +11,982
    2026 to Nov 6        82           63%    2.12   +4,954  (1 losing month)

Note on logging: the strategy host captures stdout to log/strategies/, so
print() is the logging channel here (host contract).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

# =============================================================================
# CONFIGURATION
# =============================================================================

STRATEGY_TAG = "BANKNIFTY_CPR_PA"
UNDERLYING = "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
FO_EXCHANGE = "NFO"

I_UNDERSTAND_THIS_IS_LIVE = True    # False refuses real orders outside analyzer mode
DRY_RUN = False                     # live from 2026-11-11 (user, 2026-11-09); True logs orders and places nothing
PRODUCT = "MIS"
LOTS = 1
ITM_POINTS = 200                    # strike this far in the money
BROKER_STOP = True
BROKER_STOP_IV_POINTS = 2.0
BROKER_STOP_LIMIT_SLIP = 0.05
TICK = 0.05
EXIT_RETRY_SECONDS = 20
ORDER_POLL_TRIES = 10
ORDER_POLL_SECONDS = 1.0
CANCEL_CONFIRM_TRIES = 6
CANCEL_CONFIRM_SECONDS = 0.5
MANUAL_EXIT_RECHECK_SECONDS = 2.0
COST_PTS = 5.0                      # assumed round-trip cost in the backtest

BAR_MINUTES = 5
INSIDE_TOL = 0.0015                 # "inside the CPR" band for the day zone, fraction of P
MAX_TRADES_PER_DAY = 2
SESSION_OPEN = dtime(9, 15)
NO_NEW_ENTRY_BEFORE = dtime(9, 20)  # signal candles starting from here
NO_NEW_ENTRY_AFTER = dtime(14, 30)  # ...up to here
NO_ENTRY_WINDOWS = ((dtime(13, 0), dtime(13, 55)),)   # no entries on candles starting in these
STOP_AFTER_LOSS = True              # no new entry once a trade has hit its stop today
SKIP_ZONES = frozenset({"S1S2", "aboveR2", "belowS2"})   # no trading on days opening here; empty = trade all
SQUARE_OFF = dtime(15, 10)
PRIOR_DAYS = 10                     # calendar days searched for the previous session

BAR_SECONDS = BAR_MINUTES * 60
BAR_DELAY = 6
STOP_POLL_SECONDS = 5
HISTORY_PACE = 0.4

IST = ZoneInfo("Asia/Kolkata")
STATE_DIR = Path("strategies") / "state"
STATE_FILE = STATE_DIR / "banknifty_cpr_price_action.json"

RUNGS = ("S3", "S2", "S1", "CPR", "R1", "R2", "R3")


@dataclass(frozen=True)
class Tier:
    name: str
    setups: frozenset
    zones: frozenset | None         # day zones this tier trades; None = all
    touch: float                    # a test reaches within this fraction of P of the rung
    wick: float                     # rejection/support wick >= this share of the range
    close_half: float               # close at least this far from the rung end of the range
    stop_buf: float                 # rejection/support stop beyond the signal extreme
    min_rr: float
    bo_margin: float                # breakout close beyond the rung by this fraction of P
    bo_body: float
    retest_bars: int
    bo_buf: float
    bo_rr: float
    max_risk: float = 0.006
    stop_mode: str = "candle"       # candle (signal/retest extreme) | level (the rung) | range | wider
    stop_rng: float = 0.25          # range mode: stop this share of the prior day's range from entry
    be_at: float | None = None      # stop to entry once the trade has run this share of the way to target
    entry_body: float | None = None  # confirmation: the entry candle's body in the trade direction must be
    #                                  at most this share of its range (no chasing); None = no check
    last_entry: dtime | None = None  # this tier's own entry cutoff; None = NO_NEW_ENTRY_AFTER


TIERS = (
    Tier("strict", frozenset({"rej_R1", "sup_S1", "bo_up_R1", "bo_dn_S1"}), frozenset({"CPRR1", "S1CPR"}),
         touch=0.0008, wick=0.4, close_half=0.67, stop_buf=0.002, min_rr=1.0,
         bo_margin=0.0015, bo_body=0.4, retest_bars=3, bo_buf=0.002, bo_rr=0.5),
    Tier("wide", frozenset({"rej_R1", "sup_S1", "bo_up_S1", "bo_dn_R1", "bo_up_R1", "bo_dn_S1",
                            "sup_R2", "rej_S2"}), None,
         touch=0.004, wick=0.3, close_half=0.67, stop_buf=0.003, min_rr=0.5,
         bo_margin=0.0015, bo_body=0.4, retest_bars=3, bo_buf=0.002, bo_rr=0.5),
    # the wide setups again, looser, but only entered off a small entry candle (2026-11-07)
    Tier("extra", frozenset({"rej_R1", "sup_S1", "bo_up_S1", "bo_dn_R1", "bo_up_R1", "bo_dn_S1",
                             "sup_R2", "rej_S2"}), None,
         touch=0.008, wick=0.2, close_half=0.67, stop_buf=0.003, min_rr=0.4,
         bo_margin=0.0015, bo_body=0.4, retest_bars=3, bo_buf=0.002, bo_rr=0.5,
         entry_body=0.3, last_entry=dtime(14, 55)),
)

_shutdown = False
_levels: dict = {}                  # {"date": today, "lv": levels}


# =============================================================================
# SIGNAL ENGINE  (pure; imported by the backtest)
# =============================================================================

def cpr_levels(high: float, low: float, close: float) -> dict:
    """CPR ladder from one session's high, low and close."""
    p = (high + low + close) / 3.0
    bc = (high + low) / 2.0
    tc = 2.0 * p - bc
    rng = high - low
    r1, s1 = 2.0 * p - low, 2.0 * p - high
    return {"P": p, "TC": max(tc, bc), "BC": min(tc, bc), "R1": r1, "S1": s1,
            "R2": p + rng, "S2": p - rng, "R3": r1 + rng, "S3": s1 - rng, "RNG": rng}


def zone_of(day_open: float, lv: dict) -> str:
    tol = INSIDE_TOL * lv["P"]
    if lv["BC"] - tol <= day_open <= lv["TC"] + tol:
        return "inside"
    if day_open > lv["R2"]:
        return "aboveR2"
    if day_open > lv["R1"]:
        return "R1R2"
    if day_open > lv["TC"]:
        return "CPRR1"
    if day_open < lv["S2"]:
        return "belowS2"
    if day_open < lv["S1"]:
        return "S1S2"
    return "S1CPR"


def _stop_for(tr: Tier, long: bool, entry: float, candle_stop: float, lvl: float, buf: float, day_rng: float) -> float:
    """Stop level by the tier's stop_mode; candle_stop already carries the buffer."""
    level_stop = lvl * (1 - buf) if long else lvl * (1 + buf)
    if tr.stop_mode == "level":
        return level_stop
    if tr.stop_mode == "range":
        return entry - tr.stop_rng * day_rng if long else entry + tr.stop_rng * day_rng
    if tr.stop_mode == "wider":
        return min(candle_stop, level_stop) if long else max(candle_stop, level_stop)
    return candle_stop


def _try_entry(tr: Tier, sid: str, long: bool, entry: float, stop: float, target: float, i: int):
    risk = (entry - stop) if long else (stop - entry)
    reward = (target - entry) if long else (entry - target)
    rr = tr.bo_rr if sid.startswith("bo") else tr.min_rr
    if risk <= 0 or reward <= 0 or reward < rr * risk or risk > tr.max_risk * entry:
        return None
    return {"tier": tr.name, "setup": sid, "long": long, "entry": entry, "stop": stop,
            "target": target, "risk": risk, "i": i, "be_at": tr.be_at}


def _tier_step(tr: Tier, st: dict, i: int, o, h, l, c, lo: dict, hi: dict, p: float, day_rng: float):  # noqa: E741
    """One tier on bar i: armed retests, then the pending confirmation, then new signals."""
    tch = tr.touch * p
    for sid, (long, lvl, tgt, exp) in list(st["armed"].items()):
        if i > exp or ((c[i] < lvl) if long else (c[i] > lvl)):
            del st["armed"][sid]
        elif (l[i] <= lvl + tch) if long else (h[i] >= lvl - tch):
            del st["armed"][sid]
            stop = min(l[i], lvl) * (1 - tr.bo_buf) if long else max(h[i], lvl) * (1 + tr.bo_buf)
            stop = _stop_for(tr, long, c[i], stop, lvl, tr.bo_buf, day_rng)
            e = _try_entry(tr, sid, long, c[i], stop, tgt, i)
            if e:
                return e
    if st["pending"] is not None:
        sid, long, stop, tgt, ext, lvl = st["pending"]
        st["pending"] = None
        if (c[i] > ext) if long else (c[i] < ext):
            stop = _stop_for(tr, long, c[i], stop, lvl, tr.stop_buf, day_rng)
            e = _try_entry(tr, sid, long, c[i], stop, tgt, i)
            if e:
                return e
    rng = h[i] - l[i]
    if rng <= 0:
        return None
    body = abs(c[i] - o[i])
    m = tr.bo_margin * p
    for k, rung in enumerate(RUNGS):
        lo_l, hi_l = lo[rung], hi[rung]
        below = hi[RUNGS[k - 1]] if k > 0 else None
        above = lo[RUNGS[k + 1]] if k + 1 < len(RUNGS) else None
        sid = f"rej_{rung}"
        if (below is not None and sid in tr.setups and sid not in st["used"] and st["pending"] is None
                and h[i] >= lo_l - tch and c[i] < lo_l and c[i - 1] < lo_l
                and h[i] - max(o[i], c[i]) >= tr.wick * rng
                and c[i] - l[i] <= (1 - tr.close_half) * rng):
            st["pending"] = (sid, False, h[i] * (1 + tr.stop_buf), below, l[i], lo_l)
        sid = f"sup_{rung}"
        if (above is not None and sid in tr.setups and sid not in st["used"] and st["pending"] is None
                and l[i] <= hi_l + tch and c[i] > hi_l and c[i - 1] > hi_l
                and min(o[i], c[i]) - l[i] >= tr.wick * rng
                and h[i] - c[i] <= (1 - tr.close_half) * rng):
            st["pending"] = (sid, True, l[i] * (1 - tr.stop_buf), above, h[i], hi_l)
        sid = f"bo_up_{rung}"
        if (above is not None and sid in tr.setups and sid not in st["used"]
                and c[i - 1] <= hi_l and c[i] > hi_l + m and body >= tr.bo_body * rng and c[i] > o[i]):
            st["armed"][sid] = (True, hi_l, above, i + tr.retest_bars)
        sid = f"bo_dn_{rung}"
        if (below is not None and sid in tr.setups and sid not in st["used"]
                and c[i - 1] >= lo_l and c[i] < lo_l - m and body >= tr.bo_body * rng and c[i] < o[i]):
            st["armed"][sid] = (False, lo_l, below, i + tr.retest_bars)
    return None


def replay(minutes, o, h, l, c, lv: dict, tiers=TIERS) -> dict:  # noqa: E741
    """Run one session's completed candles through the rules.

    Args:
        minutes: candle start, minutes after midnight, per candle.
        o, h, l, c: candle prices; index 0 is the 09:15 candle.
        lv: cpr_levels of the previous session.

    Returns:
        {"zone", "trades": [entry dict + exit_i/exit/why]}. The last trade may
        still be open (no exit_i), as at the bar that just closed.
    """
    zone = zone_of(o[0], lv)
    if zone in SKIP_ZONES:
        return {"zone": zone, "trades": []}
    p = lv["P"]
    lo = {"S3": lv["S3"], "S2": lv["S2"], "S1": lv["S1"], "CPR": lv["BC"],
          "R1": lv["R1"], "R2": lv["R2"], "R3": lv["R3"]}
    hi = dict(lo, CPR=lv["TC"])
    states = {tr.name: {"pending": None, "armed": {}, "used": set()} for tr in tiers}
    ef = NO_NEW_ENTRY_BEFORE.hour * 60 + NO_NEW_ENTRY_BEFORE.minute
    et = NO_NEW_ENTRY_AFTER.hour * 60 + NO_NEW_ENTRY_AFTER.minute
    tier_et = {tr.name: (tr.last_entry.hour * 60 + tr.last_entry.minute) if tr.last_entry else et for tr in tiers}
    et_all = max(tier_et.values())
    sq = SQUARE_OFF.hour * 60 + SQUARE_OFF.minute - BAR_MINUTES   # that candle closes AT square-off
    blocked = [(a.hour * 60 + a.minute, b.hour * 60 + b.minute) for a, b in NO_ENTRY_WINDOWS]
    trades, pos, lost = [], None, False
    for i in range(1, len(c)):
        if pos is not None:
            long, stop, tgt = pos["long"], pos["stop"], pos["target"]
            res = None
            if long:
                if o[i] <= stop:
                    res = (o[i], "stop")
                elif l[i] <= stop:
                    res = (stop, "stop")
                elif o[i] >= tgt:
                    res = (o[i], "target")
                elif h[i] >= tgt:
                    res = (tgt, "target")
            else:
                if o[i] >= stop:
                    res = (o[i], "stop")
                elif h[i] >= stop:
                    res = (stop, "stop")
                elif o[i] <= tgt:
                    res = (o[i], "target")
                elif l[i] <= tgt:
                    res = (tgt, "target")
            if res is None and minutes[i] >= sq:
                res = (c[i], "square-off")
            if res is not None:
                pos.update(exit_i=i, exit=res[0], why=res[1])
                lost = lost or res[1] == "stop"
                pos = None
            elif pos.get("be_at") and not pos.get("be"):
                # judged on this bar, applies from the next one
                run = (h[i] - pos["entry"]) if long else (pos["entry"] - l[i])
                if run >= pos["be_at"] * abs(tgt - pos["entry"]):
                    pos["stop"], pos["be"] = pos["entry"], True
            continue
        if (len(trades) >= MAX_TRADES_PER_DAY or not (ef <= minutes[i] <= et_all)
                or (STOP_AFTER_LOSS and lost) or any(a <= minutes[i] <= b for a, b in blocked)):
            for st in states.values():
                st["pending"], st["armed"] = None, {}
            continue
        entry = None
        for tr in tiers:
            if (tr.zones is not None and zone not in tr.zones) or minutes[i] > tier_et[tr.name]:
                continue
            e = _tier_step(tr, states[tr.name], i, o, h, l, c, lo, hi, p, lv["RNG"])
            if e and tr.entry_body is not None:
                rg = h[i] - l[i]
                body = ((c[i] - o[i]) if e["long"] else (o[i] - c[i])) / rg if rg > 0 else 0.0
                if body > tr.entry_body:      # chased candle: drop the setup for the day
                    states[tr.name]["used"].add(e["setup"])
                    continue
            if e:
                entry = e
                break
        if entry:
            for st in states.values():
                st["pending"], st["armed"] = None, {}
            states[entry["tier"]]["used"].add(entry["setup"])
            pos = entry
            trades.append(entry)
    return {"zone": zone, "trades": trades}


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
    seen = set()
    candidates = [Path.cwd() / ".env"] + [parent / ".env" for parent in Path(__file__).resolve().parents]
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
            if name.strip() == "OPENALGO_API_KEY":
                value = value.strip().strip("'\"").strip()
                if value:
                    return value
    return None


def build_client():
    from openalgo import api
    key = os.getenv("OPENALGO_API_KEY") or _api_key_from_env_file()
    if not key:
        log("FATAL: OPENALGO_API_KEY is neither in the environment nor in any .env")
        sys.exit(1)
    return api(api_key=key, host=os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000"))


def ok(resp) -> bool:
    return isinstance(resp, dict) and resp.get("status") == "success"


# =============================================================================
# MARKET DATA
# =============================================================================

def history_1m(client, start: str, end: str, retries: int = 3) -> pd.DataFrame:
    """BANKNIFTY 1m bars in IST, session hours only; empty when unavailable."""
    for attempt in range(1, retries + 1):
        try:
            result = client.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval="1m",
                                    start_date=start, end_date=end)
        except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
            log(f"history raised {exc!r}")
            result = None
        if isinstance(result, pd.DataFrame) and not result.empty:
            frame = result[~result.index.duplicated(keep="last")].sort_index()
            frame = frame.tz_localize(IST) if frame.index.tz is None else frame.tz_convert(IST)
            return frame[(frame.index.time >= SESSION_OPEN) & (frame.index.time <= dtime(15, 29))]
        time.sleep(HISTORY_PACE * (2 ** attempt))
    log(f"history: no 1m bars after {retries} attempts")
    return pd.DataFrame()


def today_levels(client) -> dict | None:
    """CPR ladder from the previous session, fetched once a day."""
    today = datetime.now(IST).date()
    if _levels.get("date") == today:
        return _levels["lv"]
    bars = history_1m(client, str(today - timedelta(days=PRIOR_DAYS)), str(today - timedelta(days=1)))
    if bars.empty:
        return None
    bars = bars[bars.index.date < today]
    last_day = max(bars.index.date)
    prev = bars[bars.index.date == last_day]
    lv = cpr_levels(float(prev["high"].max()), float(prev["low"].min()), float(prev["close"].iloc[-1]))
    _levels.update(date=today, lv=lv, prev_day=str(last_day))
    log(f"levels from {last_day} (H {prev['high'].max():.2f} L {prev['low'].min():.2f} "
        f"C {prev['close'].iloc[-1]:.2f}): " + " ".join(f"{k} {lv[k]:.2f}" for k in
                                                      ("S3", "S2", "S1", "BC", "P", "TC", "R1", "R2", "R3")))
    return lv


def today_bars(client) -> pd.DataFrame:
    """Today's COMPLETED 5m candles, anchored at 09:15."""
    today = datetime.now(IST).date()
    one = history_1m(client, str(today), str(today))
    if one.empty:
        return one
    origin = pd.Timestamp(datetime.combine(today, SESSION_OPEN), tz=IST)
    bars = one.resample(f"{BAR_MINUTES}min", origin=origin, label="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    return bars[bars.index + timedelta(minutes=BAR_MINUTES) <= datetime.now(IST)]


def index_ltp(client) -> float | None:
    try:
        q = client.quotes(symbol=UNDERLYING, exchange=INDEX_EXCHANGE)
    except Exception as exc:  # noqa: BLE001
        log(f"index quote raised {exc!r}")
        return None
    if not ok(q):
        return None
    return float((q.get("data") or {}).get("ltp") or 0) or None


def option_ltp(client, symbol: str) -> float | None:
    try:
        q = client.quotes(symbol=symbol, exchange=FO_EXCHANGE)
    except Exception as exc:  # noqa: BLE001
        log(f"quote raised {exc!r} for {symbol}")
        return None
    if not ok(q):
        return None
    return float((q.get("data") or {}).get("ltp") or 0) or None


def nearest_expiry(client) -> str | None:
    """Nearest expiry AFTER today (a 0-DTE option decays to nothing over a hold)."""
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
    """Option ITM_POINTS in the money on the nearest expiry after today."""
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
    return {"symbol": symbol, "lotsize": int(r["lotsize"])}


# =============================================================================
# ORDERS
# =============================================================================

def assert_live_allowed(client) -> bool:
    st = None
    for attempt in range(1, 4):
        try:
            st = client.analyzerstatus()
        except Exception as exc:  # noqa: BLE001
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


def order_status(client, order_id: str) -> tuple[str, float | None]:
    try:
        r = client.orderstatus(order_id=order_id, strategy=STRATEGY_TAG)
    except Exception as exc:  # noqa: BLE001
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
    """Read a market order to complete / rejected / cancelled / unknown."""
    def terminal(status, price):
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
    except Exception as exc:  # noqa: BLE001
        log(f"cancel of unsettled order {order_id} raised {exc!r}")
    return terminal(*order_status(client, order_id)) or ("unknown", None)


def market_order(client, symbol: str, action: str, quantity: int) -> tuple[str, float | None]:
    if DRY_RUN:
        log(f"DRY_RUN {action} {quantity} {symbol}")
        return "complete", None
    try:
        r = client.placeorder(strategy=STRATEGY_TAG, symbol=symbol, action=action,
                              exchange=FO_EXCHANGE, price_type="MARKET",
                              product=PRODUCT, quantity=quantity)
    except Exception as exc:  # noqa: BLE001
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
    try:
        r = client.positionbook()
    except Exception as exc:  # noqa: BLE001
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
                return None
    return 0


def round_tick(value: float) -> float:
    return round(max(TICK, int(round(value / TICK, 6)) * TICK), 2)


def option_stop_price(client, symbol: str, index_entry: float, index_stop: float) -> float | None:
    """Premium level matching the index stop, widened by theta and vega into a backstop."""
    try:
        r = client.optiongreeks(symbol=symbol, exchange=FO_EXCHANGE)
    except Exception as exc:  # noqa: BLE001
        log(f"greeks raised {exc!r}; no broker stop for {symbol}")
        return None
    if not ok(r):
        log(f"greeks failed for {symbol}: {r}")
        return None
    body = r.get("data") if isinstance(r.get("data"), dict) else r
    greeks = body.get("greeks") or {}
    try:
        delta, gamma = float(greeks["delta"]), float(greeks["gamma"])
        theta, vega = float(greeks["theta"]), float(greeks["vega"])
        entry_premium = float(body["option_price"])
    except (KeyError, TypeError, ValueError):
        log(f"greeks unreadable for {symbol}: {body!r}")
        return None
    if delta == 0.0 or entry_premium <= 0:
        log(f"unusable greeks for {symbol}: delta {delta}, premium {entry_premium}")
        return None
    move = index_stop - index_entry
    mirror = entry_premium + delta * move + 0.5 * gamma * move ** 2
    now = datetime.now(IST)
    close = now.replace(hour=SQUARE_OFF.hour, minute=SQUARE_OFF.minute, second=0, microsecond=0)
    hours = max((close - now).total_seconds() / 3600.0, 0.0)
    level = mirror - abs(theta) * hours / 24.0 - abs(vega) * BROKER_STOP_IV_POINTS
    if level <= 0 or level >= entry_premium:
        log(f"mapped stop {level:.2f} is not below the {entry_premium:.2f} entry premium; "
            f"no broker stop for {symbol}")
        return None
    trigger = round_tick(level)
    log(f"broker stop for {symbol}: index {index_entry:.2f} -> {index_stop:.2f} maps to "
        f"{mirror:.2f} (delta {delta:+.4f}); widened to trigger {trigger:.2f}")
    return trigger


def place_broker_stop(client, symbol: str, quantity: int, trigger: float) -> str | None:
    limit = round_tick(trigger * (1.0 - BROKER_STOP_LIMIT_SLIP))
    if DRY_RUN:
        log(f"DRY_RUN broker stop: SELL {quantity} {symbol} trigger {trigger:.2f} limit {limit:.2f}")
        return "DRY"
    try:
        r = client.placeorder(strategy=STRATEGY_TAG, symbol=symbol, action="SELL",
                              exchange=FO_EXCHANGE, price_type="SL", product=PRODUCT,
                              quantity=quantity, price=limit, trigger_price=trigger)
    except Exception as exc:  # noqa: BLE001
        log(f"broker stop raised {exc!r} for {symbol}")
        return None
    if not ok(r):
        log(f"broker stop REJECTED for {symbol}: {r}")
        return None
    log(f"broker stop resting: {quantity} {symbol} trigger {trigger:.2f} limit {limit:.2f} -> {r.get('orderid')}")
    return r.get("orderid")


def stop_order_state(client, order_id: str) -> str:
    """'open', 'filled', 'gone' or 'unknown'."""
    if not order_id or order_id == "DRY":
        return "gone"
    try:
        r = client.orderstatus(order_id=order_id, strategy=STRATEGY_TAG)
    except Exception as exc:  # noqa: BLE001
        log(f"orderstatus raised {exc!r} for {order_id}")
        return "unknown"
    if not ok(r):
        return "unknown"
    data = r.get("data") or r
    raw = str(data.get("order_status") or data.get("status") or "").lower()
    if "complete" in raw or "filled" in raw or "executed" in raw:
        return "filled"
    if "cancel" in raw or "reject" in raw:
        return "gone"
    return "open" if raw else "unknown"


def _cancel_stop(client, order_id: str) -> bool:
    if not order_id or order_id == "DRY":
        return True
    try:
        return ok(client.cancelorder(order_id=order_id, strategy=STRATEGY_TAG))
    except Exception as exc:  # noqa: BLE001
        log(f"cancelorder raised {exc!r} for {order_id}")
        return False


def release_broker_stop(client, pos: dict) -> str:
    """Cancel the resting stop before a market exit: 'clear', 'filled' or 'blocked'."""
    order_id = pos.get("stop_order_id")
    if not order_id:
        return "clear"
    status = stop_order_state(client, order_id)
    if status in ("filled", "gone"):
        pos.pop("stop_order_id", None)
        return "filled" if status == "filled" else "clear"
    if status == "open" and _cancel_stop(client, order_id):
        seen = "unknown"
        for attempt in range(CANCEL_CONFIRM_TRIES):
            if attempt:
                time.sleep(CANCEL_CONFIRM_SECONDS)
            s = stop_order_state(client, order_id)
            if s in ("gone", "filled"):
                pos.pop("stop_order_id", None)
                return "filled" if s == "filled" else "clear"
            if s == "open":
                seen = "open"
        if seen == "unknown":
            pos.pop("stop_order_id", None)
            return "clear"
    log(f"cannot confirm resting stop {order_id} is gone; holding off the exit to avoid a double sell")
    return "blocked"


def drop_resting_stop(client, pos: dict, why: str) -> None:
    order_id = pos.pop("stop_order_id", None)
    if not order_id:
        return
    if _cancel_stop(client, order_id) or stop_order_state(client, order_id) in ("filled", "gone"):
        log(f"resting stop {order_id} cleared: {why}")
        return
    log(f"COULD NOT CANCEL resting stop {order_id} ({why}) - CANCEL IT BY HAND AT THE BROKER")


# =============================================================================
# POSITION
# =============================================================================

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (OSError, ValueError) as exc:
            log(f"state unreadable ({exc}), starting clean")
    return {}


def save_state(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str))
        os.replace(tmp, STATE_FILE)
    except (OSError, TypeError, ValueError) as exc:
        log(f"could not persist state ({exc}); carrying on in memory")


def fresh_day(state: dict, today: str) -> dict:
    if state.get("date") != today:
        state = {"date": today, "trades": 0, "position": None, "closed": [], "taken": []}
    return state


def book_exit(client, state: dict, price: float, why: str) -> None:
    pos = state["position"]
    long = pos["direction"] == "long"
    gross = (price - pos["entry"]) if long else (pos["entry"] - price)
    premium = pos.get("premium_out") or (option_ltp(client, pos["symbol"]) if pos.get("symbol") else None)
    opt = ""
    if premium is not None and pos.get("premium_in") is not None:
        d = premium - pos["premium_in"]
        opt = f"; option {pos['premium_in']:.2f} -> {premium:.2f} ({d:+.2f}, Rs {d * pos['lotsize'] * LOTS:+,.0f})"
    log(f"{'DRY' if DRY_RUN else 'LIVE'} EXIT {why}: {pos['setup']} ({pos['tier']}) {pos['direction']} "
        f"{pos['entry']:.2f} -> {price:.2f}, index {gross:+.2f} pts{opt}")
    state["closed"].append({"setup": pos["setup"], "tier": pos["tier"], "direction": pos["direction"],
                            "entry": pos["entry"], "exit": price, "why": why, "gross": round(gross, 2),
                            "premium_in": pos.get("premium_in"), "premium_out": premium,
                            "opened": pos["opened"], "closed": datetime.now(IST).isoformat()})
    state["position"] = None


def close_position(client, state: dict, price: float, why: str) -> bool:
    """Sell the option (cancelling the backstop first), then book the exit."""
    pos = state.get("position")
    if not pos:
        return True
    if not DRY_RUN:
        last = pos.get("exit_try_at")
        if last is not None and time.monotonic() - last < EXIT_RETRY_SECONDS:
            return False
        pos["exit_try_at"] = time.monotonic()
        outcome = release_broker_stop(client, pos)
        if outcome == "filled":
            book_exit(client, state, price, "broker-stop")
            return True
        if outcome == "blocked":
            return False
        status, fill = market_order(client, pos["symbol"], "SELL", pos["qty"])
        if status != "complete":
            if net_quantity(client, pos["symbol"]) == 0:
                log(f"broker reports no open {pos['symbol']}; it was closed elsewhere")
                drop_resting_stop(client, pos, "position closed elsewhere")
                book_exit(client, state, price, "closed-elsewhere")
                return True
            log(f"exit ({why}) order {status}; retrying after {EXIT_RETRY_SECONDS}s")
            return False
        if fill:
            pos["premium_out"] = fill
    else:
        market_order(client, pos["symbol"], "SELL", pos["qty"])
    book_exit(client, state, price, why)
    return True


def closed_outside(client, state: dict) -> bool:
    """A live position sold by hand at the broker is noticed and booked out."""
    pos = state.get("position")
    if DRY_RUN or not pos:
        return False
    qty = net_quantity(client, pos["symbol"])
    if qty is None or qty > 0:
        return False
    time.sleep(MANUAL_EXIT_RECHECK_SECONDS)
    qty = net_quantity(client, pos["symbol"])
    if qty is None or qty > 0:
        return False
    log(f"{pos['symbol']} was closed outside the strategy; clearing it")
    drop_resting_stop(client, pos, "closed outside the strategy")
    book_exit(client, state, index_ltp(client) or pos["entry"], "closed-outside")
    return True


def check_exit(client, state: dict, price: float, source: str) -> None:
    pos = state.get("position")
    if not pos:
        return
    long = pos["direction"] == "long"
    if (price <= pos["stop"]) if long else (price >= pos["stop"]):
        log(f"stop {pos['stop']:.2f} reached ({source} {price:.2f})")
        close_position(client, state, price, "stop")
    elif (price >= pos["target"]) if long else (price <= pos["target"]):
        log(f"target {pos['target']:.2f} reached ({source} {price:.2f})")
        close_position(client, state, price, "target")


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
                close_position(client, state, price, "square-off")
        return state

    lv = today_levels(client)
    if lv is None:
        log("previous session unavailable; no levels, retrying next bar")
        return state
    bars = today_bars(client)
    if len(bars) < 2:
        return state

    pos = state.get("position")
    if pos and not DRY_RUN and pos.get("stop_order_id") and stop_order_state(client, pos["stop_order_id"]) == "filled":
        log(f"resting stop {pos['stop_order_id']} filled at the broker")
        pos.pop("stop_order_id", None)
        book_exit(client, state, index_ltp(client) or pos["stop"], "broker-stop")
    if state.get("position") and closed_outside(client, state):
        pass
    if state.get("position"):
        # a bar that traded through the stop or target between polls
        last = bars.iloc[-1]
        p = state["position"]
        if bars.index[-1] >= pd.Timestamp(p["entry_bar"]):
            long = p["direction"] == "long"
            if (float(last["low"]) <= p["stop"]) if long else (float(last["high"]) >= p["stop"]):
                check_exit(client, state, index_ltp(client) or p["stop"], "bar")
            elif (float(last["high"]) >= p["target"]) if long else (float(last["low"]) <= p["target"]):
                check_exit(client, state, index_ltp(client) or p["target"], "bar")

    minutes = (bars.index.hour * 60 + bars.index.minute).to_numpy()
    r = replay(minutes, bars["open"].to_numpy(float), bars["high"].to_numpy(float),
               bars["low"].to_numpy(float), bars["close"].to_numpy(float), lv)
    last_i = len(bars) - 1
    last = bars.iloc[-1]
    head = (f"bar {bars.index[-1]:%H:%M} close {float(last['close']):.2f} zone {r['zone']} "
            f"| R1 {lv['R1']:.2f} TC {lv['TC']:.2f} BC {lv['BC']:.2f} S1 {lv['S1']:.2f}")
    sig = r["trades"][-1] if r["trades"] and r["trades"][-1]["i"] == last_i else None
    if state.get("position"):
        p = state["position"]
        log(f"{head} | in {p['setup']} {p['direction']} from {p['entry']:.2f}, stop {p['stop']:.2f}, "
            f"target {p['target']:.2f}")
        return state
    if not sig:
        log(f"{head} | flat")
        return state
    key = f"{bars.index[-1].isoformat()}|{sig['setup']}"
    if key in state["taken"] or state["trades"] >= MAX_TRADES_PER_DAY:
        log(f"{head} | {sig['setup']} signal already handled or day limit reached")
        return state
    if STOP_AFTER_LOSS and any(t["why"] in ("stop", "broker-stop") for t in state["closed"]):
        log(f"{head} | {sig['setup']} signal skipped: a stop was already hit today")
        return state
    state["taken"].append(key)
    long = sig["long"]
    spot = index_ltp(client) or sig["entry"]
    if ((spot <= sig["stop"]) or (spot >= sig["target"])) if long else ((spot >= sig["stop"]) or (spot <= sig["target"])):
        log(f"{head} | {sig['setup']} signal skipped: index {spot:.2f} already past stop or target")
        return state
    leg = resolve_leg(client, "CE" if long else "PE")
    if leg is None:
        log(f"{head} | {sig['setup']} signal skipped: option leg unresolved")
        return state
    qty = leg["lotsize"] * LOTS
    premium = option_ltp(client, leg["symbol"])
    status, fill = market_order(client, leg["symbol"], "BUY", qty)
    if status != "complete":
        held = net_quantity(client, leg["symbol"]) if status == "unknown" else 0
        if not held or held < qty:
            log(f"{head} | entry order {status}; staying flat")
            return state
    if fill:
        premium = fill
    side = "long" if long else "short"
    log(f"{'DRY' if DRY_RUN else 'LIVE'} {side.upper()} {sig['setup']} ({sig['tier']} tier, zone {r['zone']}): "
        f"index {spot:.2f} (bar close {sig['entry']:.2f}), stop {sig['stop']:.2f} (risk {abs(spot - sig['stop']):.2f}), "
        f"target {sig['target']:.2f}; {leg['symbol']} x{qty} premium {premium if premium else 'n/a'}")
    state["trades"] += 1
    state["position"] = {
        "setup": sig["setup"], "tier": sig["tier"], "direction": side, "entry": spot,
        "stop": sig["stop"], "target": sig["target"], "opened": now.isoformat(),
        "entry_bar": (bars.index[-1] + timedelta(minutes=BAR_MINUTES)).isoformat(),
        "symbol": leg["symbol"], "qty": qty, "lotsize": leg["lotsize"], "premium_in": premium,
    }
    if BROKER_STOP:
        trigger = option_stop_price(client, leg["symbol"], spot, sig["stop"])
        order_id = place_broker_stop(client, leg["symbol"], qty, trigger) if trigger is not None else None
        if order_id:
            state["position"]["stop_order_id"] = order_id
        else:
            log("NO BROKER STOP on this position; it is protected only while this process runs")
    return state


def watch(client, state: dict, deadline: float) -> None:
    """Poll the index between bars while a position is open."""
    while not _shutdown and time.monotonic() + STOP_POLL_SECONDS < deadline:
        if not state.get("position") or datetime.now(IST).time() >= SQUARE_OFF:
            return
        time.sleep(STOP_POLL_SECONDS)
        price = index_ltp(client)
        if price is not None:
            check_exit(client, state, price, "index")
            if not state.get("position"):
                save_state(state)


def sleep_to_next_bar(client=None, state: dict | None = None) -> None:
    epoch = int(datetime.now(IST).timestamp())
    wait = BAR_SECONDS - (epoch % BAR_SECONDS) + BAR_DELAY
    deadline = time.monotonic() + wait
    if client is not None and state is not None and state.get("position"):
        watch(client, state, deadline)
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def summary(state: dict) -> None:
    closed = state.get("closed") or []
    if not closed:
        log("day summary: no trades")
        return
    gross = sum(t["gross"] for t in closed)
    log(f"day summary: {len(closed)} trades, index {gross:+.2f} pts gross, "
        f"{gross - COST_PTS * len(closed):+.2f} after cost")


def main() -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    client = build_client()
    if not assert_live_allowed(client):
        return 1
    log(f"{STRATEGY_TAG} starting, {'DRY_RUN, no orders' if DRY_RUN else 'LIVE'}: CPR price action, "
        f"{BAR_MINUTES}m candles, tiers {', '.join(t.name for t in TIERS)}, entries "
        f"{NO_NEW_ENTRY_BEFORE:%H:%M}-{NO_NEW_ENTRY_AFTER:%H:%M}, max {MAX_TRADES_PER_DAY} a day, "
        f"square-off {SQUARE_OFF:%H:%M}; {LOTS} lot, {ITM_POINTS} pts ITM monthly; "
        f"broker stop {'ON' if BROKER_STOP else 'OFF'}")
    state = load_state()
    try:
        while not _shutdown:
            now = datetime.now(IST)
            if now.time() < SESSION_OPEN:
                sleep_to_next_bar()
                continue
            state = fresh_day(state, str(now.date()))
            if now.time() >= SQUARE_OFF and not state.get("position"):
                summary(state)
                log("past square-off with nothing open; done for the day")
                return 0
            try:
                state = cycle(client, state)
            except Exception as exc:  # noqa: BLE001 - one bad bar must not kill the day
                log(f"cycle failed: {exc!r}")
            save_state(state)
            if datetime.now(IST).time() >= SQUARE_OFF and state.get("position"):
                time.sleep(EXIT_RETRY_SECONDS + 1)
                continue
            sleep_to_next_bar(client, state)
    finally:
        save_state(state)
    log("shut down cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
