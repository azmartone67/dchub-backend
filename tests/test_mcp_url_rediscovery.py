"""r-url-rediscovery (2026-07-18) — URL-drift rediscovery unit tests.

Pure-function tests for the sitemap/candidate helpers in
routes.mcp_presence_crawler plus static wiring guards (daily slot calls
the sweep, kill switch honored, no resurrected dead URL shapes in the
seed lists).

Context: mcp.so restructured /server/<name> → /servers/<slug> and the
mcp_so row 403/404'd for 15 days — crawl records last_http and moves on,
the drift sweep only sees copy drift, and white-glove probes listing
copy, so a moved URL was invisible to every loop until a human noticed.
These tests pin the rediscovery leg that closes that gap.

Run:  python3 -m pytest tests/test_mcp_url_rediscovery.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest

from routes.mcp_presence_crawler import (
    RESEED_BROKEN_REGISTRIES,
    SEED_REGISTRIES,
    _candidate_rank_key,
    _extract_locs,
    _looks_like_sitemap,
    _rediscover_disabled,
    _slug_candidates,
    rediscover_moved_listings,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Shaped like mcp.so's real sitemap: <loc> holds only the canonical URL;
# zh/ja variants ride in xhtml:link alternates and must NOT come back.
MCPSO_URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
  <url>
    <loc>https://mcp.so/servers/dchub-mcp-server</loc>
    <xhtml:link rel="alternate" hreflang="zh"
        href="https://mcp.so/zh/servers/dchub-mcp-server"/>
    <xhtml:link rel="alternate" hreflang="ja"
        href="https://mcp.so/ja/servers/dchub-mcp-server"/>
  </url>
  <url>
    <loc> https://mcp.so/servers/other-server </loc>
  </url>
</urlset>"""

MCPSO_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://mcp.so/sitemap.xml?section=static</loc></sitemap>
  <sitemap><loc>https://mcp.so/sitemap.xml?section=servers&amp;page=1</loc></sitemap>
