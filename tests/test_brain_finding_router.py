"""Tests for routes/brain_finding_router.py — pure, no DB, no network.

The classification is the load-bearing logic: layer4's honest backlog count
and the propose-stage routing view both rest on it. Every bucket rule is
asserted with an input that FAILS if the rule is inverted or dropped
(verify-a-guard: each test names the mutation it kills).
"""

import types

from routes import brain_finding_router as fr


def _item(issue, url="dchub://cron/x", detail="", count=1):
    return {"issue": issue, "url": url, "detail": detail, "count": count}


# ── bucket rules ─────────────────────────────────────────────────────────

def test_no_outcome_is_active_backlog():
    # Kills: treating unknown findings as triaged (would hide real work).
    out = fr.classify_items([_item("stale_loop dcpi")], outcomes={})
    assert [f["issue"] for f in out["active"]] == ["stale_loop dcpi"]
    assert out["counts"] == {"active": 1, "operator_config": 0,
                             "mcp_server": 0, "terminal": 0}


def test_nonterminal_outcome_stays_active():
    # Kills: subtracting anything with ANY outcome (proposed is progress,
    # not triage-out).
    it = _item("slow_loop press")
    oc = {(it["issue"][:200], it["url"]): "proposed"}
    out = fr.classify_items([it], outcomes=oc)
    assert len(out["active"]) == 1 and len(out["terminal"]) == 0


def test_config_not_code_routes_to_operator():
    it = _item("cron_schedule_collision daily", url="30 4 * * *")
    oc = {(it["issue"][:200], it["url"]): "config_not_code"}
    out = fr.classify_items([it], outcomes=oc)
    assert len(out["operator_config"]) == 1
    assert len(out["active"]) == 0


def test_availability_routes_to_operator():
    it = _item("endpoint_unreachable /markets")
    oc = {(it["issue"][:200], it["url"]): "not_code_availability"}
    out = fr.classify_items([it], outcomes=oc)
    assert len(out["operator_config"]) == 1


def test_refused_with_mcp_hint_routes_to_mcp_server():
    # The two live QA reds are exactly this class.
    it = _item("caller_tier=pro served to anonymous caller",
               url="get_energy_prices")
    oc = {(it["issue"][:200], it["url"]): "refused"}
    out = fr.classify_items([it], outcomes=oc)
    assert len(out["mcp_server"]) == 1
    assert out["mcp_server"][0]["last_outcome"] == "refused"


def test_no_source_map_with_server_mjs_hint_routes_to_mcp_server():
    it = _item("quota meter frozen", detail="server.mjs trial counter")
    oc = {(it["issue"][:200], it["url"]): "no_source_map"}
    out = fr.classify_items([it], outcomes=oc)
    assert len(out["mcp_server"]) == 1


def test_refused_without_hint_is_terminal_not_mcp():
    # Kills: routing every refusal to the mcp-server repo (wrong-repo spam).
    it = _item("unsafe_db_conn_pattern", url="ai_interconnection.py")
    oc = {(it["issue"][:200], it["url"]): "refused"}
    out = fr.classify_items([it], outcomes=oc)
    assert len(out["mcp_server"]) == 0 and len(out["terminal"]) == 1


def test_terminal_ack_and_permafail_prefix_are_terminal():
    a = _item("old finding a")
    b = _item("old finding b")
    oc = {(a["issue"][:200], a["url"]): "terminal_acknowledged",
          (b["issue"][:200], b["url"]): "skipped_permafail:refused"}
    out = fr.classify_items([a, b], outcomes=oc)
    assert len(out["terminal"]) == 2 and len(out["active"]) == 0


def test_long_label_matches_truncated_persistence_key():
    # layer5 persists (issue[:200], url); the router must match the same key
    # or every long-labelled finding silently reads as untriaged.
    label = "x" * 260
    it = _item(label)
    oc = {(label[:200], it["url"]): "terminal_acknowledged"}
    out = fr.classify_items([it], outcomes=oc)
    assert len(out["terminal"]) == 1


