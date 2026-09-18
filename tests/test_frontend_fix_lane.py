"""The frontend fix lane must refuse more than it applies.

The squasher's queue is 52 rows and this lane patches four of them. That ratio
IS the subject: the other 48 are not blocked by plumbing, they are rows whose
"suggested fix" would be wrong to apply, and a lane that shipped them would put
bugs into the frontend at machine speed with a brain-authored PR on top.

The eight `js_field_fallback_missing` lines below are the REAL lines from live
dchub-frontend main (read 2026-09-18), not invented fixtures. Four are value
reads with an existing fallback; four sit inside a condition where adding
`|| .company` changes what a filter matches or what a guard admits.

Stdlib + pytest; no DB, no network.
"""
import pytest

from routes import brain_frontend_fix_lane as lane

APPLY = "apply"


def _f(line_no, detail_snippet=None, issue=lane._AUTOFIXABLE_ISSUE):
    detail = f"Snippet: {detail_snippet}" if detail_snippet else ""
    return {"issue": issue, "path": "capacity-pipeline.html", "line": line_no,
            "detail": detail, "url": f"dchub-frontend/capacity-pipeline.html:{line_no}"}


def _classify_one(src_line, **kw):
    return lane._classify(_f(1, **kw), [src_line])


# ── the four that MUST be patched ────────────────────────────────────────────
@pytest.mark.parametrize("src,expect", [
    ("        const operatorInitials = (item.operator || 'UN').substring(0, 2).toUpperCase();",
     "(item.operator || item.company || 'UN')"),
    ("        <span class=\"operator-name\">${item.operator || 'Unknown'}</span>",
     "${item.operator || item.company || 'Unknown'}"),
    ("            item.operator || 'Unknown',",
     "item.operator || item.company || 'Unknown',"),
    ("      OPERATOR: item.operator || item.name,",
     "item.operator || item.company || item.name,"),
])
def test_value_reads_get_one_more_link_in_the_existing_chain(src, expect):
    new, disp, why = _classify_one(src)
    assert disp == APPLY, why
    assert expect in new
    # the final fallback is untouched -- that is what makes this safe
    assert new.count("||") == src.count("||") + 1


# ── the four that MUST NOT be patched ────────────────────────────────────────
@pytest.mark.parametrize("src", [
    "        if (item.operator && item.operator !== 'Unknown' && item.operator !== 'null') {",
    "        if (operator && item.operator !== operator) return false;",
    "            if (operator && item.operator !== operator) return false;",
    "        if (item.operator || item.location) {",
])
def test_predicate_reads_are_refused_with_a_reason(src):
    """Adding a fallback here changes what the filter matches. The detector's
    regex cannot tell these from a value read -- this lane can."""
    new, disp, why = _classify_one(src)
    assert new is None
    assert disp == "predicate_read", f"{disp}: {why}"
    assert "behaviour change" in why or "change what this filter" in why


def test_the_live_eight_split_four_and_four():
    """The whole design rests on this ratio. If a future edit makes the lane
    greedier, this is the test that says so."""
    live = [
        "        if (item.operator && item.operator !== 'Unknown' && item.operator !== 'null') {",
        "        if (operator && item.operator !== operator) return false;",
        "        const operatorInitials = (item.operator || 'UN').substring(0, 2).toUpperCase();",
        "        <span class=\"operator-name\">${item.operator || 'Unknown'}</span>",
        "        if (operator && item.operator !== operator) return false;",
        "            item.operator || 'Unknown',",
        "        if (item.operator || item.location) {",
        "      OPERATOR: item.operator || item.name,",
    ]
    d = [_classify_one(s)[1] for s in live]
    assert d.count(APPLY) == 4, d
    assert d.count("predicate_read") == 4, d


# ── the 44 that are not this lane's business ────────────────────────────────
def test_hero_stat_rows_are_never_auto_fixable():
    """severity='polish', 'may be intentional', and a suggested fix that is a
    CHOICE. Mechanically picking one would em-dash live numbers on index.html."""
    new, disp, why = lane._classify(
        _f(1, issue="bug_squash:hardcoded_hero_stat"),
        ['<span id="s-countries">140+</span>'])
    assert new is None
    assert disp == "not_auto_fixable"
    assert "polish" in why


# ── refusals that keep a re-run honest ──────────────────────────────────────
def test_a_line_that_moved_is_stale_not_patched_blind():
    new, disp, why = lane._classify(
        _f(1, detail_snippet="item.operator || 'Unknown',"),
        ["            item.somethingElse || 'Unknown',"])
    assert new is None and disp == "stale_finding", (disp, why)


def test_line_past_eof_is_stale():
    new, disp, why = lane._classify(_f(99), ["one line only"])
    assert new is None and disp == "stale_finding"
    assert "past EOF" in why


def test_rerunning_after_a_fix_is_a_noop():
    new, disp, _ = _classify_one("            item.operator || item.company || 'Unknown',")
    assert new is None and disp == "already_fixed"


