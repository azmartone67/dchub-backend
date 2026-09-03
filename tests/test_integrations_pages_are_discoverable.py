#!/usr/bin/env python3
"""Every /integrations/* page must be reachable by a crawler and by an agent.

NO NETWORK, NO DB — source-shape test, the house pattern for main.py's sitemap
builders (see tests/test_sitemap_lists_install_pages.py, which this mirrors).

WHY THIS GUARD EXISTS (2026-09-03)
==================================
Eleven /integrations/* routes were live and returning 200. Exactly ONE of them
(/integrations/cloudflare) was listed in any sitemap shard, and the whole family
was absent from llms.txt — so the pages existed, but nothing pointed a crawler
or an agent at them. #3609 listed cloudflare and said so explicitly: "the six
pre-existing siblings stay unlisted rather than being swept in on an unrelated
PR." This is that PR.

★ THE LIST IS DERIVED, NOT TRANSCRIBED. The route table in
routes/integrations_landing.py is the source of truth. A hand-copied tuple of
slugs would pass forever after someone adds /integrations/<newhost> and forgets
the sitemap — which is precisely how this family got to 1-of-11. Deriving it
means the NEXT unlisted page fails this test on the PR that adds it.

★ WHAT THIS DOES NOT CLAIM. Listing a URL does not make Google index it — see
tests/test_sitemap_thin_gate.py, where a 2026-07-01 widening failed because
absence was not the cause. These pages have never been crawled at all, so
discoverability is the only claim here.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "main.py")
ROUTES = os.path.join(ROOT, "routes", "integrations_landing.py")
DISCOVERY = os.path.join(ROOT, "ai_discovery_routes.py")

# /integrations and /integrations/mcp decorate the SAME handler and serve a
# byte-identical body; /integrations carries <link rel=canonical> pointing at
# /integrations/mcp. A sitemap must list the canonical target, not both.
NON_CANONICAL = {"/integrations": "/integrations/mcp"}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _live_routes():
    """Every /integrations* path in the blueprint's route table."""
    found = re.findall(r"@integrations_landing_bp\.route\(\s*[\"']([^\"']+)[\"']",
                       _read(ROUTES))
    paths = sorted({p for p in found if p.startswith("/integrations")})
    assert len(paths) >= 10, f"route table looks wrong, found only {paths}"
    return paths


def _sitemap_section():
    """main.py's static-page tuple list, COMMENTS STRIPPED so a path named only
    in a comment cannot satisfy an assertion (the rule the /install guard set)."""
    s = _read(MAIN)
    i = s.index("def _build_sitemap_sections(")
    return re.sub(r"(?m)^\s*#.*$", "", s[i:i + 200000])


def _llms_txt_body():
    """Just serve_llms_txt's body — NOT llms-full.txt, which is a different
    document with a different audience."""
    s = _read(DISCOVERY)
    i = s.index("def serve_llms_txt(")
    j = s.index("@app.route('/llms-full.txt')", i)
    return s[i:j]


def test_every_integration_page_is_in_a_sitemap_shard():
    body = _sitemap_section()
    for path in _live_routes():
        if path in NON_CANONICAL:
            continue
        assert f"'{path}'" in body, (
            f"{path} is a live, self-canonical, indexable page but no sitemap "
            f"shard lists it — crawlers cannot find it")


def test_non_canonical_paths_are_excluded_but_their_target_is_listed():
    """Skipping /integrations is only correct while its canonical IS listed."""
    body = _sitemap_section()
    for path, target in NON_CANONICAL.items():
        assert f"'{path}'," not in body, (
            f"{path} canonicals to {target}; listing it asks crawlers to index "
            f"a page that disclaims itself")
        assert f"'{target}'" in body, (
            f"{path} is excluded because it canonicals to {target} — but "
            f"{target} is not listed either, so the family has no entry point")


def test_listed_as_tuples_with_no_trailing_slash():
    """A sitemap URL that 3xx's is filed by Google as 'Redirect error'."""
    body = _sitemap_section()
    for path in _live_routes():
        if path in NON_CANONICAL:
            continue
        assert f"'{path}/'" not in body, f"{path}/ would redirect"
        m = re.search(r"\(\s*'%s'\s*,\s*'([\d.]+)'\s*,\s*'(\w+)'\s*\)" % re.escape(path), body)
        assert m, f"{path} is not a (path, priority, changefreq) tuple"
        assert 0.0 < float(m.group(1)) <= 1.0
        assert m.group(2) in ("always", "hourly", "daily", "weekly", "monthly",
                              "yearly", "never")


def test_llms_txt_points_agents_at_the_setup_recipes():
    """The sitemap serves crawlers; llms.txt serves agents. Both were blind."""
    body = _llms_txt_body()
    for path in _live_routes():
        if path in NON_CANONICAL:
            continue
        assert f"https://dchub.cloud{path})" in body, (
            f"{path} is missing from llms.txt — an agent reading it learns the "
            f"tools exist but not how to install them on that host")
