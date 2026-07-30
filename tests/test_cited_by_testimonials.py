"""tests/test_cited_by_testimonials.py — /cited-by customer testimonials +
admin approval workflow (2026-07-30).

The founding-customer emails promise "we'll quote you on dchub.cloud/cited-by",
and the claim/identify flows capture volunteered quotes into ai_testimonials
(source='claim_quote', approved=FALSE) — but nothing rendered them and nothing
could flip `approved`. These tests fence the fix, in order of importance:

 1. HONESTY: the /cited-by customer section reads ONLY approved
    source='claim_quote' rows (human-volunteered, human-approved). The
    seeded/probed AI rows ('seed', 'verified', 'probe_%', 'mcp-auto') must
    never render as customer voice, and the empty state admits emptiness
    instead of faking proof (test_honest_numbers.py polices the same class).
 2. XSS: quote/name/company are arbitrary public input — anyone with a
    claimed key can POST them — so the page must HTML-escape all of it
    (and the scraped ai_citations text while we're at it).
 3. ADMIN GATE: pending/approve deny without the admin key, fail CLOSED
    when no key is configured, and respond Cache-Control: no-store
    (admin GETs have been edge-cached before).
 4. PII: the customer query selects no email-ish column; the capture path
    already redacts pasted emails.

No Postgres and no `main` import: _conn is stubbed with canned cursors
(routes.cited_by._conn would otherwise `from main import get_db`).
"""
import datetime as dt

import pytest

flask = pytest.importorskip("flask")
cb = pytest.importorskip("routes.cited_by")
tp = pytest.importorskip("routes.testimonial_probe")

_TS = dt.datetime(2026, 7, 1, 12, 0, 0)

# Env names the probe admin gate reads — cleared in fixtures so a stray real
# env var on the runner can't make a "no key configured" test pass falsely.
_KEY_ENVS = ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "ADMIN_KEY")


# ─────────────────────────────── fakes ──────────────────────────────────
class _Cur:
    """Returns canned rows keyed by a marker substring of the SQL."""

    def __init__(self, rows_by_marker):
        self._rows_by_marker = rows_by_marker
        self._rows = []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        self._rows = []
        for marker, rows in self._rows_by_marker.items():
            if marker in sql:
                self._rows = rows
                break

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows_by_marker):
        self.cur = _Cur(rows_by_marker)

    def cursor(self):
        return self.cur

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _stub_cited_by(monkeypatch, testimonial_rows=(), citation_rows=(),
                   tool_rows=()):
    conn = _Conn({
        "mcp_tool_calls": list(tool_rows),
        "ai_citations": list(citation_rows),
        "ai_testimonials": list(testimonial_rows),
    })
    monkeypatch.setattr(cb, "_conn", lambda: conn)
    return conn


def _cited_by_client():
    app = flask.Flask(__name__)
    app.register_blueprint(cb.cited_by_bp)
    return app.test_client()


def _probe_client(monkeypatch, rows_by_marker=None):
    app = flask.Flask(__name__)
    app.register_blueprint(tp.testimonial_probe_bp)
    conn = _Conn(rows_by_marker or {})
    monkeypatch.setattr(tp, "_conn", lambda: conn)
    return app.test_client(), conn


# ════════════════════════ 1. honesty of the render filter ═══════════════
def test_customer_query_is_approved_claim_quote_only(monkeypatch):
    """The ONLY rows that may render as customer voice are approved
    source='claim_quote' — never 'seed'/'verified'/probe/mcp-auto."""
    conn = _stub_cited_by(monkeypatch)
    cb._gather_cited_by_data(days=30)
    sql = next(s for s in conn.cur.executed if "ai_testimonials" in s)
    flat = " ".join(sql.split()).lower()
    assert "approved = true" in flat
    assert "source = 'claim_quote'" in flat
    # PII fence: the query must not select anything email-shaped.
    assert "email" not in flat


def test_gather_maps_claim_quote_columns(monkeypatch):
    """claim_quote rows store name in agent_name and company in context."""
    _stub_cited_by(monkeypatch, testimonial_rows=[
        ("Jane Doe", "Acme DC", "DC Hub cut our diligence time in half.",
         "mcp_agent", _TS),
    ])
    data = cb._gather_cited_by_data(days=30)
    t = data["customer_testimonials"][0]
    assert t["name"] == "Jane Doe"
    assert t["company"] == "Acme DC"
    assert "diligence" in t["quote"]
    assert t["approved_at"].startswith("2026-07-01")


def test_json_endpoint_exposes_customer_testimonials(monkeypatch):
    _stub_cited_by(monkeypatch, testimonial_rows=[
        ("Jane Doe", "Acme DC", "DC Hub cut our diligence time in half.",
         "mcp_agent", _TS),
    ])
    r = _cited_by_client().get("/api/v1/cited-by")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["customer_testimonials"][0]["company"] == "Acme DC"


def test_page_renders_approved_customer_quote(monkeypatch):
    _stub_cited_by(monkeypatch, testimonial_rows=[
        ("Jane Doe", "Acme DC", "DC Hub cut our diligence time in half.",
         "mcp_agent", _TS),
    ])
    html_text = _cited_by_client().get("/cited-by").get_data(as_text=True)
    assert "What customers say" in html_text
    assert "Jane Doe" in html_text
    assert "Acme DC" in html_text
    assert "diligence time in half" in html_text


