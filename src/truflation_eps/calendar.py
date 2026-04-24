"""Earnings date discovery — for a universe of tickers, find the next scheduled
earnings date and the current analyst estimate for that quarter.

Intentionally minimal. Does not prescribe market open / lock / settle dates —
those are product-layer decisions for the market creator.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from .fmp_client import AnalystEstimate, FMPClient, TOP_10


@dataclass(frozen=True)
class UpcomingEarnings:
    symbol: str
    earnings_date: str                # 'YYYY-MM-DD'
    release_timing: Optional[str]     # 'bmo' | 'amc' | None
    estimate: Optional[AnalystEstimate]  # closest-matching analyst-estimates row


def discover_upcoming(client: FMPClient,
                      universe: list[str] = TOP_10,
                      lookahead_days: int = 90) -> list[UpcomingEarnings]:
    """For each symbol in `universe`, find the next earnings date + matched estimate."""
    today = date.today()
    horizon = today + timedelta(days=lookahead_days)
    cal = client.earnings_calendar(from_date=today, to_date=horizon)

    first_upcoming: dict[str, dict] = {}
    for r in cal:
        if r.symbol in universe and r.symbol not in first_upcoming:
            first_upcoming[r.symbol] = r

    out: list[UpcomingEarnings] = []
    for sym in universe:
        row = first_upcoming.get(sym)
        if not row:
            continue
        try:
            # limit=40 rather than 16 because FMP returns results future-first;
            # a smaller limit for well-covered tickers can skip near-term quarters entirely.
            est_list = client.analyst_estimates(sym, period="quarter", limit=40)
        except Exception:
            est_list = []
        earnings_dt = date.fromisoformat(row.date)
        matched = _closest_estimate(est_list, earnings_dt)
        out.append(
            UpcomingEarnings(
                symbol=sym,
                earnings_date=row.date,
                release_timing=None,  # FMP stable endpoint doesn't return bmo/amc
                estimate=matched,
            )
        )
    out.sort(key=lambda r: r.earnings_date)
    return out


def _closest_estimate(est_list: list[AnalystEstimate],
                      target: date) -> Optional[AnalystEstimate]:
    best, best_gap = None, None
    for est in est_list:
        try:
            qe = date.fromisoformat(est.quarter_end)
        except (ValueError, TypeError):
            continue
        gap = abs((qe - target).days)
        if best_gap is None or gap < best_gap:
            best, best_gap = est, gap
    return best