</sitemapindex>"""


class TestExtractLocs:
    def test_urlset_returns_canonical_locs_only(self):
        locs = _extract_locs(MCPSO_URLSET)
        assert locs == [
            "https://mcp.so/servers/dchub-mcp-server",
            "https://mcp.so/servers/other-server",
        ]

    def test_hreflang_alternates_never_leak(self):
        locs = _extract_locs(MCPSO_URLSET)
        assert not any("/zh/" in u or "/ja/" in u for u in locs)

    def test_sitemapindex_children(self):
        locs = _extract_locs(MCPSO_INDEX)
        assert len(locs) == 2
        assert all(_looks_like_sitemap(u) for u in locs)

    def test_entities_unescaped(self):
        # REGRESSION: mcp.so index children carry &amp; in <loc>. Fetching
        # the raw form mangles the query (amp;page=N → server ignores it,
        # serves page 1 for all 19 children) and the first live dry-run
        # declared our listing GONE because of it.
        locs = _extract_locs(MCPSO_INDEX)
        assert "https://mcp.so/sitemap.xml?section=servers&page=1" in locs
        assert not any("&amp;" in u for u in locs)

    def test_empty_and_none_are_safe(self):
        assert _extract_locs("") == []
        assert _extract_locs(None) == []
        assert _extract_locs("<html>not a sitemap</html>") == []


class TestLooksLikeSitemap:
    def test_xml_and_sitemap_urls_match(self):
        assert _looks_like_sitemap("https://x.com/sitemap.xml")
        assert _looks_like_sitemap("https://x.com/sitemap.xml?section=a&page=2")
        assert _looks_like_sitemap("https://x.com/static-sitemap-3.XML")

    def test_page_urls_do_not(self):
        assert not _looks_like_sitemap("https://mcp.so/servers/dchub-mcp-server")
        assert not _looks_like_sitemap("https://x.com/servers")


class TestSlugCandidates:
    def test_filters_to_our_slugs_case_insensitive(self):
        urls = [
            "https://mcp.so/servers/other-server",
            "https://mcp.so/servers/DCHub-mcp-server",
            "https://mcp.so/servers/dchub-backend",
            "https://yellowmcp.com/servers/cloud-dchub-mcp-server",
            "https://x.com/servers/dc-hub",
        ]
        cands = _slug_candidates(urls)
        assert "https://mcp.so/servers/other-server" not in cands
        assert len(cands) == 4

    def test_dedupes_preserving_order(self):
        u = "https://mcp.so/servers/dchub-mcp-server"
        assert _slug_candidates([u, u, u]) == [u]

    def test_empty(self):
        assert _slug_candidates([]) == []


class TestCandidateRanking:
    def test_canonical_slug_beats_readme_stuffed_duplicate(self):
        # REGRESSION with the real scores measured 2026-07-18: the
        # scraped /servers/dchub-backend duplicate out-scored the
        # canonical page 420 vs 298 (README is full of "dchub"), and the
        # first dry-run healed mcp_so to the WRONG listing.
        scored = [
            (298, "https://mcp.so/servers/dchub-mcp-server"),
            (420, "https://mcp.so/servers/dchub-backend"),
        ]
        scored.sort(key=lambda t: _candidate_rank_key(t[0], t[1]))
        assert scored[0][1] == "https://mcp.so/servers/dchub-mcp-server"

    def test_score_breaks_ties_within_slug_class(self):
        scored = [
            (10, "https://x.com/servers/cloud-dchub-mcp-server"),
            (99, "https://x.com/servers/dchub-mcp-server"),
        ]
        scored.sort(key=lambda t: _candidate_rank_key(t[0], t[1]))
        assert scored[0][1] == "https://x.com/servers/dchub-mcp-server"

    def test_underscore_slug_variant_counts(self):
        key = _candidate_rank_key(1, "https://x.com/dchub_mcp_server")
        assert key[0] == 0


class TestKillSwitch:
    def test_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("MCP_URL_REDISCOVERY_DISABLE", "1")
        assert _rediscover_disabled()
        out = rediscover_moved_listings(dry_run=True)
        assert out.get("disabled") is True
        assert out["checked"] == 0

    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("MCP_URL_REDISCOVERY_DISABLE", raising=False)
        assert not _rediscover_disabled()


class TestSeedListsCarryNoDeadShapes:
    """The exact URL shapes that already died must never be reintroduced
    by a bad merge: mcp.so singular /server/, yellowmcp /mcp/<slug> and
    /servers/dchub."""

    DEAD_SHAPES = (
        "mcp.so/server/",           # restructured to /servers/ 2026-07-18
        "yellowmcp.com/mcp/",       # dead by 2026-07-18
        "yellowmcp.com/servers/dchub",  # dead 2026-06-06 (exact old slug)
    )

    @pytest.mark.parametrize("entry", SEED_REGISTRIES,
                             ids=[e["registry_name"] for e in SEED_REGISTRIES])
    def test_seed(self, entry):
        for url in (entry.get("listing_url") or "",):
            for shape in self.DEAD_SHAPES:
                assert shape not in url, (
                    f"{entry['registry_name']} seed resurrects dead shape "
                    f"{shape!r}")

    @pytest.mark.parametrize(
        "entry", RESEED_BROKEN_REGISTRIES,
        ids=[e["registry_name"] for e in RESEED_BROKEN_REGISTRIES])
    def test_reseed(self, entry):
        for shape in self.DEAD_SHAPES:
            assert shape not in (entry.get("listing_url") or ""), (
                f"{entry['registry_name']} reseed resurrects dead shape "
                f"{shape!r}")

    def test_mcp_so_present_in_reseed(self):
        # The 2026-07-18 heal path: reseed must carry the canonical
        # plural URL so one endpoint call fixes the live row.
        names = {e["registry_name"]: e for e in RESEED_BROKEN_REGISTRIES}
        assert "mcp_so" in names
        assert names["mcp_so"]["listing_url"] == (
            "https://mcp.so/servers/dchub-mcp-server")


class TestSchedulerWiring:
    def test_daily_slot_calls_rediscovery(self):
        src = (REPO_ROOT / "crawler_scheduler.py").read_text()
        assert "rediscover_moved_listings" in src, (
            "daily auto-fix slot no longer sweeps dead-URL listings")

    def test_runner_is_live_not_dry(self):
        src = (REPO_ROOT / "crawler_scheduler.py").read_text()
        idx = src.find("rediscover_moved_listings")
        window = src[idx:idx + 400]
        assert "dry_run=False" in window, (
            "scheduler rediscovery sweep must run live — a dry run heals "
            "nothing and the 404 sits for another day")
