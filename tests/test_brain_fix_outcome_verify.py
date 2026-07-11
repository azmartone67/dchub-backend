"""PHASE 3.6 — fix-OUTCOME verifier + quarantine-consume tests. FULLY MOCKED.

NO real token, NO real GitHub, NO network, NO DB. We monkeypatch the writer's
get_file_on_main (read live main) and is_recipe_quarantined (DB read), and the
verifier's record path, and assert:

  · A fix whose anti-pattern (search_text) is GONE from the file on main AND
    whose replacement (replace_text) is present  → resolved True.
  · A NO-OP fix (search_text STILL present on main) → resolved False.
  · An unreadable file → resolved None (indeterminate, NEVER guessed).
  · A PR-shaped dict (head_ref only) is coerced via the branch log.
  · is_recipe_quarantined gates: a quarantined (file, klass) causes
    open_mechanical_draft_pr to be SKIPPED with reason 'quarantined' BEFORE any
    GitHub fetch; a NON-quarantined recipe is unaffected.
  · Recording is RECORD-ONLY and flag-gated (no-op unless BRAIN_FIX_VERIFY=1);
    verify_fix_resolved itself NEVER raises and NEVER blocks.

The module imports cleanly with NO flask; classify_mechanical lives in a
flask-importing module, lazily imported, so we install flask/internal_auth
shims (mirrors the sibling brain tests).

Run:  python3 -m pytest tests/test_brain_fix_outcome_verify.py -v
"""
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import routes.brain_fix_outcome_verify as fv  # noqa: E402 (imports w/o flask)
import routes.brain_draft_pr_writer as w      # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start every test with the verifier flag OFF (the SAFE default)."""
    monkeypatch.delenv("BRAIN_FIX_VERIFY", raising=False)
    monkeypatch.delenv("BRAIN_VERIFY_RESOLVE", raising=False)
    yield


@pytest.fixture(autouse=True)
def _flask_shim():
    """Shim flask/internal_auth when absent; drop the classifier so it
    re-imports under the shim (mirrors the sibling brain tests)."""
    saved = {k: sys.modules.get(k) for k in
             ("flask", "internal_auth", "routes.brain_mechanical_classifier")}
    installed = []
    if "flask" not in sys.modules:
        flask = types.ModuleType("flask")

        class _BP:
            def __init__(self, *a, **k):
                pass

            def _noop(self, *a, **k):
                return lambda fn: fn

            get = post = route = _noop

        flask.Blueprint = _BP
        flask.jsonify = lambda *a, **k: (a, k)
        flask.request = types.SimpleNamespace(
            headers={}, args={}, get_json=lambda *a, **k: {})
        sys.modules["flask"] = flask
        installed.append("flask")
    if "internal_auth" not in sys.modules:
        ia = types.ModuleType("internal_auth")
        ia.accepted_internal_keys = lambda: set()
        ia.is_valid_internal_key = lambda k: False
        sys.modules["internal_auth"] = ia
        installed.append("internal_auth")
    sys.modules.pop("routes.brain_mechanical_classifier", None)
    try:
        yield
    finally:
        for k in installed:
            sys.modules.pop(k, None)
        for k, v in saved.items():
            if v is not None:
                sys.modules[k] = v
            else:
                sys.modules.pop(k, None)


# ── A real interval_literal mechanical fix + fixture file contents ───
_SEARCH = "NOW() - INTERVAL '%s days'"
_REPLACE = "NOW() - %s * INTERVAL '1 day'"

_CONTENT_BEFORE = (
    "def recent(days):\n"
    "    cur.execute(\"SELECT 1 FROM t WHERE ts > " + _SEARCH + "\", (days,))\n"
    "    return cur.fetchall()\n"
)
# After the fix lands: the anti-pattern is GONE, the replacement is PRESENT.
_CONTENT_AFTER = _CONTENT_BEFORE.replace(_SEARCH, _REPLACE, 1)


def _proposal(pid=4242, file_path="routes/example.py", status="pr_opened"):
    return {
        "id": pid, "file_path": file_path,
        "search_text": _SEARCH, "replace_text": _REPLACE,
        "klass": "interval_literal", "confidence": 0.95,
        "changes_json": None, "status": status, "pr_url": None,
    }


