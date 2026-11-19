"""Sector 09:20 momentum traded with the stock's ATM option.

Pins the user's order of work (sector, then an F&O stock, then its lot size,
then one ATM lot), that exits are read off the stock price and never the
option's, that a dry run never sends an order, and that a live exit the
broker refuses is retried rather than booked.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import time as dtime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATHS = (
    ROOT / "strategies" / "examples" / "sector_920_momentum.py",
    ROOT / "strategies" / "scripts" / "sector_920_momentum_20260930231437.py",
)


def load(path: Path, monkeypatch):
    if not path.exists():
        pytest.skip(f"{path.name} is not on this machine")
    stub = types.ModuleType("openalgo")
    stub.api = type("ApiStub", (), {})
    monkeypatch.setitem(sys.modules, "openalgo", stub)
    spec = importlib.util.spec_from_file_location(f"strat_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("ORDER_POLL_SECONDS", "MANUAL_EXIT_RECHECK_SECONDS", "POLL_SECONDS"):
        monkeypatch.setattr(module, name, 0)
    monkeypatch.setattr(module, "ORDER_POLL_TRIES", 2)
    monkeypatch.setattr(module, "save_state", lambda state: None)
    # run any time of day: entry window open, square-off far away
    monkeypatch.setattr(module, "LATEST_ENTRY", dtime(23, 59, 59))
    monkeypatch.setattr(module, "SQUARE_OFF", dtime(23, 59, 59))
    return module


class Broker:
    """Scripted client for quotes, option lookups and orders."""

    def __init__(self, stock_prices=None, fno=("BRITANNIA", "WIPRO"), sell_status="complete"):
        self.stock_prices = list(stock_prices or [100.0])
        self.fno = set(fno)
        self.sell_status = sell_status
        self.orders: list[dict] = []
        self.held = 0
        self.option_quotes = 0

    # market data
    def multiquotes(self, symbols):
        moves = {"NIFTY": 0.5, "MARICO": 5.5, "NETWORK18": 3.0, "BRITANNIA": 2.4,
                 "WIPRO": 1.0, "DABUR": -0.5}
        return {"status": "success", "results": [
            {"symbol": s["symbol"], "data": {"ltp": 100 * (1 + moves.get(s["symbol"], 0) / 100),
                                             "prev_close": 100}} for s in symbols]}

    def quotes(self, symbol, exchange):
        if exchange == "NFO":
            self.option_quotes += 1
            return {"status": "success", "data": {"ltp": 50.0, "bid": 49.5, "ask": 50.5}}
        price = self.stock_prices.pop(0) if len(self.stock_prices) > 1 else self.stock_prices[0]
        return {"status": "success", "data": {"ltp": price}}

    def expiry(self, symbol, exchange, instrumenttype):
        data = ["01-JAN-20", "30-DEC-30"] if symbol in self.fno else []
        return {"status": "success", "data": data}

    def optionsymbol(self, underlying, exchange, expiry_date, offset, option_type):
        lot = {"BRITANNIA": 125, "WIPRO": 3000}[underlying]
        return {"status": "success", "symbol": f"{underlying}{expiry_date}100{option_type}",
                "lotsize": lot, "underlying_ltp": 100.0}

    # orders
    def analyzerstatus(self):
        return {"status": "success", "data": {"analyze_mode": False}}

    def placeorder(self, **kw):
        oid = str(len(self.orders) + 1)
        self.orders.append({**kw, "orderid": oid})
        if kw["action"] == "BUY":
            self.held += kw["quantity"]
        elif self.sell_status == "complete":
            self.held -= kw["quantity"]
        return {"status": "success", "orderid": oid}

    def orderstatus(self, order_id, strategy):
        order = self.orders[int(order_id) - 1]
        status = "complete" if order["action"] == "BUY" else self.sell_status
        return {"status": "success", "data": {"order_status": status, "average_price": 51.0}}

    def cancelorder(self, order_id, strategy):
        return {"status": "success"}

    def positionbook(self):
        return {"status": "success", "data": [] if not self.held else [
            {"symbol": self.orders[0]["symbol"], "exchange": "NFO", "quantity": self.held}]}


SECTORS = {"Nifty FMCG": ["MARICO", "NETWORK18", "BRITANNIA", "DABUR"],
           "Nifty IT": ["WIPRO"]}


@pytest.mark.parametrize("path", PATHS)
def test_pick_skips_beyond_4pct_and_non_fno(path, monkeypatch):
    m = load(path, monkeypatch)
    choice = m.pick(Broker(), SECTORS)
    # MARICO +5.5% is beyond 4%, NETWORK18 has no options: BRITANNIA is taken.
    assert choice["symbol"] == "BRITANNIA"
    assert choice["side"] == "long"
    assert choice["expiry"] == "30DEC30"


@pytest.mark.parametrize("path", PATHS)
def test_no_trade_when_no_fno_stock_in_sector(path, monkeypatch):
    m = load(path, monkeypatch)
    assert m.pick(Broker(fno=()), SECTORS) is None


@pytest.mark.parametrize("path", PATHS)
def test_dry_run_never_sends_an_order(path, monkeypatch):
    m = load(path, monkeypatch)
    monkeypatch.setattr(m, "TRADE_LIVE", False)
    broker = Broker()
    choice = {"side": "long", "symbol": "BRITANNIA", "expiry": "30DEC30"}
    state = {}
    assert m.open_books(broker, state, choice)
    assert broker.orders == []
    book = state["books"]["1R"]
    assert book["option"] == "BRITANNIA30DEC30100CE"
    assert book["quantity"] == 125 and not book["live"]


@pytest.mark.parametrize("path", PATHS)
def test_short_buys_one_lot_atm_pe_with_its_own_lot_size(path, monkeypatch):
    m = load(path, monkeypatch)
    monkeypatch.setattr(m, "TRADE_LIVE", True)
    broker = Broker()
    state = {}
    assert m.open_books(broker, state, {"side": "short", "symbol": "WIPRO", "expiry": "30DEC30"})
    assert len(broker.orders) == 1
    o = broker.orders[0]
    assert (o["action"], o["symbol"], o["quantity"], o["exchange"]) == ("BUY", "WIPRO30DEC30100PE", 3000, "NFO")
    assert state["books"][m.LIVE_BOOK]["live"]
    assert state["books"][m.LIVE_BOOK]["premium_in"] == 51.0
    others = [b for n, b in state["books"].items() if n != m.LIVE_BOOK]
    assert others and not any(b["live"] for b in others)


@pytest.mark.parametrize("path", PATHS)
def test_exit_is_read_off_the_stock_price(path, monkeypatch):
    m = load(path, monkeypatch)
    monkeypatch.setattr(m, "TRADE_LIVE", True)
    monkeypatch.setattr(m, "POSITION_CHECK_SECONDS", 10_000)
    # Entry at 100, then the stock falls through the 1% stop.
    broker = Broker(stock_prices=[100.0, 99.5, 98.9])
    state = {"pick": {"symbol": "BRITANNIA", "side": "long"}}
    m.open_books(broker, state, {"side": "long", "symbol": "BRITANNIA", "expiry": "30DEC30"})
    quotes_before = broker.option_quotes
    m.manage(broker, state)
    for book in state["books"].values():
        assert book["why"] == "stop"
        assert book["exit"] == 98.9
    sells = [o for o in broker.orders if o["action"] == "SELL"]
    assert len(sells) == 1 and sells[0]["quantity"] == 125
    # option quotes are read only for the paper premium record, never to decide
    assert broker.option_quotes - quotes_before <= len(state["books"])


@pytest.mark.parametrize("path", PATHS)
def test_refused_live_sell_is_not_booked(path, monkeypatch):
    m = load(path, monkeypatch)
    monkeypatch.setattr(m, "TRADE_LIVE", True)
    broker = Broker(sell_status="rejected")
    state = {}
    m.open_books(broker, state, {"side": "long", "symbol": "BRITANNIA", "expiry": "30DEC30"})
    book = state["books"][m.LIVE_BOOK]
    m.close_book(broker, m.LIVE_BOOK, book, 98.9, "stop")
    assert book["exit"] is None and book["exit_pending"] == "stop"
    # the lot was sold by hand meanwhile: the next try sees it flat, no second sell
    broker.held = 0
    book["last_exit_try"] = 0
    m.close_book(broker, m.LIVE_BOOK, book, 98.8, "stop")
    assert book["exit"] == 98.8
    assert len([o for o in broker.orders if o["action"] == "SELL"]) == 2


@pytest.mark.parametrize("path", PATHS)
def test_square_off_is_1515(path, monkeypatch):
    if not path.exists():
        pytest.skip(f"{path.name} is not on this machine")
    # load() moves SQUARE_OFF out of the way for the other tests; read the file's own value
    text = path.read_text(encoding="utf-8")
    assert "SQUARE_OFF = dtime(15, 15)" in text


@pytest.mark.parametrize("path", PATHS)
def test_open_trade_closes_at_the_time_limit(path, monkeypatch):
    m = load(path, monkeypatch)
    monkeypatch.setattr(m, "TRADE_LIVE", True)
    monkeypatch.setattr(m, "POSITION_CHECK_SECONDS", 10_000)
    # stock drifts but never reaches stop (99.0) or target; the time limit has passed
    broker = Broker(stock_prices=[100.0, 100.3])
    state = {"pick": {"symbol": "BRITANNIA", "side": "long"}}
    m.open_books(broker, state, {"side": "long", "symbol": "BRITANNIA", "expiry": "30DEC30"})
    monkeypatch.setattr(m, "SQUARE_OFF", dtime(0, 0))
    m.manage(broker, state)
    for book in state["books"].values():
        assert book["why"] == "square-off"
    sells = [o for o in broker.orders if o["action"] == "SELL"]
    assert len(sells) == 1 and sells[0]["quantity"] == 125
