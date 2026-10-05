"""Regression tests for BANKNIFTY live stop direction and breadth selection."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STRATEGY_PATHS = (
    ROOT / "strategies" / "examples" / "banknifty_2candle_3m.py",
    ROOT / "strategies" / "scripts" / "banknifty_2candle_3m_20260930231330.py",
)


def load_strategy(path: Path, monkeypatch):
    """Import the strategy with an inert OpenAlgo API class; no client is built."""
    openalgo_stub = types.ModuleType("openalgo")
    openalgo_stub.api = type("ApiStub", (), {})
    monkeypatch.setitem(sys.modules, "openalgo", openalgo_stub)
    spec = importlib.util.spec_from_file_location(f"banknifty_{path.parent.name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", STRATEGY_PATHS)
def test_atr_stop_is_on_losing_side_for_both_directions(path, monkeypatch):
    strategy = load_strategy(path, monkeypatch)
    entry, atr = 54686.75, 78.78

    short_stop = strategy.atr_stop_level(entry, atr, long=False)
    long_stop = strategy.atr_stop_level(entry, atr, long=True)

    assert short_stop == pytest.approx(54923.09)
    assert short_stop > entry
    assert long_stop == pytest.approx(54450.41)
    assert long_stop < entry


@pytest.mark.parametrize("path", STRATEGY_PATHS)
def test_breadth_book_is_the_only_live_book(path, monkeypatch):
    strategy = load_strategy(path, monkeypatch)

    assert strategy.LIVE_BOOK == "breadth"
    assert "breadth" in strategy.BOOKS
    assert strategy.BREADTH_MIN == 2.0
    assert strategy.breadth_passes(12, 2)
    assert strategy.breadth_passes(2, 12)
    assert not strategy.breadth_passes(7, 7)


@pytest.mark.parametrize("path", STRATEGY_PATHS)
@pytest.mark.parametrize("atr", [0.0, -1.0, float("nan"), float("inf")])
def test_atr_stop_rejects_invalid_atr(path, atr, monkeypatch):
    strategy = load_strategy(path, monkeypatch)
    with pytest.raises(ValueError, match="ATR must be finite and positive"):
        strategy.atr_stop_level(54686.75, atr, long=False)