def _open_proposal(pid=4242, file_path="routes/example.py"):
    """A proposal that has NOT yet been PR-opened (status='proposed') so the
    writer's idempotency gate doesn't short-circuit before the quarantine gate."""
    return _proposal(pid=pid, file_path=file_path, status="proposed")


def _wire_main(monkeypatch, *, ok=True, content=_CONTENT_AFTER):
    """Mock the writer's get_file_on_main so the verifier reads our fixture as
    'main'. The verifier imports it lazily from the writer module."""
    def fake(file_path):
        if not ok:
            return {"ok": False, "error": "contents_404: not found"}
        return {"ok": True, "content": content, "sha": "s", "head_sha": "h"}
    monkeypatch.setattr(w, "get_file_on_main", fake)


# ══════════════════════════════════════════════════════════════════════
# 1. A fix whose search_text is GONE from main resolves True.
# ══════════════════════════════════════════════════════════════════════
def test_resolved_true_when_anti_pattern_gone(monkeypatch):
    _wire_main(monkeypatch, content=_CONTENT_AFTER)
    res = fv.verify_fix_resolved(_proposal())
    assert res["resolved"] is True, res
    assert res["reason"] == "anti_pattern_gone_and_replacement_present", res
    assert res["file_path"] == "routes/example.py"
    print("RESOLVED TRUE ->", res)


# ══════════════════════════════════════════════════════════════════════
# 2. A NO-OP fix (search_text STILL present on main) resolves False.
# ══════════════════════════════════════════════════════════════════════
def test_resolved_false_when_anti_pattern_still_present(monkeypatch):
    # main still contains the anti-pattern → the fix did NOT resolve it.
    _wire_main(monkeypatch, content=_CONTENT_BEFORE)
    res = fv.verify_fix_resolved(_proposal())
    assert res["resolved"] is False, res
    assert res["reason"] == "anti_pattern_still_present_on_main", res
    print("RESOLVED FALSE (no-op) ->", res)


# ══════════════════════════════════════════════════════════════════════
# 3. An unreadable file resolves None (indeterminate — NEVER guessed).
# ══════════════════════════════════════════════════════════════════════
def test_resolved_none_when_file_unreadable(monkeypatch):
    _wire_main(monkeypatch, ok=False)
    res = fv.verify_fix_resolved(_proposal())
    assert res["resolved"] is None, res
    assert res["reason"].startswith("file_unreadable"), res
    print("INDETERMINATE (unreadable) ->", res)


def test_resolved_none_when_no_search_text(monkeypatch):
    _wire_main(monkeypatch)
    p = _proposal()
    p["search_text"] = ""
    p["changes_json"] = None
    res = fv.verify_fix_resolved(p)
    assert res["resolved"] is None, res
    assert res["reason"] == "missing_file_or_search_text", res


def test_resolved_none_when_search_gone_but_replace_absent(monkeypatch):
    # File changed some OTHER way: anti-pattern gone, but our replacement isn't
    # there → we can't positively confirm THIS fix resolved it.
    weird = "def recent(days):\n    return SOMETHING_ELSE\n"
    _wire_main(monkeypatch, content=weird)
    res = fv.verify_fix_resolved(_proposal())
    assert res["resolved"] is None, res
    assert res["reason"] == "search_gone_but_replacement_absent", res


# ══════════════════════════════════════════════════════════════════════
# 4. A PR-shaped dict (head_ref only) is coerced via the branch log.
# ══════════════════════════════════════════════════════════════════════
def test_pr_dict_coerced_via_branch_log(monkeypatch):
    _wire_main(monkeypatch, content=_CONTENT_AFTER)
    # The PR dict has only a head_ref; the verifier recovers the proposal.
    monkeypatch.setattr(
        w, "get_logged_proposal_for_branch",
        lambda branch: _proposal() if branch else None)
    pr = {"number": 999,
          "head_ref": "brain/autofix-interval_literal-4242-abc12345"}
    res = fv.verify_fix_resolved(pr)
    assert res["resolved"] is True, res
    assert res["file_path"] == "routes/example.py"


