"""End-to-end demonstration — proves the three components work on live data.

Run:
    uv run python scripts/probe_fmp.py

What it demonstrates:
  1. FMP earnings calendar — upcoming prints for the top 10
  2. FMP analyst estimates — consensus with high/low/# analysts
  3. FMP historical earnings — actuals vs estimates for calibrating σ
  4. Strategy-B bucket construction on one real ticker, end-to-end
  5. Dumps everything to results/probe_<timestamp>_*.csv for review

Scope: data-adapter-side only. Does not prescribe market open/lock/settle
dates, quorum logic, multi-source cross-checks, or settlement mechanics —
those are product-layer decisions for the market creator and the production
daemon implementation.
"""
from __future__ import annotations

import csv
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from truflation_eps.fmp_client import FMPClient, TOP_10
from truflation_eps.market_spec import (
    analyst_spread_buckets,
    historical_surprise_sigma,
    surprise_sigma_buckets,
)
from truflation_eps.calendar import discover_upcoming
from truflation_eps.yahoo_client import YahooClient


def main() -> None:
    client = FMPClient()
    yahoo = YahooClient()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    results_dir = Path(__file__).resolve().parents[1] / "results"
    results_dir.mkdir(exist_ok=True)

    # ─── 1. Upcoming earnings ─────────────────────────────────────────
    print("=" * 72)
    print("1. Upcoming earnings — top 10 in next 90 days")
    print("=" * 72)
    cal = client.earnings_calendar(
        from_date=date.today(), to_date=date.today() + timedelta(days=90)
    )
    cal_top = [r for r in cal if r.symbol in TOP_10]
    print(f"{'Symbol':8s} {'Date':12s} {'EpsEst':>10s} {'EpsAct':>10s} {'Updated':12s}")
    for r in cal_top:
        print(f"{r.symbol:8s} {r.date:12s}  {str(r.eps_estimated or '-'):>10s}  "
              f"{str(r.eps_actual or '-'):>10s}  {str(r.last_updated or '-'):12s}")
    if not cal_top:
        print("(none in this window)")
    print()

    # ─── 2. Historical surprise σ per ticker ──────────────────────────
    print("=" * 72)
    print("2. Historical surprise statistics — top 10 (last 8 quarters)")
    print("=" * 72)
    print(f"{'Symbol':8s} {'n':>4s} {'mean(surp%)':>14s} {'σ(surp%)':>12s}")
    hist_rows = []
    surprise_stats: dict[str, dict] = {}
    for sym in TOP_10:
        try:
            rows = client.historical_earnings(sym, limit=8)
        except Exception as exc:
            print(f"  {sym}: ERROR — {exc}")
            continue
        hist_rows.extend(rows)
        try:
            mean, sigma, n = historical_surprise_sigma(rows)
            surprise_stats[sym] = {"mean": mean, "sigma": sigma, "n": n}
            print(f"{sym:8s} {n:>4d} {mean*100:>+13.2f}% {sigma*100:>11.2f}%")
        except ValueError as exc:
            print(f"{sym:8s} (insufficient history: {exc})")
    print()

    # ─── 3. Analyst estimates — upcoming quarters ────────────────────
    print("=" * 72)
    print("3. Analyst estimates — top 10, upcoming quarters")
    print("=" * 72)
    est_rows = []
    for sym in TOP_10:
        try:
            # limit=40 is needed because FMP returns results future-first; with a
            # small limit, well-covered tickers can skip near-term quarters entirely.
            ests = client.analyst_estimates(sym, period="quarter", limit=40)
        except Exception as exc:
            print(f"  {sym}: ERROR — {exc}")
            continue
        today_year = date.today().year
        near = [e for e in ests
                if e.quarter_end[:4] in {str(today_year), str(today_year + 1)}
                and e.eps_avg and e.eps_avg > 0]
        est_rows.extend(near)
        if not near:
            print(f"  {sym:6s} (no near-term estimates)")
            continue
        print(f"\n  {sym}:")
        for e in sorted(near, key=lambda x: x.quarter_end)[:3]:
            print(f"    {e.quarter_end}  "
                  f"avg={e.eps_avg:.3f}  high={e.eps_high:.3f}  "
                  f"low={e.eps_low:.3f}  n={e.n_analysts}")
    print()

    # ─── 4. Bucket construction — both strategies, worked example ────
    print("=" * 72)
    print("4. Bucket construction — both strategies, worked example on TSLA")
    print("=" * 72)
    demo_sym = "TSLA"
    try:
        # limit=40 because FMP returns future-first and limit=12 would miss
        # the actual nearest upcoming quarter on well-covered tickers.
        ests = client.analyst_estimates(demo_sym, period="quarter", limit=40)
        today = date.today()
        upcoming = [e for e in ests
                    if e.quarter_end
                    and date.fromisoformat(e.quarter_end) >= today
                    and e.eps_avg and e.eps_avg > 0]
        if upcoming:
            est = sorted(upcoming, key=lambda x: x.quarter_end)[0]
            tsla_hist = client.historical_earnings(demo_sym, limit=20)
            mean, sigma, n = historical_surprise_sigma(tsla_hist)

            print(f"  Ticker: {demo_sym}")
            print(f"  Quarter end: {est.quarter_end}")
            print(f"  Current consensus epsAvg: {est.eps_avg:.3f}  "
                  f"(n={est.n_analysts} analysts, high={est.eps_high:.3f}, low={est.eps_low:.3f})")
            analyst_spread_pct = (est.eps_high - est.eps_low) / est.eps_avg * 100
            print(f"  Analyst spread (epsHigh − epsLow): {est.eps_high - est.eps_low:.3f}  "
                  f"({analyst_spread_pct:.2f}% of epsAvg)")
            print(f"  Historical surprise σ: {sigma*100:.2f}% (from {n} past prints)")
            print()

            print("  Strategy A — Analyst-spread buckets")
            print("  (boundaries at epsLow, mid(low,avg), mid(avg,high), epsHigh)")
            for b in analyst_spread_buckets(est):
                if b.lower == -math.inf:
                    rng = f"< {b.upper:.3f}"
                elif b.upper == math.inf:
                    rng = f"> {b.lower:.3f}"
                else:
                    rng = f"{b.lower:.3f} – {b.upper:.3f}"
                print(f"    Bucket {b.index} [{b.label:22s}]  {rng}")
            print()

            print("  Strategy B — Historical-surprise-σ buckets")
            print("  (boundaries at epsAvg × (1 ± 0.5σ, ±1.5σ))")
            for b in surprise_sigma_buckets(est.eps_avg, tsla_hist):
                if b.lower == -math.inf:
                    rng = f"< {b.upper:.3f}"
                elif b.upper == math.inf:
                    rng = f"> {b.lower:.3f}"
                else:
                    rng = f"{b.lower:.3f} – {b.upper:.3f}"
                print(f"    Bucket {b.index} [{b.label:22s}]  {rng}")
    except Exception as exc:
        print(f"  ERROR: {exc}")
    print()

    # ─── 5. Upcoming earnings discovery with matched estimates + timing ─
    print("=" * 72)
    print("5. Upcoming-earnings discovery (FMP estimates + Yahoo timing)")
    print("=" * 72)
    upcoming_list = []
    try:
        upcoming_list = discover_upcoming(client, lookahead_days=90, yahoo=yahoo)
        header = f"{'Symbol':8s} {'EarningsDate':14s} {'BMO/AMC':8s} {'ScheduledAt':28s} {'EpsAvg':>8s} {'High':>8s} {'Low':>8s} {'N':>4s}"
        print(header)
        for u in upcoming_list:
            est = u.estimate
            timing = (u.release_timing or '').upper() or '-'
            scheduled = u.scheduled_at or '-'
            if est:
                print(f"{u.symbol:8s} {u.earnings_date:14s} {timing:8s} {scheduled:28s} "
                      f"{est.eps_avg:>8.3f} {est.eps_high:>8.3f} {est.eps_low:>8.3f} {est.n_analysts:>4d}")
            else:
                print(f"{u.symbol:8s} {u.earnings_date:14s} {timing:8s} {scheduled:28s} (no estimate matched)")
    except Exception as exc:
        print(f"  ERROR: {exc}")
    print()

    # ─── 6. Yahoo timing — full table for top 10 ─────────────────────
    print("=" * 72)
    print("6. Yahoo Finance — release timing for top 10 (next event each)")
    print("=" * 72)
    yahoo_rows = []
    print(f"{'Symbol':8s} {'ScheduledAt':32s} {'BMO/AMC':8s} {'EpsEst':>8s} {'EpsAct':>8s}")
    for sym in TOP_10:
        try:
            ev = yahoo.next_event(sym)
        except Exception as exc:
            print(f"  {sym}: ERROR — {exc}")
            continue
        if ev is None:
            print(f"  {sym}: no upcoming event")
            continue
        yahoo_rows.append(ev)
        timing = (ev.bmo_amc or '-').upper()
        eps_est = f"{ev.eps_estimated:.3f}" if ev.eps_estimated is not None else '-'
        eps_act = f"{ev.eps_actual:.3f}" if ev.eps_actual is not None else '-'
        print(f"{sym:8s} {ev.scheduled_at:32s} {timing:8s} {eps_est:>8s} {eps_act:>8s}")
    print()

    # ─── CSV dumps ────────────────────────────────────────────────────
    out = results_dir / f"probe_{stamp}_calendar.csv"
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "date", "eps_estimated", "eps_actual",
                    "revenue_estimated", "revenue_actual", "last_updated"])
        for r in cal:
            w.writerow([r.symbol, r.date, r.eps_estimated, r.eps_actual,
                        r.revenue_estimated, r.revenue_actual, r.last_updated])
    print(f"Calendar → {out}")

    out = results_dir / f"probe_{stamp}_historical.csv"
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "date", "eps_estimated", "eps_actual",
                    "revenue_estimated", "revenue_actual", "last_updated"])
        for r in hist_rows:
            w.writerow([r.symbol, r.date, r.eps_estimated, r.eps_actual,
                        r.revenue_estimated, r.revenue_actual, r.last_updated])
    print(f"Historical → {out}")

    out = results_dir / f"probe_{stamp}_estimates.csv"
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "quarter_end", "eps_avg", "eps_high", "eps_low",
                    "n_analysts", "revenue_avg"])
        for e in est_rows:
            w.writerow([e.symbol, e.quarter_end, e.eps_avg, e.eps_high, e.eps_low,
                        e.n_analysts, e.revenue_avg])
    print(f"Estimates → {out}")

    out = results_dir / f"probe_{stamp}_surprise_stats.csv"
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "n_quarters", "mean_surprise_pct", "sigma_surprise_pct"])
        for sym, s in surprise_stats.items():
            w.writerow([sym, s["n"], f"{s['mean']*100:.4f}", f"{s['sigma']*100:.4f}"])
    print(f"Surprise stats → {out}")

    out = results_dir / f"probe_{stamp}_yahoo_timing.csv"
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "scheduled_at", "date", "hour_et", "bmo_amc",
                    "eps_estimated", "eps_actual", "surprise_pct"])
        for ev in yahoo_rows:
            w.writerow([ev.symbol, ev.scheduled_at, ev.date, ev.hour_et, ev.bmo_amc,
                        ev.eps_estimated, ev.eps_actual, ev.surprise_pct])
    print(f"Yahoo timing → {out}")

    out = results_dir / f"probe_{stamp}_upcoming.csv"
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "earnings_date", "release_timing", "scheduled_at",
                    "matched_quarter_end", "eps_avg", "eps_high", "eps_low", "n_analysts"])
        for u in upcoming_list:
            est = u.estimate
            w.writerow([
                u.symbol, u.earnings_date, u.release_timing or "", u.scheduled_at or "",
                est.quarter_end if est else "",
                est.eps_avg if est else "",
                est.eps_high if est else "",
                est.eps_low if est else "",
                est.n_analysts if est else "",
            ])
    print(f"Upcoming (FMP+Yahoo joined) → {out}")


if __name__ == "__main__":
    main()
