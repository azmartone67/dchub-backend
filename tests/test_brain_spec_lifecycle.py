"""Phase spec-lifecycle (2026-07-17) — the three lifecycle fixes that stop
[brain-spec] doc PRs from accumulating. FULLY MOCKED GitHub + DB + network;
NEVER imports main.py.

  FIX 1 — filer fingerprint dedup (routes/brain_pr_opener.open_spec_pr):
    the SAME condition arriving as two different items (#1632 agenda #100107
    vs #1634 prop #100040, filed one minute apart) must file ONCE. The filer
    stamps <!-- fingerprint:… --> into the PR body and skips filing when an
    OPEN spec PR already carries the same stamp.

  FIX 2 — spec-PR janitor (routes/brain_pr_janitor.spec_janitor_sweep):
    closes a spec DRAFT PR (with evidence) when its source item is graded,
    its underlying brain finding is resolved, or it aged out untouched
    (labeled 'stale-spec'). Dry unless BRAIN_PR_JANITOR_ENABLED=1 AND
    dry_run=False. Never merges; never touches a human-readied PR.

  FIX 3 — sentinel transient-5xx retry (routes/site_sentinel._scan_one):
    the verified /admin/funnel-health false positive — first AUTHED hit of a
    cold render 502s at the edge, an immediate retry answers 200 — must
    probe HEALTHY. Anonymous 401 on a needs_admin entry stays gated-healthy.

Run:  python3 -m pytest tests/test_brain_spec_lifecycle.py -v
"""
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import routes.brain_pr_opener as opener        # noqa: E402
import routes.brain_pr_janitor as jan          # noqa: E402
from routes.brain_proposal_dedup import condition_fingerprint  # noqa: E402


@pytest.fixture(autouse=True)
def _seal_landed_spec_scan(monkeypatch):
    """★ Hermetic seal (2026-08-16) — open_spec_pr consults
    landed_spec_with_fingerprint, which scans the REAL docs/brain-proposals/
    tree, so every test here that files a spec is silently coupled to live
    repo content. The condition fingerprint is number-INSENSITIVE by design
    (a canon bump must not re-file), so when the brain filed this file's own
    fixture condition at a new count — '320 DCPI markets' (#2745) vs the
    fixture's 311 — the scan started returning a dup,
    test_open_spec_pr_stamps_fingerprint_and_files_when_no_dup went red on
    main, and required unit-tests blocked every subsequent PR. The filer was
    correct; the test's 'no dup' premise had stopped being true.

    AUTOUSE on purpose: #2750 pinned the two tests that existed then, which
    fixes the instance and not the class — the next test written here would
    inherit the trap. This file's header promises FULLY MOCKED GitHub + DB +
    network; the docs tree is that same kind of live input, so it is pinned
    file-wide. Each test below is about a specific dedup path (open-PR,
    gating, janitor), never about the docs tree.

    The real scan is covered where it belongs and by design, against a
    fingerprint discovered FROM the tree rather than a hardcoded fixture:
    tests/test_brain_heal_fix_learn_rewire.py::
    test_landed_spec_dedup_finds_a_real_merged_fingerprint. Do not seal that.
    """
    monkeypatch.setattr(opener, "landed_spec_with_fingerprint",
                        lambda fp: None)
    yield


# ═══════════════════════════════════════════════════════════════════
#  FIX 1 — filer fingerprint dedup
# ═══════════════════════════════════════════════════════════════════

# The live duplicate pair (2026-07-17): identical condition, different items.
_HEADING_1632 = "[data_coverage] 311 DCPI markets across 7 live ISOs"
_HEADING_1634 = "[data_coverage] 311 DCPI markets across 7 live ISOs"
_DIRECTIVE = ("Approve making all 7 claimed ISOs genuinely live before "
              "adding breadth; ERCOT extraction is the active task.")


