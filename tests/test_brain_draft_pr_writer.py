"""Phase-2 mechanical DRAFT-PR writer tests — fully MOCKED GitHub client.

NO real token, NO real PR, NO network. We monkeypatch the three GitHub
primitives in routes.brain_draft_pr_writer:
    get_file_on_main          (read live main content)
    open_draft_pr_with_content (clone/branch/commit/push + gh pr create --draft)
    _mark_pr_opened           (the DB status/pr_url update)
and assert on the call ARGS — proving draft=true, base=main, head=branch
(NEVER main as head), no commit-to-main, no merge, and the status update.

The writer imports cleanly without flask; classify_mechanical lives in a
flask-importing module, so we install lightweight `flask` / `internal_auth`
shims in sys.modules BEFORE that lazy import fires. This mirrors the
no-flask test posture of tests/test_brain_v2_safety.py.

Run:  python3 -m pytest tests/test_brain_draft_pr_writer.py -v
"""
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# The writer itself imports cleanly with NO flask. classify_mechanical lives in
# a flask-importing module, lazily imported only when the writer actually runs.
import routes.brain_draft_pr_writer as w  # noqa: E402


@pytest.fixture(autouse=True)
def _flask_shim():
    """Install lightweight `flask` / `internal_auth` shims ONLY when the real
    modules are absent (no-flask CI), and ALWAYS restore sys.modules on
    teardown so this file never pollutes other test files sharing the process.
    Also drop the cached classifier module so it re-imports against whatever
    flask is present, then restore the prior cache afterward."""
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
    # Force a fresh classifier import under the current flask for this test.
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


# ── A real source file that contains `INTERVAL '%s days'` exactly once is
# used as the synthetic "live main" content, so the exactly-once check runs
# against genuinely-shaped code. We also build a tiny temp-file variant.
def _file_with_interval():
    """Return (path, content) of a repo file holding the interval bug once."""
    import glob
    import re
    needle = "INTERVAL '%s days'"
    for p in sorted(glob.glob(os.path.join(ROOT, "routes", "*.py"))):
        txt = open(p, encoding="utf-8").read()
        if txt.count(needle) == 1:
            rel = os.path.relpath(p, ROOT)
            # forbidden-path classes (keys/token/etc) would block; pick a clean one
            if re.search(r"key|token|auth|billing|stripe|webhook|worker", rel, re.I):
                continue
            return rel, txt
    return None, None


# ── Synthetic mechanical proposal (interval_literal class) ───────────
def _mech_proposal(file_path, pid=4242):
    return {
        "id": pid,
        "loop_name": "test_loop",
        "file_path": file_path,
        "search_text": "NOW() - INTERVAL '%s days'",
        "replace_text": "NOW() - %s * INTERVAL '1 day'",
        "rationale": "INTERVAL '%s days' cannot bind an int; multiply a unit interval.",
        "confidence": 0.95,
        "status": "proposed",
        "changes_json": None,
        "pr_url": None,
    }


# A self-contained content blob containing the search EXACTLY ONCE.
_CONTENT_ONE = (
    "def recent(days):\n"
    "    cur.execute(\"SELECT 1 FROM t WHERE ts > NOW() - INTERVAL '%s days'\", (days,))\n"
    "    return cur.fetchall()\n"
)
_CONTENT_ZERO = "def recent(days):\n    return []\n"
_CONTENT_TWO = _CONTENT_ONE + _CONTENT_ONE


