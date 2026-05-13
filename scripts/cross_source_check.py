"""Cross-source EPS verification — FMP vs EDGAR XBRL vs Yahoo Finance.

Goal:
  1. Pull max-history EPS for the Mag 7 from all three sources.
  2. Report depth per source per ticker (oldest record found).
  3. Join on (ticker, quarter_end) and flag any disagreement.

Expected pattern:
  - For AAPL/MSFT/GOOGL/AMZN/META: all three sources should return GAAP
    diluted, all values agree within rounding tolerance.
  - For NVDA + TSLA: FMP `epsActual` and Yahoo "Reported EPS" return the
    non-GAAP headline; EDGAR XBRL returns GAAP only — a SYSTEMATIC, expected
    divergence (this is what makes the case against settling on EDGAR for
    those tickers).

Run:
    uv run python scripts/cross_source_check.py
"""
from __future__ import annotations

import csv
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from truflation_eps.fmp_client import FMPClient
from truflation_eps.edgar_xbrl_client import EdgarXbrlClient
from truflation_eps.yahoo_client import YahooClient


MAG7 = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"]
TOLERANCE = 0.005   # USD/share — anything below this counts as agreement

# Discrepancy comparison is restricted to the period AFTER each ticker's most
# recent stock split. FMP returns split-adjusted EPS retroactively while EDGAR
# returns as-filed at the time — comparing pre-split history always shows
# split-factor gaps that aren't real data disagreements.
# Per-ticker post-split clean window:
SPLIT_CUTOFF: dict[str, str] = {
    "AAPL":  "2020-09-01",   # 4-for-1 split Aug 2020
    "MSFT":  "2010-01-01",   # no recent split — wide window
    "GOOGL": "2022-08-01",   # 20-for-1 split July 2022
    "AMZN":  "2022-07-01",   # 20-for-1 split June 2022
    "META":  "2010-01-01",   # no splits
    "NVDA":  "2024-07-01",   # 10-for-1 split June 2024 (4-for-1 prior 2021)
    "TSLA":  "2022-09-01",   # 3-for-1 split Aug 2022 (5-for-1 prior 2020)
}