def test_same_condition_same_fingerprint_across_kinds():
    """agenda #100107 and prop #100040 carried the SAME condition — the
    filer-side fingerprint must agree (their DB-stamped fingerprints do NOT,
    which is exactly why the filer computes its own)."""
    fp_a = opener.spec_condition_fingerprint(_HEADING_1632, _DIRECTIVE)
    fp_b = opener.spec_condition_fingerprint(_HEADING_1634, _DIRECTIVE)
    assert fp_a and fp_a == fp_b


def test_fingerprint_survives_live_count_drift():
    """Number-stripping: live counts drifting between cycles must hash
    identically (the failure that made exact-title dedup useless)."""
    a = opener.spec_condition_fingerprint(
        "[data_coverage] 4923 verified vs 21959 tracked facilities")
    b = opener.spec_condition_fingerprint(
        "[data_coverage] 4924 verified vs 21960 tracked facilities")
    assert a and a == b


def test_different_conditions_different_fingerprints():
    a = opener.spec_condition_fingerprint(
        "[reliability] Brain finding: site_sentinel_unhealthy:/admin/funnel-health")
    b = opener.spec_condition_fingerprint(
        "[data_coverage] 311 DCPI markets across 7 live ISOs")
    assert a and b and a != b


class _FakeGhResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_open_spec_pr_skips_when_open_pr_carries_same_fingerprint(monkeypatch):
    """The #1632/#1634 replay: an OPEN spec PR already stamps the condition's
    fingerprint (filed as agenda #100107) — filing the SAME condition as
    prop #100040 must SKIP, not open a second PR."""
    import routes.brain_guardrails as guard
    monkeypatch.setattr(guard, "can_open_pr", lambda: (True, "ok"))
    # (the landed-docs scan is sealed file-wide by _seal_landed_spec_scan)
    fp = opener.spec_condition_fingerprint(_HEADING_1632, _DIRECTIVE)
    open_prs = [{
        "number": 1632,
        "title": f"[brain-spec] agenda #100107: {_HEADING_1632}",
        "body": f"<!-- fingerprint:{fp} -->\n# Brain proposal — {_HEADING_1632}",
    }]
    calls = {"created": 0}

    def fake_gh(method, path, body=None):
        if method == "GET" and path.startswith(
                f"/repos/{opener._GITHUB_REPO}/pulls?state=open"):
            return _FakeGhResp(200, open_prs)
        if method == "POST" and path.endswith("/pulls"):
            calls["created"] += 1
            return _FakeGhResp(201, {"number": 9999, "html_url": "x"})
        # branch/commit primitives must never be reached on a dedup skip
        raise AssertionError(f"unexpected GitHub call: {method} {path}")

    monkeypatch.setattr(opener, "_gh", fake_gh)
    res = opener.open_spec_pr(_DIRECTIVE, _HEADING_1634, "prop", 100040,
                              label="prop #100040")
    assert res["ok"] is True
    assert res["acted"] is False
    assert res.get("dup_pr") == 1632
    assert calls["created"] == 0


def test_open_spec_pr_stamps_fingerprint_and_files_when_no_dup(monkeypatch):
    """No same-fingerprint PR open ⇒ file as before, with the stamp as the
    FIRST line of the body (so it survives the [:4000] truncation)."""
    import routes.brain_guardrails as guard
    monkeypatch.setattr(guard, "can_open_pr", lambda: (True, "ok"))
    # This test's premise IS "no dup anywhere" — the landed-docs scan is
    # sealed file-wide by _seal_landed_spec_scan.
    created = {}

    def fake_gh(method, path, body=None):
        if method == "GET" and "pulls?state=open" in path:
            return _FakeGhResp(200, [])
        if method == "GET" and "/git/refs/heads/main" in path:
            return _FakeGhResp(200, {"object": {"sha": "abc123"}})
        if method == "POST" and path.endswith("/git/refs"):
            return _FakeGhResp(201, {})
        if method == "PUT" and "/contents/" in path:
            return _FakeGhResp(201, {})
        if method == "POST" and path.endswith("/pulls"):
            created.update(body or {})
            return _FakeGhResp(201, {"number": 4242, "html_url": "x"})
        raise AssertionError(f"unexpected GitHub call: {method} {path}")

    monkeypatch.setattr(opener, "_gh", fake_gh)
    res = opener.open_spec_pr(_DIRECTIVE, _HEADING_1632, "agenda", 100107,
                              label="agenda #100107")
    assert res.get("acted") is True
    fp = opener.spec_condition_fingerprint(_HEADING_1632, _DIRECTIVE)
    assert (created.get("body") or "").startswith(f"<!-- fingerprint:{fp} -->")
    assert created.get("draft") is True


