"""Live order settlement for the NIFTY and BANKNIFTY two-candle strategies.

A placeorder "success" only means the broker accepted the order. On
2026-10-05 three SENSEX exit sells were accepted and then rejected, so these
pin that both two-candle strategies read every market order to a final state,
never book an exit the broker refused, never open a position on a buy that did
not fill, and never sell while a cancelled resting stop may still be live.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NIFTY_PATHS = (
    ROOT / "strategies" / "examples" / "nifty_2candle_vwap_sma45.py",
    ROOT / "strategies" / "scripts" / "nifty_2candle_vwap_sma45_20260908211324.py",
)
BNF_PATHS = (
    ROOT / "strategies" / "examples" / "banknifty_2candle_3m.py",
    ROOT / "strategies" / "scripts" / "banknifty_2candle_3m_20260930231330.py",
)
SYMBOL = "NIFTY03NOV2625900CE"


def load(path: Path, monkeypatch):
    """Import a strategy with an inert OpenAlgo API class; no client is built."""
    if not path.exists():
        pytest.skip(f"{path.name} is not on this machine")
    stub = types.ModuleType("openalgo")
    stub.api = type("ApiStub", (), {})
    monkeypatch.setitem(sys.modules, "openalgo", stub)
    spec = importlib.util.spec_from_file_location(f"strat_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("ORDER_POLL_SECONDS", "CANCEL_CONFIRM_SECONDS",
                 "MANUAL_EXIT_RECHECK_SECONDS"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, 0)
    monkeypatch.setattr(module, "ORDER_POLL_TRIES", 2)
    monkeypatch.setattr(module, "DRY_RUN", False)
    return module


class Broker:
    """Scripted client: market orders settle to `market_status`, stops rest."""

    def __init__(self, market_status="complete", held=0, cancel_lands=True,
                 place_ok=True):
        self.market_status = market_status
        self.held = held
        self.cancel_lands = cancel_lands
        self.place_ok = place_ok
        self.orders: list[dict] = []
        self.cancelled: list[str] = []

    def placeorder(self, **kw):
        if not self.place_ok:
            return {"status": "error", "message": "RMS"}
        oid = f"{kw['price_type']}-{len(self.orders) + 1}"
        self.orders.append({**kw, "orderid": oid})
        return {"status": "success", "orderid": oid}

    def orderstatus(self, *, order_id, strategy="Python", **kw):
        if order_id.startswith("SL"):
            status = "cancelled" if (order_id in self.cancelled and self.cancel_lands) \
                else "trigger pending"
            return {"status": "success", "data": {"order_status": status}}
        price = 101.5 if self.market_status == "complete" else 0
        return {"status": "success",
                "data": {"order_status": self.market_status, "average_price": price}}

    def cancelorder(self, *, order_id, strategy="Python", **kw):
        self.cancelled.append(order_id)
        return {"status": "success", "orderid": order_id}

    def positionbook(self, **kw):
        if self.held is None:
            return {"status": "error", "message": "book unavailable"}
        return {"status": "success",
                "data": [{"symbol": SYMBOL, "exchange": "NFO", "quantity": str(self.held)}]}

    def quotes(self, **kw):
        return {"status": "success", "data": {"ltp": 100.0}}

    def orderbook(self, **kw):
        return {"status": "success", "data": {"orders": []}}

    def optiongreeks(self, **kw):
        return {"status": "success", "option_price": 200.0,
                "greeks": {"delta": 0.6, "gamma": 0.0, "theta": 0.0, "vega": 0.0}}

    def market(self, action):
        return [o for o in self.orders if o["action"] == action and o["price_type"] == "MARKET"]


@pytest.mark.parametrize("path", NIFTY_PATHS + BNF_PATHS)
def test_market_order_reads_an_accepted_order_to_its_final_state(path, monkeypatch):
    strat = load(path, monkeypatch)
    assert strat.market_order(Broker("complete"), SYMBOL, "SELL", 65) == ("complete", 101.5)
    assert strat.market_order(Broker("rejected"), SYMBOL, "SELL", 65) == ("rejected", None)
    assert strat.market_order(Broker(place_ok=False), SYMBOL, "SELL", 65) == ("rejected", None)


@pytest.mark.parametrize("path", NIFTY_PATHS + BNF_PATHS)
def test_a_working_market_order_is_cancelled_then_reported_unknown(path, monkeypatch):
    strat = load(path, monkeypatch)
    broker = Broker("open")
    assert strat.market_order(broker, SYMBOL, "SELL", 65) == ("unknown", None)
    assert broker.cancelled == ["MARKET-1"]


# ----------------------------------------------------------------------------
# NIFTY
# ----------------------------------------------------------------------------

def nifty_state(stop_id="SL-0"):
    return {"date": "2026-11-02", "trades": 1, "position": {
        "direction": "long", "symbol": SYMBOL, "lotsize": 65, "lots_open": 1,
        "entry": 25900.0, "stop": 25800.0, "stop0": 25800.0, "risk": 100.0,
        "mfe": 0.0, "opened": "2026-11-02T10:20:00+05:30", "stop_order_id": stop_id,
        "stop_trigger": 60.0, "broker_basis": 25800.0}}


@pytest.mark.parametrize("path", NIFTY_PATHS)
def test_nifty_exit_keeps_the_position_when_the_sell_is_rejected(path, monkeypatch):
    strat = load(path, monkeypatch)
    broker = Broker("rejected", held=65)
    state = strat.exit_position(broker, nifty_state(), "STOP hit")
    assert state["position"] is not None              # still held, retried next bar


@pytest.mark.parametrize("path", NIFTY_PATHS)
def test_nifty_exit_clears_when_the_sell_fills(path, monkeypatch):
    strat = load(path, monkeypatch)
    broker = Broker("complete")
    state = strat.exit_position(broker, nifty_state(), "STOP hit")
    assert state["position"] is None
    assert broker.cancelled[0] == "SL-0"              # stop pulled before the sell
    assert len(broker.market("SELL")) == 1


@pytest.mark.parametrize("path", NIFTY_PATHS)
def test_nifty_does_not_sell_while_a_cancelled_stop_still_rests(path, monkeypatch):
    strat = load(path, monkeypatch)
    broker = Broker("complete", cancel_lands=False)
    state = strat.exit_position(broker, nifty_state(), "STOP hit")
    assert state["position"] is not None
    assert broker.market("SELL") == []                # no second SELL on one lot


@pytest.mark.parametrize("path", NIFTY_PATHS)
def test_nifty_square_off_clears_a_position_already_flat(path, monkeypatch):
    strat = load(path, monkeypatch)
    broker = Broker("rejected", held=0)
    state = strat.square_off(broker, nifty_state())
    assert state["position"] is None


# ----------------------------------------------------------------------------
# BANKNIFTY
# ----------------------------------------------------------------------------

def bnf_book():
    return {"trades": 1, "closed": [], "free_from": None, "position": {
        "direction": "short", "entry": 54700.0, "stop": 54930.0, "target": 54600.0,
        "risk": 230.0, "opened": "2026-11-02T10:54:00+05:30",
        "entry_bar": "2026-11-02T10:54:00+05:30", "symbol": SYMBOL, "qty": 30,
        "lotsize": 30, "live": True, "premium_in": 820.0, "stop_order_id": "SL-0"}}


@pytest.mark.parametrize("path", BNF_PATHS)
def test_bnf_exit_keeps_the_position_when_the_sell_is_rejected(path, monkeypatch):
    strat = load(path, monkeypatch)
    broker = Broker("rejected", held=30)
    book = bnf_book()
    assert strat.close_position(broker, strat.LIVE_BOOK, book, 54600.0, "target") is False
    assert book["position"] is not None
    assert book["closed"] == []


@pytest.mark.parametrize("path", BNF_PATHS)
def test_bnf_exit_books_the_real_fill(path, monkeypatch):
    strat = load(path, monkeypatch)
    broker = Broker("complete")
    book = bnf_book()
    assert strat.close_position(broker, strat.LIVE_BOOK, book, 54600.0, "target") is True
    assert book["position"] is None
    assert book["closed"][0]["premium_out"] == 101.5
    assert broker.cancelled[0] == "SL-0"


@pytest.mark.parametrize("path", BNF_PATHS)
def test_bnf_does_not_sell_while_a_cancelled_stop_still_rests(path, monkeypatch):
    strat = load(path, monkeypatch)
    broker = Broker("complete", cancel_lands=False)
    book = bnf_book()
    assert strat.close_position(broker, strat.LIVE_BOOK, book, 54600.0, "target") is False
    assert broker.market("SELL") == []


@pytest.mark.parametrize("path", BNF_PATHS)
def test_bnf_notices_a_hand_exit_and_pulls_the_stop(path, monkeypatch):
    strat = load(path, monkeypatch)
    broker = Broker(held=0)
    book = bnf_book()
    assert strat.closed_outside(broker, strat.LIVE_BOOK, book) is True
    assert book["position"] is None
    assert book["closed"][0]["why"] == "closed-outside"
    assert "SL-0" in broker.cancelled


@pytest.mark.parametrize("path", BNF_PATHS)
def test_bnf_leaves_a_held_or_unreadable_position_alone(path, monkeypatch):
    strat = load(path, monkeypatch)
    for held in (30, None):
        book = bnf_book()
        broker = Broker(held=held)
        assert strat.closed_outside(broker, strat.LIVE_BOOK, book) is False
        assert book["position"] is not None
        assert broker.cancelled == []


# ----------------------------------------------------------------------------
# NIFTY breakeven-plus lock (2026-11-02 refinement)
# ----------------------------------------------------------------------------

def nifty_bar(high: float, low: float, close: float):
    import pandas as pd
    stamp = pd.Timestamp("2026-11-02 10:30", tz="Asia/Kolkata")
    return pd.DataFrame({"open": [close], "high": [high], "low": [low], "close": [close]},
                        index=[stamp])


@pytest.mark.parametrize("path", NIFTY_PATHS)
def test_nifty_breakeven_locks_profit_once_the_trade_runs(path, monkeypatch):
    strat = load(path, monkeypatch)
    monkeypatch.setattr(strat, "BROKER_STOP", False)
    if strat.BREAKEVEN_AT_PTS is None:
        pytest.skip("breakeven is off in this copy")
    state = nifty_state(stop_id=None)
    entry = state["position"]["entry"]
    run = strat.BREAKEVEN_AT_PTS + 5
    state = strat.manage(Broker(held=65), state, nifty_bar(entry + run, entry + 5, entry + run - 10))
    assert state["position"]["stop"] == pytest.approx(entry + strat.BREAKEVEN_LOCK_PTS)


@pytest.mark.parametrize("path", NIFTY_PATHS)
def test_nifty_breakeven_waits_for_the_threshold(path, monkeypatch):
    strat = load(path, monkeypatch)
    monkeypatch.setattr(strat, "BROKER_STOP", False)
    if strat.BREAKEVEN_AT_PTS is None:
        pytest.skip("breakeven is off in this copy")
    state = nifty_state(stop_id=None)
    entry = state["position"]["entry"]
    short_of = strat.BREAKEVEN_AT_PTS - 5
    state = strat.manage(Broker(held=65), state, nifty_bar(entry + short_of, entry + 1, entry + 2))
    assert state["position"]["stop"] == pytest.approx(25800.0)



# ----------------------------------------------------------------------------
# NIFTY two-lot split: one lot locks profit, the runner goes to breakeven
# ----------------------------------------------------------------------------

def two_lot_state(strat, stop_id=None):
    state = nifty_state(stop_id=stop_id)
    pos = state["position"]
    pos["lots_open"] = 2
    pos["legs"] = {"lock": {"lots": 1, "stop": pos["stop"]},
                   "runner": {"lots": 1, "stop": pos["stop"]}}
    return state


def needs_split(strat):
    if getattr(strat, "RUNNER_LOTS", 0) < 1 or strat.BREAKEVEN_AT_PTS is None:
        pytest.skip("two-lot split is off in this copy")


@pytest.mark.parametrize("path", NIFTY_PATHS)
def test_entry_builds_a_lock_leg_and_a_runner(path, monkeypatch):
    strat = load(path, monkeypatch)
    needs_split(strat)
    legs = strat.initial_legs(25800.0)
    assert legs == {"lock": {"lots": strat.LOTS - strat.RUNNER_LOTS, "stop": 25800.0},
                    "runner": {"lots": strat.RUNNER_LOTS, "stop": 25800.0}}


@pytest.mark.parametrize("path", NIFTY_PATHS)
def test_split_moves_the_lock_leg_to_profit_and_the_runner_to_entry(path, monkeypatch):
    strat = load(path, monkeypatch)
    needs_split(strat)
    monkeypatch.setattr(strat, "BROKER_STOP", False)
    state = two_lot_state(strat)
    entry = state["position"]["entry"]
    run = strat.BREAKEVEN_AT_PTS + 5
    state = strat.manage(Broker(held=130), state, nifty_bar(entry + run, entry + 20, entry + run - 5))
    legs = state["position"]["legs"]
    assert legs["lock"]["stop"] == pytest.approx(entry + strat.BREAKEVEN_LOCK_PTS)
    assert legs["runner"]["stop"] == pytest.approx(entry + strat.RUNNER_LOCK_PTS)
    # the position stop - what the broker backstop mirrors - is the loosest leg
    assert state["position"]["stop"] == pytest.approx(entry + strat.RUNNER_LOCK_PTS)


@pytest.mark.parametrize("path", NIFTY_PATHS)
def test_lock_leg_exits_alone_and_the_backstop_is_resized(path, monkeypatch):
    strat = load(path, monkeypatch)
    needs_split(strat)
    state = two_lot_state(strat, stop_id="SL-0")
    pos = state["position"]
    entry = pos["entry"]
    pos["mfe"] = 40.0
    pos["legs"]["lock"]["stop"] = entry + 15
    pos["legs"]["runner"]["stop"] = entry
    pos["stop"] = entry
    broker = Broker("complete", held=130)
    # the bar dips to +10: through the lock leg's +15, not the runner's entry stop
    state = strat.manage(broker, state, nifty_bar(entry + 30, entry + 10, entry + 12))
    pos = state["position"]
    assert pos is not None and list(pos["legs"]) == ["runner"]
    assert pos["lots_open"] == 1
    sells = broker.market("SELL")
    assert len(sells) == 1 and sells[0]["quantity"] == 65
    assert broker.cancelled[0] == "SL-0"                     # backstop cleared before the sell
    stops = [o for o in broker.orders if o["price_type"] == "SL"]
    assert stops and stops[-1]["quantity"] == 65             # re-parked for the runner only
    assert pos["stop_order_id"] == stops[-1]["orderid"]


@pytest.mark.parametrize("path", NIFTY_PATHS)
def test_both_legs_hit_together_sell_once(path, monkeypatch):
    strat = load(path, monkeypatch)
    needs_split(strat)
    monkeypatch.setattr(strat, "BROKER_STOP", False)
    state = two_lot_state(strat)
    entry = state["position"]["entry"]
    broker = Broker("complete", held=130)
    state = strat.manage(broker, state, nifty_bar(entry + 5, entry - 150, entry - 140))
    assert state["position"] is None
    sells = broker.market("SELL")
    assert len(sells) == 1 and sells[0]["quantity"] == 130


@pytest.mark.parametrize("path", NIFTY_PATHS)
def test_refused_leg_exit_is_retried_next_bar(path, monkeypatch):
    strat = load(path, monkeypatch)
    needs_split(strat)
    monkeypatch.setattr(strat, "BROKER_STOP", False)
    state = two_lot_state(strat)
    pos = state["position"]
    entry = pos["entry"]
    pos["mfe"] = 40.0
    pos["legs"]["lock"]["stop"] = entry + 15
    pos["legs"]["runner"]["stop"] = entry
    pos["stop"] = entry
    state = strat.manage(Broker("rejected", held=130), state,
                         nifty_bar(entry + 30, entry + 10, entry + 12))
    assert state["position"]["legs"]["lock"]["exit_pending"] is True
    # next bar never comes back down to +15, but the pending exit still goes
    broker = Broker("complete", held=130)
    state = strat.manage(broker, state, nifty_bar(entry + 35, entry + 25, entry + 30))
    assert list(state["position"]["legs"]) == ["runner"]
    assert broker.market("SELL")[0]["quantity"] == 65


@pytest.mark.parametrize("path", NIFTY_PATHS)
def test_hand_sold_lot_comes_off_the_lock_leg_first(path, monkeypatch):
    strat = load(path, monkeypatch)
    needs_split(strat)
    state = two_lot_state(strat)
    strat.shrink_legs(state["position"], 1)
    assert list(state["position"]["legs"]) == ["runner"]
    assert state["position"]["lots_open"] == 1
