"""SEC EDGAR XBRL client — structured GAAP diluted EPS time series.

Source of truth for *audited* GAAP diluted EPS, going back as far as the
company has filed XBRL-tagged statements (typically ~2009 for SEC's XBRL
mandate). Free, no API key.

Used as the EDGAR primitive stream for GAAP-settling tickers in the
settlement rules (AAPL, MSFT, GOOGL, AMZN, META). NVDA and TSLA settle
on non-GAAP and use `edgar_8k_client` instead — XBRL does not carry a
non-GAAP concept.

Limitations:
  - GAAP only. The US-GAAP taxonomy has no non-GAAP / Adjusted concept.
  - 10-Q data is the per-quarter print; fiscal Q4 is not separately tagged
    in 10-Q (the 10-K replaces it). Reconstruct Q4 as FY − (Q1+Q2+Q3) if
    needed.
  - SEC requires a polite User-Agent (set in `edgar_common.DEFAULT_UA`).
    Rate limit: 10 requests/sec.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

from .edgar_common import DEFAULT_UA, cik_for

EDGAR_XBRL_BASE = "https://data.sec.gov/api/xbrl/companyconcept"


@dataclass(frozen=True)
class EdgarEpsRow:
    symbol: str
    quarter_end: str        # 'YYYY-MM-DD' — period end date
    period_start: Optional[str]
    val: float              # GAAP diluted EPS (USD/share)
    fiscal_year: Optional[int]
    fiscal_period: Optional[str]   # 'Q1' | 'Q2' | 'Q3' | 'FY'
    form: str               # '10-Q' | '10-K' | '10-K/A' | '8-K'
    filed: Optional[str]    # date the filing was submitted to SEC
    accession: Optional[str]
    frame: Optional[str]    # calendar-quarter frame, e.g. 'CY2026Q1'


class EdgarXbrlClient:
    """Reads the structured XBRL `us-gaap/EarningsPerShareDiluted` concept.

    For settlement, this provides the EDGAR primitive stream for GAAP-headline
    tickers. For NVDA/TSLA, use `Edgar8KClient` instead.
    """

    def __init__(self, user_agent: str = DEFAULT_UA, timeout: float = 20.0):
        if "@" not in user_agent:
            raise RuntimeError(
                "EDGAR requires a User-Agent with contact info. "
                "Pass user_agent='your-app/1.0 you@example.com'"
            )
        self.user_agent = user_agent
        self.timeout = timeout

    def _get(self, path: str) -> dict:
        url = f"{EDGAR_XBRL_BASE}/{path}"
        with httpx.Client(timeout=self.timeout) as c:
            r = c.get(url, headers={"User-Agent": self.user_agent,
                                    "Accept": "application/json"})
            r.raise_for_status()
            return r.json()

    def diluted_eps_history(self, symbol: str,
                            forms: tuple[str, ...] = ("10-Q", "10-K"),
                            quarterly_only: bool = False) -> list[EdgarEpsRow]:
        """Full XBRL history of `EarningsPerShareDiluted` for `symbol`.

        - `quarterly_only=True` returns only 10-Q rows (excludes 10-K full-year).
          Fiscal Q4 is NOT separately tagged in 10-Q because the 10-K replaces
          the Q4 10-Q. Reconstruct Q4 as FY − (Q1+Q2+Q3) if needed.

        Sorted oldest → newest.
        """
        cik = cik_for(symbol)
        try:
            payload = self._get(f"CIK{cik}/us-gaap/EarningsPerShareDiluted.json")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return []
            raise

        units = payload.get("units", {}).get("USD/shares", [])
        rows: list[EdgarEpsRow] = []
        for r in units:
            form = r.get("form", "")
            if forms and form not in forms:
                continue
            if quarterly_only and form != "10-Q":
                continue
            rows.append(
                EdgarEpsRow(
                    symbol=symbol.upper(),
                    quarter_end=r.get("end", ""),
                    period_start=r.get("start"),
                    val=float(r["val"]),
                    fiscal_year=r.get("fy"),
                    fiscal_period=r.get("fp"),
                    form=form,
                    filed=r.get("filed"),
                    accession=r.get("accn"),
                    frame=r.get("frame"),
                )
            )
        rows.sort(key=lambda x: x.quarter_end)
        return rows

    def latest_quarterly_eps(self, symbol: str) -> Optional[EdgarEpsRow]:
        """Most recent 10-Q EPS print, or None if no 10-Q on file."""
        history = self.diluted_eps_history(symbol, quarterly_only=True)
        return history[-1] if history else None