def test_fingerprint_dedup_fails_open_on_github_error(monkeypatch):
    """A GitHub blip must never block filing — the lookup returns None."""
    monkeypatch.setattr(
        opener, "_gh",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert opener.open_spec_pr_with_fingerprint("deadbeef" * 4) is None
    assert opener.open_spec_pr_with_fingerprint(None) is None


# ═══════════════════════════════════════════════════════════════════
#  FIX 2 — spec-PR janitor sweep
# ═══════════════════════════════════════════════════════════════════

def _spec_pr(number, title, *, draft=True, age_days=1.0, body=""):
    return {"number": number, "title": title, "head_ref":
            f"brain-spec/x-{number}", "body": body, "draft": draft,
            "created_epoch": time.time() - age_days * 86400}


@pytest.fixture(autouse=True)
def _janitor_env(monkeypatch):
    monkeypatch.delenv("BRAIN_PR_JANITOR_ENABLED", raising=False)
    monkeypatch.delenv("BRAIN_SPEC_PR_MAX_AGE_DAYS", raising=False)
    yield


@pytest.fixture
def _quiet_db(monkeypatch):
    """Default DB answers: nothing graded, nothing resolved."""
    monkeypatch.setattr(jan, "_spec_source_grade", lambda k, i: None)
    monkeypatch.setattr(jan, "_spec_finding_resolved", lambda issue: None)
    yield


def test_spec_sweep_dry_when_env_flag_off(monkeypatch, _quiet_db):
    """Env flag off ⇒ DRY even with dry_run=False: would_close populated,
    ZERO closes / labels (sentinels explode on any write)."""
    monkeypatch.setattr(jan, "list_spec_prs", lambda: {"ok": True, "prs": [
        _spec_pr(10, "[brain-spec] agenda #7: graded thing")]})
    monkeypatch.setattr(jan, "_spec_source_grade",
                        lambda k, i: "good" if (k, i) == ("agenda", 7) else None)
    monkeypatch.setattr(jan, "close_pr", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("close_pr called in dry mode")))
    monkeypatch.setattr(jan, "_spec_label_stale", lambda n: (_ for _ in ()).throw(
        AssertionError("label called in dry mode")))
    out = jan.spec_janitor_sweep(dry_run=False)  # env flag still off
    assert out["dry_run"] is True
    assert [c["pr"] for c in out["would_close"]] == [10]
    assert out["would_close"][0]["close_reason"] == "source_graded"
    assert out["closed"] == []


def test_spec_sweep_closes_graded_and_resolved_when_enabled(monkeypatch):
    monkeypatch.setenv("BRAIN_PR_JANITOR_ENABLED", "1")
    monkeypatch.setattr(jan, "list_spec_prs", lambda: {"ok": True, "prs": [
        _spec_pr(11, "[brain-spec] prop #100040: [data_coverage] 311 DCPI markets"),
        _spec_pr(12, "[brain-spec] agenda #100105: [reliability] Brain finding: "
                     "site_sentinel_unhealthy:/admin/funnel-health @ https://x"),
        _spec_pr(13, "[brain-spec] agenda #55: young, ungraded, unresolved"),
    ]})
    monkeypatch.setattr(jan, "_spec_source_grade",
                        lambda k, i: "good" if (k, i) == ("prop", 100040) else None)
    monkeypatch.setattr(
        jan, "_spec_finding_resolved",
        lambda issue: ("2026-07-16 23:01:28+00:00"
                       if issue == "site_sentinel_unhealthy:/admin/funnel-health"
                       else None))
    closed_calls = []
    monkeypatch.setattr(
        jan, "close_pr",
        lambda n, *, reason="", header="": (closed_calls.append((n, reason, header))
                                            or {"ok": True, "number": n}))
    out = jan.spec_janitor_sweep(dry_run=False)
    assert out["dry_run"] is False
    assert sorted(c["pr"] for c in out["closed"]) == [11, 12]
    reasons = {c["pr"]: c["close_reason"] for c in out["closed"]}
    assert reasons[11] == "source_graded"
    assert reasons[12] == "finding_resolved"
    # evidence + the spec header reach the close comment
    by_pr = {n: (reason, header) for n, reason, header in closed_calls}
    assert "grade" in by_pr[11][0]
    assert "resolved" in by_pr[12][0]
    assert all(h == jan.SPEC_CLOSE_HEADER for _, h in by_pr.values())
    # the young/ungraded PR survives
    assert any(s["pr"] == 13 and s["reason"] == "no_close_condition"
               for s in out["skipped"])


