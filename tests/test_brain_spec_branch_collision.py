"""Spec-PR re-filing must survive a leftover branch (2026-09-17).

Spec branch names are deterministic (kind + item_id + slug) and nothing used
to delete a branch when its PR closed. A condition whose spec PR was closed
UNMERGED is an explicit non-dedup ("closed-unmerged is a REJECTION, never a
dedup hit"), so the brain is meant to re-file it — but the leftover branch
made every re-file 422 "Reference already exists" under the same name, and
the dashboard reported only "create_branch failed".

Live at the time of the fix: 92 of 205 brain-spec/* branches were leftovers of
a closed-unmerged PR. prop #100049 (branch from PR #1647) was the reported case.
"""
import pytest

TAKEN = "brain-spec/prop-100049-a-finding-that-was-filed-once-before"


class _R:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._p = payload if payload is not None else {}

    def json(self):
        return self._p


@pytest.fixture
def opener(monkeypatch):
    o = pytest.importorskip("routes.brain_pr_opener")
    # Neutralise every gate and dedup pass so the branch step is what is tested.
    monkeypatch.setattr(o, "spec_condition_fingerprint", lambda h, d="": "a1b2c3d4")
    monkeypatch.setattr(o, "open_pr_exists", lambda t: False)
    monkeypatch.setattr(o, "open_spec_pr_with_fingerprint", lambda fp: None)
    monkeypatch.setattr(o, "landed_spec_with_fingerprint", lambda fp: None)
    monkeypatch.setattr(o, "merged_spec_pr_with_fingerprint", lambda fp: None)
    monkeypatch.setattr(o, "_get_default_branch_sha", lambda: "basesha")
    monkeypatch.setattr(o, "_commit_file", lambda *a, **k: True)
    import routes.brain_guardrails as g
    monkeypatch.setattr(g, "can_open_pr", lambda *a, **k: (True, ""))
    return o


def _wire(o, monkeypatch, *, taken=(), ref_status=None):
    """Fake GitHub. Refs named in `taken` answer 422 'Reference already exists'."""
    calls = {"refs": [], "pulls": []}

    def _gh(method, path, body=None):
        if method == "POST" and path.endswith("/git/refs"):
            ref = (body or {}).get("ref", "")
            name = ref.replace("refs/heads/", "")
            calls["refs"].append(name)
            if ref_status is not None:
                return _R(ref_status, {"message": "Resource not accessible"})
            if name in taken:
                return _R(422, {"message": "Reference already exists"})
            return _R(201, {"ref": ref})
        if method == "POST" and path.endswith("/pulls"):
            calls["pulls"].append((body or {}).get("head"))
            return _R(201, {"number": 9001,
                            "html_url": "https://github.com/x/y/pull/9001"})
        return _R(200, [])

    monkeypatch.setattr(o, "_gh", _gh)
    return calls


def test_refile_recovers_when_the_deterministic_branch_is_taken(opener, monkeypatch):
    """THE BUG: a leftover branch from a closed-unmerged PR blocked the item
    forever. The re-file must land on a sibling name, off CURRENT main."""
    calls = _wire(opener, monkeypatch, taken=(TAKEN,))
    out = opener.open_spec_pr(
        "Fix pocket-page internal link generation.",
        "a finding that was filed once before", "prop", 100049)

    assert out["ok"] is True and out["acted"] is True, out
    assert out["pr"]["number"] == 9001
    # It tried the taken name first, then actually created a different one.
    assert calls["refs"][0] == TAKEN, calls["refs"]
    assert len(calls["refs"]) >= 2, "never retried after the 422"
    used = calls["refs"][-1]
    assert used != TAKEN and used.startswith("brain-spec/prop-100049"), used
    # The PR opens off the branch that was actually created, not the taken one.
    assert calls["pulls"] == [used], calls["pulls"]


def test_a_real_create_failure_still_fails_and_names_the_reason(opener, monkeypatch):
    """A 403 is NOT a name collision — it must still fail, and say so. The old
    code returned a bare 'create_branch failed' for every cause alike."""
    calls = _wire(opener, monkeypatch, ref_status=403)
    out = opener.open_spec_pr("Do a thing.", "some heading", "prop", 4242)

    assert out["ok"] is False and out.get("acted") is False, out
    assert "403" in out["error"], out["error"]
    assert "Resource not accessible" in out["error"], out["error"]
    assert len(calls["refs"]) == 1, "a 403 must not be retried as a collision"
    assert calls["pulls"] == []


def test_a_422_that_is_not_a_collision_is_not_retried(opener, monkeypatch):
    """422 alone doesn't mean 'name taken' — a bad sha is also 422."""
    def _gh(method, path, body=None):
        if method == "POST" and path.endswith("/git/refs"):
            return _R(422, {"message": "Object does not exist"})
        return _R(200, [])
    monkeypatch.setattr(opener, "_gh", _gh)
    out = opener.open_spec_pr("Do a thing.", "h", "prop", 7)
    assert out["ok"] is False
    assert "Object does not exist" in out["error"], out["error"]


def test_control_an_uncontested_name_is_used_verbatim(opener, monkeypatch):
    """Control: with no collision the branch name is unchanged — the fix must
    not start suffixing every branch."""
    calls = _wire(opener, monkeypatch, taken=())
    out = opener.open_spec_pr("Do a thing.", "some heading", "prop", 4242)

    assert out["ok"] is True and out["acted"] is True
    assert len(calls["refs"]) == 1, calls["refs"]
    assert calls["refs"][0] == calls["pulls"][0]
    assert not calls["refs"][0].endswith("-r2")


