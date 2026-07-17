"""founder_note.py — founder-voice founding welcome (2026-07-17).

Covers the safety rails without importing main.py or touching a DB:
kill switch, dry-run default, dedupe-reservation skip, armed send path,
template personalization, and route auth.
"""
import os

import founder_note


def test_template_is_plain_text_founder_voice():
    body = founder_note.NOTE_TEMPLATE.format(first_name="Sam")
    assert "Hi Sam," in body
    assert "Founding Member sign-up just came through" in body
    assert "https://dchub.cloud/playground" in body
    assert "602-214-3714" in body
    assert "jonathan@dchub.cloud" in body
    # It must read personal — no HTML.
    assert "<" not in body and "&mdash;" not in body


def test_kill_switch_blocks_everything(monkeypatch):
    monkeypatch.setenv("FOUNDER_NOTE_DISABLE", "1")
    out = founder_note.run_founder_note(armed=True)
    assert out["ok"] is True and out.get("disabled") is True
    # The webhook timer hook must also be inert.
    called = []
    monkeypatch.setattr(founder_note, "run_founder_note",
                        lambda **kw: called.append(1))
    founder_note.schedule_founder_note_after_conversion("x@example.com")
    assert not called


def test_dry_run_previews_without_sending(monkeypatch):
    monkeypatch.delenv("FOUNDER_NOTE_DISABLE", raising=False)
    cands = [{"email": "new@example.com", "source": "welcome_email_log",
              "converted_at": "2026-07-17T00:00:00+00:00"}]
    monkeypatch.setattr(founder_note, "find_candidates",
                        lambda *a, **k: list(cands))
    sends = []
    monkeypatch.setattr(founder_note, "_send_note",
                        lambda *a, **k: sends.append(a))
    out = founder_note.run_founder_note(armed=False)
    assert out["armed"] is False
    assert out["candidates"] == 1 and out["preview"] == cands
    assert out["sent"] == 0 and not sends


def test_armed_send_and_dedupe_skip(monkeypatch):
    monkeypatch.delenv("FOUNDER_NOTE_DISABLE", raising=False)
    monkeypatch.setattr(founder_note, "find_candidates", lambda *a, **k: [
        {"email": "fresh@example.com"},
        {"email": "already-noted@example.com"},
    ])
    monkeypatch.setattr(founder_note, "_first_name", lambda e: "Fresh")
    # fresh reserves row 42; already-noted is deduped (None).
    monkeypatch.setattr(
        founder_note, "_reserve",
        lambda email: 42 if email == "fresh@example.com" else None)
    finalized = {}
    monkeypatch.setattr(
        founder_note, "_finalize",
        lambda rid, status, mid=None: finalized.update(
            {"rid": rid, "status": status, "mid": mid}))
    monkeypatch.setattr(founder_note, "_send_note",
                        lambda email, first: "re_msg_123")
    out = founder_note.run_founder_note(armed=True)
    assert out["sent"] == 1
    assert out["skipped_dedupe"] == 1
    assert finalized == {"rid": 42, "status": "sent", "mid": "re_msg_123"}


def test_armed_send_failure_keeps_reservation_as_marker(monkeypatch):
    monkeypatch.delenv("FOUNDER_NOTE_DISABLE", raising=False)
    monkeypatch.setattr(founder_note, "find_candidates",
                        lambda *a, **k: [{"email": "fail@example.com"}])
    monkeypatch.setattr(founder_note, "_first_name", lambda e: "there")
    monkeypatch.setattr(founder_note, "_reserve", lambda email: 7)
    finalized = {}
    monkeypatch.setattr(
        founder_note, "_finalize",
        lambda rid, status, mid=None: finalized.update(
            {"rid": rid, "status": status}))
    monkeypatch.setattr(founder_note, "_send_note", lambda *a: None)
    out = founder_note.run_founder_note(armed=True)
    assert out["sent"] == 0 and out["errors"] == 1
    assert finalized == {"rid": 7, "status": "send_failed"}


def test_internal_emails_are_skipped(monkeypatch):
    monkeypatch.delenv("FOUNDER_NOTE_DISABLE", raising=False)
    monkeypatch.setattr(founder_note, "find_candidates",
                        lambda *a, **k: [{"email": "ops@dchub.cloud"}])
    out = founder_note.run_founder_note(armed=False)
    assert out["candidates"] == 0


def test_route_requires_admin_key(monkeypatch):
    from flask import Flask
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekrit")
    app = Flask(__name__)
    founder_note.setup_founder_note_routes(app)
    monkeypatch.setattr(founder_note, "run_founder_note",
                        lambda **kw: {"ok": True, "armed": kw.get("armed")})
    client = app.test_client()
    assert client.post("/api/v1/admin/founder-note/run").status_code == 401
    r = client.post("/api/v1/admin/founder-note/run",
                    headers={"X-Admin-Key": "sekrit"})
    assert r.status_code == 200 and r.get_json()["armed"] is False
    r = client.post("/api/v1/admin/founder-note/run?confirm=1",
                    headers={"X-Admin-Key": "sekrit"})
    assert r.get_json()["armed"] is True