def test_spec_sweep_stale_close_labels_and_respects_activity(monkeypatch, _quiet_db):
    monkeypatch.setenv("BRAIN_PR_JANITOR_ENABLED", "1")
    monkeypatch.setattr(jan, "list_spec_prs", lambda: {"ok": True, "prs": [
        _spec_pr(21, "[brain-spec] agenda #1: untouched + old", age_days=20),
        _spec_pr(22, "[brain-spec] agenda #2: old but commented", age_days=20),
        _spec_pr(23, "[brain-spec] agenda #3: young untouched", age_days=3),
    ]})
    details = {
        21: {"comments": 0, "review_comments": 0, "commits": 1},
        22: {"comments": 2, "review_comments": 0, "commits": 1},
        23: {"comments": 0, "review_comments": 0, "commits": 1},
    }
    monkeypatch.setattr(jan, "_spec_pr_detail", lambda n: details.get(n))
    labeled = []
    monkeypatch.setattr(jan, "_spec_label_stale",
                        lambda n: labeled.append(n) or True)
    monkeypatch.setattr(jan, "close_pr",
                        lambda n, **k: {"ok": True, "number": n})
    out = jan.spec_janitor_sweep(dry_run=False)
    assert [c["pr"] for c in out["closed"]] == [21]
    assert out["closed"][0]["close_reason"] == "stale_spec"
    assert labeled == [21]
    assert any(s["pr"] == 22 and s["reason"] == "aged_but_active_or_unknown"
               for s in out["skipped"])
    assert any(s["pr"] == 23 and s["reason"] == "no_close_condition"
               for s in out["skipped"])


def test_spec_sweep_never_touches_human_readied_pr(monkeypatch, _quiet_db):
    monkeypatch.setenv("BRAIN_PR_JANITOR_ENABLED", "1")
    monkeypatch.setattr(jan, "list_spec_prs", lambda: {"ok": True, "prs": [
        _spec_pr(31, "[brain-spec] agenda #9: human flipped ready",
                 draft=False, age_days=90)]})
    monkeypatch.setattr(jan, "close_pr", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("close_pr called on a human-readied PR")))
    out = jan.spec_janitor_sweep(dry_run=False)
    assert out["closed"] == [] and out["would_close"] == []
    assert any(s["pr"] == 31 and s["reason"] == "not_draft"
               for s in out["skipped"])


def test_spec_sweep_honors_per_run_cap(monkeypatch, _quiet_db):
    monkeypatch.setenv("BRAIN_PR_JANITOR_ENABLED", "1")
    monkeypatch.setenv("BRAIN_PR_JANITOR_MAX_PER_RUN", "1")
    monkeypatch.setattr(jan, "list_spec_prs", lambda: {"ok": True, "prs": [
        _spec_pr(41, "[brain-spec] agenda #1: g"),
        _spec_pr(42, "[brain-spec] agenda #2: g"),
    ]})
    monkeypatch.setattr(jan, "_spec_source_grade", lambda k, i: "good")
    monkeypatch.setattr(jan, "close_pr",
                        lambda n, **k: {"ok": True, "number": n})
    out = jan.spec_janitor_sweep(dry_run=False)
    assert len(out["closed"]) == 1
    assert any("rate_cap_reached" in s["reason"] for s in out["skipped"])


