"""Owner-gated AI surfaces must not drive the code-fix loop.

/.well-known/mcp.json and /.well-known/mcp/server-card.json are served at the
edge by the off-repo Cloudflare zone worker. Measured 2026-09-12 by reading
x-dc-worker-version PER PATH within one second:

  /.well-known/mcp.json              4.9.66-fallback-tools-90     <- zone worker
  /.well-known/mcp/server-card.json  4.9.66-fallback-tools-90     <- zone worker
  /AGENTS.md                         4.99.0-revert-edge-...-09-11 <- repo-served
  /integrations/chatgpt/instructions.txt  4.99.0-revert-edge-...  <- repo-served

Their backend routes return 200 but their bytes never reach the public, so
their drift cannot be fixed by a code change. Emitting it with the same shape
as fixable drift made the brain propose the identical unlandable "render from
canon" fix 11 times and approve it every time: on 2026-09-12, 11 of 15
self-directed agenda items were this one class.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_surface_sentinel as S


def test_only_the_two_measured_surfaces_are_owner_gated():
    assert S._OWNER_GATED_SURFACES == {"mcp_json", "server_card"}


def test_repo_served_surfaces_are_not_silenced():
    """agents_md carries the SAME per-day literals but is repo-served, so its
    drift is a real code fix and must keep driving the loop."""
    for repo_served in ("agents_md", "chatgpt_instructions", "grok_config",
                        "mcp_server_json", "llms_txt", "llms_full",
                        "openapi", "robots", "connect", "ai_page"):
        assert repo_served not in S._OWNER_GATED_SURFACES, repo_served


def test_every_owner_gated_key_is_a_real_registered_surface():
    """A typo'd key would silence nothing and go unnoticed."""
    registered = {k for k, _u, _kind in S._SURFACES}
    assert S._OWNER_GATED_SURFACES <= registered, (
        S._OWNER_GATED_SURFACES - registered)


def _audit(monkeypatch, key, body):
    """Drive the REAL _audit_surface so its own add() closure does the
    tagging — not a reimplementation of it."""
    monkeypatch.setattr(S, "_fetch", lambda url, timeout=15: (200, body))
    canon = {"stale_markers": [], "fake_tool_denylist": [], "crawlers_required": [],
             "free_tier_calls_per_day": 10, "version": "9.9.9",
             "starter_calls_per_month": 6000, "developer_calls_per_month": 15000,
             "pro_calls_per_month": 60000}
    url = next(u for k, u, _ in S._SURFACES if k == key)
    kind = next(kd for k, _u, kd in S._SURFACES if k == key)
    return S._audit_surface(key, url, kind, canon)


# The literal that is live on BOTH mcp.json and AGENTS.md today.
_BODY = "starter tier: 200 calls/day for agents"


def test_an_owner_gated_drift_is_tagged_for_an_owner_action(monkeypatch):
    res = _audit(monkeypatch, "mcp_json", _BODY)
    d = [x for x in res["drifts"] if x["field"] == "starter_period"]
    assert d, res["drifts"]
    assert d[0]["owner_gated"] is True
    assert d[0]["fix_action"] == "owner-action"


def test_the_same_drift_on_a_repo_served_surface_stays_a_code_fix(monkeypatch):
    """agents_md carries the identical literal and IS fixable in the repo."""
    res = _audit(monkeypatch, "agents_md", _BODY)
    d = [x for x in res["drifts"] if x["field"] == "starter_period"]
    assert d, res["drifts"]
    assert d[0]["owner_gated"] is False
    assert d[0]["fix_action"] == "update-from-canon"


def test_every_drift_carries_the_tag(monkeypatch):
    """An untagged drift would reach the writer and be treated as fixable."""
    for key in ("mcp_json", "agents_md", "chatgpt_instructions"):
        res = _audit(monkeypatch, key, _BODY + " 3 calls/day")
        for d in res["drifts"]:
            assert "owner_gated" in d and "fix_action" in d, (key, d)


def test_owner_gated_drift_goes_to_wont_fix_repo_served_stays_open():
    """wont_fix is the existing exemption honoured by
    brain_findings_writer._maybe_quarantine_runaway, so a gated finding stays
    visible but leaves the autopilot's code-fix path."""
    assert S.finding_status_for({"owner_gated": True}) == "wont_fix"
    assert S.finding_status_for({"owner_gated": False}) == "open"


def test_an_untagged_drift_defaults_to_open_not_silenced():
    """Fail OPEN: a drift the sentinel forgot to tag must keep driving the
    loop, never be silently benched."""
    assert S.finding_status_for({}) == "open"
    assert S.finding_status_for({"field": "x"}) == "open"


def test_the_owner_item_names_every_gated_field_and_no_repo_served_one():
    audit = {"surfaces": [
        {"surface": "mcp_json", "drifts": [
            {"field": "pro_period", "owner_gated": True},
            {"field": "starter_period", "owner_gated": True}]},
        {"surface": "server_card", "drifts": [
            {"field": "free_tier_anon", "owner_gated": True}]},
        {"surface": "agents_md", "drifts": [
            {"field": "developer_period", "owner_gated": False}]},
    ]}
    fields = S.owner_gated_fields(audit)
    assert fields == ["mcp_json:pro_period", "mcp_json:starter_period",
                      "server_card:free_tier_anon"]
    assert not any("agents_md" in f for f in fields), fields


def test_no_gated_drift_means_no_owner_item():
    audit = {"surfaces": [{"surface": "agents_md", "drifts": [
        {"field": "developer_period", "owner_gated": False}]}]}
    assert S.owner_gated_fields(audit) == []


def test_owner_gated_fields_survives_a_malformed_audit():
    for bad in ({}, {"surfaces": None}, {"surfaces": [{"drifts": None}]}, None):
        assert S.owner_gated_fields(bad) == []


def test_the_end_to_end_tagging_reaches_the_status_decision(monkeypatch):
    """The whole chain: _audit_surface tags -> finding_status_for benches.
    Pins that the two halves agree, which a tag rename would break."""
    gated = _audit(monkeypatch, "mcp_json", _BODY)["drifts"]
    repo = _audit(monkeypatch, "agents_md", _BODY)["drifts"]
    assert gated and all(S.finding_status_for(d) == "wont_fix" for d in gated)
    assert repo and all(S.finding_status_for(d) == "open" for d in repo)
