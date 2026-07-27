"""Registry acquisition (2026-07-27) — pins the two-question verdict.

Maintenance keeps listings from rotting; only acquisition grows presence,
and directories are ~84% of new agent arrivals. The risk in an acquisition
loop is the mirror of the maintenance bug we just fixed: instead of calling
an unread listing healthy, it would call an unread directory "absent" and
generate busywork — or worse, assert we should submit somewhere that does
not exist.

So the classifier answers two questions in order, and the first gates the
second: is the directory real, and only then, are we on it.

CI-SAFETY: pure classifier, no network, no DB.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def ra():
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import registry_acquisition as m
    return m


# ── question 1 gates question 2 ──────────────────────────────────────

def test_dead_directory_never_becomes_a_submission(ra):
    # The seed list is a GUESS. A candidate that does not resolve must drop
    # out, not become "we should submit here".
    v = ra.classify_candidate(404, 200, "<html>whatever</html>")
    assert v["verdict"] == "dead_directory"


def test_unreachable_home_is_unverified_not_absent(ra):
    v = ra.classify_candidate(None, 200, "<html>no dchub here</html>")
    assert v["verdict"] == "unverified"


# ── question 2, answered only honestly ───────────────────────────────

def test_unreadable_probe_is_unknown_not_absent(ra):
    # The mirror of the maintenance bug: a 403/429 on the probe means we do
    # not know, and must not manufacture a submission task.
    for status in (403, 429, 401, None):
        v = ra.classify_candidate(200, status, "")
        assert v["verdict"] == "unverified", status
        assert "unknown" in v["reason"].lower() or "could not" in v["reason"].lower()


def test_broken_probe_url_is_not_our_absence(ra):
    # A 404 on the probe means the probe URL is wrong — our own bug, not
    # evidence about the directory's contents.
    v = ra.classify_candidate(200, 404, "")
    assert v["verdict"] == "unverified"
    assert "probe is wrong" in v["reason"]


def test_present_when_identity_found(ra):
    v = ra.classify_candidate(200, 200, "<html>… DC Hub — dchub.cloud …</html>")
    assert v["verdict"] == "present"


def test_absent_only_when_live_and_readable_and_missing(ra):
    v = ra.classify_candidate(200, 200, "<html>a list of other servers</html>")
    assert v["verdict"] == "absent"
    assert "submittable" in v["reason"]


def test_client_side_search_cannot_prove_absence(ra):
    """Found by the first live scan: opentools and mcp-get render search
    client-side, so ?q=dchub returns byte-identical HTML to ?q=<nonsense>.
    The first version called them 'absent' — which would have sent someone to
    submit to directories we may already be on."""
    page = "<html>a directory shell with no results rendered server-side</html>"
    v = ra.classify_candidate(200, 200, page, control_body=page)
    assert v["verdict"] == "unverified"
    assert "client-side" in v["reason"].lower()
    # a genuinely filtering search still yields absent
    v2 = ra.classify_candidate(200, 200, "<html>no match for dc-hub</html>",
                               control_body="<html>different: zero results</html>")
    assert v2["verdict"] == "absent"


def test_empty_probe_body_is_unknown(ra):
    v = ra.classify_candidate(200, 200, "   ")
    assert v["verdict"] == "unverified"


# ── the loop must not pretend to submit ──────────────────────────────

def test_does_not_auto_submit(ra):
    """The speculative registry webhooks were deleted 2026-07-17 after every
    POST 404'd. Almost every directory needs a manual form or a GitHub PR, so
    this produces a reviewed QUEUE. A module that pretends to submit is worse
    than one that admits it cannot."""
    src = _read(os.path.join("routes", "registry_acquisition.py"))
    code = "\n".join(l for l in src.split("\n")
                     if not l.lstrip().startswith("#"))
    for banned in ("requests.post", ".post(", 'method="POST"'):
        assert banned not in code, banned


# ── wiring ───────────────────────────────────────────────────────────

def test_registered_and_cron_ticked():
    assert "register_blueprint(registry_acquisition_bp)" in _read("main.py")
    cron = _read(os.path.join("routes", "cron_heartbeat.py"))
    assert "/api/v1/admin/registry-acquisition/scan" in cron
    assert "REGISTRY_ACQUISITION_DISABLE" in cron


def test_lane_is_advisory_not_critical():
    # Deliberate: a non-empty queue is work-to-do, not breakage. Only
    # registry_truth (broken / long-unread listings) is a RED.
    src = _read(os.path.join("routes", "seven_levers_master_shell.py"))
    i = src.index("def _lane_registry_acquisition")
    body = src[i:src.index("# ── lane 2", i)]
    assert "critical=True" not in body
    assert "critical=False" in body


def test_shell_read_path_does_no_outbound_http():
    src = _read(os.path.join("routes", "registry_acquisition.py"))
    i = src.index("def read_queue")
    body = src[i:src.index("@registry_acquisition_bp", i)]
    for banned in ("requests.", "_fetch(", "urlopen"):
        assert banned not in body, banned


# ── Q3: absence is only a TASK if there is a way in ──────────────────

def test_no_submit_path_is_not_a_submission(ra):
    """Confirmed-absent with no public route must NOT read as submittable.
    Composio's toolkits are integrations Composio builds (/request-integration,
    /submit, /toolkits/request all 404), so queueing it would manufacture work
    nobody can complete — the same busywork the unverified states prevent,
    arriving from the other direction."""
    v = ra.classify_candidate(200, 200, "<html>a list of other servers</html>",
                              None, submittable=False)
    assert v["verdict"] == "no_submit_path"
    assert "submittable" not in v["reason"]


def test_submittable_default_is_unchanged(ra):
    v = ra.classify_candidate(200, 200, "<html>a list of other servers</html>")
    assert v["verdict"] == "absent"


def test_every_seed_declares_a_submit_route_or_none(ra):
    """A seed row must either carry a real submit URL or explicitly say None —
    a placeholder that 404s is how absence became an uncompletable task."""
    for c in ra.CANDIDATE_DIRECTORIES:
        assert "submit" in c, c["name"]
        if c["submit"] is not None:
            assert c["submit"].startswith("https://"), c["name"]


def test_known_dead_submit_routes_cannot_come_back(ra):
    """Verified 2026-07-27: wong2 refuses PRs in its README and points at the
    mcpservers.org form; appcypher has PRs disabled entirely."""
    dead = {
        "https://github.com/wong2/awesome-mcp-servers/pulls",
        "https://github.com/appcypher/awesome-mcp-servers/pulls",
        "https://composio.dev/",
    }
    for c in ra.CANDIDATE_DIRECTORIES:
        assert c["submit"] not in dead, c["name"]


def test_retired_mcp_run_is_gone_and_documented(ra):
    """www.mcp.run 301s to turbomcp.ai — now a self-hosted GATEWAY product
    behind a holding page, not a directory. No probe URL can be correct."""
    assert not any(c["name"] == "mcp_run" for c in ra.CANDIDATE_DIRECTORIES)
    src = _read(os.path.join("routes", "registry_acquisition.py"))
    assert "RETIRED CANDIDATES" in src and "turbomcp.ai" in src


def test_composio_probe_is_not_a_client_side_search(ra):
    """composio.dev/toolkits returns byte-identical HTML for ?q=dchub and a
    nonsense query, so absence could never be read there. The sitemap is a
    complete server-rendered enumeration."""
    c = [x for x in ra.CANDIDATE_DIRECTORIES if x["name"] == "composio"][0]
    assert c["probe"].endswith("sitemap.xml")
    assert "?q=" not in c["probe"]


def test_queue_excludes_no_submit_path():
    src = _read(os.path.join("routes", "registry_acquisition.py"))
    i = src.index("counts, queue, unverified")
    body = src[i:src.index("@registry_acquisition_bp", i)]
    # counted and surfaced, but never appended to the submission queue
    assert '"no_submit_path": no_route' in body
    assert 'no_route.append(name)' in body


def test_queue_depth_counts_routes_not_rows():
    """mcpservers_org and wong2_awesome_mcp are the SAME directory reached two
    ways (wong2's GitHub homepage field IS mcpservers.org). Counting rows told a
    human they had 4 submissions to make when 3 forms clear the queue. Two
    probes of one directory is good detection; the WORK is still one item."""
    src = _read(os.path.join("routes", "registry_acquisition.py"))
    i = src.index("counts, queue, unverified")
    body = src[i:src.index("@registry_acquisition_bp", i)]
    assert '"queue_depth": len(routes_seen)' in body
    assert '"rows_absent": len(queue)' in body


def test_a_curated_readme_is_not_a_probe_for_the_website_behind_it(ra):
    """The 2026-07-27 duplicate submission. wong2's GitHub homepage field points
    at mcpservers.org, so the README was treated as proof of absence from the
    SITE. It is not: the README is 537 curated entries, the site is a separate
    database of 32,807 listing URLs. DC Hub was absent from the first and
    already present on the second — three listing pages.

    mcpservers_org must not come back as a candidate: the site is fully
    client-rendered (even a real listing page's raw HTML contains no "dchub"),
    so no single-fetch probe can measure presence there at all."""
    names = {c["name"] for c in ra.CANDIDATE_DIRECTORIES}
    assert "mcpservers_org" not in names
    src = _read(os.path.join("routes", "registry_acquisition.py"))
    assert "DO NOT conflate this README with the mcpservers.org WEBSITE" in src


def test_wong2_probe_is_the_readme_only(ra):
    c = [x for x in ra.CANDIDATE_DIRECTORIES if x["name"] == "wong2_awesome_mcp"][0]
    assert c["probe"].startswith("https://raw.githubusercontent.com/")
    assert c["submit"] == "https://mcpservers.org/submit"
