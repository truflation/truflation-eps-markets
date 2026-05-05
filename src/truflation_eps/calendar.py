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
from .yahoo_client import YahooClient


@dataclass(frozen=True)
class UpcomingEarnings:
    symbol: str
    earnings_date: str                # 'YYYY-MM-DD'
    release_timing: Optional[str]     # 'bmo' | 'amc' | None — sourced from Yahoo
    scheduled_at: Optional[str]       # full ISO datetime with tz, sourced from Yahoo
    estimate: Optional[AnalystEstimate]  # closest-matching analyst-estimates row


def discover_upcoming(client: FMPClient,
                      universe: list[str] = TOP_10,
                      lookahead_days: int = 90,
                      yahoo: Optional[YahooClient] = None) -> list[UpcomingEarnings]:
    """For each symbol in `universe`, find the next earnings date + matched estimate.

    If `yahoo` is provided, the release_timing (bmo/amc) and scheduled_at fields
    are populated from Yahoo Finance as a secondary source, since FMP /stable/
    endpoints don't carry release-time information.
    """
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

        timing, scheduled_at = None, None
        if yahoo is not None:
            try:
                y_row = yahoo.timing_for_date(sym, row.date)
                if y_row is not None:
                    timing = y_row.bmo_amc
                    scheduled_at = y_row.scheduled_at
            except Exception:
                pass

        out.append(
            UpcomingEarnings(
                symbol=sym,
                earnings_date=row.date,
                release_timing=timing,
                scheduled_at=scheduled_at,
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
