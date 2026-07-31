"""The sentinel→PR-body→GATE_SENTINEL_DERIVED seam, which has never run live.

`page_persistent_5xx:<path>` findings have never been written in production
(2,948 brain_findings rows, zero from the site_sentinel detector), so the L5
sentinel auto-merge path has never been exercised end to end. Gate 1 reads the
finding key out of the PR **body**; the drafter puts it there only via the
`**Loop:**` line. If those two drift apart the failure is invisible until the
day a real 5xx finally fires — and then the fix silently never merges.

These tests pin the seam from both sides:
  1. the drafter still emits a Loop line carrying `loop_name` (twin-drift guard,
     read from the REAL source — not a copy of the template);
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


def _drafter_pr_body_template() -> str:
    """Pull the real pr_body f-string out of brain_backlog_admin source.

    Uses ast so a moved/renamed function fails loudly here instead of silently
    matching nothing. An empty parse would pass every assertion, so the node
    count is asserted before anything is read out of it.
    """
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    assert len(tree.body) > 0, "brain_backlog_admin.py parsed to an empty tree"

    joined: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pr_body"
                   for t in node.targets):
            continue
        # The template is a concatenation of f-strings; collect every literal
        # chunk plus the names of the interpolated expressions.
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                joined.append(sub.value)
            elif isinstance(sub, ast.Call):
                joined.append(ast.unparse(sub))
    assert joined, "no pr_body assignment found in brain_backlog_admin.py"
    return "".join(joined)


def test_drafter_still_emits_the_loop_line_carrying_loop_name():
    """Twin-drift guard: gate 1 can only see the finding key if the drafter
    still writes loop_name into the body. Drop the Loop line and the sentinel
    lane goes permanently dead with no other symptom."""
    tpl = _drafter_pr_body_template()
    assert "**Loop:**" in tpl, (
        "brain_backlog_admin no longer emits a '**Loop:**' line — "
        "GATE_SENTINEL_DERIVED reads the finding key from it, so the sentinel "
        "auto-merge lane is now unreachable. Update both sides together."
    )
    assert "loop_name" in tpl, (
        "the Loop line no longer interpolates loop_name — the finding key "
        "never reaches the PR body and gate 1 rejects every sentinel PR."
    )


def test_gate1_accepts_a_real_drafter_body_for_a_sentinel_finding():
    """The day a real page_persistent_5xx fires, this is the path it takes."""
    S = _mod()
    path = "/api/v1/iso/zones"
    # Rendered exactly as brain_backlog_admin renders it.
    body = (
        "## Brain Layer-5 auto-proposed fix\n\n"
        "**Proposal:** #4242\n"
        f"**Loop:** `page_persistent_5xx:{path}`\n"
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
        "**Loop:** `content_publisher.py`\n"
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
