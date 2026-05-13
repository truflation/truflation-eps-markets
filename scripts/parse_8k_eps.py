"""Three-way check for non-GAAP-settling tickers: 8-K Ex-99.1 × FMP × Yahoo.

For each non-GAAP-settling ticker (NVDA, TSLA), pulls recent earnings 8-Ks
(Item 2.02), extracts non-GAAP diluted EPS from Exhibit 99.1, and compares
against both FMP's `epsActual` and Yahoo's `Reported EPS` at $0.01 tolerance.

Implements the Settlement Rules §3 quorum logic explicitly: classifies each
filing as `all_3_agree` / `edgar_fmp_agree` / `edgar_yahoo_agree` /
`fmp_yahoo_agree` / `no_quorum`. The EDGAR side is the 8-K Exhibit 99.1
extractor; this is the non-GAAP cousin of `cross_source_check.py` (which
handles the GAAP-settling tickers via EDGAR XBRL).

Validation result (5-year window, May 2021 → May 2026 — see test_historical
notes in the README):
  - TSLA: 12 of 12 earnings prints match exactly since July 2023
  - NVDA: 20 of 21 real earnings 8-Ks extracted correctly; 7 match FMP
    exactly post-June-2024-split; 13 historical divergences are pure
    split-adjustment artifact (FMP retro-adjusts, 8-K is immutable)

Run:
    uv run python scripts/parse_8k_eps.py

Outputs:
    results/parse_8k_eps_<UTC-timestamp>.csv
    results/parse_8k_eps_<UTC-timestamp>_debug.txt
"""
from __future__ import annotations

import csv
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from truflation_eps.edgar_8k_client import (  # noqa: E402
    Edgar8KClient,
    Filing8K,
    html_to_text,
)
from truflation_eps.fmp_client import EarningsRow, FMPClient  # noqa: E402
from truflation_eps.settlement_rules import TOLERANCE, determine_quorum  # noqa: E402
from truflation_eps.yahoo_client import YahooClient, YahooEarningsRow  # noqa: E402


NON_GAAP_TICKERS = ["NVDA", "TSLA"]
QUARTERS_TO_CHECK = 4
RELEASE_DATE_MAX_GAP_DAYS = 10


@dataclass
class ExtractionResult:
    ticker: str
    accession: str
    filing_date: str
    accepted_date: str
    items: str
    # 8-K extraction
    non_gaap_eps_extracted: Optional[float]
    extraction_method: str
    exhibit_filename: Optional[str]
    # FMP
    fmp_release_date: Optional[str]
    fmp_eps_actual: Optional[float]
    delta_8k_fmp: Optional[float]
    # Yahoo
    yahoo_release_date: Optional[str]
    yahoo_eps_actual: Optional[float]
    delta_8k_yahoo: Optional[float]
    delta_fmp_yahoo: Optional[float]
    # Settlement Rules §3 quorum determination
    quorum_outcome: Optional[str]    # 'all_3_agree' | 'edgar_fmp_agree'
                                     # | 'edgar_yahoo_agree' | 'fmp_yahoo_agree'
                                     # | 'no_quorum' | 'no_edgar'
    settlement_value: Optional[float]   # the value committed under quorum, or None if no quorum


def closest_fmp_release(
    fmp_rows: list[EarningsRow],
    target_date_str: str,
    max_days: int = RELEASE_DATE_MAX_GAP_DAYS,
) -> Optional[EarningsRow]:
    """Find the FMP row whose release date is nearest the 8-K filing date."""
    try:
        target = date.fromisoformat(target_date_str)
    except ValueError:
        return None
    best, best_gap = None, None
    for r in fmp_rows:
        if r.eps_actual is None:
            continue
        try:
            d = date.fromisoformat(r.date)
        except ValueError:
            continue
        gap = abs((d - target).days)
        if gap <= max_days and (best_gap is None or gap < best_gap):
            best, best_gap = r, gap
    return best


