"""Shared constants and helpers for both EDGAR clients (XBRL + 8-K).

SEC requires a User-Agent containing contact info on every request.
Rate limit: 10 requests/sec across all endpoints.

CIK map is the top universe we support. Add entries as new tickers come
online by looking them up at https://www.sec.gov/files/company_tickers.json.
"""
from __future__ import annotations

DEFAULT_UA = "truflation-eps-markets/1.0 angd1399@gmail.com"

# Mag 7 scope. 10-digit zero-padded CIK per ticker. Verified against
# https://www.sec.gov/files/company_tickers.json on 2026-05-07.
CIK_MAP: dict[str, str] = {
    "AAPL":  "0000320193",
    "MSFT":  "0000789019",
    "GOOGL": "0001652044",
    "GOOG":  "0001652044",   # same entity as GOOGL — class C share ticker
    "AMZN":  "0001018724",
    "META":  "0001326801",
    "NVDA":  "0001045810",
    "TSLA":  "0001318605",
}


def cik_for(symbol: str) -> str:
    """Look up the 10-digit zero-padded CIK for a ticker."""
    s = symbol.upper()
    if s not in CIK_MAP:
        raise KeyError(
            f"CIK for {s!r} not in CIK_MAP. Add it from "
            "https://www.sec.gov/files/company_tickers.json"
        )
    return CIK_MAP[s]
