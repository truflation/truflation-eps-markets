"""Long-window extractor validation — 5-year empirical check.

Runs the same three-way check as `parse_8k_eps.py` (EDGAR 8-K Ex-99.1 × FMP ×
Yahoo) but across a configurable historical window (default: 5 years). Used
to confirm that:

  1. The per-ticker non-GAAP extractors in `edgar_8k_client` cover all
     press-release format variants the company has used in that window.
  2. Quorum agreement holds across all historical earnings prints.

This is the production version of the offline test that backs the
"5-year empirical validation" claim in the README. Run it on demand to
re-validate against any window.

Run:
    uv run python scripts/validate_extractors.py

Configurable:
    YEARS_BACK   — window length (default 5)

Output:
    results/validate_extractors_<UTC-timestamp>.csv
    Per-row: filing date, accession, EX-99.1 filename, extracted value,
    extraction method, FMP value, Yahoo value, all pairwise deltas, quorum
    outcome, settlement value.

Known artifacts in the output (NOT extractor bugs):
  - Pre-June-2024 NVDA: 8-K = as-filed pre-split; FMP/Yahoo = retroactively
    split-adjusted to post-June-2024 units. Compounds for pre-July-2021
    quarters (40x cumulative). At settlement time this never happens —
    both sources agree on the day-of-release units.
  - Pre-July-2023 TSLA: extractor returns None because press-release format
    pre-dates the current "EPS attributable...(non-GAAP)" table phrasing.
    Out of scope — settlement is forward-only.
  - TSLA delivery 8-Ks (filed quarterly under Item 2.02): no EPS data, so
    extractor correctly returns None.
"""
from __future__ import annotations

import csv
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from truflation_eps.edgar_8k_client import Edgar8KClient, Filing8K  # noqa: E402
from truflation_eps.fmp_client import EarningsRow, FMPClient  # noqa: E402
from truflation_eps.settlement_rules import TOLERANCE, determine_quorum  # noqa: E402
from truflation_eps.yahoo_client import YahooClient, YahooEarningsRow  # noqa: E402


NON_GAAP_TICKERS = ["NVDA", "TSLA"]
YEARS_BACK = 5
RELEASE_DATE_MAX_GAP_DAYS = 10


@dataclass
class ValidationRow:
    ticker: str
    accession: str
    filing_date: str
    items: str
    exhibit_filename: Optional[str]
    edgar_8k_extracted: Optional[float]
    extraction_method: str
    fmp_release_date: Optional[str]
    fmp_eps_actual: Optional[float]
    yahoo_release_date: Optional[str]
    yahoo_eps_actual: Optional[float]
    delta_edgar_fmp: Optional[float]
    delta_edgar_yahoo: Optional[float]
    delta_fmp_yahoo: Optional[float]
    quorum_outcome: Optional[str]
    settlement_value: Optional[float]


def closest_fmp(rows: list[EarningsRow], target: str) -> Optional[EarningsRow]:
    try:
        t = date.fromisoformat(target)
    except ValueError:
        return None
    best, best_gap = None, None
    for r in rows:
        if r.eps_actual is None:
            continue
        try:
            d = date.fromisoformat(r.date)
        except ValueError:
            continue
        gap = abs((d - t).days)
        if gap <= RELEASE_DATE_MAX_GAP_DAYS and (best_gap is None or gap < best_gap):
            best, best_gap = r, gap
    return best


def closest_yahoo(rows: list[YahooEarningsRow], target: str) -> Optional[YahooEarningsRow]:
    try:
        t = date.fromisoformat(target)
    except ValueError:
        return None
    best, best_gap = None, None
    for r in rows:
        if r.eps_actual is None:
            continue
        try:
            d = date.fromisoformat(r.date)
        except ValueError:
            continue
        gap = abs((d - t).days)
        if gap <= RELEASE_DATE_MAX_GAP_DAYS and (best_gap is None or gap < best_gap):
            best, best_gap = r, gap
    return best


