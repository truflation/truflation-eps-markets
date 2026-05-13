# truflation-eps-markets

Proof-of-concept for range-based earnings-per-share (EPS) prediction markets, settled by a Truflation-signed EPS stream sourced from three independent primitive streams: SEC EDGAR, Financial Modeling Prep (FMP), and Yahoo Finance.

**Scope: demonstrate that the data works and the divergence-halt rules hold.** This repo shows that (a) FMP exposes the three data inputs needed to run a range-based EPS market end-to-end, (b) σ-calibrated bucket construction produces sensible per-ticker boundaries on real historical data, and (c) three-source agreement at $0.01 tolerance is empirically achievable on Mag 7 prints (see [§ Cross-source verification](#cross-source-verification--divergence-halt-rules) below).

**Universe scope: Mag 7 only** — AAPL, MSFT, GOOGL, AMZN, META, NVDA, TSLA.

**Out of scope**: production daemon implementation, market creation and settlement mechanics, stream-schema finalization. Those are decisions for the production implementation; this repo is designed to inform them, not preempt them.

---

## Why this exists

Polymarket currently lists 72 earnings markets, all structured as single-threshold binary ("Will TSLA beat?" Yes/No vs analyst consensus). No range markets. The open design space is multi-outcome categorical markets settled against actual reported EPS.

[Financial Modeling Prep (FMP)](https://financialmodelingprep.com) already exposes the three data pieces needed:

1. **Consensus estimate** (FMP field `epsAvg`) — the arithmetic mean of all analyst EPS estimates for a given quarter. The implicit "fair value" of the underlying.
2. **Analyst range** (FMP fields `epsHigh` / `epsLow` / `numAnalystsEps`) — the highest and lowest analyst estimates plus the number of analysts covering. The spread of professional expectations.
3. **Historical surprise distribution** (actuals vs estimates across past quarters) — the empirical standard deviation of how far actual EPS tends to deviate from consensus.

Given (1) and (3), 5-outcome categorical markets can be constructed with statistically calibrated probabilities — the type of multi-outcome market that TN supports natively.

This repo demonstrates that the inputs work. Everything else is a production implementation task.

Full field-by-field definitions are in [§ FMP Data Dictionary](#fmp-data-dictionary) below.

---

## Market frequency

### Quarterly per ticker, staggered across the earnings season

For the Mag 7 universe (AAPL, MSFT, GOOGL, AMZN, META, NVDA, TSLA):

- **4 markets per ticker per year** = **28 markets/year total**
- Earnings seasons cluster four times annually — Apr/May, Jul/Aug, Oct/Nov, Jan/Feb
- Peak density: 3–5 Mag 7 reporting in the same week
- Off-peak: ~1 Mag 7 reporting per week

### Per-market lifecycle

From the data-adapter perspective, three moments matter:

1. **Reference lock** — the moment the consensus `epsAvg` is recorded as the market's anchor. Polymarket locks this 7 days before earnings; the production implementation can choose differently.
2. **Earnings release (T)** — the company publishes actual EPS (usually after market close).
3. **Stream broadcast (T+1 to T+2)** — FMP populates `epsActual`; a production daemon signs and posts the value to the TN stream; market consumers settle.

This repo assumes FMP provides `epsActual` reliably within 48 hours of the after-close release. Verified empirically on TSLA Q1 2026 (earnings Apr 22, FMP populated by Apr 24).

---

## FMP Data Dictionary

All data in this repo comes from [Financial Modeling Prep (FMP)](https://financialmodelingprep.com) via the `/stable/` API endpoints. FMP's field names are used as-is in code so there's no translation layer to maintain. Definitions:

### Earnings calendar / historical earnings endpoints

Returned per company, per quarter. Populated before and after earnings release.

| FMP field | Our code attribute | Meaning |
|---|---|---|
| `symbol` | `symbol` | Ticker symbol (`AAPL`, `TSLA`, etc.) |
| `date` | `date` | **Earnings release date** — the day the company publishes its press release (usually after market close). Different from fiscal quarter end. |
| `epsEstimated` | `eps_estimated` | Snapshot of consensus analyst EPS estimate at the time of the earnings release. One number per ticker per quarter. |
| `epsActual` | `eps_actual` | Actual reported EPS from the company press release. `null` until the company reports. |
| `revenueEstimated` | `revenue_estimated` | Consensus analyst revenue estimate |
| `revenueActual` | `revenue_actual` | Actual reported revenue. `null` until reported. |
| `lastUpdated` | `last_updated` | ISO date FMP last refreshed this row. Useful for latency measurement — for TSLA Q1 2026 (earnings Apr 22), this was populated by `2026-04-24`. |

### Analyst estimates endpoint

Returned per company, per fiscal quarter — the live consensus inputs that drive bucket construction.

| FMP field | Our code attribute | Meaning |
|---|---|---|
| `symbol` | `symbol` | Ticker symbol |
| `date` | `quarter_end` | **Fiscal quarter end date** — the last day of the reporting period (e.g. `2026-06-30` for Q2 2026). NOT the earnings release date; earnings typically release 3–6 weeks later. |
| `epsAvg` | `eps_avg` | **Arithmetic mean** of all contributing analysts' EPS estimates for the quarter. This is the "consensus" Polymarket settles their binary markets against. |
| `epsHigh` | `eps_high` | Highest individual analyst EPS estimate |
| `epsLow` | `eps_low` | Lowest individual analyst EPS estimate |
| `numAnalystsEps` | `n_analysts` | Number of analysts contributing EPS estimates. Used as a coverage-quality threshold (suggested minimum: 10 analysts for a ticker to be listable). |
| `revenueAvg` | `revenue_avg` | Consensus revenue estimate (mean across analysts) |

### GAAP vs non-GAAP EPS — per-ticker settlement convention

The settlement convention is **fixed per ticker, locked before each market opens, and does not change during the market's life.** This matches §2 of the Settlement Rules doc.

| Ticker | Settles on              | Source convention                                                       |
|--------|-------------------------|-------------------------------------------------------------------------|
| AAPL   | GAAP diluted EPS        | Press release headline; cross-checked vs EDGAR XBRL                     |
| MSFT   | GAAP diluted EPS        | Press release headline; cross-checked vs EDGAR XBRL                     |
| GOOGL  | GAAP diluted EPS        | Press release headline; cross-checked vs EDGAR XBRL                     |
| AMZN   | GAAP diluted EPS        | Press release headline; cross-checked vs EDGAR XBRL                     |
| META   | GAAP diluted EPS        | Press release headline; cross-checked vs EDGAR XBRL                     |
| NVDA   | Non-GAAP diluted EPS    | 8-K Exhibit 99.1 press release; tracks analyst consensus                |
| TSLA   | Non-GAAP diluted EPS    | 8-K Exhibit 99.1 press release; tracks analyst consensus                |

For non-GAAP-settling tickers (NVDA, TSLA), EDGAR's structured XBRL data carries only GAAP — so the EDGAR primitive stream reads from the **8-K Exhibit 99.1** (the press release on file with the SEC) instead. See [§ Cross-source verification](#cross-source-verification--divergence-halt-rules).

---

## Cross-source verification + divergence halt rules

Each settlement market is fed by **three independent primitive streams** on TRUF.NETWORK: SEC EDGAR, FMP, and Yahoo Finance. The composite stream finalizes only when **at least two of the three return the same figure within $0.01** (the Settlement Rules §3 quorum). When no quorum is reached, the composite pauses and triggers manual review against the official earnings press release.

### EDGAR layer — two modalities depending on convention

| Convention             | EDGAR source                                       | Module                                                  |
|------------------------|----------------------------------------------------|---------------------------------------------------------|
| GAAP-settling tickers  | XBRL `us-gaap/EarningsPerShareDiluted`             | `edgar_xbrl_client.EdgarXbrlClient`                     |
| Non-GAAP-settling      | 8-K Form, Item 2.02, Exhibit 99.1 (press release)  | `edgar_8k_client.Edgar8KClient`                         |

The 8-K modality uses the SEC's `{accession}-index-headers.html` to look up Exhibit 99.1 by **type tag**, not filename pattern — filenames vary across companies (`q4fy26pr.htm`, `exhibit991.htm`, `tsla-ex99_1.htm`, even `exhbit991.htm` [TSLA typo'd one]) but the `<TYPE>EX-99.1` declaration is canonical.

### Offline verification scripts

- **`scripts/cross_source_check.py`** — pulls historical EPS from all three sources for the Mag 7, joins by quarter, reports pairwise deltas at $0.005 tolerance. Outputs three CSVs per run (`depth`, `unified`, `discrepancies-only`). Currently uses EDGAR XBRL only (the GAAP modality); for NVDA/TSLA this will systematically diverge by the GAAP-vs-non-GAAP convention gap, which is the structural reason the per-ticker settlement convention table above exists. Baseline run committed at [`results/cross_source_baseline_unified.csv`](results/cross_source_baseline_unified.csv).
- **`scripts/parse_8k_eps.py`** — for non-GAAP-settling tickers (NVDA, TSLA), pulls the **most recent 4 quarters** of 8-K Exhibit 99.1, extracts non-GAAP diluted EPS via per-ticker regex extractors, and runs the full three-way check vs FMP `epsActual` and Yahoo `Reported EPS` at $0.01 tolerance. Each filing is classified by quorum outcome (`all_3_agree` / `edgar_fmp_agree` / `edgar_yahoo_agree` / `fmp_yahoo_agree` / `no_quorum` / `no_edgar`) per Settlement Rules §3. Fast spot-check.
- **`scripts/validate_extractors.py`** — the **5-year (configurable) historical sweep** of the same three-way check, used to confirm the per-ticker extractors cover all press-release format variants in the validation window. Outputs one CSV with every filing row, every pairwise delta, and per-row quorum classification. This is the script that backs the 5-year extractor claim below.

The quorum logic itself (`determine_quorum`) lives in `src/truflation_eps/settlement_rules.py` — a single pure function over three floats, imported by both scripts so the rules are evaluated identically everywhere.

### Quorum outcomes and committed values

`determine_quorum(edgar, fmp, yahoo)` maps three EPS values to one of six outcomes per Settlement Rules §3. No averaging — the committed value is whichever value the agreeing sources returned.

| Outcome              | Meaning                                                          | Settles? | Committed value             |
|----------------------|------------------------------------------------------------------|----------|-----------------------------|
| `all_3_agree`        | All three within $0.01 — strongest case                          | Yes      | EDGAR (regulatory-primary)  |
| `edgar_fmp_agree`    | EDGAR + FMP agree; Yahoo diverges or absent                      | Yes (2-of-3) | EDGAR                   |
| `edgar_yahoo_agree`  | EDGAR + Yahoo agree; FMP diverges or absent                      | Yes (2-of-3) | EDGAR                   |
| `fmp_yahoo_agree`    | FMP + Yahoo agree; EDGAR diverges                                | Yes (2-of-3) | FMP                     |
| `no_quorum`          | No two of the three agree within $0.01                           | No — manual review | `None`            |
| `no_edgar`           | EDGAR side returned None (no 8-K Ex-99.1, fetch error, missing)  | No — manual review | `None`            |

The function has no I/O, no logging, no state — three floats in, a tuple out. Side effects (CSV writing, manual-review queuing, halt notifications) live in the scripts that call it.

### Empirical validation (5-year window, May 2021 → May 2026)

Reproducible end-to-end via `scripts/validate_extractors.py`. Per-row evidence (61 filings) committed at [`results/validate_extractors_baseline.csv`](results/validate_extractors_baseline.csv).

**Checked the last 4 quarters per ticker (May 2025 → May 2026) — all aligned.** Every real earnings print resolves as `all_3_agree` (three sources independently agree within $0.01). Per-ticker breakdown across the full 5-year baseline:

| Ticker | Real earnings 8-Ks in window | Extractor accuracy | `all_3_agree` (post-split) | `fmp_yahoo_agree` (pre-split artifact) | `no_quorum` |
|--------|-----------------------------:|-------------------:|---------------------------:|---------------------------------------:|------------:|
| NVDA   | 21                           | 20/21              | 7                          | 11                                     | 2 \*\*      |
| TSLA   | 12 (since July 2023)         | 12/12              | 12                         | 0                                      | 0           |

\* Pre-split NVDA divergences are exclusively the **retroactive split-adjustment artifact**: the 8-K archive is immutable (as-filed at the time), while FMP and Yahoo retroactively split-adjust historical EPS to current units. The two retroactive adjustments produce the same value within rounding, so FMP and Yahoo agree with each other and the row resolves as `fmp_yahoo_agree`. Never relevant at the moment of release.

\*\* The 2 NVDA `no_quorum` rows (Q2 + Q3 FY22, pre-July-2021-split) are FMP-vs-Yahoo split-adjustment rounding disagreements on values back-adjusted by 40×. Pure historical-rounding artifact. The 1 NO_EXTRACT was an off-cycle preliminary-results 8-K, not a real earnings release; the extractor correctly returned None.

This validates that **FMP's `epsActual` follows the press-release headline within $0.01 on every real earnings print in the validation window**.

### Coverage limits (worth knowing)

- **TSLA**: the table-format extractor covers July 2023 → present. Pre-July-2023 press releases use different non-GAAP phrasing and are not parsed. Out of scope — settlement is forward-only.
- **NVDA**: extractor covers both the FY26+ combined-sentence format (*"GAAP and non-GAAP earnings per diluted share were $1.76 and $1.62, respectively"*) and the FY24-FY25 separate-sentence format (*"Non-GAAP earnings per diluted share was $0.89"*). Pre-FY24 has not been validated.
- **Both**: TSLA delivery-update 8-Ks (filed quarterly under Item 2.02 with no EPS data) correctly return None.

### Three-way agreement status

| Ticker subset                       | Three-way coverage (EDGAR × FMP × Yahoo)                                              | Script                          |
|-------------------------------------|---------------------------------------------------------------------------------------|---------------------------------|
| AAPL, MSFT, GOOGL, AMZN, META       | EDGAR XBRL × FMP × Yahoo, joined on quarter_end                                       | `scripts/cross_source_check.py` |
| NVDA, TSLA                          | EDGAR 8-K Ex-99.1 × FMP × Yahoo, joined on release date, with quorum outcome per row  | `scripts/parse_8k_eps.py`       |

**Empirical quorum result on the most recent 4 quarters per ticker** (`parse_8k_eps.py` fast spot-check, May 2025 → May 2026):

| Ticker | Earnings 8-Ks tested | `all_3_agree` | `2-of-3 agree` | `no_quorum` | `no_edgar` |
|--------|---------------------:|--------------:|---------------:|------------:|-----------:|
| NVDA   | 4                    | 4             | 0              | 0           | 0          |
| TSLA   | 2 (real) + 2 (delivery) | 2          | 0              | 0           | 2          |

Every real earnings print resolves as `all_3_agree`. Delivery 8-Ks (TSLA's quarterly vehicle-delivery filings, which carry Item 2.02 but no EPS) correctly classify as `no_edgar` — the rule's natural pause state when the EDGAR side is missing.

---

### Quirks and gotchas

**FMP returns analyst estimates ordered future-to-past.** When calling the `analyst-estimates` endpoint, a small `limit` can silently miss near-term quarters on well-covered tickers. Concrete example: calling with `limit=12` for TSLA returns 2028-Q1 through 2030-Q4 because those are the 12 furthest-out quarters with analyst coverage — completely skipping the actual next upcoming quarter (2026-Q2). **Always use `limit=40` or higher** to capture the full window of near-term + forward estimates. This repo's code uses `limit=40`.

**Far-future quarters return placeholder zeros.** FMP populates `epsAvg=0, epsHigh=0, epsLow=0` for quarters where analysts haven't yet posted numeric estimates (typically 4+ years out). Filter with `eps_avg > 0` before constructing buckets or they'll divide-by-zero.

**`date` means different things on different endpoints.** On the earnings-calendar endpoint, `date` is the earnings release date. On the analyst-estimates endpoint, `date` is the fiscal quarter end date. These can differ by weeks. The `calendar.py` module renames the latter to `quarter_end` for clarity.

**`epsAvg` ≠ (`epsHigh` + `epsLow`) / 2.** The consensus is the arithmetic mean across all contributing analysts, not the midpoint of the range. For TSLA Q2 2026 as of April 24 2026: epsHigh=0.598, epsLow=0.222, midpoint=0.410, but epsAvg=0.434 (closer to high, pulled up by where the analyst-density sits).

**Legacy `/api/v3/` endpoints return 403.** FMP deprecated its legacy earnings endpoints in August 2025. Only `/stable/` variants work. Any older integration referencing `/api/v3/earnings-calendar` will 403.

**FMP `/stable/` does not return release time-of-day.** Empirically verified across `/stable/earnings-calendar`, `/stable/earnings`, and the deprecated `/api/v3/` variants — none expose a `time` field or BMO/AMC marker. The legacy v3 endpoints carried `"amc"` / `"bmo"` values but are now 403'd for any account not subscribed before Aug 31 2025. Release timing must come from a secondary source — see [§ Release timing](#release-timing--secondary-source) below.

---

## Release timing — secondary source

For any production market that needs to lock or settle relative to the actual release moment (e.g., "freeze quotes 30 min before scheduled release"), FMP alone is insufficient. The repo ships a thin Yahoo Finance client (`yahoo_client.py`) that fills this gap. No API key required.

### Source comparison

| Source                       | API key       | Time field                                                   | Coverage on Mag 7 (May 2026) | Notes |
|------------------------------|---------------|--------------------------------------------------------------|------------------------------|-------|
| **FMP `/stable/`**           | yes (paid)    | None                                                         | n/a                          | EPS values only; tracks press-release headline |
| **NASDAQ public API**        | no            | `time-pre-market` / `time-after-hours` / `time-not-supplied` | ~95% upcoming, ~1% post-release | Stub-heavy; fallback only for upcoming events |
| **Yahoo Finance (yfinance)** | no            | Full timezone-aware datetime                                 | 100%                         | Primary timing source; EPS values match FMP within rounding |
| **SEC EDGAR**                | no            | 8-K acceptance timestamp (sub-second)                        | 100% (regulatory mandate)    | XBRL for GAAP tickers; 8-K Ex-99.1 for non-GAAP tickers |

### Yahoo output (one row per scheduled event)

The `YahooClient.earnings_dates(symbol)` method returns one `YahooEarningsRow` per quarter, fields:

| Field | Meaning |
|---|---|
| `symbol` | Ticker |
| `scheduled_at` | ISO datetime in `America/New_York`, e.g. `2026-07-30T16:00:00-04:00` |
| `date` | `YYYY-MM-DD` from `scheduled_at` |
| `hour_et` | Integer hour (0–23) — 8 ⇒ BMO, 16 ⇒ AMC, 12–13 ⇒ mid-day |
| `bmo_amc` | `"bmo"` if `hour_et ≤ 9`, `"amc"` if `hour_et ≥ 14`, else `None` |
| `eps_estimated` | Yahoo's view of consensus (matches FMP within rounding) |
| `eps_actual` | Reported EPS (matches FMP within rounding) |
| `surprise_pct` | Pre-computed `(actual − estimated) / estimated × 100` |

### Coverage observed on Mag 7

```
Symbol  ScheduledAt                  BMO/AMC
AAPL    2026-07-30T16:00:00-04:00    AMC
MSFT    2026-07-29T16:00:00-04:00    AMC
GOOGL   2026-07-23T16:00:00-04:00    AMC
AMZN    2026-07-30T16:00:00-04:00    AMC
META    2026-07-29T16:00:00-04:00    AMC
NVDA    2026-05-20T16:00:00-04:00    AMC
TSLA    2026-07-22T16:00:00-04:00    AMC
```

All Mag 7 are AMC (after-market-close) reporters — typical for US large-cap tech. The `bmo_amc` classifier supports both, but every Mag 7 print currently lands at 16:00:00-04:00.

### Recommended source split

| Field                              | Primary                              | Fallback                          |
|------------------------------------|--------------------------------------|-----------------------------------|
| EPS estimate, actual, history      | FMP (paid, contractual)              | Yahoo (same values)               |
| EPS regulatory-primary reference   | **SEC EDGAR** (XBRL or 8-K Ex-99.1)  | —                                 |
| Release date + time                | Yahoo                                | NASDAQ for upcoming events only   |
| Cross-check on release day         | NASDAQ                               | —                                 |

### Risks of relying on Yahoo

1. **Unofficial endpoint** — `yfinance` scrapes `finance.yahoo.com`. Yahoo can break the page format without notice; observed ~3 incidents in the last 24 months, each fixed within days.
2. **ToS gray zone** — Yahoo's ToS technically prohibit commercial scraping. Most quant shops use it anyway and the underlying values aren't copyrightable, but a Truflation-signed stream sourcing from Yahoo deserves a legal sanity check before mainnet.
3. **No SLA** — fine for a data-adapter spec, less fine for a settling daemon. A production deployment should treat Yahoo as a redundancy/observability source and persist the captured timestamp at announcement, not query it on the settlement path.

Post-EDGAR addition, Yahoo is one of three quorum sources rather than a single point of failure. If Yahoo breaks on release day, settlement still finalizes via FMP + EDGAR agreement under the 2-of-3 rule.

---

## Bucket construction — two strategies

Two ways to construct the 5-outcome bucket boundaries. Both are implemented and demonstrated side-by-side in the probe script. The market creator can choose either depending on product goals.

### Strategy A — Analyst-spread buckets

Uses the current quarter's analyst estimates directly:

| Bucket | Label | Boundary |
|---|---|---|
| 1 | deep miss | `< epsLow` |
| 2 | mild miss | `epsLow – mid(epsLow, epsAvg)` |
| 3 | in-consensus | `mid(epsLow, epsAvg) – mid(epsAvg, epsHigh)` |
| 4 | mild beat | `mid(epsAvg, epsHigh) – epsHigh` |
| 5 | deep beat | `> epsHigh` |

Outer boundaries anchor at the most pessimistic and most optimistic analyst. Inner boundaries split the low-to-avg and avg-to-high ranges at their midpoints.

**Pros**: intuitive and legible for retail ("was the print within the analyst range or outside it?"). Reflects live forecaster disagreement. Updates as the consensus evolves.

**Cons**: analysts cluster tightly around consensus; actual results routinely print outside the `[epsLow, epsHigh]` range. Tail buckets (1 and 5) can end up capturing most of the probability mass.

### Strategy B — Historical-surprise-σ buckets

Uses the standard deviation of past surprise% for the same ticker:

```
surprise_i = (actual_i − estimated_i) / estimated_i
σ = stdev({surprise_i})
```

Places boundaries at `epsAvg × (1 ± 0.5σ, ±1.5σ)`:

| Bucket | Label | Boundary | Theoretical probability (normal approximation) |
|---|---|---|---:|
| 1 | miss > 1.5σ | `< epsAvg·(1−1.5σ)` | ~6.7% |
| 2 | miss 0.5–1.5σ | `epsAvg·(1−1.5σ) – epsAvg·(1−0.5σ)` | ~24.2% |
| 3 | in-band ±0.5σ | `epsAvg·(1−0.5σ) – epsAvg·(1+0.5σ)` | ~38.3% |
| 4 | beat 0.5–1.5σ | `epsAvg·(1+0.5σ) – epsAvg·(1+1.5σ)` | ~24.2% |
| 5 | beat > 1.5σ | `> epsAvg·(1+1.5σ)` | ~6.7% |

**Pros**: statistically calibrated — each bucket has roughly equal expected probability, making market-maker pricing cleaner. Per-ticker width reflects that ticker's own surprise history, not forecaster herding.

**Cons**: less intuitive for retail (σ is abstract). Needs ≥ 8 quarters of history. Assumes past surprise distribution is informative about the next one.

### Comparison — why they differ

Per-ticker surprise volatility — the standard deviation of `(epsActual − epsEstimated) / epsEstimated` across past quarters — varies by an order of magnitude:

| Ticker | σ of surprise % (last 8 quarters) |
|---|---:|
| NVDA  | 2.35% |
| AAPL  | 3.00% |
| MSFT  | 3.87% |
| META  | 6.42% |
| AMZN  | 10.01% |
| GOOGL | 13.50% |
| TSLA  | 19.49% |

Analyst spreads, by contrast, are usually narrower than these historical σ values because analysts herd around consensus. That means Strategy A tends to produce tighter bucket boundaries than Strategy B on the same ticker, and tail buckets (1 and 5) capture more probability under Strategy A.

### Worked example — both strategies on the same ticker

From a recent probe run on TSLA (quarter ending June 30 2026, earnings release scheduled for July 22 2026):

```
Ticker: TSLA
Quarter end: 2026-06-30
Current consensus epsAvg: 0.434  (n=16 analysts, high=0.598, low=0.222)
Analyst spread (epsHigh − epsLow): 0.376  (86.7% of epsAvg)
Historical surprise σ: 17.55%  (from 19 past prints)

Strategy A — Analyst-spread buckets
  Bucket 1 [deep miss           ]  < 0.222
  Bucket 2 [mild miss           ]  0.222 – 0.328
  Bucket 3 [in-consensus        ]  0.328 – 0.516
  Bucket 4 [mild beat           ]  0.516 – 0.598
  Bucket 5 [deep beat           ]  > 0.598

Strategy B — Historical-surprise-σ buckets
  Bucket 1 [miss > 1.5σ         ]  < 0.320
  Bucket 2 [miss 0.5–1.5σ       ]  0.320 – 0.396
  Bucket 3 [in-band ±0.5σ       ]  0.396 – 0.472
  Bucket 4 [beat 0.5–1.5σ       ]  0.472 – 0.548
  Bucket 5 [beat > 1.5σ         ]  > 0.548
```

On this particular quarter, **Strategy A produces a wider bucket span than Strategy B** — 0.222 → 0.598 (full width 0.376) versus 0.320 → 0.548 (full width 0.228). That's because analysts disagree unusually strongly on TSLA's Q2 2026 EPS (86.7% of epsAvg), which is larger than TSLA's historical surprise σ (17.5%). Most tickers most of the time are the opposite: Strategy B's boundaries are wider because analysts herd more tightly than actual results move. TSLA Q2 2026 is a reminder that the two strategies can produce very different bucket widths on the same ticker in the same quarter — it's a real product choice, not a cosmetic one.

### Choosing between them

- If the product is **retail-facing with emphasis on legibility**: Strategy A ("did they land inside or outside what analysts predicted?") may be easier to explain.
- If the product is **market-maker-friendly with calibrated probabilities**: Strategy B gives roughly equal-probability buckets by construction, which simplifies pricing.
- A mixed approach (Strategy A for some tickers, Strategy B for others) is also viable; the repo exposes both as pure functions so the market creator can pick per market.

---

## What this repo demonstrates

Run `uv run python scripts/probe_fmp.py` for the FMP + Yahoo end-to-end demo, `uv run python scripts/cross_source_check.py` for the three-source cross-check, and `uv run python scripts/parse_8k_eps.py` for the 8-K Exhibit 99.1 non-GAAP extraction proof.

What's demonstrated end-to-end:

1. **FMP earnings calendar works** — upcoming Mag 7 earnings are discoverable
2. **FMP historical data works** — 8+ quarters of `(estimated, actual)` pairs available per ticker
3. **FMP analyst estimates work** — current-quarter consensus with high/low/number of analysts
4. **Both bucket strategies construct cleanly** — analyst-spread and historical-surprise-σ, side-by-side, on the same ticker
5. **Upcoming-earnings discovery matches estimates to dates and times** — FMP estimates joined with Yahoo Finance scheduled-release timestamps and BMO/AMC markers
6. **Yahoo timing pull for the Mag 7** — 100% coverage, full timezone-aware datetimes, BMO/AMC classification
7. **EDGAR as a third primitive source** — XBRL for GAAP-settling tickers, 8-K Exhibit 99.1 parsing for non-GAAP-settling tickers, both readable without an API key
8. **Cross-source verification** — three-way agreement check at $0.01 tolerance, with empirical validation that FMP follows the press-release headline (5-year window, both NVDA + TSLA at 100% extractor accuracy on current-format releases)

---

## Repo layout

```
truflation-eps-markets/
├── README.md                 # this file
├── pyproject.toml
├── .env.example
├── .gitignore
├── src/truflation_eps/
│   ├── __init__.py
│   ├── fmp_client.py             # FMP earnings-calendar + analyst-estimates + earnings
│   ├── yahoo_client.py           # Yahoo Finance — release timing + EPS secondary
│   ├── edgar_common.py           # Shared SEC EDGAR constants (User-Agent, Mag-7 CIK map)
│   ├── edgar_xbrl_client.py      # GAAP-only XBRL EPS history (AAPL, MSFT, GOOGL, AMZN, META)
│   ├── edgar_8k_client.py        # 8-K Item 2.02 / Exhibit 99.1 + non-GAAP extractors (NVDA, TSLA)
│   ├── settlement_rules.py       # Settlement Rules §3 quorum logic — pure function over 3 floats
│   ├── market_spec.py            # Strategy A (analyst-spread) + Strategy B (historical-σ) bucket construction
│   └── calendar.py               # earnings-date discovery + matched estimate + Yahoo timing
├── scripts/
│   ├── probe_fmp.py              # end-to-end FMP + Yahoo demonstration
│   ├── cross_source_check.py     # FMP × EDGAR XBRL × Yahoo three-way (GAAP tickers, offline)
│   ├── parse_8k_eps.py           # 8-K Ex-99.1 × FMP × Yahoo three-way for NVDA/TSLA — last 4 quarters
│   └── validate_extractors.py    # 8-K Ex-99.1 × FMP × Yahoo three-way for NVDA/TSLA — 5-year window
├── tests/
└── results/                      # script outputs — CSVs per run
```

Minimal by design. The three EDGAR / FMP / Yahoo source clients exist as primitive-stream adapters; cross-source verification + halt rules live in the offline scripts and are spec'd in the Settlement Rules doc. No production daemon yet — operationalization is the production implementation path.

---

## Development setup

```bash
# Install uv if not already
curl -LsSf https://astral.sh/uv/install.sh | sh

# Set up venv + deps
cd truflation-eps-markets
uv venv
uv sync

# Configure FMP key
cp .env.example .env
# Edit .env — paste your FMP_API_KEY (register at financialmodelingprep.com)

# Run the end-to-end demonstration
uv run python scripts/probe_fmp.py
```

The probe runs the FMP endpoints against the live API, pulls release timing from Yahoo Finance, demonstrates σ-calibrated bucket construction on TSLA, and writes six CSVs into `results/`:

- `probe_<timestamp>_calendar.csv` — upcoming + historical calendar rows (FMP)
- `probe_<timestamp>_historical.csv` — per-ticker history with estimates and actuals (FMP)
- `probe_<timestamp>_estimates.csv` — analyst consensus for upcoming quarters (FMP)
- `probe_<timestamp>_surprise_stats.csv` — mean and σ of surprise% per Mag 7 ticker
- `probe_<timestamp>_yahoo_timing.csv` — next scheduled event per ticker with `scheduled_at`, `bmo_amc`, `surprise_pct` (Yahoo)
- `probe_<timestamp>_upcoming.csv` — FMP estimates joined with Yahoo timing (the production-shaped row)

---

## Open questions for production

The following are intentionally left open for the production implementation to decide. Two items previously listed here — *multi-source cross-check* and *GAAP-vs-non-GAAP convention* — are now closed: see [§ Cross-source verification](#cross-source-verification--divergence-halt-rules) and the per-ticker convention table in [§ GAAP vs non-GAAP EPS](#gaap-vs-non-gaap-eps--per-ticker-settlement-convention).

1. **Daemon operationalization.** This is not a production daemon. A production deployment would wrap the source clients in a scheduled poller, with retry logic, error handling, and signed broadcast to the stream.

2. **Reference lock timing.** When to freeze the consensus `epsAvg` as the market's anchor (T−7? T−1? continuous rebalancing?) is a product-UX decision, not a data decision.

3. **Analyst count threshold.** Mag 7 all have ≥ 10 analysts, but per-ticker `numAnalystsEps` should still be sanity-checked at market-open time. Suggested floor: `numAnalystsEps >= 10`. Configurable per-launch.

4. **Surprise-distribution stability.** σ computed from 8 quarters may under-estimate tail risk (one outlier out of 8 can swing σ by 40%). A production implementation should use the longest history FMP's `historical_earnings` endpoint returns — typically 20+ quarters for any Mag 7 name.

5. **FMP reliability during peak earnings weeks.** This repo has not observed a peak week end-to-end. One quarter of observation is worthwhile before committing — though the EDGAR + Yahoo cross-checks now act as backstops if FMP degrades.

6. **Regulatory considerations.** Prediction markets on US equity earnings may raise CFTC considerations depending on settlement venue and operator jurisdiction. Legal review is a production-side task if the market is US-visible.

7. **Stream schema.** How the broadcast value is structured on the TN protocol (one stream per ticker per quarter? one catch-all with metadata? signed attestation format?) is a protocol-layer decision. Not opinionated here.

8. **Market creator's boundary choice.** This repo demonstrates two strategies (analyst-spread and historical-surprise-σ). A market creator may choose either, or a third strategy not implemented here. `market_spec.py` is a reference implementation, not a prescription.

---

## Production deployment path

If this proof-of-concept is adopted, productionization follows a three-phase path similar to other scheduled data-adapter deployments:

- **Phase 1 — Daemon setup.** Deploy a scheduled poller wrapping `fmp_client.py`. Environment setup, API key management, error recovery, observability.
- **Phase 2 — Stream adapter.** Receive `{ticker, quarter_end, epsActual, scheduled_at, bmo_amc, timestamp}` from the daemon (EPS values from FMP, scheduled timing captured from Yahoo at announcement), sign it, broadcast to the TN protocol as the stream's latest value.
- **Phase 3 — Universe expansion.** Config-driven ticker list so adding new companies is a configuration entry, not a code change.

`fmp_client.py`, `yahoo_client.py`, `edgar_xbrl_client.py`, `edgar_8k_client.py`, `edgar_common.py`, and `calendar.py` can be copied into the daemon codebase directly. `market_spec.py` is reference material for whoever creates the downstream markets.

---