def test_empty_state_is_honest_not_fabricated(monkeypatch):
    """Zero approved quotes → the section admits emptiness, points at the
    opt-in path, and renders NO quote cards."""
    _stub_cited_by(monkeypatch)
    html_text = _cited_by_client().get("/cited-by").get_data(as_text=True)
    assert "What customers say" in html_text
    assert "No customer quotes are published yet" in html_text
    assert "keys/claim/quote" in html_text
    # no fabricated cards: the customer-card class only exists with rows
    assert 'class="quote cust"' not in html_text


# ═══════════════════════════ 2. XSS escaping ════════════════════════════
def test_page_escapes_customer_content(monkeypatch):
    _stub_cited_by(monkeypatch, testimonial_rows=[
        ('<script>alert(1)</script>', 'Ev&l "Co"',
         'Great <b>tool</b> <script>steal()</script>', "mcp_agent", _TS),
    ])
    html_text = _cited_by_client().get("/cited-by").get_data(as_text=True)
    assert "<script>alert(1)" not in html_text
    assert "<script>steal()" not in html_text
    assert "&lt;script&gt;" in html_text


def test_page_escapes_citation_content(monkeypatch):
    """ai_citations text is scraped from AI responses — also untrusted."""
    _stub_cited_by(monkeypatch, citation_rows=[
        ("Perplexity<img src=x onerror=1>", "prompt <script>p()</script>",
         "resp <script>c()</script>", _TS),
    ])
    html_text = _cited_by_client().get("/cited-by").get_data(as_text=True)
    assert "<script>p()" not in html_text
    assert "<script>c()" not in html_text
    assert "<img src=x" not in html_text


# ═══════════════════════════ 3. admin gate ══════════════════════════════
def test_pending_denied_without_key(monkeypatch):
    for n in _KEY_ENVS:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekret")
    client, _ = _probe_client(monkeypatch)
    r = client.get("/api/v1/testimonials/pending")
    assert r.status_code == 401
    assert "no-store" in r.headers.get("Cache-Control", "")


def test_pending_fails_closed_when_no_key_configured(monkeypatch):
    """The inverted-gate class: no admin env configured must DENY."""
    for n in _KEY_ENVS:
        monkeypatch.delenv(n, raising=False)
    client, _ = _probe_client(monkeypatch)
    r = client.get("/api/v1/testimonials/pending",
                   headers={"X-Admin-Key": "anything"})
    assert r.status_code == 401


def test_approve_denied_without_key(monkeypatch):
    for n in _KEY_ENVS:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekret")
    client, _ = _probe_client(monkeypatch)
    r = client.post("/api/v1/testimonials/approve", json={"ids": [1]})
    assert r.status_code == 401


def test_pending_lists_unapproved_claim_quote_rows(monkeypatch):
    for n in _KEY_ENVS:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekret")
    client, conn = _probe_client(monkeypatch, {
        "FROM ai_testimonials": [
            (12, "claim_quote", "recommendation", "mcp_agent",
             "Jane Doe", "Acme DC", "quote text", _TS),
        ],
    })
    r = client.get("/api/v1/testimonials/pending",
                   headers={"X-Admin-Key": "sekret"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["count"] == 1
    assert body["pending"][0]["id"] == 12
    assert body["pending"][0]["name"] == "Jane Doe"
    assert body["pending"][0]["company"] == "Acme DC"
    assert "no-store" in r.headers.get("Cache-Control", "")
    flat = " ".join(conn.cur.executed[0].split()).lower()
    assert "coalesce(approved, false) = false" in flat
    assert "source = %s" in flat  # default filter: claim_quote only


# ═══════════════════════ 4. approve / demote flow ═══════════════════════
def _admin(monkeypatch):
    for n in _KEY_ENVS:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekret")
    return {"X-Admin-Key": "sekret"}


def test_approve_sets_approved_at(monkeypatch):
    hdrs = _admin(monkeypatch)
    client, conn = _probe_client(monkeypatch,
                                 {"RETURNING id": [(12,)]})
    r = client.post("/api/v1/testimonials/approve",
                    json={"ids": [12, 13]}, headers=hdrs)
    assert r.status_code == 200
    body = r.get_json()
    assert body["approved"] is True
    assert body["changed"] == [12]
    assert body["unchanged"] == [13]
    flat = " ".join(conn.cur.executed[0].split()).lower()
    assert "set approved = true" in flat
    assert "approved_at = now()" in flat
    assert "no-store" in r.headers.get("Cache-Control", "")


def test_demote_clears_approved_only(monkeypatch):
    hdrs = _admin(monkeypatch)
    client, conn = _probe_client(monkeypatch,
                                 {"RETURNING id": [(12,)]})
    r = client.post("/api/v1/testimonials/approve",
                    json={"id": 12, "approved": False}, headers=hdrs)
    assert r.status_code == 200
    assert r.get_json()["approved"] is False
    flat = " ".join(conn.cur.executed[0].split()).lower()
    assert "set approved = false" in flat
    assert "approved_at = now()" not in flat


def test_approve_rejects_bad_ids(monkeypatch):
    hdrs = _admin(monkeypatch)
    client, _ = _probe_client(monkeypatch)
    assert client.post("/api/v1/testimonials/approve",
                       json={"ids": ["x"]}, headers=hdrs).status_code == 400
    assert client.post("/api/v1/testimonials/approve",
                       json={"ids": []}, headers=hdrs).status_code == 400
    assert client.post("/api/v1/testimonials/approve",
                       json={}, headers=hdrs).status_code == 400
    assert client.post("/api/v1/testimonials/approve",
                       json={"ids": list(range(300))},
                       headers=hdrs).status_code == 400