def closest_yahoo_release(
    yahoo_rows: list[YahooEarningsRow],
    target_date_str: str,
    max_days: int = RELEASE_DATE_MAX_GAP_DAYS,
) -> Optional[YahooEarningsRow]:
    """Find the Yahoo row whose release date is nearest the 8-K filing date."""
    try:
        target = date.fromisoformat(target_date_str)
    except ValueError:
        return None
    best, best_gap = None, None
    for r in yahoo_rows:
        if r.eps_actual is None:
            continue
        try:
            d = date.fromisoformat(r.date)
        except ValueError:
            continue
        gap = abs((d - target).days)
        if gap <= max_days and (best_gap is None or gap < best_gap):
            best, best_gap = r, gap
    return best


def context_around_non_gaap(text: str, max_snippets: int = 3,
                             window: int = 240) -> list[str]:
    """Text windows around 'non-GAAP' mentions — used in debug dumps."""
    snippets = []
    for m in re.finditer(r"[Nn]on[-\s]?GAAP", text):
        s = max(0, m.start() - window // 2)
        e = min(len(text), m.end() + window // 2)
        snippets.append(text[s:e].strip())
        if len(snippets) >= max_snippets:
            break
    return snippets


def main() -> None:
    edgar = Edgar8KClient()
    fmp = FMPClient()
    yahoo = YahooClient()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out_dir = Path(__file__).resolve().parents[1] / "results"
    out_dir.mkdir(exist_ok=True)
    debug_path = out_dir / f"parse_8k_eps_{stamp}_debug.txt"
    csv_path = out_dir / f"parse_8k_eps_{stamp}.csv"
    debug_f = debug_path.open("w")

    def dlog(msg: str) -> None:
        debug_f.write(msg + "\n")

    print("=" * 78)
    print("8-K Exhibit 99.1 non-GAAP EPS extraction vs FMP epsActual")
    print(f"Tickers: {', '.join(NON_GAAP_TICKERS)}  ·  "
          f"quarters: {QUARTERS_TO_CHECK}  ·  tolerance: ${TOLERANCE:.2f}")
    print("=" * 78)

    rows: list[ExtractionResult] = []

    for symbol in NON_GAAP_TICKERS:
        print(f"\n[{symbol}] fetching Item-2.02 8-Ks...")
        try:
            filings = edgar.list_earnings_8ks(symbol, want_n=QUARTERS_TO_CHECK)
        except Exception as e:
            print(f"  EDGAR submissions fetch failed: {e}")
            continue
        print(f"  → {len(filings)} earnings-related 8-Ks found")

        fmp_history = fmp.historical_earnings(symbol, limit=20)
        try:
            yahoo_history = yahoo.earnings_dates(symbol)
        except Exception as e:
            print(f"  Yahoo fetch failed: {e}")
            yahoo_history = []

        for filing in filings:
            print(f"\n  filed {filing.filing_date} "
                  f"(accession {filing.accession}, items {filing.items})")

            try:
                val, method, exhibit = edgar.non_gaap_eps_for_filing(
                    symbol, filing
                )
            except Exception as e:
                print(f"    ERROR: {e}")
                rows.append(_make_failure_row(symbol, filing, "fetch_error"))
                continue

            if exhibit is None:
                print("    EX-99.1 not declared (likely non-earnings 8-K)")
                rows.append(_make_failure_row(symbol, filing, "no_ex991"))
                continue

            print(f"    exhibit: {exhibit}")

            if val is None:
                print(f"    extraction FAILED ({method})")
                try:
                    html = edgar.fetch_exhibit_991_html(symbol, filing, exhibit)
                    text = html_to_text(html)
                    snippets = context_around_non_gaap(text)
                    dlog(f"\n--- {symbol} {filing.accession} {filing.filing_date} "
                         f"(exhibit: {exhibit}) ---")
                    if not snippets:
                        dlog("  (no 'non-GAAP' mentions found at all)")
                    for i, s in enumerate(snippets, 1):
                        dlog(f"  [snippet {i}]\n  {s}\n")
                except Exception:
                    pass
            else:
                print(f"    non-GAAP diluted EPS = ${val:.2f}  ({method})")

            fmp_match = closest_fmp_release(fmp_history, filing.filing_date)
            yahoo_match = closest_yahoo_release(yahoo_history, filing.filing_date)

            fmp_val = fmp_match.eps_actual if fmp_match else None
            yahoo_val = yahoo_match.eps_actual if yahoo_match else None

            delta_8k_fmp = (round(val - fmp_val, 4)
                            if val is not None and fmp_val is not None else None)
            delta_8k_yahoo = (round(val - yahoo_val, 4)
                              if val is not None and yahoo_val is not None else None)
            delta_fmp_yahoo = (round(fmp_val - yahoo_val, 4)
                               if fmp_val is not None and yahoo_val is not None else None)

            outcome, committed = determine_quorum(val, fmp_val, yahoo_val)

            # Pretty-print all three values + quorum outcome
            f_str = f"${fmp_val:.2f}" if fmp_val is not None else "  -  "
            y_str = f"${yahoo_val:.2f}" if yahoo_val is not None else "  -  "
            v_str = f"${val:.2f}" if val is not None else "  -  "
            print(f"    8K={v_str}  FMP={f_str}  Yahoo={y_str}  → {outcome}")

            rows.append(ExtractionResult(
                ticker=symbol,
                accession=filing.accession,
                filing_date=filing.filing_date,
                accepted_date=filing.accepted_date,
                items=filing.items,
                non_gaap_eps_extracted=val,
                extraction_method=method,
                exhibit_filename=exhibit,
                fmp_release_date=fmp_match.date if fmp_match else None,
                fmp_eps_actual=fmp_val,
                delta_8k_fmp=delta_8k_fmp,
                yahoo_release_date=yahoo_match.date if yahoo_match else None,
                yahoo_eps_actual=yahoo_val,
                delta_8k_yahoo=delta_8k_yahoo,
                delta_fmp_yahoo=delta_fmp_yahoo,
                quorum_outcome=outcome,
                settlement_value=committed,
            ))

    # Summary
    print("\n" + "=" * 78)
    print("Summary")
    print("=" * 78)

    by_ticker: dict[str, list[ExtractionResult]] = {}
    for r in rows:
        by_ticker.setdefault(r.ticker, []).append(r)

    for ticker, ticker_rows in by_ticker.items():
        n = len(ticker_rows)
        # Breakdown of quorum outcomes
        outcome_counts: dict[str, int] = {}
        for r in ticker_rows:
            outcome_counts[r.quorum_outcome or "—"] = (
                outcome_counts.get(r.quorum_outcome or "—", 0) + 1
            )
        # A "successful settlement" finalizes under any 2-of-3 agreement (or 3-of-3)
        finalized = sum(1 for r in ticker_rows
                        if r.quorum_outcome in {"all_3_agree", "edgar_fmp_agree",
                                                "edgar_yahoo_agree",
                                                "fmp_yahoo_agree"})
        paused = sum(1 for r in ticker_rows if r.quorum_outcome == "no_quorum")
        no_extract = sum(1 for r in ticker_rows
                         if r.quorum_outcome == "no_edgar")
        print(f"  {ticker:6}  {n} 8-Ks  finalized={finalized}  paused={paused}  "
              f"no_extract={no_extract}")
        for outcome, count in sorted(outcome_counts.items(),
                                      key=lambda kv: -kv[1]):
            print(f"           {outcome:18}  {count}")

    if rows:
        with csv_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()))
            w.writeheader()
            for r in rows:
                w.writerow(asdict(r))
        print(f"\n→ csv:   {csv_path}")
    print(f"→ debug: {debug_path}")
    debug_f.close()


def _make_failure_row(symbol: str, filing: Filing8K, reason: str) -> ExtractionResult:
    return ExtractionResult(
        ticker=symbol,
        accession=filing.accession,
        filing_date=filing.filing_date,
        accepted_date=filing.accepted_date,
        items=filing.items,
        non_gaap_eps_extracted=None,
        extraction_method=reason,
        exhibit_filename=None,
        fmp_release_date=None,
        fmp_eps_actual=None,
        delta_8k_fmp=None,
        yahoo_release_date=None,
        yahoo_eps_actual=None,
        delta_8k_yahoo=None,
        delta_fmp_yahoo=None,
        quorum_outcome="no_edgar",
        settlement_value=None,
    )


if __name__ == "__main__":
    main()