def _capture_gh(monkeypatch, *, content_ok=True, content=_CONTENT_ONE,
                head_sha="deadbeefcafe"):
    """Wire mocked GitHub primitives; return a dict capturing the calls."""
    calls = {"pr": None, "mark": None, "fetch": []}

    def fake_get_file_on_main(file_path):
        calls["fetch"].append(file_path)
        if not content_ok:
            return {"ok": False, "error": "contents_404: not found"}
        return {"ok": True, "content": content, "sha": "blobsha", "head_sha": head_sha}

    def fake_open_draft(*, file_path, new_content, branch, title, body, commit_msg):
        calls["pr"] = dict(file_path=file_path, new_content=new_content,
                           branch=branch, title=title, body=body,
                           commit_msg=commit_msg)
        return {"ok": True, "branch": branch,
                "pr_url": f"https://github.com/azmartone67/dchub-backend/pull/999"}

    def fake_mark(pid, pr_url):
        calls["mark"] = (pid, pr_url)
        return True

    monkeypatch.setattr(w, "get_file_on_main", fake_get_file_on_main)
    monkeypatch.setattr(w, "open_draft_pr_with_content", fake_open_draft)
    monkeypatch.setattr(w, "_mark_pr_opened", fake_mark)
    return calls


# ──────────────────────────────────────────────────────────────────────
# 1. DRY-RUN over a synthetic mechanical proposal — would_open, no side effects.
# ──────────────────────────────────────────────────────────────────────
def test_dry_run_would_open_no_side_effects(monkeypatch):
    calls = _capture_gh(monkeypatch)
    p = _mech_proposal("routes/example.py")
    res = w.open_mechanical_draft_pr(p, dry_run=True)
    assert res["ok"] and res["would_open"] is True, res
    assert res["klass"] == "interval_literal", res
    assert res["branch"].startswith("brain/autofix-interval_literal-4242-"), res
    assert res["file_path"] == "routes/example.py"
    assert res["head_sha"] == "deadbeefcafe"
    assert any(line.startswith("-") for line in res["diff_preview"]), res["diff_preview"]
    assert any(line.startswith("+") for line in res["diff_preview"]), res["diff_preview"]
    # ZERO side effects: no PR open, no DB mark.
    assert calls["pr"] is None, "dry_run must not open a PR"
    assert calls["mark"] is None, "dry_run must not touch the DB"


# ──────────────────────────────────────────────────────────────────────
# 1b. DRY-RUN against a REAL repo file holding the interval bug once.
# ──────────────────────────────────────────────────────────────────────
def test_dry_run_against_real_repo_file(monkeypatch):
    rel, content = _file_with_interval()
    if not rel:
        import pytest
        pytest.skip("no clean routes/*.py with a single INTERVAL '%s days'")
    calls = {"pr": None}
    monkeypatch.setattr(w, "get_file_on_main",
                        lambda fp: {"ok": True, "content": content,
                                    "sha": "s", "head_sha": "h"})
    monkeypatch.setattr(w, "open_draft_pr_with_content",
                        lambda **k: calls.__setitem__("pr", k) or {"ok": True})
    p = _mech_proposal(rel)
    res = w.open_mechanical_draft_pr(p, dry_run=True)
    assert res["ok"] and res["would_open"], res
    assert res["file_path"] == rel
    assert calls["pr"] is None


# ──────────────────────────────────────────────────────────────────────
# 2. ABORT: search_text matches 0 times in live main.
# ──────────────────────────────────────────────────────────────────────
def test_abort_zero_match_in_main(monkeypatch):
    calls = _capture_gh(monkeypatch, content=_CONTENT_ZERO)
    res = w.open_mechanical_draft_pr(_mech_proposal("routes/x.py"), dry_run=True)
    assert res["ok"] is False and res.get("aborted"), res
    assert res["reason"] == "search_text_absent_in_main", res
    assert calls["pr"] is None and calls["mark"] is None


# ──────────────────────────────────────────────────────────────────────
# 2b. ABORT: search_text matches >1 time in live main (ambiguous).
# ──────────────────────────────────────────────────────────────────────
def test_abort_multi_match_in_main(monkeypatch):
    calls = _capture_gh(monkeypatch, content=_CONTENT_TWO)
    res = w.open_mechanical_draft_pr(_mech_proposal("routes/x.py"), dry_run=True)
    assert res["ok"] is False and res.get("aborted"), res
    assert res["reason"].startswith("search_text_ambiguous_in_main"), res
    assert calls["pr"] is None


