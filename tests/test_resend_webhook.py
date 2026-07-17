"""routes/resend_webhook.py — delivery-truth ingest (2026-07-17).

Exercises signature enforcement, payload guards, event persistence, and the
admin read route — against a fake DB connection (never imports main.py).
"""
import base64
import hashlib
import hmac
import json

from flask import Flask

from routes import resend_webhook


class FakeCursor:
    def __init__(self, store):
        self.store = store

    def execute(self, sql, params=None):
        if sql.strip().upper().startswith("INSERT"):
            self.store.append(params)
        self._rows = []

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def close(self):
        pass


class FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return FakeCursor(self.store)

    def close(self):
        pass


def _app(monkeypatch, store):
    monkeypatch.setattr(resend_webhook, "_get_conn",
                        lambda: FakeConn(store))
    app = Flask(__name__)
    app.register_blueprint(resend_webhook.resend_webhook_bp)
    return app.test_client()


EVENT = {"type": "email.delivered",
         "created_at": "2026-07-17T12:00:00.000Z",
         "data": {"email_id": "re_abc123", "to": ["Buyer@Example.com"],
                  "subject": "Welcome to DC Hub — and thank you",
                  "created_at": "2026-07-17T11:59:58.000Z"}}


def test_event_persisted_without_secret(monkeypatch):
    monkeypatch.delenv("RESEND_WEBHOOK_SECRET", raising=False)
    store = []
    client = _app(monkeypatch, store)
    r = client.post("/api/v1/webhooks/resend", data=json.dumps(EVENT),
                    headers={"svix-id": "msg_1"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert len(store) == 1
    svix_id, etype, email, mid, subject, occurred, verified, payload = store[0]
    assert svix_id == "msg_1"
    assert etype == "email.delivered"
    assert email == "buyer@example.com"
    assert mid == "re_abc123"
    assert verified is False
    assert "re_abc123" in payload


def test_bad_json_400(monkeypatch):
    client = _app(monkeypatch, [])
    monkeypatch.delenv("RESEND_WEBHOOK_SECRET", raising=False)
    assert client.post("/api/v1/webhooks/resend",
                       data=b"not json").status_code == 400


def test_oversize_413(monkeypatch):
    client = _app(monkeypatch, [])
    monkeypatch.delenv("RESEND_WEBHOOK_SECRET", raising=False)
    assert client.post("/api/v1/webhooks/resend",
                       data=b"x" * (65 * 1024)).status_code == 413


def test_signature_enforced_when_secret_set(monkeypatch):
    raw_key = b"0123456789abcdef0123456789abcdef"
    secret = "whsec_" + base64.b64encode(raw_key).decode()
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", secret)
    store = []
    client = _app(monkeypatch, store)
    body = json.dumps(EVENT).encode()
    sid, ts = "msg_2", "1789000000"
    good = base64.b64encode(hmac.new(
        raw_key, f"{sid}.{ts}.".encode() + body, hashlib.sha256
    ).digest()).decode()

    # wrong signature -> 401, nothing stored
    r = client.post("/api/v1/webhooks/resend", data=body,
                    headers={"svix-id": sid, "svix-timestamp": ts,
                             "svix-signature": "v1,AAAA"})
    assert r.status_code == 401 and not store

    # correct signature -> stored with verified=True
    r = client.post("/api/v1/webhooks/resend", data=body,
                    headers={"svix-id": sid, "svix-timestamp": ts,
                             "svix-signature": f"v1,{good}"})
    assert r.status_code == 200
    assert len(store) == 1 and store[0][6] is True


def test_admin_events_requires_key(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekrit")
    client = _app(monkeypatch, [])
    assert client.get("/api/v1/admin/email-events").status_code == 401
    r = client.get("/api/v1/admin/email-events",
                   headers={"X-Admin-Key": "sekrit"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