def test_triaged_out_count_matches_classification():
    items = [_item("a"), _item("b"), _item("c")]
    oc = {("b", items[1]["url"]): "refused",
          ("c", items[2]["url"]): "config_not_code"}
    assert fr.triaged_out_count(items, outcomes=oc) == 2


# ── the cross-repo issue write (mocked transport) ───────────────────────

class _Resp:
    def __init__(self, code, payload=None):
        self.status_code = code
        self._payload = payload if payload is not None else []

    def json(self):
        return self._payload


class _FakeGH:
    """requests-shaped stub recording the calls the router makes."""

    def __init__(self, open_issues=None):
        self.open_issues = open_issues or []
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return _Resp(200, self.open_issues)

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw.get("json")))
        return _Resp(201, {"number": 7})

    def patch(self, url, **kw):
        self.calls.append(("PATCH", url, kw.get("json")))
        return _Resp(200, {})


def _classification_with_mcp():
    return {"mcp_server": [
        {"issue": "caller_tier=pro to anon", "url": "get_energy_prices",
         "last_outcome": "refused", "count": 3}]}


def test_sync_creates_issue_when_absent(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.delenv("FINDING_ROUTER_DISABLE", raising=False)
    monkeypatch.setattr(fr, "_state_get", lambda k: None)
    monkeypatch.setattr(fr, "_state_set", lambda k, v: True)
    gh = _FakeGH(open_issues=[])
    out = fr.sync_mcp_issue(classification=_classification_with_mcp(),
                            force=True, session=gh)
    assert out == {"ok": True, "action": "created", "number": 7, "routed": 1}
    assert gh.calls[0][0] == "GET" and gh.calls[1][0] == "POST"


def test_sync_updates_existing_issue_never_duplicates(monkeypatch):
    # Kills: dropping the dedup (a new issue every day = board spam).
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr(fr, "_state_get", lambda k: None)
    monkeypatch.setattr(fr, "_state_set", lambda k, v: True)
    gh = _FakeGH(open_issues=[
        {"number": 3, "title": "[brain-route] Findings owned by ..."}])
    out = fr.sync_mcp_issue(classification=_classification_with_mcp(),
                            force=True, session=gh)
    assert out["action"] == "updated" and out["number"] == 3
    assert not any(c[0] == "POST" for c in gh.calls)


def test_sync_skips_prs_in_issue_listing(monkeypatch):
    # GitHub's /issues listing includes PRs; matching one would PATCH a PR.
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr(fr, "_state_get", lambda k: None)
    monkeypatch.setattr(fr, "_state_set", lambda k, v: True)
    gh = _FakeGH(open_issues=[
        {"number": 9, "title": "[brain-route] x", "pull_request": {}}])
    out = fr.sync_mcp_issue(classification=_classification_with_mcp(),
                            force=True, session=gh)
    assert out["action"] == "created"


def test_sync_daily_guard_skips_without_force(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    monkeypatch.setattr(fr, "_state_get", lambda k: today)
    gh = _FakeGH()
    out = fr.sync_mcp_issue(classification=_classification_with_mcp(),
                            force=False, session=gh)
    assert out.get("skipped") == "already_synced_today"
    assert gh.calls == []


def test_sync_no_findings_writes_nothing(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr(fr, "_state_get", lambda k: None)
    gh = _FakeGH()
    out = fr.sync_mcp_issue(classification={"mcp_server": []},
                            force=True, session=gh)
    assert out.get("skipped") == "no_mcp_findings"
    assert gh.calls == []


def test_kill_switch_blocks_the_write(monkeypatch):
    monkeypatch.setenv("FINDING_ROUTER_DISABLE", "1")
    gh = _FakeGH()
    out = fr.sync_mcp_issue(classification=_classification_with_mcp(),
                            force=True, session=gh)
    assert out.get("skipped") == "FINDING_ROUTER_DISABLE=1"
    assert gh.calls == []


def test_issue_body_escapes_pipes_and_caps_rows():
    rows = [{"issue": "a|b", "url": "u", "last_outcome": "refused",
             "count": 1} for _ in range(45)]
    body = fr._issue_body(rows)
    assert "a\\|b" in body
    assert "and 5 more" in body
    assert fr._BODY_MARKER in body
