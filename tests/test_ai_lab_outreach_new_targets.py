"""
2026-09-26 restart of partner outreach (owner decision): the 9 AI-lab /
GPU-cloud targets are retired and the lane pitches agent builders, MCP clients
and energy-agent teams instead.

The autopilot (crawler_scheduler, 17:17Z) mails the latest stored draft per
slug with no human step, so "retired" has to hold at every door a draft can
pass: drafting one, drafting a batch, and sending one already in the table.
(The first batch itself is sent by hand -- _MANUAL_LANE_SLUGS, be#5651 -- so
today no known slug can be mailed from this module at all.)
"""
from __future__ import annotations

import re

import pytest
from flask import Flask

import routes.ai_lab_outreach as alo

_RETIRED = {"perplexity", "groq", "gemini", "mistral", "nvidia",
            "coreweave", "lambda", "tensorwave", "core42"}
_FIRST_BATCH = {"composio", "pipeworx", "typingmind", "paces", "transect"}


# ── the target list ─────────────────────────────────────────────────────────

def test_the_nine_original_targets_are_retired_and_only_they_are():
    retired = {t["slug"] for t in alo._TARGETS if t.get("retired")}
    assert retired == _RETIRED
    # ★ They span three categories. A category filter on 'ai_lab' alone would
    # have left 5 of them mailable.
    cats = {t["category"] for t in alo._TARGETS if t["slug"] in _RETIRED}
    assert cats == {"ai_lab", "gpu_cloud", "hyperscaler_oem"}


def test_the_active_list_is_the_approved_first_batch():
    assert alo._ACTIVE_SLUGS == _FIRST_BATCH
    assert alo._ACTIVE_CATEGORIES == {"agent_builder", "mcp_client", "energy_agent"}
    for t in alo._ACTIVE_TARGETS:
        assert t["target_email"] and "@" in t["target_email"], t["slug"]
        assert t["contact_url"].startswith("https://"), t["slug"]


def test_active_pitches_carry_no_literal_figures():
    """Figures may enter the body only through _canon_public(). A digit in the
    per-target prose is how the August false claims got through."""
    for t in alo._ACTIVE_TARGETS:
        assert not re.search(r"\d", t["value_pitch"]), (t["slug"], t["value_pitch"])


# ── the copy ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("slug", sorted(_FIRST_BATCH))
def test_the_claim_gate_passes_every_new_pitch(slug):
    target = next(t for t in alo._TARGETS if t["slug"] == slug)
    _subject, body = alo._draft_pitch(target)
    assert alo._canon_public()["facilities"] in body
    assert alo._claim_gate(body) == []


@pytest.mark.parametrize("slug", sorted(_FIRST_BATCH))
def test_the_pitch_drops_the_key_the_partner_link_and_the_unverifiable_claims(slug, monkeypatch):
    # If the old pre-mint path were still wired, this would hand it a key.
    import routes.partner_key_issuer as pki
    monkeypatch.setattr(pki, "_issue_internal",
                        lambda **kw: {"ok": True, "key": "dch_live_SENTINELKEY"},
                        raising=False)
    target = next(t for t in alo._TARGETS if t["slug"] == slug)
    _subject, body = alo._draft_pitch(target)
    for banned in ("dch_live_", "SENTINELKEY", "/partners/", "keys/claim",
                   "cited by", "the only", "23+", "17 high-value", "calls/day"):
        assert banned not in body, (slug, banned)
    assert "https://dchub.cloud/connect" in body


# ── the doors ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(alo, "_admin_authorized", lambda: True)
    monkeypatch.setattr(alo, "_ensure_table", lambda: None)
    monkeypatch.setattr(alo, "_db_conn", lambda: None)
    app = Flask(__name__)
    app.register_blueprint(alo.ai_lab_outreach_bp)
    return app.test_client()