def main() -> None:
    fmp = FMPClient()
    edgar = EdgarXbrlClient()
    yahoo = YahooClient()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = Path(__file__).resolve().parents[1] / "results"
    out.mkdir(exist_ok=True)

    print("=" * 78)
    print("Cross-source EPS verification — FMP vs EDGAR XBRL vs Yahoo Finance")
    print("=" * 78)

    fmp_rows: dict[str, list] = {}
    edgar_rows: dict[str, list] = {}
    yahoo_rows: dict[str, list] = {}

    # ─── 1. Pull max history from each source ─────────────────────────
    print("\n[1] Pulling history from each source...")
    for sym in MAG7:
        # FMP: limit=200 captures full available history (most tickers have 20-30 quarters)
        try:
            fmp_rows[sym] = fmp.historical_earnings(sym, limit=200)
            print(f"  FMP    {sym:6} {len(fmp_rows[sym]):>4} rows")
        except Exception as e:
            print(f"  FMP    {sym:6} ERROR: {e}")
            fmp_rows[sym] = []

        # EDGAR: 10-Q only, single-quarter records only (drop YTD-cumulative).
        # XBRL stores both `(start, end)` 3-month-period and `(FY-start, end)`
        # YTD-cumulative records under the same `end` date. We want the
        # 3-month-period rows only — keep where end - start ≈ 90 days.
        try:
            from datetime import date as _d
            raw = edgar.diluted_eps_history(sym, quarterly_only=True)
            single_q = []
            for r in raw:
                if not r.period_start:
                    continue
                try:
                    span = (_d.fromisoformat(r.quarter_end)
                            - _d.fromisoformat(r.period_start)).days
                except Exception:
                    continue
                if 80 <= span <= 100:    # ~3 months
                    single_q.append(r)
            # Dedupe: keep the latest-filed record per (start, end). XBRL
            # stores both as-filed and post-restatement values; the latest
            # filing carries the most up-to-date / split-adjusted view.
            best: dict[tuple, object] = {}
            for r in single_q:
                key = (r.period_start, r.quarter_end)
                cur = best.get(key)
                if cur is None or (r.filed or "") > (cur.filed or ""):
                    best[key] = r
            edgar_rows[sym] = sorted(best.values(), key=lambda r: r.quarter_end)
            print(f"  EDGAR  {sym:6} {len(edgar_rows[sym]):>4} rows  "
                  f"(filtered from {len(raw)} raw, "
                  f"{len(single_q)} single-Q, deduped to latest-filed)")
        except Exception as e:
            print(f"  EDGAR  {sym:6} ERROR: {e}")
            edgar_rows[sym] = []

        # Yahoo: from the earnings_dates table — Reported EPS field
        try:
            yahoo_rows[sym] = [
                r for r in yahoo.earnings_dates(sym)
                if r.eps_actual is not None
            ]
            print(f"  YAHOO  {sym:6} {len(yahoo_rows[sym]):>4} rows")
        except Exception as e:
            print(f"  YAHOO  {sym:6} ERROR: {e}")
            yahoo_rows[sym] = []

    # ─── 2. Depth report — how far back does each source go? ──────────
    print("\n" + "=" * 78)
    print("[2] Historical depth per source per ticker")
    print("=" * 78)
    print(f"{'Ticker':8} {'FMP rows':>10} {'FMP earliest':>14} "
          f"{'EDGAR rows':>11} {'EDGAR earliest':>16} "
          f"{'Yahoo rows':>11} {'Yahoo earliest':>16}")
    depth_rows = []
    for sym in MAG7:
        f_rows = fmp_rows[sym]
        e_rows = edgar_rows[sym]
        y_rows = yahoo_rows[sym]
        f_earliest = min((r.date for r in f_rows), default="-")
        e_earliest = e_rows[0].quarter_end if e_rows else "-"
        y_earliest = min((r.date for r in y_rows), default="-")
        print(f"{sym:8} {len(f_rows):>10} {f_earliest:>14} "
              f"{len(e_rows):>11} {e_earliest:>16} "
              f"{len(y_rows):>11} {y_earliest:>16}")
        depth_rows.append({
            "ticker": sym,
            "fmp_rows": len(f_rows), "fmp_earliest": f_earliest,
            "edgar_rows": len(e_rows), "edgar_earliest": e_earliest,
            "yahoo_rows": len(y_rows), "yahoo_earliest": y_earliest,
        })

    out_depth = out / f"cross_source_{stamp}_depth.csv"
    with out_depth.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(depth_rows[0].keys()))
        w.writeheader()
        w.writerows(depth_rows)
    print(f"\n→ depth: {out_depth}")

    # ─── 3. Discrepancy join ──────────────────────────────────────────
    # Approach: index each source by (ticker, quarter_end approximate) and
    # match within ±10 days because FMP uses *release date* and EDGAR uses
    # *fiscal-quarter-end* — they're 3-6 weeks apart per ticker.
    print("\n" + "=" * 78)
    print("[3] Discrepancy detection — matching quarters across sources")
    print("=" * 78)

    print(f"  (Per-ticker cutoff: comparison restricted to post-most-recent-split "
          f"window. FMP is split-adjusted, EDGAR is as-filed at the time; "
          f"pre-split values would always disagree by the split factor.)")

    discrepancies = []
    matches = []

    for sym in MAG7:
        # Build a master index keyed by quarter_end (EDGAR's date semantics)
        # then attach FMP and Yahoo by closest release-date match.
        e_rows = sorted(edgar_rows[sym], key=lambda r: r.quarter_end)
        f_rows = sorted(fmp_rows[sym], key=lambda r: r.date)
        y_rows = sorted(yahoo_rows[sym], key=lambda r: r.date)
        cutoff = _to_date(SPLIT_CUTOFF[sym])

        for e in e_rows:
            qe = _to_date(e.quarter_end)
            if qe < cutoff:
                continue
            f_match = _closest_within_days(f_rows, qe, max_days=60,
                                           date_attr="date")
            y_match = _closest_within_days(y_rows, qe, max_days=60,
                                           date_attr="date")

            row = {
                "ticker": sym,
                "quarter_end": e.quarter_end,
                "fiscal_period": e.fiscal_period,
                "edgar_val": e.val,
                "edgar_form": e.form,
                "edgar_filed": e.filed,
                "fmp_val": (f_match.eps_actual if f_match
                            and f_match.eps_actual is not None else None),
                "fmp_release_date": f_match.date if f_match else None,
                "yahoo_val": y_match.eps_actual if y_match else None,
                "yahoo_release_date": y_match.date if y_match else None,
            }

            # Discrepancy logic
            disagreements = []
            if (row["fmp_val"] is not None
                    and abs(row["fmp_val"] - row["edgar_val"]) > TOLERANCE):
                disagreements.append(
                    f"FMP-vs-EDGAR Δ={row['fmp_val'] - row['edgar_val']:+.3f}")
            if (row["yahoo_val"] is not None
                    and abs(row["yahoo_val"] - row["edgar_val"]) > TOLERANCE):
                disagreements.append(
                    f"Yahoo-vs-EDGAR Δ={row['yahoo_val'] - row['edgar_val']:+.3f}")
            if (row["fmp_val"] is not None
                    and row["yahoo_val"] is not None
                    and abs(row["fmp_val"] - row["yahoo_val"]) > TOLERANCE):
                disagreements.append(
                    f"FMP-vs-Yahoo Δ={row['fmp_val'] - row['yahoo_val']:+.3f}")

            row["disagreements"] = "; ".join(disagreements) if disagreements else ""
            (discrepancies if disagreements else matches).append(row)

    # Print summary
    print(f"  Total quarters matched across sources: {len(matches) + len(discrepancies)}")
    print(f"  Quarters where all 3 sources agree (within ${TOLERANCE}): {len(matches)}")
    print(f"  Quarters with disagreement: {len(discrepancies)}")

    # Top-level breakdown by ticker
    print(f"\n  {'Ticker':8} {'matched':>8} {'disagrees':>10}")
    for sym in MAG7:
        m = sum(1 for r in matches if r["ticker"] == sym)
        d = sum(1 for r in discrepancies if r["ticker"] == sym)
        print(f"  {sym:8} {m:>8} {d:>10}")

    # Show first few discrepancies per ticker
    print("\n  Discrepancy examples (first 3 per ticker):")
    for sym in MAG7:
        sym_discs = [r for r in discrepancies if r["ticker"] == sym][:3]
        if not sym_discs:
            continue
        print(f"\n  {sym}:")
        for r in sym_discs:
            f_v = f"{r['fmp_val']:.3f}" if r['fmp_val'] is not None else "-"
            e_v = f"{r['edgar_val']:.3f}" if r['edgar_val'] is not None else "-"
            y_v = f"{r['yahoo_val']:.3f}" if r['yahoo_val'] is not None else "-"
            print(f"    {r['quarter_end']} ({r['fiscal_period']}) "
                  f"FMP={f_v}  EDGAR={e_v}  Yahoo={y_v}  → {r['disagreements']}")

    # ─── 4. Write CSVs ────────────────────────────────────────────────
    out_unified = out / f"cross_source_{stamp}_unified.csv"
    all_rows = matches + discrepancies
    if all_rows:
        with out_unified.open("w", newline="") as f:
            fieldnames = list(all_rows[0].keys())
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in sorted(all_rows, key=lambda x: (x["ticker"], x["quarter_end"])):
                w.writerow(r)
        print(f"\n→ unified: {out_unified}")

    out_disc = out / f"cross_source_{stamp}_discrepancies.csv"
    if discrepancies:
        with out_disc.open("w", newline="") as f:
            fieldnames = list(discrepancies[0].keys())
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in sorted(discrepancies, key=lambda x: (x["ticker"], x["quarter_end"])):
                w.writerow(r)
        print(f"→ discrepancies-only: {out_disc}")


def _to_date(s: str):
    from datetime import date
    return date.fromisoformat(s)


def _closest_within_days(rows, target, max_days, date_attr="date"):
    best, best_gap = None, None
    for r in rows:
        try:
            d = _to_date(getattr(r, date_attr))
        except Exception:
            continue
        gap = abs((d - target).days)
        if gap <= max_days and (best_gap is None or gap < best_gap):
            best, best_gap = r, gap
    return best


if __name__ == "__main__":
    main()
