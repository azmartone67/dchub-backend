"""r-closeloop (2026-07-28) — the auto-path must be able to give up.

WHY THIS EXISTS
---------------
The Smithery listing sat two revisions stale while the white-glove loop
reported clean every day. The loop was not broken in the ordinary sense —
every stage did what it was written to do:

  * white-glove DETECTED the drift                     ✓
  * classified Smithery as AUTO_PATH_REGISTRIES        ✓
  * fired auto_resubmit_listing("smithery")            ✓
  * -> _submitter_manifest_refresh() returned ok=True  ✗ while doing nothing
  * so Smithery was excluded from the human-gated issue as "handled"

Membership in AUTO_PATH_REGISTRIES is a PROMISE that something automated
will fix the listing, and the human-gated branch excludes those registries
on the strength of that promise. Nothing checked whether it was kept, so a
submitter that returned success unconditionally made the drift invisible
rather than fixed. The registry fell in the gap: too automated for the
human loop, and the automation was a no-op.

The fix is not "try harder" — it is that the auto-path must be ABLE TO
REPORT THAT IT CANNOT FINISH, and that report must move the registry into
the human queue. These tests pin that, including the two ways it must NOT
fire: a stale upstream manifest is our own job (heal it, don't escalate),
and an unreadable manifest is not evidence a registry's crawler is broken.

Run:  python3 -m pytest tests/test_white_glove_auto_path_closure.py -v
"""
from __future__ import annotations

import inspect

import pytest

from routes import mcp_presence_crawler as pc
from routes.white_glove_propagation import AUTO_PATH_REGISTRIES


# ── the submitter's verdict ───────────────────────────────────────────
def test_escalates_when_our_manifest_is_already_canon(monkeypatch):
    """Upstream is correct and the listing is still wrong -> the re-crawl
    is not landing. Waiting cannot fix that, so hand it to a human."""
    monkeypatch.setattr(pc, "_upstream_manifest_matches_canon",
                        lambda: (True, "manifest carries canon"))
    r = pc._submitter_manifest_refresh("smithery")
    assert r["escalate"] is True, "a listing we cannot fix must reach a human"
    assert r["ok"] is False, "claiming ok here is how the drift stayed invisible"


def test_does_not_escalate_when_our_own_manifest_is_stale(monkeypatch):
    """Our half isn't done yet — heal the manifest and let the crawl run.
    Escalating here would page a human for our own unfinished work."""
    monkeypatch.setattr(pc, "_upstream_manifest_matches_canon",
                        lambda: (False, "manifest missing canon facilities=12,650+"))
    r = pc._submitter_manifest_refresh("smithery")
    assert not r.get("escalate"), "the auto-path can still fix this one"
    assert r["ok"] is True
    assert r["requires_manifest_update"] is True


def test_unreadable_manifest_fails_closed_to_the_manifest_branch(monkeypatch):
    """A failed HTTP call is not evidence that a registry is broken.
    Fail-closed means 'heal the manifest', never 'escalate to a human'."""
    monkeypatch.setattr(pc.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    ok, detail = pc._upstream_manifest_matches_canon()
    assert ok is False
    r_ok, _ = (False, None)
    monkeypatch.setattr(pc, "_upstream_manifest_matches_canon", lambda: (ok, detail))
    r = pc._submitter_manifest_refresh("smithery")
    assert not r.get("escalate"), "network failure must not page a human"


def test_every_auto_path_registry_can_report_failure():
    """The promise is per-registry, so the escape hatch must be too. A
    registry whose submitter can only ever say ok=True re-creates the
    exact gap this module exists to close."""
    src = inspect.getsource(pc._submitter_manifest_refresh)
    assert "escalate" in src, (
        "_submitter_manifest_refresh has no failure path — every registry "
        "routed through it would again be silently absorbed")
    assert AUTO_PATH_REGISTRIES, "auto-path set is empty — nothing to guard"


# ── the loop must act on that verdict ─────────────────────────────────
def test_white_glove_routes_escalated_registries_to_the_human_queue():
    """Detecting 'cannot finish' is useless if the caller still filters the
    registry out by membership. Derived from the source so a refactor that
    drops the escalation cannot leave this test passing.
    """
    from routes import white_glove_propagation as wg
    src = inspect.getsource(wg.run_white_glove_propagation)
    assert 'r.get("escalate")' in src, "the escalate verdict is never read"
    # The human-gated filter must consider escalation, not membership alone.
    assert "or n in escalated" in src, (
        "human_drifted still filters on AUTO_PATH membership alone — an "
        "escalated registry would be dropped exactly as Smithery was")


def test_smithery_is_still_an_auto_path_registry():
    """Guards the fix against the wrong remedy. Deleting Smithery from the
    auto-path set would also 'fix' the symptom, at the cost of giving up an
    automated channel that does work once the manifest is healed."""
    assert "smithery" in AUTO_PATH_REGISTRIES


# ── the copy the loop hands a human must not itself be stale ──────────
def test_paste_ready_copy_uses_the_same_tool_count_the_detector_checks(monkeypatch):
    """The generator used the PINNED advertised count while the detector
    compared against the LIVE count. They disagreed (80 vs 81), so the loop
    emitted paste-ready copy that its own detector flagged the next morning
    — it could not converge. One quantity, one origin."""
    monkeypatch.setattr(pc, "_our_actual_tool_count", lambda: 99)
    desc = pc._build_canonical_description("smithery")
    assert "99" in desc, (
        "the corrected copy ignores the live tool count, so pasting it "
        "re-introduces the drift the loop just reported")


def test_description_survives_an_unavailable_live_count(monkeypatch):
    """Fail-soft: if the live probe is down we still emit copy, just from
    the pinned floor. An exception here would take the whole daily job out."""
    monkeypatch.setattr(pc, "_our_actual_tool_count", lambda: None)
    desc = pc._build_canonical_description("smithery")
    assert desc and "tools" in desc.lower()
