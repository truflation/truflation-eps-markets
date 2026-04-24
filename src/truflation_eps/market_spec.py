"""5-outcome categorical bucket construction.

Two strategies exposed:

  Strategy A — Analyst-spread buckets
    Uses the current quarter's analyst estimate spread (epsHigh, epsAvg, epsLow).
    Outer boundaries land at epsLow and epsHigh; inner boundaries at the
    midpoints between low-avg and avg-high. Reflects what forecasters
    currently disagree about.

  Strategy B — Historical-surprise-σ buckets
    Uses the standard deviation of past surprise% distribution for the ticker
    and places boundaries at epsAvg × (1 ± 0.5σ, ±1.5σ). Reflects how far
    actuals have historically drifted from consensus.

These produce materially different bucket widths. See the README for the
distinction — analyst spread is typically tighter than historical σ because
analysts herd around consensus but actual results don't.

The winning bucket is the one containing the actual reported EPS at settlement.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .fmp_client import AnalystEstimate


@dataclass(frozen=True)
class BucketSpec:
    index: int        # 1..5
    label: str
    lower: float      # math.inf or -math.inf for unbounded ends
    upper: float


# ─── Strategy A — Analyst-spread buckets ───────────────────────────────────


def analyst_spread_buckets(est: AnalystEstimate) -> list[BucketSpec]:
    """Construct 5 buckets from FMP's current-quarter analyst estimates.

    Bucket 1: (-inf,                   epsLow]              "deep miss"
    Bucket 2: (epsLow,                 mid(epsLow, epsAvg)] "mild miss"
    Bucket 3: (mid(epsLow, epsAvg),    mid(epsAvg, epsHigh)] "in-consensus"
    Bucket 4: (mid(epsAvg, epsHigh),   epsHigh]             "mild beat"
    Bucket 5: (epsHigh,                +inf)                "deep beat"

    Outer boundaries anchor at what the most pessimistic/optimistic analyst
    expects. The middle three buckets carve the space between them.
    """
    if est.eps_low is None or est.eps_avg is None or est.eps_high is None:
        raise ValueError(
            f"missing analyst estimate fields for {est.symbol} {est.quarter_end}"
        )
    if not (est.eps_low <= est.eps_avg <= est.eps_high):
        raise ValueError(
            f"invalid ordering for {est.symbol}: "
            f"low={est.eps_low} avg={est.eps_avg} high={est.eps_high}"
        )

    mid_low_avg = (est.eps_low + est.eps_avg) / 2
    mid_avg_high = (est.eps_avg + est.eps_high) / 2

    return [
        BucketSpec(1, "deep miss",    -math.inf,     est.eps_low),
        BucketSpec(2, "mild miss",    est.eps_low,   mid_low_avg),
        BucketSpec(3, "in-consensus", mid_low_avg,   mid_avg_high),
        BucketSpec(4, "mild beat",    mid_avg_high,  est.eps_high),
        BucketSpec(5, "deep beat",    est.eps_high,  math.inf),
    ]


# ─── Strategy B — Historical-surprise-σ buckets ────────────────────────────


def historical_surprise_sigma(history) -> tuple[float, float, int]:
    """Compute (mean, σ, n) of the surprise% distribution from a list of
    EarningsRow-like objects. Surprise% = (actual − estimated) / estimated.
    """
    surprises = []
    for r in history:
        act = getattr(r, "eps_actual", None)
        est = getattr(r, "eps_estimated", None)
        if act is None or est is None or est == 0:
            continue
        surprises.append((act - est) / est)

    n = len(surprises)
    if n < 2:
        raise ValueError(f"need ≥ 2 historical prints; got {n}")

    mean = sum(surprises) / n
    var = sum((x - mean) ** 2 for x in surprises) / (n - 1)
    sigma = math.sqrt(var)
    return mean, sigma, n


def surprise_sigma_buckets(
    eps_avg: float,
    history,
    k_inner: float = 0.5,
    k_outer: float = 1.5,
    min_history: int = 8,
) -> list[BucketSpec]:
    """Construct 5 buckets from historical surprise% distribution.

    Boundaries at eps_avg × (1 ± k_inner·σ) and eps_avg × (1 ± k_outer·σ).

    Theoretical probabilities under a normal approximation:
      Bucket 1 (miss > k_outer·σ):      ~6.7%
      Bucket 2 (miss k_inner–k_outer·σ): ~24.2%
      Bucket 3 (in-band ±k_inner·σ):    ~38.3%
      Bucket 4 (beat k_inner–k_outer·σ): ~24.2%
      Bucket 5 (beat > k_outer·σ):      ~6.7%

    Requires at least `min_history` usable past prints to calibrate σ.
    """
    _mean, sigma, n = historical_surprise_sigma(history)
    if n < min_history:
        raise ValueError(
            f"insufficient history: {n} usable prints (need ≥ {min_history})"
        )

    b_outer_low  = eps_avg * (1 - k_outer * sigma)
    b_inner_low  = eps_avg * (1 - k_inner * sigma)
    b_inner_high = eps_avg * (1 + k_inner * sigma)
    b_outer_high = eps_avg * (1 + k_outer * sigma)

    return [
        BucketSpec(1, f"miss > {k_outer}σ",         -math.inf,    b_outer_low),
        BucketSpec(2, f"miss {k_inner}–{k_outer}σ", b_outer_low,  b_inner_low),
        BucketSpec(3, f"in-band ±{k_inner}σ",       b_inner_low,  b_inner_high),
        BucketSpec(4, f"beat {k_inner}–{k_outer}σ", b_inner_high, b_outer_high),
        BucketSpec(5, f"beat > {k_outer}σ",         b_outer_high, math.inf),
    ]


# ─── Shared ────────────────────────────────────────────────────────────────


def bucket_for_actual(actual_eps: float, buckets: list[BucketSpec]) -> int:
    """Return the 1-indexed bucket whose range contains the actual EPS."""
    for b in buckets:
        if b.lower < actual_eps <= b.upper:
            return b.index
    return buckets[-1].index
