"""
tests/test_media_outreach_security.py — Media outreach PII / admin-gate security
(2026-06-19).

These tests fence the LIVE unauthenticated PII leaks + the broken (inverted)
admin gate fixed in routes/media_outreach.py. The media_outreach_log + the
media_pitch_drafts tables carry JOURNALIST recipient emails + names, and the
/media/outreach dashboard renders them — so EVERY one of these endpoints must be
admin-gated, FAIL-CLOSED.

The single regression that must never come back: the old
`if _ADMIN_KEY and provided != _ADMIN_KEY` inverted gate, which (a) let EVERYONE
through when no admin key was configured, and (b) proceeded when a key WAS set but
no header was sent. The canonical _admin_ok() denies in both cases.

For EACH fixed endpoint we assert:
  · DENIED (403 / 401) WITHOUT a key, and
  · ALLOWED (200) WITH a valid admin key (env + header) or a stubbed _admin_ok.
Plus:
  · the gate FAILS CLOSED when NO admin-key env is configured (the inverted-gate
    bug), and
  · stage-weekly STILL accepts the legitimate internal cron (a dchub-* UA).
  · the PII responses carry Cache-Control: no-store, private (never edge-cached),
  · the dashboard no longer advertises `public, max-age=600`,
  · the journalists endpoint stays PUBLIC by design (spec item — not PII-gated).

Nothing here touches Postgres: _conn / _ensure_schema* are stubbed, and the gate
short-circuits before any DB touch on the denied paths.
"""
import pytest

mo = pytest.importorskip("routes.media_outreach")


# Env names the canonical gate reads. We clear ALL of them in fixtures so a stray
# real env var on the runner can't make a "no key configured" test pass falsely.
_KEY_ENVS = ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "INTERNAL_KEY")


def _clear_keys(monkeypatch):
    for n in _KEY_ENVS:
        monkeypatch.delenv(n, raising=False)


@pytest.fixture()
def client(monkeypatch):
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(mo.media_outreach_bp)
    # Never touch Postgres: schema-init is a no-op and the gate runs before any
    # DB read on the denied paths.
    monkeypatch.setattr(mo, "_ensure_schema", lambda: None)
    monkeypatch.setattr(mo, "_ensure_drafts_schema", lambda: None)
    return app.test_client()


# ════════════════════════════════════════════════════════════════════
#  _admin_ok — the canonical fail-closed gate (the inverted-gate fence)
# ════════════════════════════════════════════════════════════════════
def test_admin_ok_denies_when_no_key_configured(monkeypatch):
    """THE regression fence: with NO admin key in the env the gate must DENY —
    even when a request carries some key. (The old inverted gate ALLOWED here.)"""
    _clear_keys(monkeypatch)
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    # No env key + a request that sends a key header -> still denied.
    with app.test_request_context("/", headers={"X-Admin-Key": "anything"}):
        assert mo._admin_ok() is False
    # No env key + no key sent -> denied.
    with app.test_request_context("/"):
        assert mo._admin_ok() is False