def test_spec_title_and_finding_regexes():
    m = jan._SPEC_TITLE_RE.match(
        "[brain-spec] agenda #100107: [data_coverage] 311 DCPI markets")
    assert m and m.group(1) == "agenda" and m.group(2) == "100107"
    # the finding label embeds '/' — the reconciler's variant would truncate
    fm = jan._SPEC_FINDING_RE.search(
        "[reliability] Brain finding: site_sentinel_unhealthy:"
        "/admin/funnel-health @ https://dchub.cloud/admin/fu")
    assert fm and fm.group(1) == "site_sentinel_unhealthy:/admin/funnel-health"


# ═══════════════════════════════════════════════════════════════════
#  FIX 3 — sentinel: cold-render 5xx retry + gated-401 healthy
# ═══════════════════════════════════════════════════════════════════

class _FakeRaw:
    def __init__(self, body):
        self._body = body

    def read(self, n, decode_content=True):
        return self._body[:n]


class _FakeHttpResp:
    def __init__(self, status_code, body=b""):
        self.status_code = status_code
        self.raw = _FakeRaw(body)
        self.content = body
        self.headers = {}
        self.url = None

    def close(self):
        pass


def _probe(monkeypatch, entry, responses, admin_key="k"):
    """Run site_sentinel._scan_one against a scripted response sequence."""
    import requests as real_requests
    import routes.site_sentinel as ss
    seq = list(responses)
    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        return seq.pop(0)

    monkeypatch.setattr(real_requests, "get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(ss, "_ADMIN_KEY", admin_key)
    out = ss._scan_one(entry)
    return out, calls["n"]


_FH_ENTRY = {"path": "/admin/funnel-health", "category": "high",
             "min_bytes": 2000, "label": "Admin Funnel Health",
             "needs_admin": True}


def test_sentinel_cold_502_then_200_probes_healthy(monkeypatch):
    """The verified live failure: first authed hit of the cold render dies as
    an edge 502 (~10s), the immediate retry answers 200 — the probe must
    retry and report HEALTHY (this was PR #1636's false premise)."""
    out, n = _probe(monkeypatch, _FH_ENTRY, [
        _FakeHttpResp(502, b"error code: 502"),
        _FakeHttpResp(200, b"<html>" + b"x" * 40000),
    ])
    assert out["status_code"] == 200
    assert out["healthy"] is True
    assert n == 2


def test_sentinel_persistent_5xx_still_flags(monkeypatch):
    """A page that is REALLY down fails all 3 attempts and is flagged."""
    out, n = _probe(monkeypatch, _FH_ENTRY, [
        _FakeHttpResp(502, b"error code: 502")] * 3)
    assert out["healthy"] is False
    assert out["reason"] == "http_status:502"
    assert n == 3


def test_sentinel_anonymous_401_on_gated_admin_stays_healthy(monkeypatch):
    """No admin key in the sentinel runtime ⇒ 401 is the gate WORKING."""
    out, n = _probe(monkeypatch, _FH_ENTRY,
                    [_FakeHttpResp(401, b'{"error":"unauthorized"}')],
                    admin_key="")
    assert out["healthy"] is True
    assert out.get("gated") is True
    assert "admin_gated_no_key" in out["reason"]
    assert n == 1  # 401 is not retried


def test_sentinel_expected_5xx_not_retried(monkeypatch):
    """An entry that EXPECTS 503 (e.g. the QA precheck API) must accept it
    on the first attempt — no retry, healthy path."""
    entry = {"path": "/api/v1/admin/qa/state-of-2026-precheck",
             "category": "high", "min_bytes": 0, "needs_admin": True,
             "expected_status": [200, 202, 503, 504]}
    out, n = _probe(monkeypatch, entry, [_FakeHttpResp(503, b"warming")])
    assert n == 1
    assert out["status_code"] == 503
