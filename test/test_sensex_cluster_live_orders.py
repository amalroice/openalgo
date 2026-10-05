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
                 slm_status=("open", 0.0), cancel_ok=True):
        self.buy_ok = buy_ok
        self.buy_status = buy_status
        self.sell_status = sell_status
        self.slm_ok = slm_ok
        self.slm_status = slm_status
        self.cancel_ok = cancel_ok
        self.greeks_ok = True
        self.index_price = 75000.0
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
        elif order_id.startswith("BUY"):
            state, price = self.buy_status
        else:
            state, price = self.sell_status
        return {"status": "success",
                "data": {"order_status": state, "average_price": price}}

    def quotes(self, *, symbol, exchange, **kw):
        if symbol == scb.UNDERLYING and exchange == scb.INDEX_EXCHANGE:
            return {"status": "success", "data": {"ltp": self.index_price}}
        return {"status": "error", "message": "quote unavailable"}

    def optiongreeks(self, *, symbol, exchange, **kw):
        self.events.append(f"greeks:{symbol}:{exchange}")
        if not self.greeks_ok:
            return {"status": "error", "message": "Greeks unavailable"}
        return {"status": "success", "option_price": 400.0,
                "greeks": {"delta": 0.8, "gamma": 0.0, "theta": 0.0, "vega": 0.0}}

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
    assert slm["quantity"] == LEG["lotsize"]
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
