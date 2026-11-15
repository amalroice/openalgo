"""Append one day of 5m bars to workspace/indicators/data/long2016 for NIFTY, BANKNIFTY,
INDIAVIX and the NIFTY 50 constituents. Each changed file is backed up once as
<name>.bak<YYYYMMDD> before writing.

Run:  uv run python backtesting/robustness/fetch_day.py 2026-11-13
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "workspace" / "indicators" / "backtests"))

import nifty_percent_trail_replay as hz  # noqa: E402
from openalgo import api  # noqa: E402

from database.auth_db import get_api_key_for_tradingview  # noqa: E402

DATA = REPO / "workspace/indicators/data/long2016"
_CLIENT = []


def _client():
    if not _CLIENT:
        _CLIENT.append(
            api(api_key=get_api_key_for_tradingview("Amal Roice"), host="http://127.0.0.1:5000")
        )
    return _CLIENT[0]


def hist(sym: str, ex: str, DAY: str) -> pd.DataFrame:
    for attempt in range(1, 4):
        try:
            res = _client().history(
                symbol=sym, exchange=ex, interval="5m", start_date=DAY, end_date=DAY
            )
            if hasattr(res, "empty"):
                return res
            print(f"  {sym}: {str(res)[:120]}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym} raised {exc!r}", flush=True)
        time.sleep(1.5 * attempt)
    return pd.DataFrame()


def append(path: Path, new: pd.DataFrame, DAY: str) -> int:
    if new.empty:
        return 0
    old = pd.read_parquet(path)
    new = new.copy()
    new.index = pd.to_datetime(new.index)
    if new.index.tz is None:
        new.index = new.index.tz_localize("Asia/Kolkata")
    new.index = new.index.tz_convert(old.index.tz)
    new.index.name = old.index.name
    for c in old.columns:
        if c not in new:
            new[c] = 0
    new = new[old.columns].astype(old.dtypes.to_dict())
    bak = path.with_name(path.name + f".bak{DAY.replace('-', '')}")
    if not bak.exists():
        shutil.copy2(path, bak)
    both = pd.concat([old, new]).sort_index()
    both = both[~both.index.duplicated(keep="last")]
    both.to_parquet(path)
    return len(both) - len(old)


def fetch(DAY: str, quiet: bool = False) -> int:
    """Append DAY's 5m bars to every file. Returns how many files gained bars."""
    jobs = [
        ("NIFTY", "NSE_INDEX", "NIFTY_NSE_INDEX"),
        ("BANKNIFTY", "NSE_INDEX", "BANKNIFTY_NSE_INDEX"),
        ("INDIAVIX", "NSE_INDEX", "INDIAVIX_NSE_INDEX"),
    ]
    jobs += [(s, "NSE", f"{hz.FILE_FIXUP.get(s, s)}_NSE") for s in hz.CONSTITUENTS]
    changed = 0
    for sym, ex, stem in jobs:
        path = DATA / f"{stem}.parquet"
        if not path.is_file():
            print(f"  no file {path.name}, skipped", flush=True)
            continue
        n = append(path, hist(sym, ex, DAY), DAY)
        changed += n > 0
        if not quiet:
            print(f"{stem}: +{n} bars", flush=True)
        time.sleep(0.3)
    return changed


if __name__ == "__main__":
    fetch(sys.argv[1])
