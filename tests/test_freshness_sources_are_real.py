"""Guard: a freshness domain must watch a table the product actually uses.

routes/freshness_public._DOMAIN_SOURCE maps each domain to the table whose
newest row IS that domain's freshness. Point it at the wrong table and the
domain sits in permanent breach describing something nobody feeds — the
surveillance sweep goes red every 15 minutes and the alarm gets trained away.

THIS HAS NOW HAPPENED THREE TIMES, each fixed only after it was noticed by
hand:

  · dcpi -> "dcpi_scores"  — NO SUCH TABLE. The real-age query failed silently,
    fell back to a drifting surface stamp, permanent false breach.
    Fixed to market_power_scores.
  · news -> "news_items"   — a phantom/variant of news_articles. Same shape.
  · mna  -> "ai_deals"     — a REAL table, but abandoned. Measured 2026-08-10:
        deals     4,893 rows · newest 2026-08-10 19:40 · +98 in 7d
        ai_deals    862 rows · newest 2026-07-26        ·  +0 in 7d
    The scraper writes `deals`; every public M&A surface reads `deals`; the
    freshness SLA watched `ai_deals`. M&A data was arriving normally the whole
    time the domain reported breach.

The first two were caught by the table not existing. The third could not be —
`ai_deals` exists, queries fine, and returns a real timestamp.

★ HONEST LIMIT OF THE GENERIC CHECK. `test_freshness_source_is_a_table_the_
product_reads` does NOT catch the ai_deals case, and mutation testing proved
it: pointing `mna` back at `ai_deals` leaves that test GREEN, because
routes/admin_ai_deals.py reads the table. "Something reads it" is a floor, not
proof the domain is watching what users are served. The generic test therefore
only catches the dcpi/news shape — a source nothing reads at all — and the
`mna` case is held by an explicit pin below. Do not read a green generic test
as "every domain watches the right table".

No DB and no network — pure source analysis. Nothing runs at module scope.

Run locally:
    python3 -m pytest tests/test_freshness_sources_are_real.py -v
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FRESHNESS = ROOT / "routes" / "freshness_public.py"

# Files that read a table for MEASUREMENT rather than to serve it. A source
# table appearing only here is not evidence the product uses it.
_MONITOR_ONLY = re.compile(
    r"(^tests/|freshness_|surveillance_|_master_shell|brain_|"
    r"health|watchdog|deadman|audit|qa_|smoke)")


def _real_sources() -> dict[str, str]:
    """domain -> table, parsed from the shipped _DOMAIN_SOURCE literal."""
    src = FRESHNESS.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"_DOMAIN_SOURCE\s*(?::[^=]+)?=\s*\{(.*?)\n\}", src, re.S)
    assert m, "_DOMAIN_SOURCE literal not found — parser needs updating"
    body = "\n".join(ln for ln in m.group(1).splitlines()
                     if not ln.lstrip().startswith("#"))
    return {d: t for d, t in re.findall(
        r'"([a-z_]+)"\s*:\s*\(\s*"([a-z_0-9]+)"', body)}


def _product_readers(table: str) -> list[str]:
    """Non-monitoring source files that SELECT from this table."""
    pat = re.compile(rf"\bFROM\s+{re.escape(table)}\b", re.I)
    hits = []
    for p in list(ROOT.glob("*.py")) + list((ROOT / "routes").glob("*.py")):
        rel = str(p.relative_to(ROOT))
        if _MONITOR_ONLY.search(rel):
            continue
        try:
            if pat.search(p.read_text(encoding="utf-8", errors="replace")):
                hits.append(rel)
        except Exception:  # noqa: BLE001
            continue
    return hits


def test_real_sources_parses():
    """Vacuity guard: if the parser stops matching, every test below passes
    trivially and this file silently stops guarding."""
    srcs = _real_sources()
    assert len(srcs) >= 5, f"parsed only {srcs} — the _DOMAIN_SOURCE parser broke"
    assert "mna" in srcs and "news" in srcs


@pytest.mark.parametrize("domain", sorted(_real_sources()))
def test_freshness_source_is_a_table_the_product_reads(domain):
    """The mna -> ai_deals case: a real, queryable table that nothing serves.

    A freshness domain exists to answer 'is the data our users see fresh'. If
    no product code reads the table, the answer is about something users never
    see, and a breach there is noise by construction.
    """
    table = _real_sources()[domain]
    readers = _product_readers(table)
    assert readers, (
        f"freshness domain {domain!r} watches {table!r}, but no non-monitoring "
        f"source file SELECTs from it — so its status describes a table the "
        f"product does not serve. This is the mna->ai_deals defect: the table "
        f"existed and queried fine while the live pipeline wrote `deals`.")


def test_mna_watches_the_table_the_scraper_writes():
    """Pinned explicitly — this one cost a permanently red surveillance sweep.

    deal_scraper.run_scrape reports db_stats.total_deals against `deals`
    (4,893 live), and routes/transactions_browser.py + routes/hyperscaler_brief.py
    both serve `deals`. `ai_deals` is written by a scheduler path that has not
    landed a row since 2026-07-26.
    """
    assert _real_sources()["mna"] == "deals", (
        "mna freshness must track `deals` — the table the scraper writes and "
        "every public M&A surface reads")
