"""
tests/test_brain_innovation_email.py — DAILY operator EMAIL DIGEST of the brain
innovation front.

This module pushes the SAME consolidated digest the dashboard renders
(routes.brain_innovation_dashboard.build_digest) to the operator inbox via the
SAME resilient TRANSACTIONAL sender the ops alerters use
(email_fallback.send_email_resilient — Resend primary / SendGrid fallback). It
ships DARK behind BRAIN_INNOVATION_EMAIL_ENABLED (default OFF).

These tests STUB the digest builder + the email sender — NO real network, NO DB —
and cover:
  · compose renders all three sections from a stubbed digest (+ the deep link);
  · send is FLAG-GATED: flag off => the sender is NEVER called (boom-sentinel) +
    returns {sent:false, skipped:"disabled"};
  · skipped:"no_recipient" / "no_provider" paths;
  · happy path calls the transactional sender EXACTLY ONCE with the env recipient;
  · the endpoint is admin-gated (403) and ?dry=1 returns html WITHOUT sending;
  · a registration wiring-guard asserting the route lands on a real app.
"""
import pytest

bie = pytest.importorskip("routes.brain_innovation_email")


# A representative consolidated digest — the SAME shape
# brain_innovation_dashboard.build_digest() returns (counts + three flattened
# streams). The email module pulls this from the dashboard builder; we stub that
# builder so nothing touches Postgres.
_DIGEST = {
    "ok": True,
    "generated_at": "2026-06-19T12:00:00+00:00",
    "counts": {"agenda": 1, "investigations": 1, "proposals": 1},
    "agenda": [
        {"id": 1, "title": "[reliability] cap synchronous self-calls",
         "question": None, "area": "reliability", "confidence": 0.72,
         "grade": None, "created_at": "2026-06-19T00:00:00",
         "recommendation": "Cap the self-calls.",
         "decision_for_human": "Ship the cache?",
         "refutation_attempted": True, "refutation_survived": True,
         "refutation_unparsed": False, "weaknesses": ["short window"]},
    ],
    "investigations": [
        {"id": 2, "title": None, "question": "Why is external reach flat?",
         "area": None, "confidence": 0.4, "grade": "good",
         "created_at": "2026-06-19T01:00:00",
         "recommendation": "Fix the dead Smithery URL.",
         "decision_for_human": None,
         "refutation_attempted": True, "refutation_survived": False,
         "refutation_unparsed": False, "weaknesses": []},
    ],
    "proposals": [
        {"id": 3, "title": "Ship usage-based pack as front door",
         "question": None, "area": "conversion_revenue", "confidence": 0.8,
         "leverage_rank": 1.4, "grade": None,
         "created_at": "2026-06-19T02:00:00",
         "recommendation": "Lead with the $5 pack.",
         "decision_for_human": "Approve the front-door swap.",
         "refutation_attempted": True, "refutation_survived": True,
         "refutation_unparsed": False, "weaknesses": ["assumes Stripe floor"]},
    ],
}


@pytest.fixture()
def _stub_digest(monkeypatch):
    """Stub the dashboard's build_digest so compose pulls the canned digest —
    no DB, and proves the email reuses the SAME builder (no re-query)."""
    import routes.brain_innovation_dashboard as dash
    monkeypatch.setattr(dash, "build_digest", lambda *a, **k: _DIGEST)
    return _DIGEST


# ════════════════════════════════════════════════════════════════════
#  compose_innovation_email — all three sections from the stubbed digest
# ════════════════════════════════════════════════════════════════════
def test_compose_renders_all_three_sections(_stub_digest):
    msg = bie.compose_innovation_email()
    assert set(msg.keys()) == {"subject", "html", "text"}
    assert "Brain Innovation Digest" in msg["subject"]
    html = msg["html"]
    # Three section headers.
    assert "Self-directed agenda" in html
    assert "Investigations" in html
    assert "Proposals" in html
    # Per-item content: heading + confidence + refutation verdict + decision.
    assert "cap synchronous self-calls" in html
    assert "Why is external reach flat?" in html
    assert "Ship usage-based pack as front door" in html
    assert "confidence" in html
    assert "survived refutation" in html  # agenda item survived
    assert "refuted" in html              # investigation was refuted
    assert "Ship the cache?" in html      # decision-for-human rendered
    # Deep link to the dashboard (operator appends ?admin_key=).
    assert "https://dchub.cloud/api/v1/brain/innovation/dashboard" in html
    # Plain-text fallback mirrors the sections.
    assert "Self-directed agenda" in msg["text"]
    assert "Why is external reach flat?" in msg["text"]


