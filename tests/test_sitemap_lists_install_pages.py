#!/usr/bin/env python3
"""The /install/<client> pages must be in a sitemap shard.

NO NETWORK, NO DB — source-shape test, the house pattern for main.py's sitemap
builders (see tests/test_sitemap_thin_gate.py).

WHY THIS GUARD EXISTS (2026-08-25)
==================================
`/api/v1/ops/install-stats` reads `clients_tracked: 0` — no key has ever been
minted from an install page. The ledger is not broken: its own web-% control,
run through the identical `_ledger()` SQL, reads 122 minted. The pages are not
broken either: all five return 200, carry no robots meta (so index,follow), are
self-canonical, and correctly POST `client_name: "install-<client>"` — the exact
population the ledger counts.

They were simply unreachable: absent from EVERY sitemap shard, with exactly one
inbound link on the site (connect-mcp.html).

★ THE LIMIT OF THIS CHANGE, stated because tests/test_sitemap_thin_gate.py
earned it the hard way: listing a URL does not make Google index it, and the
2026-07-01 widening failed precisely because absence was NOT the cause there —
those facility pages were crawled and REJECTED as thin. The install pages are a
different case: they have never been crawled at all. Discoverability is the only
claim this makes. Conversion is measured separately, by install-stats.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "main.py")

# Every client that has a page in dchub-frontend/install/.
CLIENTS = ("claude", "chatgpt", "cursor", "grok", "perplexity")


def _static_section():
    """The static-page tuple list inside _build_sitemap_sections, comments
    stripped so a path named only in a comment cannot satisfy the assertions."""
    with open(SRC, encoding="utf-8") as fh:
        s = fh.read()
    i = s.index("def _build_sitemap_sections(")
    body = s[i:i + 200000]
    return re.sub(r"(?m)^\s*#.*$", "", body)


def test_every_install_page_is_listed():
    body = _static_section()
    for c in CLIENTS:
        assert f"'/install/{c}'" in body, (
            f"/install/{c} is live, indexable and mints an install-{c} key, but "
            f"no sitemap shard lists it")


def test_the_listed_paths_have_no_trailing_slash():
    """A sitemap URL that 3xx's is filed by Google as 'Redirect error' — the
    same rule that removed /assets and /for-ai from this list."""
    body = _static_section()
    for c in CLIENTS:
        assert f"'/install/{c}/'" not in body, f"/install/{c}/ redirects"


def test_they_are_listed_as_crawlable_tuples_not_bare_strings():
    """The section is a list of (path, priority, changefreq); a bare string
    would be emitted with no priority and silently skipped by the builder."""
    body = _static_section()
    for c in CLIENTS:
        m = re.search(r"\(\s*'/install/%s'\s*,\s*'([\d.]+)'\s*,\s*'(\w+)'\s*\)" % c, body)
        assert m, f"/install/{c} is not a (path, priority, changefreq) tuple"
        assert 0.0 < float(m.group(1)) <= 1.0
        assert m.group(2) in ("always", "hourly", "daily", "weekly", "monthly",
                             "yearly", "never")
