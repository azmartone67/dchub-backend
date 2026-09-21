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

# ★ 2026-09-07 — THIS TUPLE WAS THE BUG, not the sitemap.
# It read ("claude", "chatgpt", "cursor", "grok", "perplexity") under the
# comment above claiming it was "every client that has a page in
# dchub-frontend/install/". It was five of TWELVE. The list and the sentence
# asserting its completeness were written in the same commit and went stale
# together, so the guard could not notice: it verified the five it already
# knew and reported green while seven live, indexable pages sat in no shard —
# including gemini-cli, the only Gemini install surface.
#
# All twelve verified 200 on the apex, 2026-09-07.
CLIENTS = ("claude", "chatgpt", "cursor", "grok", "perplexity",
           "gemini-cli", "claude-code", "claude-desktop", "cline",
           "vscode", "windsurf", "antigravity")

# A floor, because a hardcoded roster's failure mode is SHRINKING silently:
# drop an entry from CLIENTS and every assertion below still passes.
_CLIENTS_FLOOR = 12

# Where dchub-frontend is checked out, if it is. CI checks it out with
# continue-on-error (private repo), so absence is normal and must be reported
# as NOT RUN rather than passing quietly.
_FRONTEND_ROOTS = (
    os.environ.get("DCHUB_FRONTEND_ROOT") or "",
    os.path.join(os.path.dirname(ROOT), "dchub-frontend"),
    os.path.join(ROOT, "dchub-frontend"),
)


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


def test_the_client_roster_cannot_shrink_silently():
    """★ The 2026-08-25 version could not fail this way: every assertion here
    iterates CLIENTS, so deleting an entry deletes its own check. The floor is
    the only thing that notices a roster getting smaller."""
    assert len(CLIENTS) >= _CLIENTS_FLOOR, (
        f"CLIENTS has {len(CLIENTS)} entries, below the pinned floor of "
        f"{_CLIENTS_FLOOR}. If an install page was genuinely retired, lower "
        f"the floor in the same commit and say which page and why.")
    assert len(set(CLIENTS)) == len(CLIENTS), f"duplicate client in {CLIENTS}"


def _frontend_install_dir():
    for root in _FRONTEND_ROOTS:
        if not root:
            continue
        d = os.path.join(root, "install")
        if os.path.isdir(d):
            return d
    return None


def test_clients_matches_the_frontend_directory_when_it_is_available():
    """The real canon is dchub-frontend/install/*.html. When that checkout is
    present, CLIENTS must equal it exactly — this is the check the hardcoded
    tuple was pretending to be.

    ★ SKIPS LOUDLY when the frontend is absent. `could not run` is not `ran and
    passed`, and this suite's whole failure mode was a check that looked green
    while measuring nothing."""
    d = _frontend_install_dir()
    if d is None:
        import pytest
        pytest.skip(
            "dchub-frontend not checked out (private repo; CI uses "
            "continue-on-error) — CLIENTS could not be cross-checked against "
            "the real directory. The floor test still applies.")
    on_disk = {f[:-5] for f in os.listdir(d) if f.endswith(".html")}
    missing = on_disk - set(CLIENTS)
    extra = set(CLIENTS) - on_disk
    assert not missing, (
        f"install pages exist in the frontend but are absent from CLIENTS, so "
        f"nothing checks they are in a sitemap shard: {sorted(missing)}")
    assert not extra, (
        f"CLIENTS names pages that no longer exist in the frontend; a sitemap "
        f"URL that 404s is worse than an unlisted one: {sorted(extra)}")


# ── the reserved probe namespace (2026-09-20) ─────────────────────────────────
# /api/v1/ops/install-stats now excludes `install-verify-%` from every install
# figure, because the ONE key in that population was our own durability probe
# and the endpoint published it as somebody's install. That exclusion is only
# safe while no page slug can land inside the reserved namespace: ship
# /install/verify-foo and its real mints would be filed as ours and disappear
# from the ledger. Both the roster and the frontend directory are checked, so
# this holds whether or not the hardcoded tuple is current.
_RESERVED_SLUG_PREFIX = "verify-"


def test_no_install_page_slug_enters_the_reserved_probe_namespace():
    bad = [c for c in CLIENTS if c.startswith(_RESERVED_SLUG_PREFIX)]
    assert not bad, (
        f"/install/{bad} would mint install-{_RESERVED_SLUG_PREFIX}… , which "
        f"install_stats.py reserves for our own probes and subtracts from the "
        f"install totals — a real install channel would be counted as ours")


def test_the_frontend_directory_also_stays_out_of_the_reserved_namespace():
    """Same rule, measured against the real canon rather than the tuple."""
    d = _frontend_install_dir()
    if d is None:
        import pytest
        pytest.skip(
            "dchub-frontend not checked out — the reserved-namespace rule was "
            "checked against CLIENTS only, not against the real directory.")
    bad = sorted(f[:-5] for f in os.listdir(d)
                 if f.endswith(".html") and f.startswith(_RESERVED_SLUG_PREFIX))
    assert not bad, (
        f"install pages exist whose slug is inside the reserved probe namespace "
        f"install-{_RESERVED_SLUG_PREFIX}…: {bad}. Either rename them or drop "
        f"the exclusion in routes/install_stats.py — as it stands their mints "
        f"are subtracted from the published install figures.")


def test_the_funnel_surface_names_the_same_pages_as_this_roster():
    """flask_mcp_endpoints.install_artifact_30d publishes a `pages` list beside
    its install ladder. It read five of the twelve for 13 days — the SAME stale
    roster bug this file was rewritten for on 2026-09-07, in a second place.
    Pin the literal to CLIENTS so the two cannot drift apart again."""
    import ast
    src_path = os.path.join(ROOT, "flask_mcp_endpoints.py")
    with open(src_path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    published = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        if "ladder" not in keys or "pages" not in keys:
            continue
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value == "pages":
                published = [e.value for e in v.elts if isinstance(e, ast.Constant)]
    assert published is not None, (
        "the install ladder's `pages` literal is gone or no longer a list of "
        "string constants — this guard now checks nothing")
    assert set(published) == set(CLIENTS), (
        "install_artifact_30d.pages disagrees with the install page roster: "
        f"only there {sorted(set(published) - set(CLIENTS))}, "
        f"missing {sorted(set(CLIENTS) - set(published))}")