@pytest.mark.parametrize("slug", sorted(_RETIRED))
def test_a_retired_target_cannot_be_drafted(client, slug):
    r = client.post(f"/api/v1/admin/ai-lab-outreach/draft/{slug}")
    assert r.status_code == 410
    assert r.get_json()["error"] == "target_retired"


def test_an_active_target_can_be_drafted(client):
    r = client.post("/api/v1/admin/ai-lab-outreach/draft/composio")
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_draft_all_requires_a_category(client):
    r = client.post("/api/v1/admin/ai-lab-outreach/draft-all")
    assert r.status_code == 400
    assert r.get_json()["error"] == "category_required"
    for retired_cat in ("ai_lab", "gpu_cloud", "hyperscaler_oem"):
        r = client.post(f"/api/v1/admin/ai-lab-outreach/draft-all?category={retired_cat}")
        assert r.status_code == 400, retired_cat


def test_draft_all_drafts_one_category_and_nothing_else(client):
    r = client.post("/api/v1/admin/ai-lab-outreach/draft-all?category=energy_agent")
    assert r.status_code == 200
    slugs = {d["slug"] for d in r.get_json()["drafts"]}
    assert slugs == {"paces", "transect"}


# ── the send choke point ────────────────────────────────────────────────────

class _Cur:
    def __init__(self, conn):
        self.conn = conn
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
    def fetchone(self):
        return self.conn.row
    def fetchall(self):
        return self.conn.rows


class _Conn:
    def __init__(self, row=None, rows=None):
        self.row, self.rows, self.executed = row, rows or [], []
    def cursor(self):
        return _Cur(self)
    def commit(self):
        pass
    def rollback(self):
        pass
    def close(self):
        pass


def _row(slug):
    return (7, slug, "subj", "body", "https://x", "draft", None,
            "someone@example.com", None)


@pytest.fixture
def no_resend(monkeypatch):
    monkeypatch.setenv("DCHUB_RESEND_API_KEY", "re_test_not_real")
    import requests
    def _boom(*a, **k):
        raise AssertionError("Resend was called")
    monkeypatch.setattr(requests, "post", _boom)


@pytest.mark.parametrize("force", [False, True])
def test_a_stored_draft_for_a_retired_target_is_never_sent(monkeypatch, no_resend, force):
    conn = _Conn(row=_row("perplexity"))
    monkeypatch.setattr(alo, "_db_conn", lambda: conn)
    monkeypatch.setattr(alo, "_claim_gate", lambda body: [])
    resp, status = alo._perform_resend_send(7, force=force)
    assert status == 410 and resp["error"] == "target_retired"
    # refused before any write: the row is left as it was
    assert not any(sql.lstrip().upper().startswith("UPDATE") for sql, _ in conn.executed)


def test_auto_send_selects_only_active_slugs(client, monkeypatch):
    conn = _Conn(rows=[])
    monkeypatch.setattr(alo, "_db_conn", lambda: conn)
    r = client.post("/api/v1/admin/ai-lab-outreach/auto-send?limit=3")
    assert r.status_code == 200
    sql, params = conn.executed[0]
    assert "AND target_slug = ANY(%s)" in sql
    assert set(params[1]) == _FIRST_BATCH and params[-1] == 3


def test_a_gate_blocked_draft_returns_409_not_a_name_error(monkeypatch, no_resend):
    """`logger` was used on this path and never defined, so a blocked draft
    raised NameError and aborted the whole /auto-send loop."""
    # composio is manual-lane (stops before the gate), so reach the gate with
    # a hypothetical active, non-manual slug.
    conn = _Conn(row=_row("example-active"))
    monkeypatch.setattr(alo, "_db_conn", lambda: conn)
    monkeypatch.setattr(alo, "_ACTIVE_SLUGS",
                        alo._ACTIVE_SLUGS | {"example-active"})
    monkeypatch.setattr(alo, "_claim_gate",
                        lambda body: [{"kind": "over_claim", "detail": "x"}])
    resp, status = alo._perform_resend_send(7)
    assert status == 409 and resp["error"] == "claims_failed_verification"
