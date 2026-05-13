"""SEC EDGAR 8-K client — earnings press releases (Item 2.02 / Exhibit 99.1).

For NVDA and TSLA, the settlement rules use **non-GAAP** diluted EPS, which
the structured XBRL data does not carry. This client reads the company's
press release (Form 8-K, Item 2.02 "Results of Operations and Financial
Condition", Exhibit 99.1) directly from SEC EDGAR and extracts the non-GAAP
diluted EPS figure via per-ticker regex extractors.

The press release is the **manual reference** named in §4 of the settlement
rules. This client is also used to validate that FMP's `epsActual` follows
the press-release headline (per `scripts/parse_8k_eps.py`).

Empirical validation (5-year window, May 2021 → May 2026):
  - NVDA: 20 of 21 real earnings 8-Ks extracted correctly. 13 pre-June-2024
    quarters show split-adjustment artifact vs FMP (FMP retro-adjusts; 8-K
    is as-filed). The one NO_EXTRACT was an off-cycle preliminary-results
    announcement, not a real earnings release.
  - TSLA: 12 of 12 earnings 8-Ks extracted correctly since July 2023.
    Delivery-update 8-Ks (filed quarterly under Item 2.02 with no EPS data)
    correctly return None. Pre-July-2023 format change is not currently
    supported (out of scope — settlement is forward-only).

Run-time invariants:
  - SEC requires a polite User-Agent (set in `edgar_common.DEFAULT_UA`).
  - Rate limit: 10 requests/sec.
"""
from __future__ import annotations

import html as ihtml
import re
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

import httpx

from .edgar_common import DEFAULT_UA, cik_for

EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
EDGAR_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data"


# ─── Data classes ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Filing8K:
    symbol: str
    accession: str          # e.g. "0001045810-26-000019"
    filing_date: str        # YYYY-MM-DD
    accepted_date: str      # YYYY-MM-DDTHH:MM:SS — precise submission timestamp
    primary_document: str
    items: str              # e.g. "2.02,9.01"


# ─── HTML → text normalization ─────────────────────────────────────────────


def html_to_text(html: str) -> str:
    """Strip HTML tags + collapse whitespace while preserving paragraph breaks.

    Used to prepare press-release HTML for regex extraction.
    """
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                  flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"</(p|div|tr|h\d|td|th|li|br)[^>]*>", "\n", html,
                  flags=re.IGNORECASE)
    html = re.sub(r"<br[^>]*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ")
                .replace("&amp;", "&")
                .replace("&#160;", " ")
                .replace("&#8217;", "'")
                .replace("&#8212;", "—")
                .replace("&#8220;", '"')
                .replace("&#8221;", '"'))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ─── Per-ticker non-GAAP EPS extractors ────────────────────────────────────


# NVDA changed press-release format starting Q1 FY26 (announced 2025-05-28).
#
# FY26+ format combines GAAP and non-GAAP in one sentence:
#   "For the quarter, GAAP and non-GAAP earnings per diluted share were
#    $1.76 and $1.62, respectively."
# or, when both values are equal:
#   "...earnings per diluted share were both $1.30."
#
# FY24-FY25 format separates them into two sentences with singular "was":
#   "GAAP earnings per diluted share was $0.89... Non-GAAP earnings per
#    diluted share was $0.89..."

NVDA_PATTERNS: list[tuple[str, str, int]] = [
    # FY26+ format, GAAP == non-GAAP collapse: "were both $X.XX"
    (r"[Ff]or the (?:fourth |third |second |first )?quarter,?\s+"
     r"GAAP\s+and\s+non[-\s]?GAAP\s+earnings\s+per\s+diluted\s+share\s+"
     r"were\s+both\s+\$\s*(\d+\.\d{2})",
     "narrative_both", 1),

    # FY26+ format, split values: take group 2 (non-GAAP)
    (r"[Ff]or the (?:fourth |third |second |first )?quarter,?\s+"
     r"GAAP\s+and\s+non[-\s]?GAAP\s+earnings\s+per\s+diluted\s+share\s+"
     r"were\s+\$\s*(\d+\.\d{2})\s+and\s+\$\s*(\d+\.\d{2})",
     "narrative_split", 2),

    # FY24-FY25 format: anchored on "For the quarter" so the fiscal-year
    # value appearing later in the same release doesn't get picked up.
    (r"[Ff]or the (?:fourth |third |second |first )?quarter,?"
     r"[\s\S]{0,400}?"
     r"[Nn]on[-\s]?GAAP\s+earnings\s+per\s+diluted\s+share\s+"
     r"(?:was|were|of)\s+\$\s*(\d+\.\d{2})",
     "for_quarter_anchored_was", 1),

    # Last-resort fallback (unanchored). Only triggers if "For the quarter"
    # context is missing entirely — unlikely on a real earnings release.
    (r"[Nn]on[-\s]?GAAP\s+earnings\s+per\s+diluted\s+share\s+"
     r"(?:was|were|of)\s+\$\s*(\d+\.\d{2})",
     "standalone_non_gaap", 1),
]


