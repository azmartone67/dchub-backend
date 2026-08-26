"""Sitemap-INDEX probes, and the truncation rule that keeps them honest.

WHY THIS EXISTS
---------------
A sitemap is the strongest absence test this loop has — a complete,
server-rendered enumeration needs no control probe. But the useful ones are
INDEXES, not leaves. A one-URL fetch of an index returns only shard URLs, never
a listing, so it can NEVER contain our identity token: it would read `absent`
forever, including after we are listed.

That is a false-absence generator, the exact failure the control probe, the
403/429 rule and the empty-body rule all exist to prevent. It is also why
mcpservers.org was REMOVED as "not measurable by this loop" (its sitemap is 10
shards) rather than probed badly.

`_fetch_probe` follows an index one level. The rule that makes that safe is
`enumeration_complete`: if any shard was skipped — cap hit or failed fetch — the
verdict must be `unverified`, never `absent`. Presence is exempt, because
finding the token is positive evidence and needs no completeness.
"""
import importlib

import pytest

ra = importlib.import_module("routes.registry_acquisition")


INDEX = ("<?xml version='1.0'?><sitemapindex><sitemap><loc>https://x.test/a.xml"
         "</loc></sitemap><sitemap><loc>https://x.test/b.xml</loc></sitemap>"
         "</sitemapindex>")


def _stub(monkeypatch, table):
    """Route _fetch through a dict of {url: (status, body)}."""
    calls = []

    def fake(url):
        calls.append(url)
        return table.get(url, (404, ""))

    monkeypatch.setattr(ra, "_fetch", fake)
    return calls


# ---------------------------------------------------------------------------
# The bug: an index fetched as one URL can never contain the token.
# ---------------------------------------------------------------------------

def test_index_is_followed_into_its_shards(monkeypatch):
    """★ THE BUG. Without following, the body is just shard URLs and every
    sitemap-index candidate reads `absent` forever, including after we list."""
    calls = _stub(monkeypatch, {
        "https://x.test/sitemap.xml": (200, INDEX),
        "https://x.test/a.xml": (200, "<url><loc>https://x.test/other</loc></url>"),
        "https://x.test/b.xml": (200, "<url><loc>https://x.test/dchub</loc></url>"),
    })
    status, body, complete = ra._fetch_probe("https://x.test/sitemap.xml")
    assert status == 200
    assert complete is True
    assert "dchub" in body.lower(), (
        "the shard holding our listing was never fetched — this probe would "
        "report absence no matter what the directory contains")
    assert "https://x.test/a.xml" in calls and "https://x.test/b.xml" in calls


def test_present_is_reached_through_a_followed_index(monkeypatch):
    """End to end: the token lives in a shard, and the verdict is `present`."""
    _stub(monkeypatch, {
        "https://x.test/sitemap.xml": (200, INDEX),
        "https://x.test/a.xml": (200, "<loc>https://x.test/nope</loc>"),
        "https://x.test/b.xml": (200, "<loc>https://x.test/dchub-mcp</loc>"),
    })
    status, body, complete = ra._fetch_probe("https://x.test/sitemap.xml")
    v = ra.classify_candidate(200, status, body, None, True, complete)
    assert v["verdict"] == "present"


# ---------------------------------------------------------------------------
# The rule that makes following safe.
# ---------------------------------------------------------------------------

def test_a_failed_shard_makes_the_enumeration_incomplete(monkeypatch):
    _stub(monkeypatch, {
        "https://x.test/sitemap.xml": (200, INDEX),
        "https://x.test/a.xml": (200, "<loc>https://x.test/nope</loc>"),
        # b.xml missing -> 404 from the stub
    })
    _, _, complete = ra._fetch_probe("https://x.test/sitemap.xml")
    assert complete is False, "a shard that failed to fetch was counted as read"


def test_truncated_enumeration_is_never_absent(monkeypatch):
    """★ THE LOAD-BEARING RULE. Same discipline as the 403/429/empty-body
    paths: what we could not read is UNKNOWN, not evidence of absence."""
    v = ra.classify_candidate(200, 200, "<loc>https://x.test/nope</loc>",
                              None, True, False)
    assert v["verdict"] == "unverified", (
        "a partial enumeration was reported as absence — this is how the loop "
        "manufactures a submission task for a directory we may already be on")
    assert "trunc" in v["reason"].lower()


def test_complete_enumeration_still_yields_absent(monkeypatch):
    """The rule must not swallow real absences, or the loop finds nothing."""
    v = ra.classify_candidate(200, 200, "<loc>https://x.test/nope</loc>",
                              None, True, True)
    assert v["verdict"] == "absent"


def test_shard_cap_marks_incomplete(monkeypatch):
    many = "<sitemapindex>" + "".join(
        f"<sitemap><loc>https://x.test/s{i}.xml</loc></sitemap>"
        for i in range(ra.SITEMAP_MAX_SHARDS + 5)) + "</sitemapindex>"
    table = {"https://x.test/sitemap.xml": (200, many)}
    for i in range(ra.SITEMAP_MAX_SHARDS + 5):
        table[f"https://x.test/s{i}.xml"] = (200, "<loc>nope</loc>")
    calls = _stub(monkeypatch, table)
    _, _, complete = ra._fetch_probe("https://x.test/sitemap.xml")
    assert complete is False
    assert len(calls) <= ra.SITEMAP_MAX_SHARDS + 1, (
        "the shard cap did not bound the fetch count — run_scan is on a weekly "
        "cron and an unbounded index would hang it")