def test_pr_dict_no_branch_is_indeterminate(monkeypatch):
    res = fv.verify_fix_resolved({"number": 1})  # no branch, no proposal fields
    assert res["resolved"] is None, res
    assert res["reason"] == "no_proposal_recoverable", res


def test_verifier_never_raises_on_garbage():
    for bad in (None, 42, "x", [], {"head_ref": 12345}):
        res = fv.verify_fix_resolved(bad)
        assert res["resolved"] is None, (bad, res)


# ══════════════════════════════════════════════════════════════════════
# 5. QUARANTINE-CONSUME: a quarantined (file, klass) skips the draft PR.
# ══════════════════════════════════════════════════════════════════════
def _capture_writer_gh(monkeypatch, *, content=_CONTENT_BEFORE):
    """Wire the writer's GitHub primitives; capture whether main was fetched."""
    calls = {"fetch": [], "pr": None}

    def fake_get_file_on_main(file_path):
        calls["fetch"].append(file_path)
        return {"ok": True, "content": content, "sha": "s", "head_sha": "h"}

    def fake_open(**k):
        calls["pr"] = k
        return {"ok": True, "branch": k.get("branch"), "pr_url": ".../pull/1"}

    monkeypatch.setattr(w, "get_file_on_main", fake_get_file_on_main)
    monkeypatch.setattr(w, "open_draft_pr_with_content", fake_open)
    monkeypatch.setattr(w, "_mark_pr_opened", lambda *a, **k: True)
    # Disable cross-proposal dedup so it never interferes with this assertion.
    monkeypatch.setattr(w, "find_open_pr_for_file_class",
                        lambda **k: None)
    return calls


def test_quarantined_recipe_skips_draft_pr(monkeypatch):
    """A quarantined (file, klass) is skipped with reason 'quarantined' BEFORE
    any GitHub fetch — the quarantine status is finally CONSUMED."""
    calls = _capture_writer_gh(monkeypatch)
    # The writer imports is_recipe_quarantined lazily from THIS module — patch it.
    monkeypatch.setattr(fv, "is_recipe_quarantined",
                        lambda file_path, klass="": True)
    res = w.open_mechanical_draft_pr(_open_proposal(), dry_run=False)
    assert res.get("skipped") is True, res
    assert res["reason"] == "quarantined", res
    # Skipped BEFORE fetching main and BEFORE opening a PR.
    assert calls["fetch"] == [], "quarantine must short-circuit before fetch"
    assert calls["pr"] is None, "quarantine must open NOTHING"
    print("QUARANTINED -> skipped ->", res)


def test_non_quarantined_recipe_proceeds(monkeypatch):
    """A NON-quarantined recipe is unaffected — it proceeds normally (dry-run
    previews). Proves the gate doesn't block legit fixes."""
    calls = _capture_writer_gh(monkeypatch)
    monkeypatch.setattr(fv, "is_recipe_quarantined",
                        lambda file_path, klass="": False)
    res = w.open_mechanical_draft_pr(_open_proposal(), dry_run=True)
    assert res.get("would_open") is True, res
    assert res.get("reason") != "quarantined", res
    # It DID fetch main (got past the quarantine gate).
    assert calls["fetch"], "non-quarantined recipe should reach the main fetch"


def test_is_recipe_quarantined_false_without_db(monkeypatch):
    """No DB configured ⇒ is_recipe_quarantined returns False (fail-open: a DB
    hiccup must NEVER wrongly block a legitimate fix). Never raises."""
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert fv.is_recipe_quarantined("routes/example.py", "interval_literal") is False
    assert fv.is_recipe_quarantined("", "interval_literal") is False
    assert fv.is_recipe_quarantined(None, None) is False


# ══════════════════════════════════════════════════════════════════════
# 6. RECORD-ONLY + flag-gated: recording is a no-op unless BRAIN_FIX_VERIFY=1.
# ══════════════════════════════════════════════════════════════════════
def test_record_is_noop_when_flag_off(monkeypatch):
    monkeypatch.delenv("BRAIN_FIX_VERIFY", raising=False)
    # If recording tried to touch the DB it would import psycopg2 + connect;
    # with the flag off it must short-circuit to False before any of that.
    assert fv._flag_on() is False
    assert fv.record_fix_outcome({"resolved": True, "reason": "x"}) is False


