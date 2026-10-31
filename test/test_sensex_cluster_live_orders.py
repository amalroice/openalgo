"""Live order paths of the SENSEX cluster strategy, against a stub client.

This is the repo's first paper-to-live conversion, so the sequencing that can
lose money is pinned here: the entry fill is read before the backstop is
parked, the SL-M backstop is cancelled BEFORE every sell, a backstop caught
mid-fire reads as already flat instead of being sold twice, and a failed exit
keeps the position for a retry instead of assuming flat.
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pandas as pd
import pytest

MODULE_PATH = (Path(__file__).resolve().parents[1]
               / "strategies" / "examples" / "sensex_cluster_breakout.py")
spec = importlib.util.spec_from_file_location("sensex_cluster_breakout", MODULE_PATH)
scb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scb)

LEG = {"symbol": "SENSEX08OCT2675000CE", "lotsize": 20}


class StubClient:
    """Scripted openalgo client: orderids encode what each order was."""

    def __init__(self, buy_ok=True, buy_status=("complete", 412.5),
                 sell_status=("complete", 355.0), slm_ok=True,
                 slm_status=("open", 0.0), cancel_ok=True, cancel_lands=True,
                 held=None):
        self.buy_ok = buy_ok
        self.buy_status = buy_status
        self.sell_status = sell_status
        self.slm_ok = slm_ok
        self.slm_status = slm_status
        self.cancel_ok = cancel_ok
        self.cancel_lands = cancel_lands    # an accepted cancel really removes the order
        self.held = held                    # positionbook quantity; None = unreadable
        self.greeks_ok = True
        self.index_price = 75000.0
        self.option_price = None            # quote for LEG when set
        self.events: list[str] = []
        self.orders: list[dict] = []
        self.cancelled: list[str] = []
        self._n = 0

    def placeorder(self, **kw):
        self.events.append(f"place:{kw['action']}:{kw['price_type']}")
        self._n += 1
        kind = "SLM" if kw["price_type"] == "SL-M" else kw["action"]
        if kind == "BUY" and not self.buy_ok:
            return {"status": "error", "message": "margin"}
        if kind == "SLM" and not self.slm_ok:
            return {"status": "error", "message": "trigger out of band"}
        orderid = f"{kind}{self._n}"
        self.orders.append({**kw, "orderid": orderid})
        return {"status": "success", "orderid": orderid}

    def orderstatus(self, *, order_id, strategy="Python", **kw):
        self.events.append(f"status:{order_id}")
        if order_id.startswith("SLM"):
            state, price = self.slm_status
            if order_id in self.cancelled and self.cancel_lands:
                state, price = "cancelled", 0.0
        elif order_id.startswith("BUY"):
            state, price = self.buy_status
        else:
            state, price = self.sell_status
        return {"status": "success",
                "data": {"order_status": state, "average_price": price}}

    def quotes(self, *, symbol, exchange, **kw):
        if symbol == scb.UNDERLYING and exchange == scb.INDEX_EXCHANGE:
            return {"status": "success", "data": {"ltp": self.index_price}}
        if symbol == LEG["symbol"] and self.option_price is not None:
            return {"status": "success", "data": {"ltp": self.option_price}}
        return {"status": "error", "message": "quote unavailable"}

    def optiongreeks(self, *, symbol, exchange, **kw):
        self.events.append(f"greeks:{symbol}:{exchange}")
        if not self.greeks_ok:
            return {"status": "error", "message": "Greeks unavailable"}
        return {"status": "success", "option_price": 400.0,
                "greeks": {"delta": 0.8, "gamma": 0.0, "theta": 0.0, "vega": 0.0}}

    def positionbook(self, **kw):
        if self.held is None:
            return {"status": "error", "message": "book unavailable"}
        return {"status": "success",
                "data": [{"symbol": LEG["symbol"], "exchange": "BFO",
                          "quantity": str(self.held)}]}

    def cancelorder(self, *, order_id, strategy="Python", **kw):
        if self.cancel_ok or order_id.startswith(("BUY", "SELL")):
            self.cancelled.append(order_id)
            return {"status": "success", "orderid": order_id}
        return {"status": "error", "message": "cannot cancel"}


@pytest.fixture(autouse=True)
def fast_order_poll(monkeypatch):
    """No real sleeping or long poll loops in unit tests."""
    monkeypatch.setattr(scb, "ORDER_POLL_TRIES", 1)
    monkeypatch.setattr(scb, "ORDER_POLL_SECONDS", 0)
    monkeypatch.setattr(scb, "CANCEL_CONFIRM_SECONDS", 0)
    monkeypatch.setattr(scb, "MANUAL_EXIT_RECHECK_SECONDS", 0)


def placed(client, kind, price_type):
    return [o for o in client.orders
            if o["action"] == kind and o["price_type"] == price_type]


def test_greek_mapped_trigger_uses_option_greeks_and_tick_rounding():
    trigger = scb.greek_mapped_slm_trigger(400.0, 0.8, 0.0001, -4.0, 10.0, -100.0, 6.0)
    projected = 400.0 - 80.0 + 0.5 * 0.0001 * 100.0**2
    expected = projected - 4.0 * 6.0 / 24.0 - 10.0 * scb.SLM_IV_BUFFER_POINTS
    assert trigger == pytest.approx(math.floor(expected / scb.SLM_TICK) * scb.SLM_TICK)
    assert trigger < 400.0
    assert scb.greek_mapped_slm_trigger(100.0, 0.8, 0.0, 0.0, 0.0, -200.0, 0.0) is None
    assert scb.greek_mapped_slm_trigger(400.0, 0.0, 0.0, 0.0, 0.0, -100.0, 0.0) is None
    assert scb.greek_mapped_slm_trigger(1.0, 0.8, 0.0, 0.0, 0.0, -2.0, 0.0) is None


def test_initial_index_stop_caps_risk_and_keeps_tighter_box_edge():
    assert scb.initial_index_stop(75100.0, 74800.0, 75300.0, 100.0, True) == 74850.0
    assert scb.initial_index_stop(74900.0, 74700.0, 75200.0, 100.0, False) == 75150.0
    assert scb.initial_index_stop(75100.0, 74900.0, 75150.0, 100.0, True) == 74900.0
    assert scb.initial_index_stop(74900.0, 74850.0, 75000.0, 100.0, False) == 75000.0
    with pytest.raises(ValueError, match="ATR must be finite and positive"):
        scb.initial_index_stop(75000.0, 74900.0, 75100.0, float("nan"), True)


def test_live_entry_fills_then_parks_the_backstop():
    client = StubClient()
    fills = scb.live_entry(client, LEG, spot=75000.0, stop=74850.0)
    assert fills is not None
    assert fills["buy_orderid"].startswith("BUY")
    assert fills["slm_orderid"].startswith("SLM")
    assert fills["qty"] == LEG["lotsize"] * scb.LIVE_LOTS
    assert fills["premium_in"] == 412.5
    # the buy is placed first, the SL-M only after the fill is read
    assert client.orders[0]["action"] == "BUY"
    slm = placed(client, "SELL", "SL-M")[0]
    assert slm["trigger_price"] == "292.50"  # fill 412.5 less 0.8 delta x 150 index points
    assert slm["quantity"] == LEG["lotsize"] * scb.LIVE_LOTS
    assert client.events.index("status:BUY1") < client.events.index(f"greeks:{LEG['symbol']}:BFO")
    assert client.events.index(f"greeks:{LEG['symbol']}:BFO") < client.events.index("place:SELL:SL-M")


def test_initial_index_stop_rejects_unusable_atr():
    with pytest.raises(ValueError, match="ATR must be finite and positive"):
        scb.initial_index_stop(75000.0, 74900.0, 75100.0, 0.0, True)


def test_live_entry_flattens_if_stop_is_crossed_during_buy():
    client = StubClient()
    client.index_price = 74840.0
    assert scb.live_entry(client, LEG, spot=75000.0, stop=74850.0) is None
    assert len(placed(client, "SELL", "MARKET")) == 1


def test_live_entry_flattens_when_greeks_are_unavailable():
    client = StubClient()
    client.greeks_ok = False
    assert scb.live_entry(client, LEG, spot=75000.0, stop=74850.0) is None
    assert len(placed(client, "SELL", "MARKET")) == 1
    assert placed(client, "SELL", "SL-M") == []


def test_live_entry_flattens_when_broker_rejects_stop():
    client = StubClient(slm_ok=False)
    assert scb.live_entry(client, LEG, spot=75000.0, stop=74850.0) is None
    assert len(placed(client, "SELL", "MARKET")) == 1


def test_live_entry_marks_unsettled_emergency_flatten_for_retries(monkeypatch):
    monkeypatch.setattr(scb, "index_ltp", lambda _client: 75000.0)
    client = StubClient(slm_ok=False, sell_status=("open", 0.0))
    fills = scb.live_entry(client, LEG, spot=75000.0, stop=74850.0)
    assert fills is not None
    assert fills["emergency_exit"] is True
    book = {"position": {"direction": "long", "entry": 75000.0,
                          "stop": 74850.0, "entry_bar": "2026-10-05T10:00:00+05:30",
                          "symbol": LEG["symbol"], "buy_orderid": fills["buy_orderid"],
                          "qty": LEG["lotsize"], "slm_orderid": None,
                          "emergency_exit": True}}
    scb.manage(client, "boxatr", book, pd.DataFrame())
    assert book["position"] is not None


def test_live_entry_rejected_buy_drops_the_signal():
    client = StubClient(buy_ok=False)
    assert scb.live_entry(client, LEG, spot=75000.0, stop=74850.0) is None
    assert placed(client, "SELL", "SL-M") == []


def test_live_entry_unsettled_buy_is_cancelled_and_dropped():
    client = StubClient(buy_status=("open", 0.0))
    assert scb.live_entry(client, LEG, spot=75000.0, stop=74850.0) is None
    assert any(c.startswith("BUY") for c in client.cancelled)
    assert placed(client, "SELL", "SL-M") == []


def test_live_close_cancels_the_backstop_before_selling():
    client = StubClient()
    pos = {"symbol": LEG["symbol"], "qty": LEG["lotsize"], "buy_orderid": "BUY1",
           "slm_orderid": "SLM2"}
    how, premium = scb.live_close(client, pos)
    assert (how, premium) == ("closed", 355.0)
    assert pos["slm_orderid"] is None
    # cancel landed before the sell was ever sent
    assert client.cancelled == ["SLM2"]
    assert len(client.orders) == 1 and client.orders[0]["action"] == "SELL"


def test_live_close_reads_a_backstop_that_already_fired():
    client = StubClient(cancel_ok=False, slm_status=("complete", 380.0))
    pos = {"symbol": LEG["symbol"], "qty": LEG["lotsize"], "buy_orderid": "BUY1",
           "slm_orderid": "SLM2"}
    how, premium = scb.live_close(client, pos)
    assert (how, premium) == ("already-flat", 380.0)
    assert placed(client, "SELL", "MARKET") == []     # never sold twice


def test_live_close_refuses_to_sell_while_the_backstop_still_lives():
    client = StubClient(cancel_ok=False, slm_status=("trigger pending", 0.0))
    pos = {"symbol": LEG["symbol"], "qty": LEG["lotsize"], "buy_orderid": "BUY1",
           "slm_orderid": "SLM2"}
    how, premium = scb.live_close(client, pos)
    assert (how, premium) == ("failed", None)
    assert placed(client, "SELL", "MARKET") == []


def live_book(**overrides):
    position = {"direction": "long", "entry": 75000.0, "stop": 74850.0, "risk": 150.0,
                "mfe": 40.0, "opened": "2026-10-05T10:00:00+05:30",
                "entry_bar": "2026-10-05T10:00:00+05:30",
                "symbol": LEG["symbol"], "lotsize": LEG["lotsize"],
                "premium_in": 412.5, "buy_orderid": "BUY1", "slm_orderid": "SLM2",
                "slm_trigger": 253.5, "qty": LEG["lotsize"]}
    position.update(overrides)
    return {"trades": 1, "position": position, "cluster_from": None, "closed": []}


def test_close_position_keeps_a_position_whose_live_exit_failed():
    client = StubClient(cancel_ok=False, slm_status=("trigger pending", 0.0))
    book = live_book()
    assert scb.close_position(client, "boxatr", book, 74850.0, "stop",
                              "2026-10-05T10:05:00+05:30") is False
    assert book["position"] is not None               # kept for the next poll
    assert book["closed"] == []


def test_close_position_settles_a_live_exit_and_accounts_the_fill():
    client = StubClient()
    book = live_book()
    assert scb.close_position(client, "boxatr", book, 74900.0, "stop",
                              "2026-10-05T10:05:00+05:30") is True
    assert book["position"] is None
    assert book["cluster_from"] == "2026-10-05T10:05:00+05:30"
    closed = book["closed"][0]
    assert closed["premium_in"] == 412.5              # actual fill, not the quote
    assert closed["premium_out"] == 355.0             # actual fill
    assert closed["gross"] == pytest.approx(-100.0)   # index: 75000 -> 74900 short-fall


def test_live_close_waits_for_an_accepted_cancel_to_land():
    client = StubClient(cancel_lands=False, slm_status=("trigger pending", 0.0))
    pos = {"symbol": LEG["symbol"], "qty": LEG["lotsize"], "buy_orderid": "BUY1",
           "slm_orderid": "SLM2"}
    assert scb.live_close(client, pos) == ("failed", None)
    assert placed(client, "SELL", "MARKET") == []     # never two SELLs on one lot
    assert pos["slm_orderid"] == "SLM2"               # still tracked for the retry


def test_live_close_reads_a_flat_book_after_a_rejected_sell():
    # 2026-10-05: the lot was already gone, so every SELL was a fresh short
    client = StubClient(sell_status=("rejected", 0.0), held=0)
    pos = {"symbol": LEG["symbol"], "qty": LEG["lotsize"], "buy_orderid": "BUY1",
           "slm_orderid": "SLM2"}
    how, _premium = scb.live_close(client, pos)
    assert how == "outside"
    assert pos["slm_orderid"] is None


def test_live_close_keeps_a_held_position_after_a_rejected_sell():
    client = StubClient(sell_status=("rejected", 0.0), held=LEG["lotsize"])
    pos = {"symbol": LEG["symbol"], "qty": LEG["lotsize"], "buy_orderid": "BUY1",
           "slm_orderid": "SLM2"}
    assert scb.live_close(client, pos) == ("failed", None)


def test_close_position_books_out_a_position_closed_outside():
    client = StubClient(sell_status=("rejected", 0.0), held=0)
    book = live_book()
    assert scb.close_position(client, "boxatr", book, 74900.0, "square-off",
                              "2026-10-05T15:05:00+05:30") is True
    assert book["position"] is None
    assert book["closed"][0]["why"] == "square-off"


def test_manage_books_out_a_fired_backstop_without_selling():
    client = StubClient(slm_status=("complete", 250.0), held=0)
    book = live_book()
    scb.manage(client, "boxatr", book, pd.DataFrame())
    assert book["position"] is None
    assert book["closed"][0]["why"] == "backstop"
    assert book["closed"][0]["premium_out"] == 250.0
    assert placed(client, "SELL", "MARKET") == []


def test_manage_notices_a_hand_exit_and_pulls_the_backstop():
    client = StubClient(held=0)
    book = live_book()
    scb.manage(client, "boxatr", book, pd.DataFrame())
    assert book["position"] is None
    assert book["closed"][0]["why"] == "closed-outside"
    assert "SLM2" in client.cancelled
    assert placed(client, "SELL", "MARKET") == []


def no_bars():
    """An empty bar frame with the time index manage() expects."""
    return pd.DataFrame(columns=["high", "low"],
                        index=pd.DatetimeIndex([], tz="Asia/Kolkata"))


def test_manage_leaves_a_held_position_alone():
    client = StubClient(held=LEG["lotsize"])
    book = live_book()
    scb.manage(client, "boxatr", book, no_bars())
    assert book["position"] is not None
    assert client.cancelled == []


def test_manage_does_nothing_when_the_book_is_unreadable():
    client = StubClient(held=None)
    book = live_book()
    scb.manage(client, "boxatr", book, no_bars())
    assert book["position"] is not None
    assert client.cancelled == []



# ----------------------------------------------------------------------------
# Two-lot split: the lock lot and the runner (2026-11-02)
# ----------------------------------------------------------------------------

def split_book(live=True, lock_stop=74850.0, runner_stop=74850.0):
    book = live_book(qty=2 * LEG["lotsize"])
    pos = book["position"]
    pos["lots"] = 2
    pos["legs"] = {"lock": {"lots": 1, "stop": lock_stop},
                   "runner": {"lots": 1, "stop": runner_stop}}
    pos["stop"] = min(lock_stop, runner_stop)
    if not live:
        for k in ("buy_orderid", "slm_orderid", "qty"):
            pos.pop(k, None)
    return book


def test_entry_builds_a_lock_leg_and_a_runner():
    legs = scb.initial_legs(74850.0)
    assert legs == {"lock": {"lots": scb.LIVE_LOTS - scb.RUNNER_LOTS, "stop": 74850.0},
                    "runner": {"lots": scb.RUNNER_LOTS, "stop": 74850.0}}


def test_gap_day_doubles_both_legs():
    legs = scb.initial_legs(74850.0, scb.lot_multiplier(scb.GAP_SIZE_PCT))
    assert legs == {"lock": {"lots": (scb.LIVE_LOTS - scb.RUNNER_LOTS) * scb.GAP_LOT_MULTIPLIER, "stop": 74850.0},
                    "runner": {"lots": scb.RUNNER_LOTS * scb.GAP_LOT_MULTIPLIER, "stop": 74850.0}}


def test_lot_multiplier_only_on_a_gap_day():
    assert scb.lot_multiplier(None) == 1
    assert scb.lot_multiplier(scb.GAP_SIZE_PCT - 1e-6) == 1
    assert scb.lot_multiplier(scb.GAP_SIZE_PCT) == scb.GAP_LOT_MULTIPLIER
    assert scb.lot_multiplier(0.02) == scb.GAP_LOT_MULTIPLIER


def test_open_gap_reads_first_open_against_previous_close():
    idx_prev = pd.date_range("2026-10-30 15:20", periods=2, freq="5min", tz=scb.IST)
    prior = pd.DataFrame({"open": [74400.0, 74500.0], "high": [74600.0, 74550.0],
                          "low": [74300.0, 74450.0], "close": [74500.0, 74000.0]}, index=idx_prev)
    idx = pd.date_range("2026-11-02 09:15", periods=2, freq="5min", tz=scb.IST)
    frame = pd.DataFrame({"open": [73630.0, 73700.0], "high": [73800.0, 73750.0],
                          "low": [73600.0, 73650.0], "close": [73700.0, 73720.0]}, index=idx)
    assert scb.open_gap_pct(prior, frame) == pytest.approx(0.005)       # down 0.5% from 74000
    assert scb.lot_multiplier(scb.open_gap_pct(prior, frame)) == scb.GAP_LOT_MULTIPLIER
    assert scb.open_gap_pct(None, frame) is None
    assert scb.open_gap_pct(prior, frame.iloc[0:0]) is None


def test_live_entry_buys_the_lots_it_is_given():
    client = StubClient()
    fills = scb.live_entry(client, LEG, spot=75000.0, stop=74850.0, lots=4)
    assert fills["qty"] == LEG["lotsize"] * 4
    assert placed(client, "SELL", "SL-M")[0]["quantity"] == LEG["lotsize"] * 4


def test_atr_locks_arm_per_leg_and_the_position_stop_is_the_loosest():
    book = split_book()
    pos = book["position"]
    atr = 50.0
    pos["atr"] = atr
    entry = pos["entry"]
    # past the lock lot's arm, short of the runner's
    pos["mfe"] = scb.LOCK_AT_ATR * atr + 5
    scb.advance_legs("boxatr", pos)
    assert pos["legs"]["lock"]["stop"] == pytest.approx(entry + scb.LOCK_ATR * atr)
    assert pos["legs"]["runner"]["stop"] < entry
    assert pos["stop"] == pos["legs"]["runner"]["stop"]
    # past the runner's arm too
    pos["mfe"] = scb.RUNNER_LOCK_AT_ATR * atr + 5
    scb.advance_legs("boxatr", pos)
    assert pos["legs"]["runner"]["stop"] >= entry + scb.RUNNER_LOCK_ATR * atr
    assert pos["stop"] == min(pos["legs"]["lock"]["stop"], pos["legs"]["runner"]["stop"])


def test_atr_locks_mirror_for_a_short():
    book = split_book(lock_stop=75150.0, runner_stop=75150.0)
    pos = book["position"]
    pos["direction"] = "short"
    pos["stop"] = 75150.0
    pos["atr"] = 50.0
    pos["mfe"] = scb.LOCK_AT_ATR * 50.0 + 5
    scb.advance_legs("boxatr", pos)
    assert pos["legs"]["lock"]["stop"] == pytest.approx(pos["entry"] - scb.LOCK_ATR * 50.0)
    assert pos["legs"]["runner"]["stop"] == 75150.0
    assert pos["stop"] == 75150.0


def test_locks_wait_for_the_arming_move():
    book = split_book()
    pos = book["position"]
    pos["atr"] = 50.0
    pos["mfe"] = scb.LOCK_AT_ATR * 50.0 - 5
    scb.advance_legs("boxatr", pos)
    assert pos["legs"]["lock"]["stop"] < pos["entry"]
    assert pos["legs"]["runner"]["stop"] < pos["entry"]


def test_position_without_entry_atr_trails_only():
    book = split_book()
    pos = book["position"]
    pos.pop("atr", None)
    pos["mfe"] = 1000.0
    scb.advance_legs("boxatr", pos)
    level = scb.trail_level(pos["entry"], 1000.0, True)
    assert pos["legs"]["lock"]["stop"] == pytest.approx(level)
    assert pos["legs"]["runner"]["stop"] == pytest.approx(level)


def test_paper_lock_leg_exits_alone_and_the_runner_stays():
    client = StubClient()
    entry = 75000.0
    book = split_book(live=False, lock_stop=entry + 30, runner_stop=entry + 10)
    hits = scb.leg_hits(book["position"], entry + 25, entry + 25)
    assert hits == ["lock"]
    assert scb.close_legs(client, "boxatr", book, hits, entry + 30, "stop",
                          "2026-11-02T11:00:00+05:30") is True
    pos = book["position"]
    assert list(pos["legs"]) == ["runner"] and pos["lots"] == 1
    assert book["closed"][0]["lots"] == 1 and book["closed"][0]["gross"] == pytest.approx(30.0)


def test_live_lock_leg_sells_one_lot_and_reparks_the_backstop():
    client = StubClient(held=2 * LEG["lotsize"])
    client.option_price = 450.0
    entry = 75000.0
    client.index_price = entry + 28
    book = split_book(lock_stop=entry + 30, runner_stop=entry + 10)
    assert scb.close_legs(client, "boxatr", book, ["lock"], entry + 28, "stop",
                          "2026-11-02T11:00:00+05:30") is True
    pos = book["position"]
    assert "SLM2" in client.cancelled                       # old backstop cleared first
    sells = placed(client, "SELL", "MARKET")
    assert len(sells) == 1 and sells[0]["quantity"] == LEG["lotsize"]
    slms = placed(client, "SELL", "SL-M")
    assert slms and slms[-1]["quantity"] == LEG["lotsize"]  # re-parked for the runner only
    assert pos["qty"] == LEG["lotsize"] and pos["slm_orderid"] == slms[-1]["orderid"]
    assert not pos.get("emergency_exit")
    assert client.events.index("place:SELL:MARKET") < client.events.index("place:SELL:SL-M")


def test_live_leg_exit_without_a_new_backstop_flattens_the_rest():
    client = StubClient(held=2 * LEG["lotsize"])      # no option quote: the re-park cannot size
    entry = 75000.0
    client.index_price = entry + 28
    book = split_book(lock_stop=entry + 30, runner_stop=entry + 10)
    scb.close_legs(client, "boxatr", book, ["lock"], entry + 28, "stop", "2026-11-02T11:00:00+05:30")
    assert book["position"]["emergency_exit"] is True


def test_both_legs_hit_close_the_whole_position_in_one_sell():
    client = StubClient()
    book = split_book()
    pos = book["position"]
    hits = scb.leg_hits(pos, 74800.0, 74800.0)
    assert sorted(hits) == ["lock", "runner"]
    assert scb.close_legs(client, "boxatr", book, hits, 74845.0, "stop",
                          "2026-11-02T11:00:00+05:30") is True
    assert book["position"] is None
    sells = placed(client, "SELL", "MARKET")
    assert len(sells) == 1 and sells[0]["quantity"] == 2 * LEG["lotsize"]
    assert book["closed"][0]["lots"] == 2


def test_refused_leg_sale_is_retried_and_the_backstop_put_back():
    client = StubClient(sell_status=("rejected", 0.0), held=2 * LEG["lotsize"])
    client.option_price = 450.0
    entry = 75000.0
    client.index_price = entry + 28
    book = split_book(lock_stop=entry + 30, runner_stop=entry + 10)
    assert scb.close_legs(client, "boxatr", book, ["lock"], entry + 28, "stop",
                          "2026-11-02T11:00:00+05:30") is False
    pos = book["position"]
    assert pos["legs"]["lock"]["exit_pending"] is True
    assert placed(client, "SELL", "SL-M")[-1]["quantity"] == 2 * LEG["lotsize"]
    assert scb.leg_hits(pos, entry + 50, entry + 60) == ["lock"]   # retried next poll
