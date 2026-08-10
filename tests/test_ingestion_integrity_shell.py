"""Guard: the ingestion-integrity board must convict the three failures that
were live on 2026-08-10, and must NEVER render PASS on a surface it could not
read.

FENCES routes/ingestion_integrity_master_shell.py and the osm_crawler
User-Agent / status mapping. Every test drives the REAL shipped functions
(_lane_cron_auth, _lane_workflow_present, _lane_producer_liveness, _population)
with the module's own network helpers monkeypatched. No DB, no network, nothing
at module scope.

──────────────────────────────────────────────────────────────────────────
THE THREE LIVE FAILURES, ONE PER LANE (all measured 2026-08-10):

1. cron_auth — GitHub secret DCHUB_INTERNAL_KEY last set 2026-05-04 did not
   match the 64-char value on Railway. Step 1 of SECURITY_KEY_ROTATION.md was
   never run and INTERNAL_AUTH_LEGACY_OK=0 had closed the legacy bypass, so
   every X-Internal-Key caller 401'd. data-sync: 23 failures in 40 runs, all on
   the subsea step.

2. workflow_present — daily-infra-sync.yml was absent from the default branch.
   GitHub reported state=deleted; the 04:08 cron stopped firing after
   2026-07-25 and nothing went red, because a workflow that does not exist
   cannot fail.

3. producer_liveness — overpass-api.de answered HTTP 406 to a User-Agent
   containing "crawler" (3/3 deterministic; the identical query under
   "DCHub/1.0" got 429, i.e. accepted). 406 was absent from osm_crawler's
   handled set, fell through to a generic "error", and produced 12 consecutive
   zero-row runs inside GREEN runs.

──────────────────────────────────────────────────────────────────────────
★ Every "could not read" assertion is `is None`, never `not passed` —
`assert not x` passes on False and would let a lane that WRONGLY CONVICTS slide
through as if it had correctly abstained.

Run locally:
    python3 -m pytest tests/test_ingestion_integrity_shell.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture()
def mod():
    import routes.ingestion_integrity_master_shell as m
    return m


def _by_id(checks, cid):
    for c in checks:
        if c["id"] == cid:
            return c
    raise AssertionError(f"no check {cid!r} in {[c['id'] for c in checks]}")


# ── lane 2 · a workflow that left the default branch ─────────────────────────

def _wf(name, state, wid=1):
    return {"id": wid, "name": name, "path": f".github/workflows/{name}",
            "state": state}


def test_deleted_workflow_that_ran_recently_is_convicted(mod, monkeypatch):
    """THE daily-infra-sync CASE. state=deleted plus a run inside the window is
    a producer that stopped without anyone deciding it should."""
    def fake_gh(path, timeout=10):
        if "/actions/workflows?" in path:
            return {"total_count": 2, "workflows": [
                _wf("data-sync.yml", "active", 1),
                _wf("daily-infra-sync.yml", "deleted", 2)]}, None
        if "/workflows/2/runs" in path:
            return {"workflow_runs": [{"created_at": "2026-07-25T05:14:23Z"}]}, None
        return {"workflow_runs": []}, None

    monkeypatch.setattr(mod, "_gh", fake_gh)
    monkeypatch.setattr(mod, "_age_days", lambda iso: 16.0)

    checks = mod._lane_workflow_present()
    ghosts = _by_id(checks, "no_ghosts")
    assert ghosts["pass"] is False, "a deleted, recently-active workflow must convict"
    assert "daily-infra-sync.yml" in ghosts["detail"]
    assert mod._lane_verdict(checks) == "FAIL"


def test_deleted_workflow_long_quiet_is_a_retirement_not_a_finding(mod, monkeypatch):
    """Something retired last quarter is deleted ON PURPOSE. Convicting it
    would train the board to be ignored."""
    def fake_gh(path, timeout=10):
        if "/actions/workflows?" in path:
            return {"total_count": 1,
                    "workflows": [_wf("ancient.yml", "deleted", 9)]}, None
        return {"workflow_runs": [{"created_at": "2025-01-01T00:00:00Z"}]}, None

    monkeypatch.setattr(mod, "_gh", fake_gh)
    monkeypatch.setattr(mod, "_age_days", lambda iso: 400.0)

    checks = mod._lane_workflow_present()
    assert _by_id(checks, "no_ghosts")["pass"] is True


def test_unreadable_github_api_is_indeterminate_not_pass(mod, monkeypatch):
    """An unreadable inventory must NOT read as 'no workflows are deleted'."""
    monkeypatch.setattr(mod, "_gh", lambda p, timeout=10: (None, "HTTP 403"))
    checks = mod._lane_workflow_present()
    assert _by_id(checks, "list")["pass"] is None
    assert mod._lane_verdict(checks) == "?", "unreadable must never render PASS"


def test_truncated_workflow_page_is_flagged(mod, monkeypatch):
    """A deleted workflow could sit past the page boundary — say so rather than
    implying the sweep was complete."""
    def fake_gh(path, timeout=10):
        if "/actions/workflows?" in path:
            return {"total_count": 157,
                    "workflows": [_wf("a.yml", "active", 1)]}, None
        return {"workflow_runs": []}, None

    monkeypatch.setattr(mod, "_gh", fake_gh)
    checks = mod._lane_workflow_present()
    assert _by_id(checks, "list_complete")["pass"] is None
    assert mod._lane_verdict(checks) == "?"


# ── lane 3 · producers that return nothing ───────────────────────────────────

def _board(monkeypatch, mod, feeds):
    monkeypatch.setattr(mod, "_jget",
                        lambda url, timeout=10: ({"feeds": feeds}, None))
    monkeypatch.setattr(mod, "_local", lambda p: "http://x" + p)


def test_status_error_feed_is_convicted(mod, monkeypatch):
    """THE osm-crawl CASE, half one."""
    _board(monkeypatch, mod, [
        {"feed": "osm-crawl", "status": "error", "reasons": ["status=error"]},
        {"feed": "data-sync", "status": "success", "reasons": []}])
    checks = mod._lane_producer_liveness()
    err = _by_id(checks, "no_errors")
    assert err["pass"] is False
    assert "osm-crawl" in err["detail"]


def test_zero_row_streak_is_convicted(mod, monkeypatch):
    """THE osm-crawl CASE, half two: a producer returning nothing INSIDE a
    successful run. This is the shape that hid for 12 runs."""
    _board(monkeypatch, mod, [
        {"feed": "news-ner-discovery", "status": "success",
         "reasons": ["5 consecutive zero-row runs"]}])
    checks = mod._lane_producer_liveness()
    starved = _by_id(checks, "no_starved")
    assert starved["pass"] is False
    assert "news-ner-discovery" in starved["detail"]


def test_streak_below_threshold_does_not_convict(mod, monkeypatch):
    _board(monkeypatch, mod, [
        {"feed": "eia-pricing-ingest", "status": "success",
         "reasons": ["1 consecutive zero-row runs"]}])
    checks = mod._lane_producer_liveness()
    assert _by_id(checks, "no_starved")["pass"] is True


def test_clean_board_passes(mod, monkeypatch):
    """The guard must be able to read GREEN, or a permanently-red board is
    indistinguishable from a broken one."""
    _board(monkeypatch, mod, [
        {"feed": "transmission-ingest", "status": "success", "reasons": []},
        {"feed": "gas-pipeline-ingest", "status": "success", "reasons": []}])
    checks = mod._lane_producer_liveness()
    assert mod._lane_verdict(checks) == "PASS"


def test_unreadable_board_is_indeterminate(mod, monkeypatch):
    monkeypatch.setattr(mod, "_jget", lambda url, timeout=10: (None, "refused"))
    monkeypatch.setattr(mod, "_local", lambda p: "http://x" + p)
    checks = mod._lane_producer_liveness()
    assert _by_id(checks, "board")["pass"] is None
    assert mod._lane_verdict(checks) == "?"


# ── lane 1 · can CI still authenticate ───────────────────────────────────────

def test_failing_internal_key_caller_is_convicted(mod, monkeypatch):
    """THE key-drift CASE. The shell cannot read the secret VALUE — nothing
    can — so it convicts on the observed run outcome, which is the signal that
    actually moved."""
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "x" * 64)
    monkeypatch.setattr(mod, "_internal_key_workflows",
                        lambda: (["data-sync.yml"], None))
    monkeypatch.setattr(mod, "_gh", lambda p, timeout=10: (
        {"workflow_runs": [{"conclusion": "failure",
                            "created_at": "2026-08-10T12:21:08Z"}]}, None))
    checks = mod._lane_cron_auth()
    latest = _by_id(checks, "latest_run")
    assert latest["pass"] is False
    assert "data-sync.yml" in latest["detail"]
    assert mod._lane_verdict(checks) == "FAIL"


def test_missing_internal_key_on_host_is_convicted(mod, monkeypatch):
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    monkeypatch.setattr(mod, "_internal_key_workflows", lambda: ([], "no dir"))
    checks = mod._lane_cron_auth()
    assert _by_id(checks, "key_present")["pass"] is False


def test_unshipped_workflow_dir_is_indeterminate_not_pass(mod, monkeypatch):
    """Railway may not ship .github/. 'I found no callers' must not read as
    'every caller is healthy'."""
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "x" * 64)
    monkeypatch.setattr(mod, "_internal_key_workflows",
                        lambda: ([], "no .github/workflows in the deployed tree"))
    checks = mod._lane_cron_auth()
    assert _by_id(checks, "callers")["pass"] is None
    assert mod._lane_verdict(checks) == "?"


def test_all_callers_unreadable_is_indeterminate(mod, monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "x" * 64)
    monkeypatch.setattr(mod, "_internal_key_workflows",
                        lambda: (["data-sync.yml"], None))
    monkeypatch.setattr(mod, "_gh", lambda p, timeout=10: (None, "HTTP 502"))
    checks = mod._lane_cron_auth()
    assert _by_id(checks, "latest_run")["pass"] is None


def test_green_callers_pass(mod, monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "x" * 64)
    monkeypatch.setattr(mod, "_internal_key_workflows",
                        lambda: (["data-sync.yml"], None))
    monkeypatch.setattr(mod, "_gh", lambda p, timeout=10: (
        {"workflow_runs": [{"conclusion": "success",
                            "created_at": "2026-08-10T15:45:30Z"}]}, None))
    assert mod._lane_verdict(mod._lane_cron_auth()) == "PASS"


# ── the published population must not lie ────────────────────────────────────

def test_population_publishes_the_limit_it_cannot_measure(mod):
    pop = mod._population()
    assert pop["lanes"] == [lid for lid, _, _ in mod._LANES], \
        "population must be built from the executed lane list, not hand-typed"
    assert "write-only" in pop["cannot_measure"], \
        "the shell must publish that it cannot read GitHub secret values"


# ── the osm_crawler fix itself ───────────────────────────────────────────────

def test_crawler_user_agent_does_not_contain_the_blocked_token():
    """overpass-api.de 406s any User-Agent containing 'crawler' (measured 3/3
    on 2026-08-10). This is the whole fix; if it regresses, every bbox returns
    zero again and the run reports a misleading 'swept 0 POIs'."""
    import routes.osm_crawler as oc
    assert "crawler" not in oc.USER_AGENT.lower(), (
        f"USER_AGENT {oc.USER_AGENT!r} contains the token overpass-api.de "
        f"rejects with HTTP 406")


def test_client_rejection_is_its_own_status_not_generic_error(monkeypatch):
    """406 must NOT fall through to 'error'. Lumping a permanent client
    rejection in with transient failures is what made 12 zero-row runs look
    like an outage."""
    import urllib.error
    import urllib.request

    import routes.osm_crawler as oc

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 406, "Not Acceptable", {}, None)

    # _query_bbox does `import urllib.request` INSIDE the function, so the name
    # resolves to the real module every call — patch it there, not on oc.
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    elements, status = oc._query_bbox((39.0, -77.6, 39.2, -77.2))
    assert elements == []
    assert status == "rejected", (
        f"406 mapped to {status!r}; a permanent client rejection must be "
        f"distinguishable from a transient one")


def test_transient_failures_keep_their_existing_statuses(monkeypatch):
    """The new branch must not swallow the throttle/timeout handling the
    backoff logic depends on."""
    import urllib.error
    import urllib.request

    import routes.osm_crawler as oc

    for code, expected in ((429, "throttle"), (504, "timeout"), (500, "error")):
        def boom(*a, _c=code, **k):
            raise urllib.error.HTTPError("u", _c, "x", {}, None)
        monkeypatch.setattr(urllib.request, "urlopen", boom)
        _, status = oc._query_bbox((39.0, -77.6, 39.2, -77.2))
        assert status == expected, f"HTTP {code} → {status!r}, want {expected!r}"


def test_a_workflow_that_only_mentions_the_key_in_a_comment_is_not_a_caller(mod):
    """Caught on this module's own first live tick. daily-infra-sync.yml was
    rewritten to send X-Admin-Key and explains the OLD X-Internal-Key bug in a
    header comment; raw matching listed it as a key caller, so it would have
    been probed — and could have been convicted — for a credential it no longer
    sends."""
    body = (
        "# 2026-08-10: it used to send X-Internal-Key, which was the bug.\n"
        "#   the old body read DCHUB_INTERNAL_KEY from secrets.\n"
        "name: daily-infra-sync\n"
        "        run: curl -H \"X-Admin-Key: $ADMIN_KEY\" $URL\n")
    assert not mod._KEY_MARK.search(mod._uncommented(body)), \
        "a commented-out mention must not count as sending the credential"
    live = "        run: curl -H \"X-Internal-Key: $INTERNAL_KEY\" $URL\n"
    assert mod._KEY_MARK.search(mod._uncommented(live)), \
        "a real live sender must still be detected"


# ── the pagination blind spot the lane found in itself ───────────────────────

def test_workflow_inventory_walks_every_page(mod, monkeypatch):
    """GitHub caps the listing at 100/page and the repo has 159 workflows. The
    first shipped version read one page and sat permanently indeterminate,
    blind to a ghost in the unread remainder — measured live on its first tick:
    "100 of 159 workflows read"."""
    pages = {1: [_wf(f"a{i}.yml", "active", i) for i in range(100)],
             2: [_wf(f"b{i}.yml", "active", 100 + i) for i in range(59)]}
    seen = []

    def fake_gh(path, timeout=10):
        import re
        page = int(re.search(r"[?&]page=(\d+)", path).group(1))
        seen.append(page)
        return {"total_count": 159, "workflows": pages.get(page, [])}, None

    monkeypatch.setattr(mod, "_gh", fake_gh)
    wfs, total, err = mod._all_workflows()
    assert err is None
    assert total == 159
    assert len(wfs) == 159, f"walked {len(wfs)} of 159 — page 2 was dropped"
    assert seen == [1, 2], f"expected two page reads, got {seen}"


def test_ghost_on_the_second_page_is_still_convicted(mod, monkeypatch):
    """The whole point: a deleted workflow past the page boundary must not be
    invisible."""
    pages = {1: [_wf(f"a{i}.yml", "active", i) for i in range(100)],
             2: [_wf("daily-infra-sync.yml", "deleted", 777)]}

    def fake_gh(path, timeout=10):
        import re
        if "/actions/workflows?" in path:
            page = int(re.search(r"[?&]page=(\d+)", path).group(1))
            return {"total_count": 101, "workflows": pages.get(page, [])}, None
        return {"workflow_runs": [{"created_at": "2026-07-25T05:14:23Z"}]}, None

    monkeypatch.setattr(mod, "_gh", fake_gh)
    monkeypatch.setattr(mod, "_age_days", lambda iso: 16.0)
    checks = mod._lane_workflow_present()
    ghosts = _by_id(checks, "no_ghosts")
    assert ghosts["pass"] is False, "a ghost past the page boundary must convict"
    assert "daily-infra-sync.yml" in ghosts["detail"]


def test_first_page_failure_learns_nothing(mod, monkeypatch):
    monkeypatch.setattr(mod, "_gh", lambda p, timeout=10: (None, "HTTP 502"))
    wfs, total, err = mod._all_workflows()
    assert wfs is None, "a failed first page must report UNKNOWN, not an empty inventory"


def test_partial_walk_reports_what_it_missed(mod, monkeypatch):
    """A page failing mid-walk must not render as a complete sweep."""
    def fake_gh(path, timeout=10):
        import re
        page = int(re.search(r"[?&]page=(\d+)", path).group(1))
        if page == 1:
            return {"total_count": 159,
                    "workflows": [_wf(f"a{i}.yml", "active", i)
                                  for i in range(100)]}, None
        return None, "HTTP 502"

    monkeypatch.setattr(mod, "_gh", fake_gh)
    checks = mod._lane_workflow_present()
    incomplete = _by_id(checks, "list_complete")
    assert incomplete["pass"] is None
    assert mod._lane_verdict(checks) == "?", \
        "a partial inventory must never render a confident PASS"
    # The verdict alone is not enough to act on. "100 of 159, HTTP 502" sends
    # someone to the API; "100 of 159" alone looks like a cap we chose.
    assert "502" in incomplete["detail"], (
        f"the page-failure reason must survive into the detail, got: "
        f"{incomplete['detail']!r}")
