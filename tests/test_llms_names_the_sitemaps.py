#!/usr/bin/env python3
"""tests/test_llms_names_the_sitemaps.py — llms.txt must name the URL inventory,
and the AI sitemap must never reach robots.txt.

NO NETWORK, NO DB.

BEFORE THIS CHANGE llms.txt named no sitemap at all — 16KB of instructions for
AI crawlers with no pointer to the page inventory. A crawler could only learn
our URLs by following links, and measured 2026-09-05 that is exactly what they
were doing: GPTBot reached 12,001 distinct /facilities/ URLs against the 6,266
the sitemap publishes.

THE ASYMMETRY THIS FILE PROTECTS. There are two sitemaps on purpose:

  /sitemap.xml      the RANKING set. Submitted to GSC and Bing Webmaster, and
                    capacity-gated, because a search engine crawls a thin page
                    and declines it while spending a limited budget.
  /sitemap-ai.xml   the RETRIEVAL set. Ungated, for crawlers building an entity
                    index, which are not budget-constrained on us.

robots.txt's `Sitemap:` directive is read by EVERY crawler, Googlebot and
Bingbot included. Advertising the ungated family there hands them the ~14,200
URLs the gate exists to withhold — and it would look like a harmless one-line
improvement. That is the mistake this file is here to catch.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "ai_discovery_routes.py")


def _src():
    return open(SRC, encoding="utf-8").read()


def _llms_body():
    """The llms.txt template only, comments stripped."""
    s = _src()
    i = s.index("def serve_llms_txt(")
    j = s.index("\n    @app.route(", i + 10)
    return "\n".join(l for l in s[i:j].splitlines()
                     if not l.lstrip().startswith("#"))


def test_llms_txt_names_both_sitemaps():
    body = _llms_body()
    for url in ("https://dchub.cloud/sitemap.xml",
                "https://dchub.cloud/sitemap-ai.xml"):
        assert url in body, (
            "llms.txt does not name %s — an AI crawler reading this file has no "
            "way to learn the URL inventory except by following links" % url)


def test_llms_txt_says_which_sitemap_a_retrieval_crawler_wants():
    """Naming both without distinguishing them is worse than naming one: the
    reader picks the first, which is the gated set."""
    body = _llms_body()
    seg = body[body.index("sitemap-ai.xml") - 900:body.index("sitemap-ai.xml") + 900]
    assert re.search(r"entity index|grounding an answer", seg), (
        "the AI sitemap is listed but not explained — say what it is FOR")
    assert "superset" in seg, (
        "the relationship between the two must be stated, or a crawler cannot "
        "tell whether fetching both double-counts")


def test_the_ai_sitemap_is_absent_from_the_robots_sitemap_directive():
    """★ THE load-bearing assertion. robots.txt `Sitemap:` is read by Googlebot
    and Bingbot, so listing the ungated family there hands them the URLs the
    capacity gate exists to withhold — as a one-line 'improvement'."""
    s = _src()
    directives = re.findall(r"^Sitemap:\s*(\S+)", s, re.M)
    assert directives, "no Sitemap: directives found — the parse is wrong"
    for url in directives:
        assert "sitemap-ai" not in url, (
            "robots.txt advertises %s. That directive is read by every crawler, "
            "so the ungated family would reach Google and Bing and the capacity "
            "gate becomes void." % url)


def test_robots_still_advertises_the_ranking_sitemap():
    """The guard above must not pass by robots.txt advertising nothing."""
    s = _src()
    directives = re.findall(r"^Sitemap:\s*(\S+)", s, re.M)
    # ★ EXACT match, not endswith(): `.../answers/sitemap.xml` also ends with
    # "/sitemap.xml", so a loose check stayed GREEN when the canonical
    # directive was deleted — caught by mutation M4, not by reading it.
    assert "https://dchub.cloud/sitemap.xml" in directives, (
        "robots.txt no longer names the canonical sitemap (found %r) — the "
        "guard above would pass on a robots.txt that advertises no sitemap "
        "index at all" % (directives,))


# ── the widened announcement (2026-09-05) ───────────────────────────────────
# MEASURED, and the reason this file grew: llms.txt is read by Perplexity about
# ONCE EVERY SIX WEEKS. Fetches by external AI platforms over the 60d to
# 2026-09-05, from ai_requests:
#
#     surface           all   perplexity   chatgpt
#     /llms.txt          17            2         9
#     /AGENTS.md         23            2        10
#     /.well-known/      31            5        14
#
# Perplexity last read llms.txt on 2026-09-02 — three days BEFORE it started
# naming /sitemap-ai.xml. For contrast it fetched 7,968 facility pages in the
# same window: it crawls pages ~4,000x more often than it reads our
# instructions. One announcement surface is a six-week channel; three is still
# slow but roughly triples the chance of an early catch.
#
# The fast channel is robots.txt `Sitemap:`, and it stays off-limits — see
# test_the_ai_sitemap_is_absent_from_the_robots_sitemap_directive above.

_MAIN = os.path.join(ROOT, "main.py")
_AGENTS = os.path.join(ROOT, "routes", "agents_md_fallback.py")


def test_agents_md_names_the_ai_sitemap():
    src = open(_AGENTS, encoding="utf-8").read()
    assert "https://dchub.cloud/sitemap-ai.xml" in src, (
        "/AGENTS.md does not name the AI sitemap — it is read as often as "
        "llms.txt (23 vs 17 fetches/60d) and is half the widened channel")
    assert "https://dchub.cloud/sitemap.xml" in src, (
        "/AGENTS.md dropped the ranking sitemap")


def test_agents_md_distinguishes_the_two_sitemaps():
    """Two rows in a table with no explanation makes a reader take the first,
    which is the gated one."""
    src = open(_AGENTS, encoding="utf-8").read()
    seg = src[src.index("sitemap-ai.xml"):src.index("sitemap-ai.xml") + 1400]
    assert "superset" in seg, "the relationship between the two must be stated"
    assert "entity index" in seg or "grounding an answer" in seg, (
        "say what the retrieval set is FOR, or the row is noise")


def test_ai_agents_json_names_the_ai_sitemap_in_machine_indexes():
    src = open(_MAIN, encoding="utf-8").read()
    i = src.index('"machine_indexes"')
    block = src[i:i + 600]
    assert "sitemap-ai.xml" in block, (
        "/.well-known/ai-agents.json machine_indexes omits the AI sitemap — it "
        "is the one .well-known file with measurable Perplexity reads (7/60d)")
    assert '"sitemap"' in block, "machine_indexes dropped the ranking sitemap"


def test_every_announcement_surface_agrees_on_the_url():
    """Three surfaces now carry this URL. A typo in one is a dead pointer that
    nothing else catches, because each file is tested by a different suite."""
    URL = "https://dchub.cloud/sitemap-ai.xml"
    for path, label in ((SRC, "llms.txt"), (_AGENTS, "AGENTS.md"),
                        (_MAIN, "ai-agents.json")):
        src = open(path, encoding="utf-8").read()
        assert URL in src, f"{label} does not carry the exact URL {URL}"
        # a near-miss is worse than an absence: it 404s instead of 503-ing
        for typo in ("sitemap_ai.xml", "sitemap-ai.xhtml", "sitemaps-ai.xml",
                     "/sitemap-ai/", "sitemap-ai.xml.xml"):
            assert typo not in src, f"{label} carries a malformed variant: {typo}"
