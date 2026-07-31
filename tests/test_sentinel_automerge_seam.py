"""The sentinel→PR-body→GATE_SENTINEL_DERIVED seam, which never worked.

site_sentinel DOES fire — `last_brain_finding_at` is set on three paths
(/api/v1/admin/qa/state-of-2026-precheck 06-07, /admin/qa/state-of-2026 06-12,
/admin/funnel-health 06-26). But gate 1 reads the finding key out of the PR
**body**, and until the `**Finding:**` line existed the key reached NO field of
the proposal at all: across 287 brain_proposed_code_fixes rows, `loop_name LIKE
'page_persistent_5xx%'` was 0, and no other column carried it either. For a
backend-issue proposal loop_name is the finding URL (loop_state["name"] = url).
So gate 1 matched nothing and rejected 100% of PRs on merit-independent
grounds — proven live on PR #1341, a genuine sentinel fix to
routes/site_sentinel.py whose Loop line read `https://dchub.cloud/api/v1/iso/
zones`. These tests keep that seam wired.

These tests pin the seam from both sides:
  1. the drafter still emits a **Finding:** line carrying the SOURCE FINDING key
     (twin-drift guard, read from the REAL source — not a copy of the template);
  2. gate 1 accepts a body in exactly that shape and extracts the path;
  3. gate 1 still REJECTS a body without the key (the safety property that
     correctly produced 45/45 rejects — widening it is a deliberate decision,
     not something a refactor may do by accident).

No network, no DB.
"""
import ast
import importlib
import pathlib

SRC = (pathlib.Path(__file__).resolve().parent.parent
       / "routes" / "brain_backlog_admin.py")


def _mod():
    import routes.sentinel_auto_merge as S
    return importlib.reload(S)


DRAFTER_FN = "_open_draft_pr_for_proposal"


def _drafter_fn_node():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    assert len(tree.body) > 0, "brain_backlog_admin.py parsed to an empty tree"
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == DRAFTER_FN), None)
    assert fn is not None, (
        f"{DRAFTER_FN} not found in brain_backlog_admin.py — it was renamed or "
        f"moved. This test cannot guard a function it cannot find; re-point it."
    )
    return fn


def _drafter_string_constants() -> list[str]:
    """Every string LITERAL in the drafter, with no variable names mixed in.

    Kept separate from the rendered template so an assertion can distinguish
    the dict key `prop["issue_key"]` from the local named `_issue_key` — a
    substring check cannot, and silently passed a repointed lookup.
    """
    out = [sub.value for sub in ast.walk(_drafter_fn_node())
           if isinstance(sub, ast.Constant) and isinstance(sub.value, str)]
    assert out, f"{DRAFTER_FN} yielded no string constants"
    return out


def _drafter_pr_body_template() -> str:
    """Pull the real draft-PR-writing function out of brain_backlog_admin source.

    Extracts the WHOLE function rather than just the `pr_body` assignment,
    because parts of the body are assembled in helper locals (the conditional
    `**Finding:**` line is one). Narrowing to the pr_body node would miss those
    and the drift guard would silently pass.

    Uses ast so a moved/renamed function fails loudly here instead of matching
    nothing. An empty parse passes every `in` assertion, so both the tree and
    the located function are asserted before anything is read out of them.
    """
    fn = _drafter_fn_node()
    joined: list[str] = []
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            joined.append(sub.value)
        elif isinstance(sub, ast.Name):
            joined.append(sub.id)
    assert joined, f"{DRAFTER_FN} yielded no string constants"

    # Presence of the literal ANYWHERE in the function is not enough: the line
    # could be built into a local that pr_body never interpolates, leaving the
    # PR body without it while this guard still passed. Require that whatever
    # carries the Finding line is actually REFERENCED by the pr_body assignment.
    body_assign = next(
        (n for n in ast.walk(fn)
         if isinstance(n, ast.Assign)
         and any(isinstance(t, ast.Name) and t.id == "pr_body" for t in n.targets)),
        None)
    assert body_assign is not None, f"no pr_body assignment in {DRAFTER_FN}"
    reached = "".join(
        (sub.value if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
         else sub.id if isinstance(sub, ast.Name) else "")
        for sub in ast.walk(body_assign))
    assert "_finding_line" in reached or "**Finding:**" in reached, (
        "the Finding line is built but pr_body never interpolates it — the "
        "finding key would not reach the PR body and gate 1 would reject every "
        "sentinel PR. (This is exactly the failure mode the old Loop-line "
        "version of this test missed.)"
    )
    return "".join(joined)


