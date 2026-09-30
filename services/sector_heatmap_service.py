"""Sector heatmap universe service.

Serves the NSE sectoral-index constituent lists consumed by the
``/sectorheatmap`` page. The lists live in ``data/sector_constituents.json``,
generated from NSE's published constituent CSVs by
``scripts/update_sector_constituents.py``.

Only the static universe is returned here. Live prices and the percentage
change are computed on the client from the shared market-data WebSocket, so
this service performs pure file/cache reads and never calls the broker (no new
file descriptors).
"""

from __future__ import annotations

import json
import os
from typing import Any

from database.token_db_enhanced import get_tokens_bulk
from utils.logging import get_logger

logger = get_logger(__name__)

DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "sector_constituents.json")
STOCK_EXCHANGE = "NSE"
INDEX_EXCHANGE = "NSE_INDEX"

# The file changes only when the update script is re-run; reload it then.
_cache: dict[str, Any] = {"mtime": None, "data": None}


def _load() -> dict[str, Any]:
    """Read the constituent file, reusing the parsed copy until it changes."""
    mtime = os.path.getmtime(DATA_FILE)
    if _cache["mtime"] != mtime:
        with open(DATA_FILE, encoding="utf-8") as f:
            _cache["data"] = json.load(f)
        _cache["mtime"] = mtime
    return _cache["data"]


def get_sector_universe() -> tuple[bool, dict[str, Any], int]:
    """Return every sector with its constituents, validated against the master contract.

    Stocks the broker's master contract does not list are dropped (and counted),
    so the page never subscribes to a symbol the feed cannot resolve. A
    sector's official index symbol is included only when the master lists it;
    otherwise the page derives the sector's change from its stocks alone.

    Returns:
        (success, response body, HTTP status code).
    """
    try:
        raw = _load()
    except FileNotFoundError:
        return False, {"status": "error", "message": "Sector list not found. Run scripts/update_sector_constituents.py"}, 500
    except (OSError, ValueError) as e:
        logger.exception(f"Could not read the sector constituent file: {e}")
        return False, {"status": "error", "message": "Sector list is unreadable"}, 500

    sectors = raw.get("sectors", [])
    stock_symbols = sorted({s["symbol"] for sec in sectors for s in sec.get("stocks", [])})
    index_symbols = sorted({sec["index_symbol"] for sec in sectors if sec.get("index_symbol")})
    pairs = [(s, STOCK_EXCHANGE) for s in stock_symbols] + [(s, INDEX_EXCHANGE) for s in index_symbols]
    tokens = get_tokens_bulk(pairs)
    known = {pair for pair, token in zip(pairs, tokens, strict=True) if token}

    out, missing = [], set()
    for sec in sectors:
        stocks = []
        for s in sec.get("stocks", []):
            if (s["symbol"], STOCK_EXCHANGE) in known:
                stocks.append({"symbol": s["symbol"], "name": s.get("name", ""), "industry": s.get("industry", "")})
            else:
                missing.add(s["symbol"])
        index_symbol = sec.get("index_symbol")
        out.append({
            "key": sec["key"],
            "name": sec["name"],
            "index_symbol": index_symbol if (index_symbol, INDEX_EXCHANGE) in known else None,
            "stocks": stocks,
        })

    symbols = [{"symbol": s, "exchange": STOCK_EXCHANGE} for s in stock_symbols if (s, STOCK_EXCHANGE) in known]
    symbols += [{"symbol": sec["index_symbol"], "exchange": INDEX_EXCHANGE} for sec in out if sec["index_symbol"]]
    if missing:
        logger.info(f"Sector heatmap: {len(missing)} constituents not in the master contract: {sorted(missing)}")

    return True, {
        "status": "success",
        "data": {
            "sectors": out,
            "symbols": symbols,
            "counts": {"sectors": len(out), "stocks": len(stock_symbols) - len(missing), "missing": len(missing)},
            "generated_at": raw.get("generated_at"),
        },
    }, 200
