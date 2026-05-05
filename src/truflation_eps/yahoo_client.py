"""Yahoo Finance client — release-time secondary source.

FMP /stable/ endpoints (the primary EPS data source) do not return release
date-time or BMO/AMC markers. Yahoo's per-ticker `earnings_dates` table does:
each row carries a timezone-aware datetime (e.g. `2026-04-30 16:00:00-04:00`)
plus EPS Estimate / Reported EPS / Surprise%.

Used as a secondary source for release-timing only. EPS values come from FMP.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import yfinance as yf


# AMC = After Market Close (15:00 ET or later, conventionally 16:00)
# BMO = Before Market Open (09:30 ET or earlier, conventionally 06:00–09:00)
AMC_HOUR_THRESHOLD = 14   # any release at/after 14:00 ET is treated as AMC
BMO_HOUR_THRESHOLD = 9    # any release at/before 09:00 ET is treated as BMO


@dataclass(frozen=True)
class YahooEarningsRow:
    symbol: str
    scheduled_at: str          # ISO datetime with tz, e.g. '2026-04-30T16:00:00-04:00'
    date: str                  # 'YYYY-MM-DD' in America/New_York
    hour_et: int               # 0-23
    bmo_amc: Optional[str]     # 'bmo' | 'amc' | None (mid-day / unclassified)
    eps_estimated: Optional[float]
    eps_actual: Optional[float]
    surprise_pct: Optional[float]


def _classify(hour: int) -> Optional[str]:
    if hour >= AMC_HOUR_THRESHOLD:
        return "amc"
    if hour <= BMO_HOUR_THRESHOLD:
        return "bmo"
    return None


def _to_float(v) -> Optional[float]:
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


class YahooClient:
    """Thin wrapper around yfinance for release-time data.

    No API key required. Uses Yahoo Finance's unofficial public endpoints via
    yfinance. Coverage on US mega-caps observed at 100% with 25 quarters of
    history per ticker.
    """

    def earnings_dates(self, symbol: str) -> list[YahooEarningsRow]:
        """Return all known earnings events for `symbol`, sorted newest first.

        Each row has a scheduled datetime (timezone-aware), bmo/amc marker,
        and (for past quarters) the EPS estimate / actual / surprise%.
        """
        df = yf.Ticker(symbol).earnings_dates
        if df is None or len(df) == 0:
            return []

        out: list[YahooEarningsRow] = []
        for ts, row in df.iterrows():
            # ts is a timezone-aware Timestamp in America/New_York
            hour = int(ts.hour)
            out.append(
                YahooEarningsRow(
                    symbol=symbol,
                    scheduled_at=ts.isoformat(),
                    date=ts.strftime("%Y-%m-%d"),
                    hour_et=hour,
                    bmo_amc=_classify(hour),
                    eps_estimated=_to_float(row.get("EPS Estimate")),
                    eps_actual=_to_float(row.get("Reported EPS")),
                    surprise_pct=_to_float(row.get("Surprise(%)")),
                )
            )
        return out

    def next_event(self, symbol: str,
                   now: Optional[datetime] = None) -> Optional[YahooEarningsRow]:
        """Return the next scheduled earnings event for `symbol`, or None."""
        rows = self.earnings_dates(symbol)
        if not rows:
            return None
        # rows include past + future; filter to future only
        if now is None:
            from datetime import datetime as _dt, timezone as _tz
            now = _dt.now(_tz.utc)
        future = [r for r in rows if datetime.fromisoformat(r.scheduled_at) > now]
        if not future:
            return None
        future.sort(key=lambda r: r.scheduled_at)
        return future[0]

    def timing_for_date(self, symbol: str, target_date: str) -> Optional[YahooEarningsRow]:
        """Find the Yahoo row whose date matches `target_date` ('YYYY-MM-DD').

        Used to attach a release-time to an FMP earnings-calendar row when the
        date is already known from the primary source.
        """
        for r in self.earnings_dates(symbol):
            if r.date == target_date:
                return r
        return None