# ──────────────────────────────────────────────────────────────────────
# 3. SKIP: proposal already pr_opened (idempotent).
# ──────────────────────────────────────────────────────────────────────
def test_skip_already_pr_opened(monkeypatch):
    calls = _capture_gh(monkeypatch)
    p = _mech_proposal("routes/x.py")
    p["status"] = "pr_opened"
    p["pr_url"] = "https://github.com/azmartone67/dchub-backend/pull/1"
    res = w.open_mechanical_draft_pr(p, dry_run=False)
    assert res["ok"] and res.get("skipped") and res["reason"] == "already_pr_opened", res
    assert calls["pr"] is None and calls["mark"] is None
    assert calls["fetch"] == [], "must not even fetch main for an already-open proposal"


# ──────────────────────────────────────────────────────────────────────
# 4. ABORT: non-mechanical proposal passed directly (defense-in-depth).
# ──────────────────────────────────────────────────────────────────────
def test_abort_non_mechanical_direct(monkeypatch):
    calls = _capture_gh(monkeypatch)
    # A proposal that adds an import + control flow => never mechanical.
    p = {
        "id": 7,
        "loop_name": "evil",
        "file_path": "routes/x.py",
        "search_text": "x = compute(value)",
        "replace_text": "import os\nif value:\n    x = os.system(value)",
        "rationale": "nope",
        "confidence": 0.99,
        "status": "proposed",
        "pr_url": None,
    }
    res = w.open_mechanical_draft_pr(p, dry_run=True)
    assert res["ok"] is False and res.get("aborted"), res
    assert res["reason"] == "not_mechanical", res
    assert calls["pr"] is None and calls["fetch"] == []


# ──────────────────────────────────────────────────────────────────────
# 4b. ABORT: forbidden path (billing) — even a clean transform is refused.
# ──────────────────────────────────────────────────────────────────────
def test_abort_forbidden_path(monkeypatch):
    calls = _capture_gh(monkeypatch)
    p = _mech_proposal("routes/billing_webhook.py")
    res = w.open_mechanical_draft_pr(p, dry_run=True)
    assert res["ok"] is False and res.get("aborted"), res
    assert res["reason"] == "not_mechanical", res
    assert calls["pr"] is None


# ──────────────────────────────────────────────────────────────────────
# 5. APPLY (mocked): asserts the DRAFT PR call args + the status UPDATE.
# ──────────────────────────────────────────────────────────────────────
def test_apply_opens_draft_pr_and_updates_status(monkeypatch):
    monkeypatch.setenv("DCHUB_L22_REAL_PR", "1")
    # Make the writer see a token via the L22 config (monkeypatch the module).
    import routes.brain_layer22_pr_writer as l22  # imported lazily by writer
    monkeypatch.setattr(l22, "GH_TOKEN", "ghp_faketoken", raising=False)
    monkeypatch.setattr(l22, "UPSTREAM_REPO", "azmartone67/dchub-backend", raising=False)
    monkeypatch.setattr(l22, "FORK_OWNER", "dchub-cloud-bot", raising=False)
    monkeypatch.setattr(l22, "DEFAULT_BASE", "main", raising=False)

    calls = _capture_gh(monkeypatch)
    p = _mech_proposal("routes/example.py", pid=555)
    res = w.open_mechanical_draft_pr(p, dry_run=False)

    assert res["ok"] and res["pr_url"].endswith("/pull/999"), res
    assert res["status_updated"] is True, res

    # The PR write fired with the replacement applied to the file content.
    pr = calls["pr"]
    assert pr is not None, "apply must call open_draft_pr_with_content"
    assert pr["file_path"] == "routes/example.py"
    assert "%s * INTERVAL '1 day'" in pr["new_content"], pr["new_content"]
    assert "INTERVAL '%s days'" not in pr["new_content"], "old bug must be gone"
    # head=THIS branch (never main); base is enforced inside the primitive.
    assert pr["branch"].startswith("brain/autofix-interval_literal-555-")
    assert pr["branch"] != "main"
    # The status UPDATE fired with (id, pr_url).
    assert calls["mark"] == (555, res["pr_url"]), calls["mark"]
    print("ASSERTED PR CALL ARGS:", {
        "file_path": pr["file_path"], "branch": pr["branch"],
        "title": pr["title"]})