def extract_nvda_non_gaap(text: str) -> tuple[Optional[float], str]:
    """Extract NVDA's non-GAAP diluted EPS from press-release plain text.

    Returns (value, method_label). value is None and label is 'no_match'
    if no pattern matched (e.g. off-cycle pre-announcement 8-K).
    """
    for pattern, label, group_idx in NVDA_PATTERNS:
        m = re.search(pattern, text)
        if m:
            try:
                return float(m.group(group_idx)), label
            except (ValueError, IndexError):
                continue
    return None, "no_match"


# TSLA (current format, July 2023 → present):
# Non-GAAP EPS appears in a quarterly comparison table:
#   "EPS attributable to common stockholders, diluted (non-GAAP) 0.27 0.40
#    0.50 0.50 0.41 52%"
# Five trailing quarters (oldest → newest), then YoY %. The last decimal
# before the % is the current quarter. Losses appear in parentheses.

def extract_tsla_non_gaap(text: str) -> tuple[Optional[float], str]:
    """Extract TSLA's non-GAAP (adjusted) diluted EPS from press-release text.

    Format support: July 2023 → present. Pre-July-2023 press releases use
    different phrasing and return None — out of scope for forward-only
    settlement.
    """
    m = re.search(
        r"EPS\s+attributable\s+to\s+common\s+stockholders,?\s+"
        r"diluted\s+\(non[-\s]?GAAP\)",
        text,
    )
    if not m:
        return None, "no_match"

    segment = text[m.end(): m.end() + 200]
    num_pattern = re.compile(r"(\(?-?\$?\(?(\d+\.\d{2})\)?)")
    candidates: list[tuple[float, bool]] = []
    for nm in num_pattern.finditer(segment):
        full = nm.group(1)
        value_str = nm.group(2)
        next_char = segment[nm.end():nm.end() + 1]
        is_pct = next_char == "%"
        is_neg = full.startswith("(") and full.endswith(")")
        v = float(value_str)
        if is_neg:
            v = -v
        candidates.append((v, is_pct))

    non_pct = [c for c in candidates if not c[1]]
    if not non_pct:
        return None, "no_decimal_values"
    return non_pct[-1][0], "tsla_table_last_decimal_before_yoy"


# Per-ticker extractor registry — non-GAAP-settling tickers only.
NON_GAAP_EXTRACTORS: dict[str, Callable[[str], tuple[Optional[float], str]]] = {
    "NVDA": extract_nvda_non_gaap,
    "TSLA": extract_tsla_non_gaap,
}


# ─── Client ────────────────────────────────────────────────────────────────