def test_index_naming_no_shards_is_incomplete(monkeypatch):
    """An index that enumerates nothing proves nothing."""
    _stub(monkeypatch, {"https://x.test/sitemap.xml":
                        (200, "<sitemapindex></sitemapindex>")})
    _, _, complete = ra._fetch_probe("https://x.test/sitemap.xml")
    assert complete is False


# ---------------------------------------------------------------------------
# Non-sitemap probes must be untouched.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    "<html>a search page with no dchub</html>",
    "<urlset><loc>https://x.test/leaf</loc></urlset>",   # LEAF sitemap (composio)
])
def test_ordinary_and_leaf_probes_are_not_followed(monkeypatch, body):
    """composio's probe is a leaf sitemap and every other probe is a search
    page. Both must behave exactly as before: one fetch, complete=True."""
    calls = _stub(monkeypatch, {"https://x.test/p": (200, body)})
    status, got, complete = ra._fetch_probe("https://x.test/p")
    assert (status, got, complete) == (200, body, True)
    assert calls == ["https://x.test/p"], "a non-index probe made extra fetches"


def test_unreadable_probe_short_circuits_before_following(monkeypatch):
    calls = _stub(monkeypatch, {"https://x.test/p": (403, "")})
    status, _, complete = ra._fetch_probe("https://x.test/p")
    assert status == 403 and complete is True
    assert calls == ["https://x.test/p"]


# ---------------------------------------------------------------------------
# The new seed.
# ---------------------------------------------------------------------------

def test_explainx_is_seeded_with_a_sitemap_probe_and_a_real_submit_url():
    """A seed row must carry a real submit URL or an explicit None — a
    placeholder that 404s is exactly how this loop failed before."""
    row = [c for c in ra.CANDIDATE_DIRECTORIES if c["name"] == "explainx"]
    assert row, "explainx seed missing"
    row = row[0]
    assert row["probe"].endswith("sitemap.xml"), (
        "explainx's /mcp-servers page yields ~19,950 chars of visible text from "
        "295KB of HTML — paginated or part-hydrated, so absence is unreadable "
        "there; the sitemap is the honest probe")
    assert row["submit"] == "https://www.explainx.ai/submit"


def test_arcade_is_documented_as_not_seedable_not_added():
    """Arcade owns Smithery now, so the next session will reach for it. There is
    no registry surface to probe — the note must exist AND the row must not."""
    assert not [c for c in ra.CANDIDATE_DIRECTORIES
                if "arcade" in c["name"].lower()], (
        "arcade seeded despite having no listing URL — its probe could only 404")
    import inspect
    src = inspect.getsource(ra)
    assert "arcade_registry" in src, (
        "no note explaining why arcade is absent — a future session will "
        "re-litigate it from the acquisition headline")


def test_every_seed_row_has_the_required_shape():
    for c in ra.CANDIDATE_DIRECTORIES:
        assert c["home"].startswith("https://"), c["name"]
        assert c["probe"].startswith("https://"), c["name"]
        assert c["submit"] is None or c["submit"].startswith("https://"), c["name"]


# ---------------------------------------------------------------------------
# fleur: the branded domain was the wrong corpus.
# ---------------------------------------------------------------------------

def test_fleur_probes_the_github_registry_not_the_marketing_site():
    """★ CORRECTED 2026-08-26. fleurmcp.com is a one-page marketing site whose
    every path returns the same 72,867-byte landing page — which is why this row
    read `submit=None, no route in`. The catalog actually lives in
    github.com/fleuristes/app-registry (clone, edit apps.json, open a PR).

    Same error class the wong2 row already taught: an identifier POINTING AT a
    property does not make that property's contents the corpus.
    """
    row = [c for c in ra.CANDIDATE_DIRECTORIES if c["name"] == "fleur"][0]
    assert "fleurmcp.com" not in row["probe"], (
        "probing the marketing site again — every path there returns the same "
        "landing page, so absence is unreadable and the row can only ever be "
        "unverified")
    assert row["probe"].startswith("https://raw.githubusercontent.com/"), (
        "the honest probe is the raw apps.json: a complete server-rendered "
        "enumeration that needs no control probe")
    assert row["submit"] == "https://github.com/fleuristes/app-registry", (
        "submit=None would keep a directory with a REAL PR route out of the "
        "submission queue forever")


def test_fleur_absence_is_readable_from_a_raw_json_probe():
    """A raw JSON file cannot render client-side, so `absent` is meaningful."""
    body = '[{"name":"Fetch","config":{"runtime":"uvx"}}]'
    v = ra.classify_candidate(200, 200, body, None, True, True)
    assert v["verdict"] == "absent"
    body_present = '[{"name":"DC Hub","sourceUrl":"https://dchub.cloud/mcp"}]'
    v2 = ra.classify_candidate(200, 200, body_present, None, True, True)
    assert v2["verdict"] == "present"


def test_fleur_row_records_that_the_route_is_dormant():
    """★ Measured 2026-08-26, after submitting: app-registry last merged a PR
    2025-03-14 and last committed 2025-03-29, with 17 PRs open. The route is
    real but ~17 months idle, so the queue entry must not read as an available
    win — and classify_candidate cannot see this, because merge velocity is not
    fetchable from the probe URL (same blind spot as a pivoted directory).
    """
    import inspect
    src = inspect.getsource(ra)
    assert "DORMANT" in src, "the dormancy measurement is not recorded"
    assert "2025-03-14" in src, "no merge-velocity evidence pinned to the row"
    row = [c for c in ra.CANDIDATE_DIRECTORIES if c["name"] == "fleur"][0]
    assert row["submit"] == "https://github.com/fleuristes/app-registry", (
        "route flipped to None — it is dormant, not absent; it works again the "
        "moment the repo revives")