def test_compose_never_raises_on_digest_failure(monkeypatch):
    """Best-effort: if the digest builder blows up, compose still returns a valid
    envelope (a degraded one) rather than raising."""
    import routes.brain_innovation_dashboard as dash

    def _boom(*a, **k):
        raise RuntimeError("db exploded")
    monkeypatch.setattr(dash, "build_digest", _boom)
    msg = bie.compose_innovation_email()
    assert set(msg.keys()) == {"subject", "html", "text"}
    assert "Brain Innovation Digest" in msg["subject"]
    # Zero-count sections still render their empty-state notes.
    assert "Self-directed agenda" in msg["html"]


def test_compose_caps_items_per_section(monkeypatch):
    """The email is a glance — cap each section at ~6 and note the overflow."""
    import routes.brain_innovation_dashboard as dash
    many = {
        "ok": True, "generated_at": "2026-06-19T12:00:00+00:00",
        "counts": {"agenda": 10, "investigations": 0, "proposals": 0},
        "agenda": [
            {"id": i, "title": f"item {i}", "question": None, "area": "x",
             "confidence": 0.5, "grade": None, "created_at": "2026-06-19T00:00:00",
             "recommendation": None, "decision_for_human": None,
             "refutation_attempted": False, "refutation_survived": None,
             "refutation_unparsed": False, "weaknesses": []}
            for i in range(10)
        ],
        "investigations": [], "proposals": [],
    }
    monkeypatch.setattr(dash, "build_digest", lambda *a, **k: many)
    html = bie.compose_innovation_email()["html"]
    assert "item 0" in html and "item 5" in html   # first 6 shown (0..5)
    assert "item 6" not in html                     # capped
    assert "+4 more on the dashboard" in html       # overflow noted


# ════════════════════════════════════════════════════════════════════
#  send_innovation_digest — flag-gated guard order
# ════════════════════════════════════════════════════════════════════
def _boom_sender(*a, **k):
    raise AssertionError("send_email_resilient must NOT be called on this path")


def test_send_flag_off_never_sends(monkeypatch):
    """FLAG OFF is the FIRST guard: returns skipped:disabled and the transactional
    sender is NEVER called (boom-sentinel). Ships dark."""
    monkeypatch.delenv("BRAIN_INNOVATION_EMAIL_ENABLED", raising=False)
    # Wire a boom-sentinel into the sender so any send attempt fails the test.
    import email_fallback
    monkeypatch.setattr(email_fallback, "send_email_resilient", _boom_sender)
    out = bie.send_innovation_digest()
    assert out == {"sent": False, "skipped": "disabled"}


def test_send_skips_when_no_recipient(monkeypatch):
    """Flag ON but no recipient env -> skipped:no_recipient, sender never called."""
    monkeypatch.setenv("BRAIN_INNOVATION_EMAIL_ENABLED", "1")
    monkeypatch.delenv("BRAIN_DIGEST_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_ALERT_EMAIL", raising=False)
    import email_fallback
    monkeypatch.setattr(email_fallback, "send_email_resilient", _boom_sender)
    out = bie.send_innovation_digest()
    assert out["sent"] is False
    assert out["skipped"] == "no_recipient"


def test_send_skips_when_no_provider_key(monkeypatch):
    """Flag ON + recipient set but NO provider key -> skipped:no_provider,
    sender never called (degrades gracefully, never crashes)."""
    monkeypatch.setenv("BRAIN_INNOVATION_EMAIL_ENABLED", "1")
    monkeypatch.setenv("BRAIN_DIGEST_EMAIL", "ops@example.com")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("DCHUB_RESEND_API_KEY", raising=False)
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    import email_fallback
    monkeypatch.setattr(email_fallback, "send_email_resilient", _boom_sender)
    out = bie.send_innovation_digest()
    assert out["sent"] is False
    assert out["skipped"] == "no_provider"
    assert out["to"] == "ops@example.com"


def test_send_happy_path_calls_transactional_sender_once(monkeypatch, _stub_digest):
    """Flag ON + recipient + provider key -> compose + send via the REUSED
    transactional sender EXACTLY ONCE, to the env recipient. Returns sent:true."""
    monkeypatch.setenv("BRAIN_INNOVATION_EMAIL_ENABLED", "1")
    monkeypatch.setenv("BRAIN_DIGEST_EMAIL", "ops@example.com")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")

    calls = []

    def _capture(to_email, subject, **kw):
        calls.append({"to": to_email, "subject": subject, "kw": kw})
        return True

    import email_fallback
    monkeypatch.setattr(email_fallback, "send_email_resilient", _capture)

    out = bie.send_innovation_digest()
    assert out["sent"] is True
    assert out["to"] == "ops@example.com"
    # EXACTLY ONE send, to the env recipient, with html + text content.
    assert len(calls) == 1
    assert calls[0]["to"] == "ops@example.com"
    assert "Brain Innovation Digest" in calls[0]["subject"]
    assert calls[0]["kw"].get("html_content")
    assert calls[0]["kw"].get("text_content")


