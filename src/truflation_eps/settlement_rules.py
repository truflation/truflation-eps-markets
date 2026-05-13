"""Settlement Rules §3 quorum logic — three-source agreement decision.

For Mag 7 EPS markets, the composite settlement stream finalizes only when
at least two of three primitive streams (EDGAR / FMP / Yahoo) return the
same value within $0.01. Pure function over three floats. Lives in the
library so all callers (scripts, future daemon, audit tools) apply the
exact same rule.

The committed value is whichever value the agreeing sources returned. No
averaging. For full 3-of-3 agreement, the EDGAR value is treated as the
canonical regulatory-primary reference.
"""
from __future__ import annotations

from typing import Optional

# Settlement Rules §3 tolerance, USD/share.
TOLERANCE = 0.01


def determine_quorum(
    edgar: Optional[float],
    fmp: Optional[float],
    yahoo: Optional[float],
    tol: float = TOLERANCE,
) -> tuple[str, Optional[float]]:
    """Apply Settlement Rules §3 quorum logic to three EPS values.

    Returns (outcome, committed_value).

    Outcomes:
      - 'no_edgar'        — EDGAR side returned None (e.g. 8-K extractor miss,
                            XBRL fetch error, or no 8-K Item 2.02 declared)
      - 'all_3_agree'     — all three within ±tol of each other
      - 'edgar_fmp_agree' — EDGAR and FMP agree; Yahoo diverges or absent
      - 'edgar_yahoo_agree' — EDGAR and Yahoo agree; FMP diverges or absent
      - 'fmp_yahoo_agree' — FMP and Yahoo agree; EDGAR diverges
      - 'no_quorum'       — no two of the three agree within tol

    Per Settlement Rules §3, the committed_value is the value the agreeing
    sources returned (no averaging). For 'all_3_agree' we return the EDGAR
    value as the canonical regulatory-primary reference. For 'no_quorum'
    and 'no_edgar' the committed_value is None — the market is paused.
    """
    if edgar is None:
        return "no_edgar", None

    pairs = {
        "edgar_fmp":    (fmp is not None and abs(edgar - fmp) <= tol),
        "edgar_yahoo":  (yahoo is not None and abs(edgar - yahoo) <= tol),
        "fmp_yahoo":    (fmp is not None and yahoo is not None
                         and abs(fmp - yahoo) <= tol),
    }

    if pairs["edgar_fmp"] and pairs["edgar_yahoo"] and pairs["fmp_yahoo"]:
        return "all_3_agree", edgar
    if pairs["edgar_fmp"]:
        return "edgar_fmp_agree", edgar
    if pairs["edgar_yahoo"]:
        return "edgar_yahoo_agree", edgar
    if pairs["fmp_yahoo"]:
        return "fmp_yahoo_agree", fmp
    return "no_quorum", None