def test_admin_ok_denies_when_key_configured_but_none_provided(monkeypatch):
    """Key configured but the request sends nothing -> DENY (the other half of the
    inverted-gate bug, which used to PROCEED here)."""
    _clear_keys(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekret")
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    with app.test_request_context("/"):
        assert mo._admin_ok() is False
    with app.test_request_context("/?admin_key=wrong"):
        assert mo._admin_ok() is False


def test_admin_ok_allows_valid_key_header_query_and_cookie(monkeypatch):
    """A matching key via header, ?admin_key=, OR the dchub_innov_key cookie all
    satisfy the gate (the browser-openable forms)."""
    _clear_keys(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekret")
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    with app.test_request_context("/", headers={"X-Admin-Key": "sekret"}):
        assert mo._admin_ok() is True
    with app.test_request_context("/", headers={"X-Internal-Key": "sekret"}):
        assert mo._admin_ok() is True
    with app.test_request_context("/?admin_key=sekret"):
        assert mo._admin_ok() is True
    with app.test_request_context("/", headers={"Cookie": "dchub_innov_key=sekret"}):
        assert mo._admin_ok() is True


def test_admin_ok_accepts_internal_and_plain_internal_env(monkeypatch):
    """The gate reads DCHUB_INTERNAL_KEY / INTERNAL_KEY too (the brain crons)."""
    _clear_keys(monkeypatch)
    monkeypatch.setenv("INTERNAL_KEY", "ikey")
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    with app.test_request_context("/", headers={"X-Internal-Key": "ikey"}):
        assert mo._admin_ok() is True
    with app.test_request_context("/", headers={"X-Internal-Key": "nope"}):
        assert mo._admin_ok() is False


# ════════════════════════════════════════════════════════════════════
#  Item 1 — GET /api/v1/media/outreach-log  (journalist recipient PII)
# ════════════════════════════════════════════════════════════════════
def test_outreach_log_denied_without_key(client, monkeypatch):
    """403 for anon — and the DB must NEVER be read (no PII leak)."""
    monkeypatch.setattr(mo, "_admin_ok", lambda: False)

    def _boom():
        raise AssertionError("outreach-log must not hit the DB for a non-admin")
    monkeypatch.setattr(mo, "_conn", _boom)
    resp = client.get("/api/v1/media/outreach-log")
    assert resp.status_code == 403
    assert resp.get_json()["ok"] is False
    # …and the rejection is itself uncacheable.
    assert "no-store" in resp.headers.get("Cache-Control", "")


def test_outreach_log_fails_closed_with_no_env_key(client, monkeypatch):
    """With NO admin key configured, the live endpoint must 403 (regression)."""
    _clear_keys(monkeypatch)

    def _boom():
        raise AssertionError("must not read the DB when the gate denies")
    monkeypatch.setattr(mo, "_conn", _boom)
    resp = client.get("/api/v1/media/outreach-log",
                      headers={"X-Admin-Key": "anything"})
    assert resp.status_code == 403


def test_outreach_log_allowed_with_admin_key(client, monkeypatch):
    """200 for an admin; PII responses are no-store/private (never edge-cached)."""
    _clear_keys(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekret")
    monkeypatch.setattr(mo, "_conn", lambda: _FakeConn([]))
    resp = client.get("/api/v1/media/outreach-log",
                      headers={"X-Admin-Key": "sekret"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["log"] == []
    cc = resp.headers.get("Cache-Control", "")
    assert "no-store" in cc and "private" in cc


# ════════════════════════════════════════════════════════════════════
#  Item 2 — GET /media/outreach  (HTML dashboard that renders PII)
# ════════════════════════════════════════════════════════════════════
def test_outreach_page_denied_without_key(client, monkeypatch):
    monkeypatch.setattr(mo, "_admin_ok", lambda: False)
    resp = client.get("/media/outreach")
    assert resp.status_code == 403
    assert resp.mimetype == "text/html"
    # No public caching on the rejection either.
    assert "no-store" in resp.headers.get("Cache-Control", "")
    # …and no admin key is leaked into a Set-Cookie on a rejection.
    assert "dchub_innov_key" not in resp.headers.get("Set-Cookie", "")


def test_outreach_page_allowed_with_admin_key_and_not_public_cached(client, monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekret")
    resp = client.get("/media/outreach?admin_key=sekret")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    cc = resp.headers.get("Cache-Control", "")
    # The public-cache leak (`public, max-age=600`) must be GONE.
    assert "public" not in cc
    assert "no-store" in cc and "private" in cc
    body = resp.get_data(as_text=True)
    assert 'name="robots" content="noindex,nofollow"' in body
    # The page carries the admin key onto its (now-gated) fetches.
    assert "X-Admin-Key" in body
    # …and Set-Cookie remembers the key so the operator pastes it once.
    assert "dchub_innov_key=sekret" in resp.headers.get("Set-Cookie", "")


# ════════════════════════════════════════════════════════════════════
#  Item 3 — GET /api/v1/media/drafts  (the INVERTED gate, now fail-closed)
# ════════════════════════════════════════════════════════════════════
def test_drafts_denied_without_key(client, monkeypatch):
    monkeypatch.setattr(mo, "_admin_ok", lambda: False)

    def _boom():
        raise AssertionError("drafts must not hit the DB for a non-admin")
    monkeypatch.setattr(mo, "_conn", _boom)
    resp = client.get("/api/v1/media/drafts")
    assert resp.status_code == 403
    assert resp.get_json()["ok"] is False


def test_drafts_fails_closed_with_no_env_key(client, monkeypatch):
    """The inverted-gate fence on the drafts endpoint: no env key -> 403, NEVER
    the old behavior of letting everyone read pending journalist drafts."""
    _clear_keys(monkeypatch)

    def _boom():
        raise AssertionError("drafts must not read the DB when the gate denies")
    monkeypatch.setattr(mo, "_conn", _boom)
    resp = client.get("/api/v1/media/drafts", headers={"X-Admin-Key": "anything"})
    assert resp.status_code == 403


def test_drafts_allowed_with_admin_key(client, monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekret")
    monkeypatch.setattr(mo, "_conn", lambda: _FakeConn([]))
    resp = client.get("/api/v1/media/drafts", headers={"X-Admin-Key": "sekret"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


# ════════════════════════════════════════════════════════════════════
#  Item 4 — POST /api/v1/media/stage-weekly  (internal-UA OR admin key)
# ════════════════════════════════════════════════════════════════════
def test_stage_weekly_denied_without_key_or_internal_ua(client, monkeypatch):
    """No admin key AND a non-internal UA -> 401. The DB must not be opened."""
    _clear_keys(monkeypatch)

    def _boom():
        raise AssertionError("stage-weekly must not open the DB for an outsider")
    monkeypatch.setattr(mo, "_conn", _boom)
    resp = client.post("/api/v1/media/stage-weekly",
                       headers={"User-Agent": "Mozilla/5.0 (random browser)"})
    assert resp.status_code == 401


def test_stage_weekly_fails_closed_with_no_env_key(client, monkeypatch):
    """Regression: with no admin key configured + an outsider UA, stage-weekly
    must still 401 (the old inverted gate would have let it through)."""
    _clear_keys(monkeypatch)

    def _boom():
        raise AssertionError("must not open the DB when denied")
    monkeypatch.setattr(mo, "_conn", _boom)
    resp = client.post("/api/v1/media/stage-weekly",
                       headers={"User-Agent": "curl/8.0", "X-Admin-Key": "anything"})
    assert resp.status_code == 401


def test_stage_weekly_accepts_internal_ua(client, monkeypatch):
    """The legitimate cron path must KEEP working: a dchub-* User-Agent is allowed
    even with NO admin key configured (that's how the weekly cron calls it)."""
    _clear_keys(monkeypatch)
    monkeypatch.setattr(mo, "_conn", lambda: _FakeConn([]))
    resp = client.post("/api/v1/media/stage-weekly?top=1",
                       headers={"User-Agent": "dchub-media-cron/1.0"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_stage_weekly_accepts_brain_ua(client, monkeypatch):
    """A 'brain' User-Agent (the autopilot) is also an accepted internal caller."""
    _clear_keys(monkeypatch)
    monkeypatch.setattr(mo, "_conn", lambda: _FakeConn([]))
    resp = client.post("/api/v1/media/stage-weekly?top=1",
                       headers={"User-Agent": "dchub-brain-autopilot"})
    assert resp.status_code == 200


def test_stage_weekly_accepts_admin_key(client, monkeypatch):
    """An admin key (no internal UA) is also accepted."""
    _clear_keys(monkeypatch)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekret")
    monkeypatch.setattr(mo, "_conn", lambda: _FakeConn([]))
    resp = client.post("/api/v1/media/stage-weekly?top=1",
                       headers={"User-Agent": "curl/8.0", "X-Admin-Key": "sekret"})
    assert resp.status_code == 200


# ════════════════════════════════════════════════════════════════════
#  Item 5 (spec) — journalists stays PUBLIC by design (NOT PII-gated)
# ════════════════════════════════════════════════════════════════════
def test_journalists_is_public(client, monkeypatch):
    """The curated journalist LIST is intentionally public (spec: 'Do NOT weaken
    any OTHER gate'). It must NOT start requiring an admin key."""
    _clear_keys(monkeypatch)
    resp = client.get("/api/v1/media/journalists")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


# ════════════════════════════════════════════════════════════════════
#  Wiring guard — the routes are actually registered (false-DONE fence)
# ════════════════════════════════════════════════════════════════════
def test_blueprint_wires_all_fixed_routes():
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(mo.media_outreach_bp)
    rules = {r.rule for r in app.url_map.iter_rules()}
    for r in ("/api/v1/media/outreach-log", "/media/outreach",
              "/api/v1/media/drafts", "/api/v1/media/stage-weekly"):
        assert r in rules, f"route not registered: {r}"


# ── Minimal psycopg2-shaped fakes so the allowed paths never touch Postgres ──
class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **k):
        return None

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return None


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)

    def commit(self):
        return None

    def close(self):
        return None


# ════════════════════════════════════════════════════════════════════
#  pitch_draft + pitch_send — SAME inverted gate, fail-OPEN (now fixed).
#  pitch_send sends REAL journalist email via Resend + writes the log, so this
#  was the worst: with no admin key configured, anyone could draft AND send.
# ════════════════════════════════════════════════════════════════════
def test_pitch_draft_denied_without_key(client, monkeypatch):
    monkeypatch.setattr(mo, "_admin_ok", lambda: False)
    resp = client.post("/api/v1/media/pitch-draft", json={"recipient_email": "x@y.com"})
    assert resp.status_code == 403


def test_pitch_draft_fails_closed_with_no_env_key(client, monkeypatch):
    """No admin key configured + a key header -> still DENY (the inverted-gate bug)."""
    _clear_keys(monkeypatch)
    resp = client.post("/api/v1/media/pitch-draft",
                       headers={"X-Admin-Key": "anything"},
                       json={"recipient_email": "x@y.com"})
    assert resp.status_code == 403


def test_pitch_draft_allowed_passes_gate(client, monkeypatch):
    """An admin passes the gate (reaches body validation -> 400, never 401/403)."""
    monkeypatch.setattr(mo, "_admin_ok", lambda: True)
    resp = client.post("/api/v1/media/pitch-draft", json={})  # missing recipient_email
    assert resp.status_code == 400


def test_pitch_send_denied_without_key_never_sends(client, monkeypatch):
    """403 for anon — and the Resend call + DB write must NEVER be reached."""
    monkeypatch.setattr(mo, "_admin_ok", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("pitch_send must not send/write for a non-admin")
    monkeypatch.setattr(__import__("requests"), "post", _boom)
    monkeypatch.setattr(mo, "_conn", _boom)
    resp = client.post("/api/v1/media/pitch-send",
                       json={"recipient_email": "x@y.com", "subject": "s", "body": "b"})
    assert resp.status_code == 403


def test_pitch_send_fails_closed_with_no_env_key(client, monkeypatch):
    _clear_keys(monkeypatch)

    def _boom(*a, **k):
        raise AssertionError("must not send when the gate denies")
    monkeypatch.setattr(__import__("requests"), "post", _boom)
    resp = client.post("/api/v1/media/pitch-send",
                       headers={"X-Admin-Key": "anything"},
                       json={"recipient_email": "x@y.com", "subject": "s", "body": "b"})
    assert resp.status_code == 403


def test_pitch_send_allowed_passes_gate_without_real_email(client, monkeypatch):
    """An admin passes the gate; with no Resend key configured it stops at the
    missing-provider guard (503) BEFORE any send — the stubbed Resend call must
    never fire, proving no real journalist email leaves in tests."""
    monkeypatch.setattr(mo, "_admin_ok", lambda: True)
    monkeypatch.setattr(mo, "_RESEND_KEY", "")  # no provider -> 503 before send

    def _boom(*a, **k):
        raise AssertionError("no real Resend call may fire in tests")
    monkeypatch.setattr(__import__("requests"), "post", _boom)
    resp = client.post("/api/v1/media/pitch-send",
                       json={"recipient_email": "x@y.com", "subject": "s", "body": "b"})
    assert resp.status_code == 503  # gate passed; stopped at the provider guard, no send