class Edgar8KClient:
    """Reads Form 8-K Item 2.02 earnings releases and their Exhibit 99.1.

    Use for non-GAAP-settling tickers (NVDA, TSLA). For GAAP-settling
    tickers, use `EdgarXbrlClient` instead — XBRL structured data is the
    cleaner source for GAAP figures.
    """

    def __init__(self, user_agent: str = DEFAULT_UA, timeout: float = 30.0):
        if "@" not in user_agent:
            raise RuntimeError(
                "EDGAR requires a User-Agent with contact info."
            )
        self.user_agent = user_agent
        self.timeout = timeout
        self._headers = {"User-Agent": user_agent}
        self._json_headers = {"User-Agent": user_agent, "Accept": "application/json"}

    # ── 8-K discovery ────────────────────────────────────────────────────

    def list_earnings_8ks(
        self,
        symbol: str,
        since: Optional[date] = None,
        want_n: Optional[int] = None,
    ) -> list[Filing8K]:
        """List 8-K filings for `symbol` that carry Item 2.02.

        - `since`: only filings on/after this date (default: no lower bound)
        - `want_n`: cap the result count (default: return all matching)

        Item 2.02 filters out non-earnings 8-Ks (board changes, M&A, etc.).
        TSLA also files Item-2.02 8-Ks for quarterly vehicle delivery
        numbers — those won't contain non-GAAP EPS and the extractor will
        return None for them.
        """
        cik = cik_for(symbol)
        url = EDGAR_SUBMISSIONS_URL.format(cik=cik)
        with httpx.Client(timeout=20, headers=self._json_headers) as c:
            r = c.get(url)
            r.raise_for_status()
            data = r.json()

        recent = data.get("filings", {}).get("recent", {}) or {}
        out: list[Filing8K] = []
        for form, acc, fdate, adate, pdoc, items in zip(
            recent.get("form", []),
            recent.get("accessionNumber", []),
            recent.get("filingDate", []),
            recent.get("acceptanceDateTime", []),
            recent.get("primaryDocument", []),
            recent.get("items", []),
        ):
            if form != "8-K":
                continue
            if "2.02" not in (items or ""):
                continue
            if since is not None:
                try:
                    if date.fromisoformat(fdate) < since:
                        continue
                except ValueError:
                    continue
            out.append(Filing8K(
                symbol=symbol,
                accession=acc,
                filing_date=fdate,
                accepted_date=adate,
                primary_document=pdoc,
                items=items,
            ))
            if want_n is not None and len(out) >= want_n:
                break
        return out

    # ── Exhibit 99.1 fetch ───────────────────────────────────────────────

    def find_exhibit_991_filename(
        self, symbol: str, filing: Filing8K
    ) -> Optional[str]:
        """Look up Exhibit 99.1's actual filename from the SEC headers file.

        `{accession}-index-headers.html` contains the structured SGML header
        listing each document with its `<TYPE>` (e.g. `EX-99.1`) and its
        `<FILENAME>`. Bulletproof across filename variants: `q4fy26pr.htm`,
        `exhibit991.htm`, `tsla-ex99_1.htm`, `exhbit991.htm` [TSLA typo'd
        one], etc.

        Returns None if no EX-99.1 is declared (e.g. some delivery 8-Ks).
        """
        cik_unpadded = str(int(cik_for(symbol)))
        acc_no_dashes = filing.accession.replace("-", "")
        url = (f"{EDGAR_ARCHIVES_URL}/{cik_unpadded}/{acc_no_dashes}/"
               f"{filing.accession}-index-headers.html")

        with httpx.Client(timeout=20, headers=self._headers) as c:
            r = c.get(url)
            r.raise_for_status()
            body = ihtml.unescape(r.text)

        m = re.search(
            r"<TYPE>EX-?99\.?0?1\b.*?<FILENAME>([^\s<]+)",
            body, re.DOTALL,
        )
        return m.group(1).strip() if m else None

    def fetch_exhibit_991_html(
        self, symbol: str, filing: Filing8K, filename: str,
    ) -> str:
        """Pull the raw HTML of an exhibit by filename within an 8-K."""
        cik_unpadded = str(int(cik_for(symbol)))
        acc_no_dashes = filing.accession.replace("-", "")
        url = f"{EDGAR_ARCHIVES_URL}/{cik_unpadded}/{acc_no_dashes}/{filename}"
        with httpx.Client(timeout=self.timeout, headers=self._headers) as c:
            r = c.get(url)
            r.raise_for_status()
            return r.text

    # ── End-to-end ───────────────────────────────────────────────────────

    def non_gaap_eps_for_filing(
        self, symbol: str, filing: Filing8K,
    ) -> tuple[Optional[float], str, Optional[str]]:
        """Convenience: find EX-99.1, fetch HTML, run per-ticker extractor.

        Returns (value, method_label, exhibit_filename). Any of those can be
        None: missing EX-99.1, network error, or extractor mismatch.
        """
        extractor = NON_GAAP_EXTRACTORS.get(symbol.upper())
        if extractor is None:
            raise ValueError(
                f"No non-GAAP extractor registered for {symbol!r}. "
                f"Supported: {list(NON_GAAP_EXTRACTORS)}"
            )

        exhibit = self.find_exhibit_991_filename(symbol, filing)
        if exhibit is None:
            return None, "no_ex991_declared", None

        html = self.fetch_exhibit_991_html(symbol, filing, exhibit)
        text = html_to_text(html)
        val, method = extractor(text)
        return val, method, exhibit