# ──────────────────────────────────────────────────────────────────────
# 5b. The real git-CLI primitive ALWAYS passes --draft, base=main, head=branch.
#     We capture the exact `gh pr create` argv by mocking L22._safe_run.
# ──────────────────────────────────────────────────────────────────────
def test_draft_pr_primitive_rest_api_draft_base_main_head_branch(monkeypatch):
    """The REST primitive ALWAYS opens a DRAFT PR with base=main + head=the
    feature branch, creates the branch ref off main, and commits onto the branch
    — NEVER main, NEVER a merge."""
    import routes.brain_layer22_pr_writer as l22
    monkeypatch.setattr(l22, "GH_TOKEN", "ghp_faketoken", raising=False)
    monkeypatch.setattr(l22, "UPSTREAM_REPO", "azmartone67/dchub-backend", raising=False)
    monkeypatch.setattr(l22, "DEFAULT_BASE", "main", raising=False)

    seen = {"refs": None, "contents": None, "pulls": None}

    class _Resp:
        def __init__(self, code, payload=None, text=""):
            self.status_code = code; self._p = payload or {}; self.text = text
        def json(self): return self._p

    def fake_get(url, headers=None, params=None, timeout=None):
        if "/git/ref/heads/main" in url:
            return _Resp(200, {"object": {"sha": "basesha123"}})
        if "/contents/" in url:
            return _Resp(200, {"sha": "filesha456"})
        return _Resp(404, text="unexpected get " + url)

    def fake_post(url, headers=None, json=None, timeout=None):
        if url.endswith("/git/refs"):
            seen["refs"] = json
            return _Resp(201, {})
        if url.endswith("/pulls"):
            seen["pulls"] = json
            return _Resp(201, {"html_url": "https://github.com/azmartone67/dchub-backend/pull/1234"})
        return _Resp(404, text="unexpected post " + url)

    def fake_put(url, headers=None, json=None, timeout=None):
        seen["contents"] = json
        return _Resp(201, {})

    import requests as _rq
    monkeypatch.setattr(_rq, "get", fake_get)
    monkeypatch.setattr(_rq, "post", fake_post)
    monkeypatch.setattr(_rq, "put", fake_put)

    res = w.open_draft_pr_with_content(
        file_path="routes/example.py", new_content="x = 1\n",
        branch="brain/autofix-interval_literal-9-abc12345",
        title="t", body="b", commit_msg="m")
    assert res["ok"] and res["pr_url"].endswith("/pull/1234"), res

    # INVARIANTS on the REST payloads.
    assert seen["pulls"]["draft"] is True, seen["pulls"]
    assert seen["pulls"]["base"] == "main", seen["pulls"]
    assert seen["pulls"]["head"] == "brain/autofix-interval_literal-9-abc12345", seen["pulls"]
    assert seen["pulls"]["head"] != "main", "head must NEVER be main"
    assert seen["refs"]["ref"] == "refs/heads/brain/autofix-interval_literal-9-abc12345", seen["refs"]
    assert seen["refs"]["ref"] != "refs/heads/main", "never create/move the main ref"
    assert seen["contents"]["branch"] == "brain/autofix-interval_literal-9-abc12345", seen["contents"]
    assert seen["contents"]["branch"] != "main", "content commits onto the branch, never main"
    print("ASSERTED REST draft-PR invariants:", seen)


# ──────────────────────────────────────────────────────────────────────
# 6. apply=True but flag/token absent => skipped (no GitHub write).
# ──────────────────────────────────────────────────────────────────────
def test_apply_skipped_without_flag_or_token(monkeypatch):
    monkeypatch.setenv("DCHUB_L22_REAL_PR", "0")
    import routes.brain_layer22_pr_writer as l22
    monkeypatch.setattr(l22, "GH_TOKEN", "", raising=False)
    calls = _capture_gh(monkeypatch)
    res = w.open_mechanical_draft_pr(_mech_proposal("routes/x.py"), dry_run=False)
    assert res["ok"] and res.get("skipped"), res
    assert res["reason"] in ("DCHUB_L22_REAL_PR != 1", "no_token"), res
    assert calls["pr"] is None and calls["mark"] is None