def test_drafter_emits_the_finding_line_carrying_the_issue_key():
    """Twin-drift guard: gate 1 can only see the finding key if the drafter
    writes issue_key into the body. Drop the Finding line and the sentinel lane
    goes permanently dead with no other symptom.

    NOTE: an earlier version of this test asserted the **Loop:** line carried
    the key. It does not and never did — for a backend-issue proposal loop_name
    is the finding URL. That test was green against a body shape production
    never produces; this one pins the field that actually carries the key."""
    tpl = _drafter_pr_body_template()
    assert "**Finding:**" in tpl, (
        "brain_backlog_admin no longer emits a '**Finding:**' line — "
        "GATE_SENTINEL_DERIVED reads the finding key from it, so the sentinel "
        "auto-merge lane is now unreachable. Update both sides together."
    )
    # Exact-match against the STRING CONSTANTS only. A substring check against
    # the whole rendering matches the local variable `_issue_key` and passes
    # even when the lookup has been repointed at another field.
    assert "issue_key" in _drafter_string_constants(), (
        "the drafter no longer reads prop['issue_key'] — the Finding line is "
        "being built from some other field, so the finding key never reaches "
        "the PR body and gate 1 rejects every sentinel PR."
    )


def test_loop_name_alone_does_not_satisfy_gate1():
    """Regression pin for the ACTUAL production bug (PR #1341): a real sentinel
    fix whose Loop line carried the finding URL was rejected, because a URL is
    not the issue key. If someone 'fixes' gate 1 by matching URLs, this fails."""
    S = _mod()
    body = (
        "## Brain Layer-5 auto-proposed fix\n\n"
        "**Proposal:** #114\n"
        "**Loop:** `https://dchub.cloud/api/v1/iso/zones`\n"
        "**Confidence:** 0.85 (threshold >=0.75)\n"
    )
    ok, reason, _ = S._gate_sentinel_derived({"body": body})
    assert ok is False, "a bare finding URL must not pass as sentinel-derived"
    assert "GATE_SENTINEL_DERIVED" in reason


def test_gate1_accepts_a_real_drafter_body_for_a_sentinel_finding():
    """The day a real page_persistent_5xx fires, this is the path it takes."""
    S = _mod()
    path = "/api/v1/iso/zones"
    # Rendered exactly as brain_backlog_admin renders it: the key rides the
    # **Finding:** line, while **Loop:** carries the URL as it does in prod.
    body = (
        "## Brain Layer-5 auto-proposed fix\n\n"
        "**Proposal:** #4242\n"
        f"**Finding:** `page_persistent_5xx:{path}`\n"
        "**Loop:** `https://dchub.cloud/api/v1/iso/zones`\n"
        "**Confidence:** 0.97 (threshold ≥0.95)\n"
        "**File:** `routes/iso_zones.py` (100→120 chars)\n\n"
    )
    ok, reason, extra = S._gate_sentinel_derived({"body": body})
    assert ok is True, f"gate 1 rejected a genuine sentinel PR: {reason}"
    issue, found = S._find_linked_sentinel_finding(body)
    assert issue == f"page_persistent_5xx:{path}"
    assert found == path, f"path mis-extracted: {found!r} (trailing backtick?)"


def test_gate1_still_rejects_a_body_with_no_sentinel_finding():
    """The 45/45 historical rejects were CORRECT. Keep them correct."""
    S = _mod()
    body = (
        "## Brain Layer-5 auto-proposed fix\n\n"
        "**Proposal:** #10\n"
        "**Loop:** `content_publisher.py`\n"   # no Finding line at all\n
        "**Confidence:** 0.99 (threshold ≥0.95)\n"
    )
    ok, reason, _ = S._gate_sentinel_derived({"body": body})
    assert ok is False
    assert "GATE_SENTINEL_DERIVED" in reason
    # High confidence must not rescue a non-sentinel PR: gate 1 runs first.
    assert "0.99" not in reason


def test_lookback_window_is_tunable_and_bounded():
    """The window decides what gets LOOKED at, never what may merge."""
    import os
    S = _mod()
    assert S._lookback_days() == 30            # widened from a hardcoded 7
    for raw, want in (("7", 7), ("0", 1), ("999", 90), ("nonsense", 30)):
        os.environ["SENTINEL_AUTO_MERGE_LOOKBACK_DAYS"] = raw
        assert S._lookback_days() == want, f"{raw!r} -> {S._lookback_days()}"
    os.environ.pop("SENTINEL_AUTO_MERGE_LOOKBACK_DAYS", None)


def test_sweep_heartbeat_is_not_counted_as_a_decision():
    """A heartbeat row must never look like a merge to any consumer."""
    S = _mod()
    logged: list[tuple] = []
    S._log_decision = lambda pr, d, r, e=None: logged.append((pr, d, r, e))
    S._log_sweep_heartbeat({"scanned": 0, "allowed": 0, "rejected": 0,
                            "merged": 0, "dry_run": False})
    assert len(logged) == 1
    pr, decision, reason, _extra = logged[0]
    assert pr == {"number": 0}          # sorts outside every real PR number
    assert decision == "sweep"          # not 'allow' -> excluded from the cap
    assert "scanned=0" in reason
