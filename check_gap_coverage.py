#!/usr/bin/env python3
"""
check_gap_coverage.py — is the competitor-gap source UNREACHED or ALREADY KNOWN?

★THE QUESTION. The competitor-gap crawler is the inventory growth engine: it
added 960 of the ~1,490 new distinct buildings in the last 60 days (64%), vs
openstreetmap 334, epa_echo_air 131, news_ner 49, peeringdb 18.

Cloudscene's sitemap carries 11,859 data-center URLs and we hold 962 distinct
(8.1%). Until `competitor_gap_sweeps` existed, NOTHING in the database could say
whether the other 92% was never reached or already known — a candidate matching
an existing facility is dropped in diff_gaps (`dropped_existing`) and left no
trace, because `coverage_gaps` only ever stores gaps.

That one unknown decides the whole inventory roadmap:

  COVERAGE LOW  -> the sweep has not visited most of the sitemap. Raising
                   COMPETITOR_GAP_PAGE_LIMIT / run frequency is free and worth
                   thousands of facilities, with zero new integration risk.
  COVERAGE HIGH + dropped_existing dominant
                -> the source is genuinely tapped out. The next lever is a NEW
                   source, which is a project (see the Baxtel notes: 12,804
                   permitted URLs, but a naive page parser returned the SAME
                   address for two different facilities, so it needs a
                   DOM-targeted extractor verified against known-correct
                   samples before it can be trusted).

★Coverage is reconstructed from windows, not stored URLs. The sweep walks a
CONTIGUOUS slice (offset = day_of_year * limit, wrapping mod sitemap length,
step == window width), so the union of [window_offset, window_offset+window_size)
intervals is exactly what has been visited.

★window_size is the URL slice width, NOT `parsed` — many URLs yield no candidate
(wrong path shape, unparseable slug), so parsed < window_size. Conflating them
would understate coverage and make a fully swept sitemap look permanently
partial.

Usage:  DATABASE_URL=... python3 check_gap_coverage.py
"""
from __future__ import annotations

import os
import sys


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union of half-open [start, end) intervals."""
    out: list[tuple[int, int]] = []
    for s, e in sorted(intervals):
        if e <= s:
            continue
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def main() -> int:
    dsn = os.environ.get("DATABASE_URL") or ""
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    import psycopg2
    c = psycopg2.connect(dsn, sslmode="require", connect_timeout=20)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('competitor_gap_sweeps')")
            if not cur.fetchone()[0]:
                print("competitor_gap_sweeps does not exist yet — run the "
                      "2026-07-29 migration. UNMEASURED, not zero.",
                      file=sys.stderr)
                return 3
            cur.execute(
                """SELECT slug, COUNT(*), MAX(locs_total),
                          SUM(COALESCE(dropped_existing,0)),
                          SUM(COALESCE(true_gaps,0)),
                          SUM(COALESCE(inserted,0)),
                          MIN(run_at)::date, MAX(run_at)::date
                     FROM competitor_gap_sweeps
                    GROUP BY slug ORDER BY 2 DESC""")
            rows = cur.fetchall()
            if not rows:
                print("No sweep rows recorded yet. The recorder ships with the "
                      "crawler — wait for the next daily run.")
                return 0
            for (slug, runs, locs, dexist, gaps, ins, first, last) in rows:
                cur.execute(
                    """SELECT window_offset, window_size
                         FROM competitor_gap_sweeps
                        WHERE slug = %s AND COALESCE(window_size,0) > 0""",
                    (slug,))
                iv = [(int(o or 0), int(o or 0) + int(w or 0))
                      for o, w in cur.fetchall()]
                merged = _merge(iv)
                swept = sum(e - s for s, e in merged)
                total = int(locs or 0)
                pct = (100.0 * min(swept, total) / total) if total else 0.0
                print(f"\n=== {slug}   ({runs} run(s), {first} -> {last})")
                print(f"  sitemap URLs        : {total:,}")
                print(f"  window coverage     : {min(swept, total):,} "
                      f"({pct:.1f}%)")
                print(f"  already had (drops) : {int(dexist or 0):,}")
                print(f"  true gaps found     : {int(gaps or 0):,}")
                print(f"  inserted            : {int(ins or 0):,}")
                known = int(dexist or 0) + int(gaps or 0)
                if known:
                    print(f"  of what we evaluated, "
                          f"{100.0*int(dexist or 0)/known:.0f}% we ALREADY had")
                if pct < 60:
                    print("  VERDICT: UNREACHED — most of the sitemap has never "
                          "been visited. Raising COMPETITOR_GAP_PAGE_LIMIT or "
                          "run frequency is the cheap win. No new source needed.")
                elif int(dexist or 0) > 4 * max(int(gaps or 0), 1):
                    print("  VERDICT: TAPPED OUT — swept broadly and almost "
                          "everything was already known. Further crawling of "
                          "this source will not move inventory; the next lever "
                          "is a NEW source.")
                else:
                    print("  VERDICT: STILL PRODUCTIVE — good coverage and gaps "
                          "are still being found. Keep it running.")
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
