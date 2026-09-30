"""Refresh data/sector_constituents.json from NSE's published index constituent lists.

The Sector Heatmap tool (/sectorheatmap) groups stocks by NSE sectoral index.
NSE revises index constituents twice a year (March and September reviews), so
re-run this after a review:

    uv run python scripts/update_sector_constituents.py

Each list comes from nsearchives.nseindia.com/content/indices/<file>.csv, which
carries Company Name, Industry, Symbol, Series and ISIN. Only EQ-series rows are
kept. A list that fails to download keeps its previous entry, so a partial NSE
outage never empties a sector.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from datetime import UTC, datetime, timezone
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parents[1] / "data" / "sector_constituents.json"
BASE = "https://nsearchives.nseindia.com/content/indices/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# (key, display name, NSE csv file, OpenAlgo NSE_INDEX symbol or None)
# The index symbol is only a hint for showing the official index change; the
# service checks it against the broker's master contract at runtime.
SECTORS = [
    ("AUTO", "Nifty Auto", "ind_niftyautolist.csv", "NIFTYAUTO"),
    ("BANK", "Nifty Bank", "ind_niftybanklist.csv", "BANKNIFTY"),
    ("CONSUMER_DURABLES", "Nifty Consumer Durables", "ind_niftyconsumerdurableslist.csv", None),
    ("FIN_SERVICE", "Nifty Financial Services", "ind_niftyfinancelist.csv", "FINNIFTY"),
    ("FIN_SERVICE_25_50", "Nifty Financial Services 25/50", "ind_niftyfinancialservices25-50list.csv", None),
    ("FMCG", "Nifty FMCG", "ind_niftyfmcglist.csv", "NIFTYFMCG"),
    ("HEALTHCARE", "Nifty Healthcare", "ind_niftyhealthcarelist.csv", None),
    ("IT", "Nifty IT", "ind_niftyitlist.csv", "NIFTYIT"),
    ("MEDIA", "Nifty Media", "ind_niftymedialist.csv", "NIFTYMEDIA"),
    ("METAL", "Nifty Metal", "ind_niftymetallist.csv", "NIFTYMETAL"),
    ("OIL_GAS", "Nifty Oil & Gas", "ind_niftyoilgaslist.csv", None),
    ("PHARMA", "Nifty Pharma", "ind_niftypharmalist.csv", "NIFTYPHARMA"),
    ("PVT_BANK", "Nifty Private Bank", "ind_nifty_privatebanklist.csv", "NIFTYPVTBANK"),
    ("PSU_BANK", "Nifty PSU Bank", "ind_niftypsubanklist.csv", "NIFTYPSUBANK"),
    ("REALTY", "Nifty Realty", "ind_niftyrealtylist.csv", "NIFTYREALTY"),
    ("MIDSMALL_FIN_SERVICE", "Nifty MidSmall Financial Services", "ind_niftymidsmallfinancailservice_list.csv", None),
    ("MIDSMALL_HEALTHCARE", "Nifty MidSmall Healthcare", "ind_niftymidsmallhealthcare_list.csv", None),
    ("MIDSMALL_IT_TELECOM", "Nifty MidSmall IT & Telecom", "ind_niftymidsmallitAndtelecom_list.csv", None),
]


def fetch(client: httpx.Client, filename: str) -> list[dict]:
    """Download one NSE constituent CSV and return its EQ-series rows."""
    resp = client.get(BASE + filename)
    resp.raise_for_status()
    rows = []
    for row in csv.DictReader(io.StringIO(resp.text)):
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        if row.get("Series") != "EQ" or not row.get("Symbol"):
            continue
        rows.append({"symbol": row["Symbol"], "name": row.get("Company Name", ""),
                     "industry": row.get("Industry", "")})
    return rows


def main() -> int:
    previous = {}
    if OUT.exists():
        previous = {s["key"]: s for s in json.loads(OUT.read_text(encoding="utf-8")).get("sectors", [])}

    sectors, failed = [], []
    with httpx.Client(headers=HEADERS, timeout=20.0, follow_redirects=True) as client:
        for key, name, filename, index_symbol in SECTORS:
            try:
                stocks = fetch(client, filename)
                if not stocks:
                    raise ValueError("no EQ rows")
            except (httpx.HTTPError, ValueError) as exc:
                failed.append(f"{name}: {exc}")
                if key in previous:
                    sectors.append(previous[key])
                continue
            sectors.append({"key": key, "name": name, "source": BASE + filename,
                            "index_symbol": index_symbol, "stocks": stocks})
            print(f"{name:36} {len(stocks):3} stocks")

    if not sectors:
        print("nothing downloaded; data/sector_constituents.json left unchanged")
        return 1
    payload = {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
               "sectors": sectors}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    unique = len({s["symbol"] for sec in sectors for s in sec["stocks"]})
    print(f"wrote {OUT} - {len(sectors)} sectors, {unique} unique stocks")
    for line in failed:
        print(f"FAILED (kept previous list if any): {line}")
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