def test_exhausting_every_candidate_name_fails_loudly(opener, monkeypatch):
    """If every candidate is taken, that is a failure — never a silent skip."""
    def _gh(method, path, body=None):
        if method == "POST" and path.endswith("/git/refs"):
            return _R(422, {"message": "Reference already exists"})
        return _R(200, [])
    monkeypatch.setattr(opener, "_gh", _gh)
    out = opener.open_spec_pr("Do a thing.", "h", "prop", 7)
    assert out["ok"] is False and "create_branch failed" in out["error"]


def test_branch_name_stays_within_the_90_char_cap(opener):
    """The suffix must fit inside the cap, not push the name past it."""
    long_pref = "brain-spec/prop-100049-" + ("x" * 200)
    for i in range(1, 4):
        sfx = f"-r{i + 1}"
        assert len((long_pref[:90 - len(sfx)] + sfx)) == 90


# ── the leak that created the landmines ─────────────────────────────────

def _pr_payload(ref="brain-spec/prop-1-x", full="azmartone67/dchub-backend",
                merged=None):
    return {"number": 1, "merged_at": merged,
            "head": {"ref": ref, "repo": {"full_name": full}}}


def _delete_wire(o, monkeypatch, payload, del_status=204):
    seen = []

    def _gh(method, path, body=None):
        if method == "GET" and "/pulls/" in path:
            return _R(200, payload)
        if method == "DELETE":
            seen.append(path)
            return _R(del_status)
        return _R(200, {})
    monkeypatch.setattr(o, "_gh", _gh)
    return seen


def test_closing_a_brain_draft_deletes_its_branch(opener, monkeypatch):
    seen = _delete_wire(opener, monkeypatch, _pr_payload())
    assert opener._delete_pr_branch(1) is True
    assert seen == ["/repos/azmartone67/dchub-backend/git/refs/heads/"
                    "brain-spec/prop-1-x"], seen


@pytest.mark.parametrize("payload,why", [
    (_pr_payload(merged="2026-01-01T00:00:00Z"), "merged PR"),
    (_pr_payload(full="someone/fork"), "fork branch"),
    (_pr_payload(ref="main"), "main"),
    (_pr_payload(ref="feat/human-work"), "a human's branch"),
    (_pr_payload(ref=""), "empty ref"),
])
def test_branch_delete_refuses_everything_it_should(opener, monkeypatch,
                                                    payload, why):
    seen = _delete_wire(opener, monkeypatch, payload)
    assert opener._delete_pr_branch(1) is False, why
    assert seen == [], f"issued a DELETE for {why}: {seen}"


# ── the janitor must actually CALL the delete, not just own the helper ──

def test_the_expiry_janitor_deletes_the_branch_it_closes(opener, monkeypatch):
    """End-to-end through expire_stale_draft_prs: closing a stale brain draft
    must leave no branch behind. Without this the helper exists but nothing
    calls it, and the landmines keep accumulating one per close."""
    stale = {"number": 77, "draft": True, "created_at": "2020-01-01T00:00:00Z",
             "title": "[brain-spec] prop #5: something stale",
             "head": {"ref": "brain-spec/prop-5-something-stale",
                      "repo": {"full_name": "azmartone67/dchub-backend"}}}
    deleted, patched = [], []

    def _gh(method, path, body=None):
        if method == "GET" and path.endswith("pulls?state=open&per_page=100"):
            return _R(200, [stale])
        if method == "GET" and path.endswith("/pulls/77"):
            return _R(200, dict(stale, merged_at=None))
        if method == "PATCH" and path.endswith("/pulls/77"):
            patched.append(body)
            return _R(200, {})
        if method == "DELETE":
            deleted.append(path)
            return _R(204)
        return _R(200, {})

    monkeypatch.setattr(opener, "_gh", _gh)
    out = opener.expire_stale_draft_prs(days=5)

    assert out["ok"] is True and 77 in out["closed"], out
    assert patched == [{"state": "closed"}], patched
    assert deleted == ["/repos/azmartone67/dchub-backend/git/refs/heads/"
                       "brain-spec/prop-5-something-stale"], deleted


def test_a_failed_close_does_not_delete_the_branch(opener, monkeypatch):
    """Ordering guard: if the PATCH fails the PR is still OPEN — deleting its
    branch would orphan a live PR."""
    stale = {"number": 77, "draft": True, "created_at": "2020-01-01T00:00:00Z",
             "title": "[brain-spec] prop #5: something stale",
             "head": {"ref": "brain-spec/prop-5-something-stale",
                      "repo": {"full_name": "azmartone67/dchub-backend"}}}
    deleted = []

    def _gh(method, path, body=None):
        if method == "GET" and path.endswith("pulls?state=open&per_page=100"):
            return _R(200, [stale])
        if method == "PATCH":
            return _R(403, {"message": "nope"})
        if method == "DELETE":
            deleted.append(path)
            return _R(204)
        return _R(200, dict(stale, merged_at=None))

    monkeypatch.setattr(opener, "_gh", _gh)
    out = opener.expire_stale_draft_prs(days=5)
    assert out["closed"] == [], out
    assert deleted == [], "deleted the branch of a PR that is still open"