def main() -> None:
    edgar = Edgar8KClient()
    fmp = FMPClient()
    yahoo = YahooClient()

    since = date.today() - timedelta(days=365 * YEARS_BACK)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out_dir = Path(__file__).resolve().parents[1] / "results"
    out_dir.mkdir(exist_ok=True)
    csv_path = out_dir / f"validate_extractors_{stamp}.csv"

    print("=" * 80)
    print(f"Long-window extractor validation  (window: {YEARS_BACK} years, "
          f"from {since.isoformat()})")
    print(f"Tickers: {', '.join(NON_GAAP_TICKERS)}  ·  tolerance: ${TOLERANCE:.2f}")
    print("=" * 80)

    all_rows: list[ValidationRow] = []

    for symbol in NON_GAAP_TICKERS:
        print(f"\n[{symbol}]")
        try:
            filings = edgar.list_earnings_8ks(symbol, since=since)
        except Exception as e:
            print(f"  ERROR listing 8-Ks: {e}")
            continue
        print(f"  {len(filings)} Item-2.02 8-Ks since {since.isoformat()}")

        # Pull as much history as FMP exposes — for Mag 7 this is typically
        # 100+ rows. limit=200 captures the full window safely.
        fmp_history = fmp.historical_earnings(symbol, limit=200)
        try:
            yahoo_history = yahoo.earnings_dates(symbol)
        except Exception as e:
            print(f"  Yahoo fetch failed: {e}")
            yahoo_history = []

        ticker_rows: list[ValidationRow] = []
        for f in filings:
            try:
                val, method, exhibit = edgar.non_gaap_eps_for_filing(symbol, f)
            except Exception as e:
                print(f"  {f.filing_date}  ERROR: {e}")
                ticker_rows.append(ValidationRow(
                    ticker=symbol, accession=f.accession,
                    filing_date=f.filing_date, items=f.items,
                    exhibit_filename=None, edgar_8k_extracted=None,
                    extraction_method="fetch_error",
                    fmp_release_date=None, fmp_eps_actual=None,
                    yahoo_release_date=None, yahoo_eps_actual=None,
                    delta_edgar_fmp=None, delta_edgar_yahoo=None,
                    delta_fmp_yahoo=None,
                    quorum_outcome="no_edgar", settlement_value=None,
                ))
                continue

            fmp_match = closest_fmp(fmp_history, f.filing_date)
            yahoo_match = closest_yahoo(yahoo_history, f.filing_date)
            fmp_val = fmp_match.eps_actual if fmp_match else None
            yahoo_val = yahoo_match.eps_actual if yahoo_match else None

            d_ef = (round(val - fmp_val, 4)
                    if val is not None and fmp_val is not None else None)
            d_ey = (round(val - yahoo_val, 4)
                    if val is not None and yahoo_val is not None else None)
            d_fy = (round(fmp_val - yahoo_val, 4)
                    if fmp_val is not None and yahoo_val is not None else None)

            outcome, committed = determine_quorum(val, fmp_val, yahoo_val)

            v_str = f"${val:.2f}" if val is not None else "  -  "
            f_str = f"${fmp_val:.2f}" if fmp_val is not None else "  -  "
            y_str = f"${yahoo_val:.2f}" if yahoo_val is not None else "  -  "
            print(f"  {f.filing_date}  ex991={(exhibit or '-'):28}  "
                  f"8K={v_str}  FMP={f_str}  Yahoo={y_str}  → {outcome}")

            ticker_rows.append(ValidationRow(
                ticker=symbol, accession=f.accession,
                filing_date=f.filing_date, items=f.items,
                exhibit_filename=exhibit, edgar_8k_extracted=val,
                extraction_method=method,
                fmp_release_date=fmp_match.date if fmp_match else None,
                fmp_eps_actual=fmp_val,
                yahoo_release_date=yahoo_match.date if yahoo_match else None,
                yahoo_eps_actual=yahoo_val,
                delta_edgar_fmp=d_ef, delta_edgar_yahoo=d_ey,
                delta_fmp_yahoo=d_fy,
                quorum_outcome=outcome, settlement_value=committed,
            ))

        all_rows.extend(ticker_rows)

        # Per-ticker summary
        outcome_counts: dict[str, int] = {}
        for r in ticker_rows:
            outcome_counts[r.quorum_outcome or "—"] = (
                outcome_counts.get(r.quorum_outcome or "—", 0) + 1
            )
        n = len(ticker_rows)
        finalized = sum(c for k, c in outcome_counts.items()
                        if k in {"all_3_agree", "edgar_fmp_agree",
                                 "edgar_yahoo_agree", "fmp_yahoo_agree"})
        no_edgar = outcome_counts.get("no_edgar", 0)
        no_quorum = outcome_counts.get("no_quorum", 0)
        print(f"\n  {symbol} summary: total={n}  finalized={finalized}  "
              f"paused/no_quorum={no_quorum}  no_edgar_extraction={no_edgar}")
        for outcome, count in sorted(outcome_counts.items(), key=lambda kv: -kv[1]):
            print(f"      {outcome:24}  {count}")

    # Overall summary
    print("\n" + "=" * 80)
    total = len(all_rows)
    finalized = sum(1 for r in all_rows
                    if r.quorum_outcome in {"all_3_agree", "edgar_fmp_agree",
                                            "edgar_yahoo_agree", "fmp_yahoo_agree"})
    no_edgar = sum(1 for r in all_rows if r.quorum_outcome == "no_edgar")
    no_quorum = sum(1 for r in all_rows if r.quorum_outcome == "no_quorum")
    print(f"OVERALL: total={total}  finalized={finalized}  "
          f"paused/no_quorum={no_quorum}  no_edgar={no_edgar}")

    if all_rows:
        with csv_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(asdict(all_rows[0]).keys()))
            w.writeheader()
            for r in all_rows:
                w.writerow(asdict(r))
        print(f"\n→ csv: {csv_path}")


if __name__ == "__main__":
    main()