def test_verify_and_record_never_blocks_and_returns_verdict(monkeypatch):
    """verify_and_record runs the verification and attaches `recorded`; with the
    flag off it records nothing but STILL returns the honest verdict (never
    blocks)."""
    _wire_main(monkeypatch, content=_CONTENT_AFTER)
    monkeypatch.delenv("BRAIN_FIX_VERIFY", raising=False)
    out = fv.verify_and_record(_proposal(), pr_number=5)
    assert out["resolved"] is True, out
    assert out["recorded"] is False, out  # flag off ⇒ not recorded


def test_record_enabled_only_when_flag_on(monkeypatch):
    monkeypatch.setenv("BRAIN_FIX_VERIFY", "1")
    assert fv._record_enabled() is True
    # An explicit BRAIN_VERIFY_RESOLVE=0 opt-out disables persistence.
    monkeypatch.setenv("BRAIN_VERIFY_RESOLVE", "0")
    assert fv._record_enabled() is False


# ══════════════════════════════════════════════════════════════════════
# 7. STATIC GUARD: the verifier never merges / never writes prod code.
# ══════════════════════════════════════════════════════════════════════
def test_no_merge_in_verifier_source():
    src = open(os.path.join(ROOT, "routes", "brain_fix_outcome_verify.py"),
               encoding="utf-8").read()
    assert "/merge" not in src, "verifier must never hit the REST merge endpoint"
    assert "squash_merge" not in src, "verifier must never merge"


# ══════════════════════════════════════════════════════════════════════
# 8. R66 (2026-07-11): recording routes through the CANONICAL writer.
#    The old hand-rolled INSERT targeted columns that don't exist on the
#    live brain_fix_outcomes table (created by brain_learning._SCHEMA with
#    still_broken/checked_at/evidence_note) — every verdict was silently
#    dropped for weeks. Now record_fix_outcome must delegate to
#    brain_learning.record_proposal_outcome with the tri-state mapped
#    resolved True/False/None → still_broken False/True/None.
# ══════════════════════════════════════════════════════════════════════
def _fake_brain_learning(captured):
    import types as _t
    mod = _t.ModuleType("routes.brain_learning")
    mod._ensure_schema = lambda: True

    def record_proposal_outcome(pid, kind, still_broken,
                                evidence_url=None, evidence_note=None):
        captured.append({"pid": pid, "kind": kind,
                         "still_broken": still_broken,
                         "url": evidence_url, "note": evidence_note})
        return True

    mod.record_proposal_outcome = record_proposal_outcome
    return mod


@pytest.mark.parametrize("resolved,expected_broken", [
    (True, False),   # fix held      → NOT still broken
    (False, True),   # no-op/drifted → still broken
    (None, None),    # indeterminate → NULL (checked, excluded by readers)
])
def test_record_maps_tristate_through_canonical_writer(
        monkeypatch, resolved, expected_broken):
    monkeypatch.setenv("BRAIN_FIX_VERIFY", "1")
    captured = []
    monkeypatch.setitem(sys.modules, "routes.brain_learning",
                        _fake_brain_learning(captured))
    ok = fv.record_fix_outcome(
        {"resolved": resolved, "reason": "why", "file_path": "routes/x.py"},
        proposal_id=77, pr_number=1600, branch="brain/autofix-k-77-abcd")
    assert ok is True
    assert len(captured) == 1
    rec = captured[0]
    assert rec["pid"] == 77 and rec["kind"] == "code"
    assert rec["still_broken"] is expected_broken
    assert rec["note"].startswith("ground-truth: why")
    assert "routes/x.py" in rec["note"]
    assert "/pull/1600" in rec["url"]


def test_record_refuses_without_proposal_id(monkeypatch):
    """No proposal to attribute the verdict to ⇒ refuse (never guess),
    without ever importing the writer."""
    monkeypatch.setenv("BRAIN_FIX_VERIFY", "1")
    captured = []
    monkeypatch.setitem(sys.modules, "routes.brain_learning",
                        _fake_brain_learning(captured))
    assert fv.record_fix_outcome({"resolved": True, "reason": "x"}) is False
    assert captured == []