# ──────────────────────────────────────────────────────────────────────
# 7. Batch driver: rate cap + per-row isolation.
# ──────────────────────────────────────────────────────────────────────
def test_batch_respects_rate_cap(monkeypatch):
    _capture_gh(monkeypatch)
    rows = [_mech_proposal("routes/f%d.py" % i, pid=i) for i in range(8)]
    out = w.open_mechanical_draft_prs(rows, apply=False, max_prs=3)
    assert out["dry_run"] is True
    assert len(out["previewed"]) == 3, out
    capped = [s for s in out["skipped"] if "rate_cap_reached" in s.get("reason", "")]
    assert len(capped) == 5, out


def test_batch_isolates_bad_rows(monkeypatch):
    _capture_gh(monkeypatch)
    good = _mech_proposal("routes/good.py", pid=1)
    bad = {"id": 2, "file_path": "routes/auth_login.py",  # forbidden => skipped
           "search_text": "NOW() - INTERVAL '%s days'",
           "replace_text": "NOW() - %s * INTERVAL '1 day'",
           "confidence": 0.95, "status": "proposed", "pr_url": None}
    out = w.open_mechanical_draft_prs([bad, good], apply=False)
    ids_prev = [p["id"] for p in out["previewed"]]
    ids_skip = [s["id"] for s in out["skipped"]]
    assert 1 in ids_prev, out
    assert 2 in ids_skip, out


# ──────────────────────────────────────────────────────────────────────
# 9. CROSS-PROPOSAL dedup (the #183/#184 piling-up bug).
#    Two DIFFERENT proposals for the SAME (file_path, klass) must NOT both
#    open a PR; the 2nd is marked dup_skipped. Different file => both open.
# ──────────────────────────────────────────────────────────────────────
def test_cross_proposal_dedup_db_marks_second_dup_skipped(monkeypatch):
    """A single proposal whose (file_path, klass) is ALREADY owned by another
    OPEN draft PR (found via the DB helper) is skipped + marked dup_skipped,
    opening NOTHING — even though it is itself a clean mechanical fix."""
    calls = _capture_gh(monkeypatch)
    marked = {"dup": None}

    # The DB says #183 already has an open PR for routes/db_utils.py + this klass.
    monkeypatch.setattr(
        w, "find_open_pr_for_file_class",
        lambda *, file_path, klass, exclude_id=None: {
            "id": 183,
            "pr_url": "https://github.com/azmartone67/dchub-backend/pull/183",
        })
    monkeypatch.setattr(
        w, "_mark_dup_skipped",
        lambda pid, dup_of_pr_url="", dup_of_id=None: marked.__setitem__(
            "dup", (pid, dup_of_id)) or True)

    p184 = _mech_proposal("routes/db_utils.py", pid=184)
    res = w.open_mechanical_draft_pr(p184, dry_run=False)

    assert res["ok"] and res.get("skipped"), res
    assert res["reason"] == "dup_open_pr_same_file_class", res
    assert res["dup_of_id"] == 183, res
    # NOTHING opened, NO normal status mark, and we never even fetched main.
    assert calls["pr"] is None and calls["mark"] is None, calls
    assert calls["fetch"] == [], "dup must short-circuit before fetching main"
    # The 2nd proposal was marked dup_skipped so it is not retried.
    assert marked["dup"] == (184, 183), marked