def test_two_occurrences_on_one_line_is_ambiguous_not_a_guess():
    new, disp, why = _classify_one(
        "      a = item.operator || 'x'; b = item.operator || 'y';")
    assert new is None and disp == "ambiguous_line", (disp, why)


def test_a_bare_read_beside_a_chained_one_is_ambiguous_not_first_match_wins():
    """★ The case the OTHER ambiguity check exists for, and the one a
    two-chained-reads test never reaches. Here exactly ONE read is followed by
    `||`, so the hit count is 1 and the first guard passes -- but the line holds
    a second, BARE `item.operator`, and `str.replace(old, new, 1)` rewrites the
    FIRST occurrence, which is the bare one. Without the count check the lane
    silently patches a different expression than the finding named.

    Found by mutation M6: deleting the count check left the suite green,
    because the only test covering it was answered by the hit-count guard."""
    new_line, disp, why = _classify_one(
        "      const a = item.operator; const b = item.operator || 'x';")
    assert new_line is None, f"patched the wrong occurrence: {new_line}"
    assert disp == "ambiguous_line", (disp, why)
    assert "2x" in why


def test_a_bare_read_with_no_existing_fallback_is_not_touched():
    """Only an EXISTING chain is extended. A bare read has no declared
    'this may be missing', so rewriting it is a guess about intent."""
    new, disp, _ = _classify_one("      const name = item.operator;")
    assert new is None and disp == "no_value_read"


# ── the lane must not assume it can write ───────────────────────────────────
def test_write_capability_reads_the_push_permission_not_the_status_code(monkeypatch):
    class R:
        status_code = 200
        def json(self): return {"permissions": {"pull": True, "push": False}}
    monkeypatch.setattr(lane, "_gh", lambda *a, **k: R())
    cap = lane._write_capability()
    assert cap["can_write"] is False
    assert cap["reason"] == "token_is_read_only"
    assert "DCHUB_FRONTEND_RO_TOKEN" in cap["detail"]


def test_a_404_is_reported_as_invisible_not_as_read_only(monkeypatch):
    class R:
        status_code = 404
        text = "Not Found"
        def json(self): return {}
    monkeypatch.setattr(lane, "_gh", lambda *a, **k: R())
    cap = lane._write_capability()
    assert cap["can_write"] is False
    assert cap["reason"] == "repo_not_visible_to_token"


def test_push_true_is_the_only_thing_that_grants_write(monkeypatch):
    class R:
        status_code = 200
        def json(self): return {"permissions": {"pull": True, "push": True}}
    monkeypatch.setattr(lane, "_gh", lambda *a, **k: R())
    assert lane._write_capability()["can_write"] is True


# ── the gate must agree with the key the operator actually holds ────────────
def _call_gate(gate, env, header, monkeypatch):
    """Run an _admin_ok() with a controlled env + header, via a real request
    context so request.headers behaves as it does in production."""
    import main  # noqa: F401  (imported for its app factory side effects)
    for k in ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "BRAIN_ADMIN_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from flask import Flask
    app = Flask(__name__)
    with app.test_request_context("/", headers={"X-Admin-Key": header}):
        return gate()


def test_this_lane_and_the_squasher_accept_the_same_admin_key(monkeypatch):
    """★ The regression this pins. The lane's first gate read BRAIN_ADMIN_KEY
    first and the sibling squasher gate reads DCHUB_ADMIN_KEY. Both names exist
    on the service with DIFFERENT values, so the operative key opened
    /bug-squash/actionable (200) and was refused here (401): the endpoint
    shipped gated SHUT while "admin-gated" read as correct in review.

    A gate being PRESENT is not the property. Agreeing with the key the
    operator holds is."""
    from routes.brain_bug_squash import _admin_ok as squasher_gate

    env = {"DCHUB_ADMIN_KEY": "the-operative-key",
           "BRAIN_ADMIN_KEY": "a-different-stale-key"}
    assert _call_gate(squasher_gate, env, "the-operative-key", monkeypatch) is True
    assert _call_gate(lane._admin_ok, env, "the-operative-key", monkeypatch) is True, (
        "the lane refuses the key its sibling accepts")


def test_the_gate_is_closed_when_no_key_is_configured(monkeypatch):
    assert _call_gate(lane._admin_ok, {}, "anything", monkeypatch) is False


def test_a_wrong_key_is_refused(monkeypatch):
    assert _call_gate(lane._admin_ok, {"DCHUB_ADMIN_KEY": "right"},
                      "wrong", monkeypatch) is False


def test_the_credential_is_never_accepted_from_the_query_string(monkeypatch):
    """The sibling accepts ?admin_key=. This lane opens pull requests, and a
    credential in a URL lands in access logs."""
    from flask import Flask
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "the-operative-key")
    app = Flask(__name__)
    with app.test_request_context("/?admin_key=the-operative-key"):
        assert lane._admin_ok() is False
