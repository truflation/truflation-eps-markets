"""FMP (Financial Modeling Prep) client — earnings calendar + analyst estimates.

Uses the /stable/ endpoints (legacy /api/v3 returns 403 since Aug 2025).
All three endpoints verified live on 2026-04-24.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

FMP_BASE = "https://financialmodelingprep.com/stable"
TOP_10 = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AVGO", "V", "JPM"]


@dataclass(frozen=True)
class EarningsRow:
    symbol: str
    date: str
    eps_estimated: Optional[float]
    eps_actual: Optional[float]
    revenue_estimated: Optional[float]
    revenue_actual: Optional[float]
    last_updated: Optional[str]


@dataclass(frozen=True)
class AnalystEstimate:
    symbol: str
    quarter_end: str
    eps_avg: Optional[float]
    eps_high: Optional[float]
    eps_low: Optional[float]
    n_analysts: Optional[int]
    revenue_avg: Optional[float]


class FMPClient:
    def __init__(self, api_key: Optional[str] = None, timeout: float = 15.0):
        self.api_key = api_key or os.environ.get("FMP_API_KEY")
        if not self.api_key:
            raise RuntimeError("FMP_API_KEY missing (set in .env or pass to constructor)")
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> list | dict:
        p = {**params, "apikey": self.api_key}
        with httpx.Client(timeout=self.timeout) as c:
            r = c.get(f"{FMP_BASE}/{path}", params=p)
            r.raise_for_status()
            return r.json()

    def earnings_calendar(self, from_date: Optional[date] = None,
                          to_date: Optional[date] = None) -> list[EarningsRow]:
        """Upcoming + historical earnings events in a date range.

        Returns [EarningsRow(...)] sorted by date ascending.
        """
        from_d = (from_date or date.today()).isoformat()
        to_d = (to_date or (date.today() + timedelta(days=30))).isoformat()
        rows = self._get("earnings-calendar", {"from": from_d, "to": to_d})
        if not isinstance(rows, list):
            return []
        out = [
            EarningsRow(
                symbol=r["symbol"],
                date=r.get("date", ""),
                eps_estimated=r.get("epsEstimated"),
                eps_actual=r.get("epsActual"),
                revenue_estimated=r.get("revenueEstimated"),
                revenue_actual=r.get("revenueActual"),
                last_updated=r.get("lastUpdated"),
            )
            for r in rows
        ]
        out.sort(key=lambda r: r.date)
        return out

    def historical_earnings(self, symbol: str, limit: int = 20) -> list[EarningsRow]:
        """Per-ticker history of earnings prints with estimates + actuals."""
        rows = self._get("earnings", {"symbol": symbol, "limit": limit})
        if not isinstance(rows, list):
            return []
        return [
            EarningsRow(
                symbol=r["symbol"],
                date=r.get("date", ""),
                eps_estimated=r.get("epsEstimated"),
                eps_actual=r.get("epsActual"),
                revenue_estimated=r.get("revenueEstimated"),
                revenue_actual=r.get("revenueActual"),
                last_updated=r.get("lastUpdated"),
            )
            for r in rows
        ]

    def analyst_estimates(self, symbol: str,
                          period: str = "quarter",
                          limit: int = 20) -> list[AnalystEstimate]:
        """Analyst consensus per quarter — avg, high, low, # analysts.

        This is what enables range markets: epsHigh/epsLow give the natural outer
        boundaries, epsAvg is the consensus threshold (Polymarket's single binary
        pivot is just the avg).
        """
        rows = self._get(
            "analyst-estimates",
            {"symbol": symbol, "period": period, "limit": limit},
        )
        if not isinstance(rows, list):
            return []
        return [
            AnalystEstimate(
                symbol=r.get("symbol", symbol),
                quarter_end=r.get("date", ""),
                eps_avg=r.get("epsAvg"),
                eps_high=r.get("epsHigh"),
                eps_low=r.get("epsLow"),
                n_analysts=r.get("numAnalystsEps"),
                revenue_avg=r.get("revenueAvg"),
            )
            for r in rows
        ]