def test_cross_proposal_dedup_distinct_file_still_opens(monkeypatch):
    """Dedup must NOT block a legitimately-distinct fix: a DIFFERENT file (or
    different class) yields no DB match, so the proposal opens normally."""
    calls = _capture_gh(monkeypatch)
    seen_q = {"args": None}

    def fake_find(*, file_path, klass, exclude_id=None):
        seen_q["args"] = (file_path, klass, exclude_id)
        return None  # no other open PR for THIS file+class

    monkeypatch.setattr(w, "find_open_pr_for_file_class", fake_find)
    monkeypatch.setattr(w, "_mark_dup_skipped",
                        lambda *a, **k: pytest.fail("must NOT mark distinct fix"))

    p = _mech_proposal("routes/some_other.py", pid=900)
    res = w.open_mechanical_draft_pr(p, dry_run=True)
    assert res["ok"] and res["would_open"] is True, res
    # The dedup query ran with the right (file, klass) and excluded self.
    assert seen_q["args"][0] == "routes/some_other.py"
    assert seen_q["args"][1] == "interval_literal"
    assert seen_q["args"][2] == 900


def test_batch_cross_proposal_dedup_same_file_one_pr(monkeypatch):
    """The #183/#184 scenario in ONE batch run: two DIFFERENT proposals for the
    SAME file+class — only the FIRST previews/opens; the 2nd is dup_skipped.
    (Neither is yet 'pr_opened' in the DB, so the in-run guard must catch it.)"""
    _capture_gh(monkeypatch)
    # No DB: the per-proposal DB dedup helper finds nothing; the batch in-run
    # guard is what must stop the duplicate.
    monkeypatch.setattr(w, "find_open_pr_for_file_class",
                        lambda *, file_path, klass, exclude_id=None: None)
    marked = []
    monkeypatch.setattr(w, "_mark_dup_skipped",
                        lambda pid, dup_of_pr_url="", dup_of_id=None:
                        marked.append((pid, dup_of_id)) or True)

    p183 = _mech_proposal("routes/db_utils.py", pid=183)
    p184 = _mech_proposal("routes/db_utils.py", pid=184)  # same file + class
    out = w.open_mechanical_draft_prs([p183, p184], apply=False)

    prev_ids = [p["id"] for p in out["previewed"]]
    dup_skips = [s for s in out["skipped"]
                 if s.get("reason") == "dup_open_pr_same_file_class"]
    assert prev_ids == [183], out          # only the first opened/previewed
    assert len(dup_skips) == 1, out
    assert dup_skips[0]["id"] == 184, out
    assert dup_skips[0]["dup_of_id"] == 183, out
    assert (184, 183) in marked, marked    # 2nd marked dup_skipped


def test_batch_cross_proposal_dedup_different_files_both_open(monkeypatch):
    """Two proposals for DIFFERENT files (same class) must BOTH open — the
    dedup keys on (file, klass), so distinct files never collide."""
    _capture_gh(monkeypatch)
    monkeypatch.setattr(w, "find_open_pr_for_file_class",
                        lambda *, file_path, klass, exclude_id=None: None)
    monkeypatch.setattr(w, "_mark_dup_skipped",
                        lambda *a, **k: pytest.fail("distinct files must not dup"))

    a = _mech_proposal("routes/file_a.py", pid=10)
    b = _mech_proposal("routes/file_b.py", pid=11)
    out = w.open_mechanical_draft_prs([a, b], apply=False)
    prev_ids = sorted(p["id"] for p in out["previewed"])
    assert prev_ids == [10, 11], out
    assert not [s for s in out["skipped"]
                if s.get("reason") == "dup_open_pr_same_file_class"], out


# ──────────────────────────────────────────────────────────────────────
# 8. STATIC GUARD: no merge call / no commit-to-main anywhere in the writer.
# ──────────────────────────────────────────────────────────────────────
def test_no_merge_or_commit_to_main_in_source():
    src = open(os.path.join(ROOT, "routes", "brain_draft_pr_writer.py"),
               encoding="utf-8").read()
    assert "pr merge" not in src.lower(), "must not contain a PR-merge call"
    assert "--merge" not in src, "must not pass --merge"
    assert "/merge" not in src, "must not hit the REST merge endpoint"
    # head is always the feature branch; never `--head main` or pushing to main.
    assert "push" in src  # sanity: the push exists
    import re
    assert not re.search(r"--head['\"]?\s*,\s*['\"]main", src), "head must not be main"
