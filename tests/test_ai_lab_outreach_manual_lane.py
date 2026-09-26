"""
2026-09-26: the restarted partner outreach (composio, pipeworx, typingmind,
paces, transect) is sent by hand from Gmail and tracked in HubSpot. The
17:17Z cron's /auto-send mails ANY status='draft' row with an email and never
consults _TARGETS, so these slugs are excluded at both the auto-send SELECT
and the shared _perform_resend_send choke point.
"""
from __future__ import annotations

import pytest

import routes.ai_lab_outreach as alo


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


def _draft_row(slug):
    return (7, slug, "subj", "body", "https://x", "draft", None,
            f"hello@{slug}.example", None)


@pytest.fixture
def no_real_send(monkeypatch):
    monkeypatch.setenv("DCHUB_RESEND_API_KEY", "re_test")
    calls = []
    monkeypatch.setattr(alo, "_claim_gate",
                        lambda body: calls.append(body) or [], raising=True)
    return calls


def test_the_five_slugs_are_the_owner_approved_batch():
    assert alo._MANUAL_LANE_SLUGS == {
        "composio", "pipeworx", "typingmind", "paces", "transect"}


@pytest.mark.parametrize("force", [False, True])
def test_send_helper_refuses_a_manual_lane_slug_even_with_force(
        monkeypatch, no_real_send, force):
    conn = _Conn(row=_draft_row("composio"))
    monkeypatch.setattr(alo, "_db_conn", lambda: conn, raising=True)
    resp, status = alo._perform_resend_send(7, force=force)
    assert status == 409 and resp["error"] == "manual_lane_slug"
    # It stopped before the claim gate, so nothing past it (Resend) ran.
    assert no_real_send == []


def test_send_helper_still_reaches_the_gate_for_other_slugs(
        monkeypatch, no_real_send):
    """Guard-the-guard: the refusal above must be slug-specific, not a
    helper that now refuses everything."""
    conn = _Conn(row=_draft_row("coreweave"))
    monkeypatch.setattr(alo, "_db_conn", lambda: conn, raising=True)
    try:
        alo._perform_resend_send(7)
    except Exception:
        pass  # whatever happens after the gate is out of scope here
    assert no_real_send == ["body"], "a non-manual slug never reached the claim gate"


def test_auto_send_select_excludes_the_manual_slugs(monkeypatch):
    conn = _Conn(rows=[])
    monkeypatch.setattr(alo, "_db_conn", lambda: conn, raising=True)
    monkeypatch.setattr(alo, "_admin_authorized", lambda: True, raising=True)
    monkeypatch.setattr(alo, "_ensure_table", lambda: None, raising=True)
    from flask import Flask
    app = Flask(__name__)
    with app.test_request_context("/api/v1/admin/ai-lab-outreach/auto-send?limit=3",
                                  method="POST"):
        alo.auto_send()
    selects = [(sql, p) for sql, p in conn.executed if "DISTINCT ON" in sql]
    assert len(selects) == 1
    sql, params = selects[0]
    assert "NOT (target_slug = ANY(%s))" in sql
    assert set(params[0]) == set(alo._MANUAL_LANE_SLUGS)
    assert params[1] == 3
