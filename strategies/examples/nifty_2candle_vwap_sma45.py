"""NIFTY 50 two-candle VWAP + SMA(45) breakout, ATM weekly options.

Signals are read off NIFTY 50 SPOT on 5-minute candles; orders go to the ATM
option of the nearest weekly expiry - CE on a long signal, PE on a short. Every
stop, target and trail is measured in INDEX points, never in premium.

Entry (long; short is the mirror):
    1. Two consecutive candles lie completely above both VWAP and SMA(45) -
       each candle's low is above the higher of the two lines.
    2. The pair is the first of its run: the candle before candle 1 was not
       itself completely above both lines.
    3. Candle 2 closes above candle 1's HIGH (a short mirrors it: candle 2
       closes below candle 1's LOW). Clearing candle 1's whole range, not just
       its close, is what makes this a breakout.
    Entry is a market order as soon as candle 2 closes, and never before
    NO_NEW_ENTRY_BEFORE (10:00 since 2026-09-23; 10:30 before that). Over
    2026-01-01 to 2026-09-09 the average trade by entry hour climbed
    monotonically from -7.58 index points at 09h to +9.89 at 13h; skipping the
    first hour turned that year from -329.7 points to +541.5 and halved the
    drawdown. The full start-time sweep is at NO_NEW_ENTRY_BEFORE.
    4. India VIX at or above VIX_MIN - OFF since 2026-09-22 night
       (VIX_MIN = None), replaced by the breadth gate in 6. It was 11 earlier
       that day, 10 from 2026-09-21, and 12 before that; see VIX_MIN.
    5. SMA(45) on candle 2 has moved in the trade's direction over the last
       SLOPE_BARS candles: higher than it was for a long, lower for a short.
       Added 2026-09-21 over 6 bars, switched off 2026-09-22, back on that
       night over 2 bars, then off again (SLOPE_BARS = None) in favour of
       the breadth gate alone. See the tables at SLOPE_BARS.
    6. NIFTY 50 breadth leans with the trade: advancers at least ADR_MIN
       (1.5) times decliners for a long, the reverse for a short, read from
       multiquotes when the signal fires. Added 2026-09-22; see ADR_MIN.

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

    TESTED AND REJECTED, 2026-09-17: dropping the buffer and the line swap on
    wide trades, so the stop sits exactly on candle 1's extreme. Swept over
    2016-10-06 to 2026-09-17 against leaving the buffer alone (10,544 pts):

        buffer off above 30 pts   8,302    PF 1.15   stop-outs 53.6%
        buffer off above 40 pts   7,526    PF 1.13   stop-outs 51.7%
        buffer off above 50 pts   9,422    PF 1.16   stop-outs 50.0%
        buffer off above 60 pts  10,473    PF 1.17   stop-outs 49.6%
        buffer off above 70 pts  11,043    PF 1.18   stop-outs 49.4%

    Every threshold that touches a meaningful number of trades loses, and the
    high ones only look good because they touch almost none - at 70 the rule is
    inert and converges on doing nothing. Tightening pushes the stop-out rate
    back toward the 51.8% this buffer was introduced to escape. Do not retry it.

Management (2 lots):
    target     OFF (TARGET_R = None). When set, the whole position is sold
               the moment the index trades TARGET_R times R past the entry, R
               being the entry less candle 1's low. Backtests badly; see there.
    close stop OFF (CLOSE_STOP = False). When on, a candle that CLOSES below
               candle 1's low (short: above its high) exits.
    trail      the stop sits TRAIL_DIST_PCT of the entry behind the best
               price, moved in TRAIL_STEP_PCT steps, and never below the
               candle stop. No partial booking, no line exit.
    lock       once the trade has run LOCK_AT_R times its initial risk, the
               stop is never worse than LOCK_TO_R times that risk in profit.
    broker     the resting broker stop is a mechanical-failure backstop only.
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

I_UNDERSTAND_THIS_IS_LIVE = True    # must be True to run outside analyzer mode
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

LOTS = 1                            # cut from 2 on 2026-09-20 for the first live week

# Percent trailing stop, replacing 1-lot booking at +1.5R (+1R when the stop was
# over 30 pts) and the line trail on 2026-09-18. With nothing booked, the
# wide-stop 1R rule and its shadow comparison went too. Replayed
# 2016-10-03..2026-09-10 on this script's own entries, net of 3 pts a trade,
# closes filled at the bar close:
#
#                        last 2y   last 5y   2018-01..2021-09     full
#     book + line trail   +1,450      +582        -2,164         -1,774
#     trail 0.5% / 0.2%   +3,327    +2,725        -1,650           +860
#
# Nearby settings (0.4-0.6% distance, 0.1-0.2% step) land close together, so
# this is not a knife-edge fit. 2018-2021 loses under every stop rule tested.
# If stops filled AT the stop price instead of the bar close, the old rule
# would lead; this script exits at the close, so the close figures apply.
# Figures are index points; option decay over the longer holds is not in them.
#
# BANKNIFTY's 0.1% step was tried here on 2026-09-20 and reverted the same day.
# It was measured on NIFTY for the first time on a harness rebuilt from this
# script, since the original was never committed; that rebuild reproduces the
# 0.2% row above to within 3-26% per window, so read the levels below as
# approximate but the comparison as sound - same harness, same 2,096 entries.
# See backtests/nifty_percent_trail_replay.py.
#
#                        last 2y   last 5y   2018-01..2021-09     full    maxDD
#     trail 0.5% / 0.2%   +3,049    +2,959        -1,594         +1,087   -3,788
#     trail 0.5% / 0.1%   +2,823    +2,784        -1,761           +767   -3,947
#
# Worse in all four windows. Year by year it is better in 6 of 11 but loses
# +1,212 -> +892 overall, the gap coming almost entirely from 2019 (-251) and
# 2026 (-276), so this is noise rather than an edge either way - across a
# 0.4-0.6% x 0.1-0.3% sweep the full-window result moves by ~570 points with no
# monotonic pattern. The point is that there is no evidence FOR 0.1% on NIFTY.
#
# That sweep did show the whole 0.6% distance row beating 0.5% at every step and
# in every window, cutting 2018-2021 from -1,594 to between -310 and -869. A
# whole-row effect, not one lucky cell - but untested out of sample and not
# adopted. Do not change the distance on the strength of that sweep alone.
#
# TIGHTENED to 0.4% / 0.1% on 2026-09-22 by the user's decision, favouring
# recent years. Re-run on the live config of that evening (slope off, VIX 11,
# 1 lot, Rs = net index pts x 65), 0.4%/0.1% vs 0.5%/0.2%:
#
#                      2023-2026                 last 2y                  2016-10..2026-09
#     0.5% / 0.2%   +2.47L PF 1.32 DD -55k   +1.76L PF 1.37 DD -55k   +1.64L PF 1.09 DD -1.24L
#     0.4% / 0.1%   +2.59L PF 1.35 DD -44k   +1.80L PF 1.40 DD -44k   +1.62L PF 1.09 DD -1.32L
#
# A small, recent-only gain; the full window is a tie. Tighter than 0.4% loses
# in every window (0.3%: 10y +0.52L, 0.2%: -0.99L). A VIX-switched version
# (tight only at VIX <= 14) was no better than either. Set 0.005 / 0.002 to
# restore. Reproduce: session 06861671 scratchpad trail_tight.py, trail_vix2.py.
TRAIL_DIST_PCT = 0.004              # stop distance behind the best price
TRAIL_STEP_PCT = 0.001              # the stop moves once per step of this size

# Profit booking and the close stop, added 2026-09-21 on the user's rule and
# switched off after the backtest below. R is the entry less candle 1's LOW (a short: candle 1's
# HIGH less the entry) - the raw candle extreme, not the buffered stop. The whole
# position is sold the moment the index trades TARGET_R of it past the entry;
# the index is polled every TARGET_POLL_SECONDS between bars so the booking does
# not wait for a candle to close. Separately, a candle that CLOSES beyond candle
# 1's extreme exits. That runs beside the touch stop, not instead of it, and the
# resting broker stop stays underneath both as the mechanical-failure backstop.
TARGET_R = None                     # 1.5 books the whole position at 1.5R; off, see below
CLOSE_STOP = False                  # True exits on a close beyond candle 1; off, see below
TARGET_POLL_SECONDS = 5

# SWITCHED OFF the same evening, 2026-09-21, after a two-year replay
# (2024-09-11..2026-09-18, net index points per lot, target filled AT its price,
# which flatters it):
#
#                              NIFTY   PF    BANKNIFTY   PF
#     trail only (VIX 10)     +3,186  1.39     +6,201   1.68
#     + close stop            +2,478  1.39     +5,727   1.63   (added alone, VIX as before)
#     + 1.5R target             +649  1.11       +595   1.07   (added alone, VIX as before)
#     all three                  +27  1.00     +1,401   1.19
#
# The target lifts the win rate to ~52% but caps the few long runs to square-off
# that carry this strategy - the median target is only 39 NIFTY points away.
# The code stays so either rule can be re-enabled by flipping the switch.
# Reproduce: workspace/indicators/backtests/twocandle_target_close_stop_replay.py

# TESTED AND REJECTED, 2026-09-20: protecting profit earlier. The complaint is
# real - this trail is inert on most trades. Median favourable move is 24.4 pts
# against the ~91 pts (0.6% of entry) where the stop first lifts above entry, so
# it engages on 13.2% of trades. Locking breakeven once a trade runs a fixed
# distance looks like the fix and is not:
#
#                           last 2y  last 5y  2018-01..2021-09   full   maxDD
#     none (this setting)    +3,003   +2,744        +228       +2,996  -1,697
#     breakeven at +20 pts   +1,051      +67        +340         +519  -1,742
#     breakeven at +30 pts   +1,503     +730        +264       +1,195  -2,047
#     breakeven at +50 pts   +2,418   +1,767        +145       +1,981  -1,864
#     +30 then trail 30 pts    +880     -452        -858       -1,194  -3,006
#
# Every variant loses and tighter is worse; the fixed 30-pt trail turns the
# strategy negative while posting the HIGHEST win rate of the set at 43.6%.
# That is the signature of cutting a fat tail. Winners' median favourable move
# is 61.9 pts and losers' is 11.4, so a lock between the two scratches winners
# without saving losers. This is the same lesson as the STOP_BUFFER note in the
# docstring, on its third appearance. If the late engagement bothers you the
# lever is TRAIL_DIST_PCT, not earlier protection.
# Reproduce: backtests/nifty_percent_trail_replay.py, replay(lock_pts=30).
#
# ADOPTED 2026-09-21, user's decision, together with SLOPE_BARS: a lock sized
# in R rather than fixed points, set late enough to leave the winners alone.
# Once the trade has run LOCK_AT_R x its initial risk (entry to the buffered
# stop), the stop is lifted to at least LOCK_TO_R x risk in profit. Median risk
# is ~40 pts, so it arms around +60 - just under the winners' median favourable
# move above - where the fixed 20-50 pt locks armed on ordinary noise. Replayed
# with the slope filter, 2018-01..2026-09, net of 3 pts:
#
#                          net     PF   maxDD   win 2018-22 / 2023-26
#     slope, no lock     +4,710   1.26  -1,149   37.2% / 45.5%
#     slope + lock       +4,512   1.26  -1,091   40.6% / 48.9%
#
# It costs ~200 points over nine years for ~3.5 points of win rate and a
# slightly shallower drawdown - a comfort trade, not an edge. None disables it.
LOCK_AT_R = 1.5                     # arm once the best move reaches this many R
LOCK_TO_R = 0.5                     # then the stop is at least this many R in profit

MAX_TRADES_PER_DAY = 2
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

# Regime filter: take no signal below this India VIX. Set to None to disable.
# Lowered from 12 (at or below skipped) to 10 (10.00 and up trades) on
# 2026-09-21 by the user's decision, then raised to 11 (11.00 and up trades) on
# 2026-09-22, also the user's decision; the table below is the case for 12.
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
#
# SWITCHED OFF 2026-09-22 night, on the user's decision, once the breadth gate
# (ADR_MIN) went in. NIFTY alone, 1 lot, slope off, ADR >= 1.5 on:
#
#                  2016-10..2026-09          last 2y                   last month
#     VIX 11     +2.51L PF 1.27 DD -99k   +2.28L PF 2.09 Sh 2.26   6 trades   -487
#     VIX off    +2.79L PF 1.29 DD -91k   +2.49L PF 2.10 Sh 2.40   7 trades -1,615
#
# Breadth does the regime job; the floor only cost trades. Last month's one
# extra trade is the 08-26 short, -1,128. Reproduce: session febea895
# scratchpad nifty_vix_adr.py. Set 11.0 to restore.
VIX_MIN = None

# Trend filter, added 2026-09-21 on the user's decision: take a long only when
# SMA(45) on candle 2 is higher than it was SLOPE_BARS candles earlier, a short
# only when it is lower. None disables it. Replayed 2018-01..2026-09 with every
# other setting as live (VIX 10, 10:30-14:30, 2 a day), net of 3 pts a trade:
#
#                        trades    net     PF   maxDD   losing years
#     no slope filter     1,497  +2,972   1.10  -1,833       4
#     slope over 3          1,071  +4,785   1.24  -1,106       3
#     slope over 6            973  +4,710   1.26  -1,149       2
#     slope over 12           903  +4,627   1.28  -1,038       2
#
# The length barely matters, so 6 is not a fitted optimum. It helps in both
# halves: 2023-26 PF 1.30 -> 1.59; 2018-22 goes from -992 to -53, i.e. it cuts
# the losses of the bad years rather than creating an edge in them. Found on
# SENSEX first and carried here. On BANKNIFTY it HURTS, so it is not in that copy.
# Reproduce: session 684aa630 scratchpad sensex/other.py, other2.py.
#
# SWITCHED OFF 2026-09-22 on the user's decision, after a day it blocked the
# only setup and a month (2026-08-19..09-18) in which it cut the three
# strategies' combined net from +Rs 2,507 to -Rs 9,110 - almost all of that one
# blocked +Rs 12,009 short on 09-15. Over 2016-10..2026-09 at 1 lot, off vs on:
# PF 1.09 vs 1.24, Sharpe 0.39 vs 0.76, maxDD -Rs 1.19L vs -0.72L.
#
# SWITCHED BACK ON the same evening, on the user's decision, with the new
# 0.4% / 0.1% trail and VIX 11. NIFTY alone, 1 lot, off vs on:
#
#                     2023-2026                 last 2y                  2016-10..2026-09
#     slope off   +2.59L PF 1.35 DD -44k   +1.80L PF 1.40 Sh 1.54   +1.62L PF 1.09 Sh 0.38
#     slope on    +3.00L PF 1.67 DD -26k   +1.89L PF 1.70 Sh 1.85   +3.05L PF 1.27 Sh 0.83
#
# Better in every window but the last month. SENSEX keeps it off; BANKNIFTY
# never had it. Reproduce: session 06861671 scratchpad nifty_slope_trail.py.
#
# SWITCHED OFF AGAIN later on 2026-09-22 by the user's decision, to keep the
# rules simple.
#
# BACK ON over 2 bars, same night, on the user's decision. The 6-bar filter
# had blocked that morning's 10:40 short (slope +3.21 over 6 bars, +0.41 over
# 3, -0.12 over 2). NIFTY alone, 1 lot, same trail and VIX 11:
#
#                   2016-10..2026-09                 2023 onwards        last 2y
#     no filter   +1.62L PF 1.09 Sh 0.38 DD -1.32L   +2.59L PF 1.35   +1.80L PF 1.40 Sh 1.54
#     over 2      +2.70L PF 1.20 Sh 0.70 DD -0.86L   +3.12L PF 1.60   +2.03L PF 1.66 Sh 1.91
#     over 3      +2.87L PF 1.23 Sh 0.76 DD -0.83L   +3.16L PF 1.63   +1.96L PF 1.66 Sh 1.90
#     over 6      +3.05L PF 1.27 Sh 0.83 DD -0.63L   +3.00L PF 1.67   +1.89L PF 1.70 Sh 1.85
#
# 2 trails 3 and 6 over ten years but matches them in the last two. Every
# length lost the last month (2026-08-19..09-18). Reproduce: session febea895
# scratchpad nifty_slope_bars.py.
#
# OFF again the same night, on the user's decision, leaving the breadth gate
# (ADR_MIN) as the only trend filter. Last month (2026-08-19..09-18): slope 2
# + ADR -12,496 on 5 trades, ADR alone -487 on 6 - the gap is the +12,009
# 09-15 short, which any slope length blocks. Last 2y ADR alone: +2.28L PF 2.09
# Sh 2.26 DD -27k. Reproduce: session febea895 scratchpad
# nifty_month_adr_alone.py. Set 2 to restore.
SLOPE_BARS = None

# Breadth gate, added 2026-09-22 on the user's decision, first on top of the
# slope and, from later that night, on its own (SLOPE_BARS = None):
# the NIFTY 50 advance-decline ratio (constituents above vs below their own
# previous close) must lean with the trade by at least ADR_MIN - advancers at
# least 1.5x decliners for a long, decliners at least 1.5x advancers for a
# short. Read once from multiquotes when a signal fires. None disables it.
# NIFTY alone, 1 lot, slope over 2, same trail and VIX 11:
#
#                     2016-10..2026-09                last 2y                        last month
#     slope 2 only   +2.70L PF 1.20 DD -86k   +2.03L PF 1.66 Sh 1.91 DD -27k   8 trades -8,775
#     + ADR >= 1.5   +2.31L PF 1.30 DD -87k   +1.95L PF 2.09 Sh 2.05 DD -16k   5 trades -12,496
#
# Fewer, better trades over two years; worse in the last month, where it
# removed two winners (08-24, 09-11) and one small loser (08-21). The backtest
# uses today's constituents throughout, so older years flatter it. Reproduce:
# session febea895 scratchpad nifty_slope_adr.py, nifty_month_trades.py.
ADR_MIN = 1.5
ADR_MIN_STOCKS = 40                 # skip the signal if fewer quotes are readable

MIN_CLEARANCE = 0.0                 # points a candle must clear both lines by
STOP_BUFFER = 10.0                  # stop sits this far beyond candle 1
LINE_PROXIMITY = 25.0               # swap to the line when it is this close

SESSION_OPEN = dtime(9, 15)
# Entries are allowed from NO_NEW_ENTRY_BEFORE, measured on candle 2's CLOSE
# time (the wall clock when the signal is evaluated), until NO_NEW_ENTRY_AFTER.
# Moved 10:30 -> 10:00 on 2026-09-23 at the user's request, live from 2026-09-24.
#
# Swept on this config (breadth ADR >= 1.5, no slope, no VIX, 0.4%/0.1% trail,
# lock 1.5R -> 0.5R), 2016-10..2026-09, 1 lot of 75, cost 3 pts a trade:
#
#     start   10y net    PF     2023+ net   PF     last 2y net   PF
#     09:15    +83,389  1.04     +131,404  1.12       +119,201  1.18
#     09:45   +230,671  1.13     +178,031  1.21       +157,129  1.32
#     10:00   +308,132  1.21     +221,280  1.33       +232,346  1.63
#     10:15   +348,920  1.28     +314,749  1.59       +292,354  2.01
#     10:30   +322,447  1.29     +343,050  1.75       +287,160  2.10
#     11:00   +148,100  1.16     +171,311  1.48       +224,014  2.13
#
# Split by the bucket each earlier start adds over 10 years: the 91 trades
# entered 10:15-10:30 are worth +241 each, and the 146 entered 10:00-10:15
# lose -244 each. 10:00 therefore buys back the good bucket and the bad one
# together; 10:15 keeps only the good one. Told to the user 2026-09-23 with
# 10:15 offered as the alternative - they asked for 10:00.
NO_NEW_ENTRY_BEFORE = dtime(10, 0)
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
    if SLOPE_BARS:
        combined["sma_slope"] = combined["sma"] - combined["sma"].shift(SLOPE_BARS)
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


def read_breadth(client) -> tuple[int, int] | None:
    """NIFTY 50 advancers and decliners against each stock's previous close.

    Returns:
        (advancers, decliners), or None when fewer than ADR_MIN_STOCKS
        constituents return a usable quote.
    """
    symbols = [{"symbol": s, "exchange": EQUITY_EXCHANGE} for s in CONSTITUENTS]
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
    if readable < ADR_MIN_STOCKS:
        log(f"breadth: only {readable}/{len(CONSTITUENTS)} quotes readable")
        return None
    return advancers, decliners


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
        # 2026-09-20 this compared closes, which is a much weaker breakout and
        # was not the intended rule. Replayed 2016-10..2026-09 on this script's
        # own frame, net of 3 pts a trade:
        #
        #                       last 2y  last 5y  2018-01..2021-09   full   maxDD
        #     close beyond close  +3,049   +2,959       -1,594      +1,087  -3,788
        #     close beyond range  +3,003   +2,744         +228      +2,996  -1,697
        #
        # 2,096 trades become 1,476, the average trade +0.52 -> +2.03 and the
        # win rate 33.7% -> 36.6%. The last two years give up 46 points; in
        # exchange 2018-2021 stops losing for the first time under any rule
        # tested here, and the full-window drawdown more than halves.
        if direction == "long" and second["close"] <= first["high"]:
            continue
        if direction == "short" and second["close"] >= first["low"]:
            continue
        if SLOPE_BARS:
            slope = float(second.get("sma_slope", float("nan")))
            # NaN fails both comparisons, so an unreadable slope skips.
            if not (slope > 0 if direction == "long" else slope < 0):
                log(f"{direction} signal skipped: SMA({SMA_PERIOD}) moved "
                    f"{slope:+.2f} over {SLOPE_BARS} bars, against the trade")
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

def exit_position(client, state: dict, why: str) -> dict:
    """Clear the resting stop, then sell the whole open position.

    Returns:
        The updated state dict.
    """
    position = state["position"]
    outcome = release_broker_stop(client, state) if BROKER_STOP else "clear"
    if outcome == "filled":
        return state
    if outcome == "blocked":
        # Selling now could double up on a stop that is still live, so the
        # exit simply waits a bar. Another five minutes of exposure beats an
        # accidental naked short.
        return state
    qty = int(position["lotsize"]) * int(position["lots_open"])
    if send(client, position["symbol"], "SELL", qty):
        log(f"{why}; closed {qty}")
        state["position"] = None
    else:
        abandon_if_flat(client, state)
    return state


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

    excursion = (float(bar["high"]) - entry) if long else (entry - float(bar["low"]))
    position["mfe"] = max(float(position.get("mfe", 0.0)), excursion)

    breached = (float(bar["low"]) <= stop) if long else (float(bar["high"]) >= stop)
    if breached:
        return exit_position(client, state, f"STOP hit at {stop:.2f} on the index")

    # The live watch between bars normally books the target; this catches a bar
    # that reached it while the watch was not looking (a slow cycle, a restart).
    target = position.get("target")
    if target is not None:
        reached = (float(bar["high"]) >= target) if long else (float(bar["low"]) <= target)
        if reached:
            return exit_position(client, state, f"TARGET {TARGET_R}R reached at "
                                 f"{target:.2f} on the index")

    extreme = position.get("c1_low" if long else "c1_high")
    if CLOSE_STOP and extreme is not None:
        close = float(bar["close"])
        if (close < extreme) if long else (close > extreme):
            side = "low" if long else "high"
            return exit_position(client, state, f"CLOSE STOP: candle closed {close:.2f} "
                                 f"beyond candle 1's {side} {extreme:.2f}")

    # Ratchet the stop behind the best price. It only ever moves in the trade's
    # favour, and is checked against the NEXT bar, as the backtest does.
    level = trail_level(entry, position["mfe"], long)
    if level is not None:
        moved = max(stop, level) if long else min(stop, level)
        if moved != stop:
            position["stop"] = moved
            log(f"trail: best +{position['mfe']:.2f} pts, stop {stop:.2f} -> {moved:.2f}")
            stop = moved

    # Profit lock, applied after the trail so it only ever tightens further.
    risk = float(position.get("risk") or 0.0)
    if LOCK_AT_R is not None and risk > 0 and position["mfe"] >= LOCK_AT_R * risk:
        floor = entry + LOCK_TO_R * risk if long else entry - LOCK_TO_R * risk
        moved = max(stop, floor) if long else min(stop, floor)
        if moved != stop:
            position["stop"] = moved
            log(f"lock: best +{position['mfe']:.2f} pts is {LOCK_AT_R}R of "
                f"{risk:.2f}, stop {stop:.2f} -> {moved:.2f} (+{LOCK_TO_R}R)")

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

    if VIX_MIN is not None:
        vix = read_vix(client)
        if vix is None:
            # The whole point of the gate is to know the regime before committing,
            # so an unreadable VIX skips the signal rather than trading blind.
            log("VIX unreadable; skipping this signal rather than guessing the regime")
            return state
        if vix < VIX_MIN:
            log(f"{sig['direction']} signal skipped: India VIX {vix:.2f} is "
                f"below VIX_MIN {VIX_MIN}")
            return state
        log(f"India VIX {vix:.2f} clears VIX_MIN {VIX_MIN}")

    if ADR_MIN is not None:
        breadth = read_breadth(client)
        if breadth is None:
            # Same reasoning as the VIX gate: no reading, no trade.
            log("breadth unreadable; skipping this signal")
            return state
        adv, dec = breadth
        with_trade, against = (adv, dec) if sig["direction"] == "long" else (dec, adv)
        if not (with_trade > 0 and with_trade >= ADR_MIN * against):
            log(f"{sig['direction']} signal skipped: {adv} advancing / {dec} declining, "
                f"needs {ADR_MIN}x in the trade's direction")
            return state
        log(f"breadth {adv} advancing / {dec} declining clears ADR_MIN {ADR_MIN}")

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
        f"(risk {risk:.2f} pts), buying {quantity} "
        f"{leg['symbol']}")
    if not send(client, leg["symbol"], "BUY", quantity):
        return state

    state["trades"] = state.get("trades", 0) + 1
    state["position"] = {
        "direction": sig["direction"], "symbol": leg["symbol"],
        "lotsize": leg["lotsize"], "lots_open": LOTS,
        "entry": spot, "stop": stop, "risk": risk,
        "mfe": 0.0, "opened": now.isoformat(),
        "c1_low": sig["c1_low"], "c1_high": sig["c1_high"], "target": None,
    }
    if TARGET_R is not None:
        long = sig["direction"] == "long"
        r_pts = (spot - sig["c1_low"]) if long else (sig["c1_high"] - spot)
        target = spot + TARGET_R * r_pts if long else spot - TARGET_R * r_pts
        state["position"]["target"] = target
        log(f"target {target:.2f}: {TARGET_R}R of {r_pts:.2f} pts to candle 1's "
            f"{'low' if long else 'high'}")

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


def index_ltp(client) -> float | None:
    """Last traded index price, or None when the quote cannot be read."""
    try:
        q = client.quotes(symbol=UNDERLYING, exchange=INDEX_EXCHANGE)
    except Exception as exc:  # noqa: BLE001 - a blip must not stop the strategy
        log(f"index quote raised {exc!r}")
        return None
    if not ok(q):
        log(f"index quote failed: {q}")
        return None
    return float((q.get("data") or {}).get("ltp") or 0) or None


def watch_target(client, state: dict, deadline: float) -> None:
    """Poll the index until the deadline and book the target when it trades.

    Runs on the main thread in the gap between bars, so it never races
    cycle() for the position. Any exit attempt ends the watch; a blocked one
    is retried by the next bar's manage().
    """
    while not _shutdown and time.monotonic() + TARGET_POLL_SECONDS < deadline:
        position = state.get("position")
        if not position or position.get("target") is None:
            return
        if datetime.now(IST).time() >= SQUARE_OFF:
            return
        time.sleep(TARGET_POLL_SECONDS)
        price = index_ltp(client)
        if price is None:
            continue
        target = float(position["target"])
        long = position["direction"] == "long"
        if (price >= target) if long else (price <= target):
            exit_position(client, state, f"TARGET {TARGET_R}R hit at {price:.2f} on "
                          f"the index (target {target:.2f})")
            save_state(state)
            return


def sleep_to_next_bar(client=None, state: dict | None = None) -> None:
    """Block until shortly after the next 5-minute candle closes.

    With a position open, the wait is spent watching the profit target.
    """
    now = datetime.now(IST)
    epoch = int(now.timestamp())
    wait = BAR_SECONDS - (epoch % BAR_SECONDS) + BAR_DELAY
    deadline = time.monotonic() + wait
    if client is not None and state is not None and state.get("position"):
        watch_target(client, state, deadline)
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


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
    log(f"filters: VIX >= {VIX_MIN}, SMA slope over {SLOPE_BARS} bars, "
        f"advance-decline >= {ADR_MIN}x; "
        f"lock +{LOCK_TO_R}R once +{LOCK_AT_R}R")

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
            sleep_to_next_bar(client, state)
    finally:
        save_state(state)
        if _FEED is not None:
            _FEED.stop()

    log("shut down cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