def test_send_recipient_falls_back_to_admin_alert_email(monkeypatch, _stub_digest):
    """BRAIN_DIGEST_EMAIL unset falls back to ADMIN_ALERT_EMAIL (the ops address) —
    never a hardcoded address."""
    monkeypatch.setenv("BRAIN_INNOVATION_EMAIL_ENABLED", "1")
    monkeypatch.delenv("BRAIN_DIGEST_EMAIL", raising=False)
    monkeypatch.setenv("ADMIN_ALERT_EMAIL", "fallback@example.com")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    seen = {}
    import email_fallback
    monkeypatch.setattr(email_fallback, "send_email_resilient",
                        lambda to, subj, **kw: seen.update(to=to) or True)
    out = bie.send_innovation_digest()
    assert out["sent"] is True
    assert seen["to"] == "fallback@example.com"


def test_send_degrades_when_sender_returns_false(monkeypatch, _stub_digest):
    """If the transactional sender returns False (both providers failed), we report
    sent:false with an error — never raise."""
    monkeypatch.setenv("BRAIN_INNOVATION_EMAIL_ENABLED", "1")
    monkeypatch.setenv("BRAIN_DIGEST_EMAIL", "ops@example.com")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    import email_fallback
    monkeypatch.setattr(email_fallback, "send_email_resilient",
                        lambda *a, **k: False)
    out = bie.send_innovation_digest()
    assert out["sent"] is False
    assert out["error"] == "send_failed"
    assert out["to"] == "ops@example.com"


# ════════════════════════════════════════════════════════════════════
#  Endpoint (Flask test client) — admin-gated + ?dry=1
# ════════════════════════════════════════════════════════════════════
@pytest.fixture()
def client():
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(bie.brain_innovation_email_bp)
    return app.test_client()


def test_endpoint_is_admin_gated(client, monkeypatch):
    """POST must 403 for a non-admin — and never send."""
    monkeypatch.setattr(bie, "_admin_ok", lambda: False)

    def _boom():
        raise AssertionError("send must not run for a non-admin")
    monkeypatch.setattr(bie, "send_innovation_digest", _boom)
    resp = client.post("/api/v1/brain/innovation/digest/email")
    assert resp.status_code == 403
    assert resp.get_json()["ok"] is False


def test_endpoint_runs_send_and_returns_skip_reason(client, monkeypatch):
    """For an admin, the endpoint runs send_innovation_digest and echoes its skip
    reason so the cron logs are legible (here: flag off => disabled)."""
    monkeypatch.setattr(bie, "_admin_ok", lambda: True)
    monkeypatch.setattr(bie, "send_innovation_digest",
                        lambda: {"sent": False, "skipped": "disabled"})
    resp = client.post("/api/v1/brain/innovation/digest/email")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["sent"] is False
    assert data["skipped"] == "disabled"


def test_endpoint_dry_returns_html_without_sending(client, monkeypatch, _stub_digest):
    """?dry=1 composes + returns the html WITHOUT sending (preview) — the sender is
    never invoked."""
    monkeypatch.setattr(bie, "_admin_ok", lambda: True)

    def _boom():
        raise AssertionError("dry=1 must NOT send")
    monkeypatch.setattr(bie, "send_innovation_digest", _boom)
    resp = client.post("/api/v1/brain/innovation/digest/email?dry=1")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["dry"] is True
    assert data["sent"] is False
    assert "Self-directed agenda" in data["html"]
    assert "https://dchub.cloud/api/v1/brain/innovation/dashboard" in data["html"]


def test_admin_gate_accepts_query_param_key(client, monkeypatch):
    """Admin gate accepts ?admin_key= (mirrors the brain-dashboard pattern)."""
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekret")
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    monkeypatch.delenv("INTERNAL_KEY", raising=False)
    monkeypatch.setattr(bie, "send_innovation_digest",
                        lambda: {"sent": False, "skipped": "disabled"})
    ok = client.post("/api/v1/brain/innovation/digest/email?admin_key=sekret")
    assert ok.status_code == 200
    bad = client.post("/api/v1/brain/innovation/digest/email?admin_key=wrong")
    assert bad.status_code == 403
    none = client.post("/api/v1/brain/innovation/digest/email")
    assert none.status_code == 403


# ── wiring guard ─────────────────────────────────────────────────────
def test_register_helper_wires_route_onto_a_real_app():
    """Guard against the false-DONE where module + tests pass but the app never
    registers the blueprint (the endpoint 404s in prod). The register helper must
    put the email route in a real app's URL map."""
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    bie.register_brain_innovation_email(app)
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/v1/brain/innovation/digest/email" in rules
