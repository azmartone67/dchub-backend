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
