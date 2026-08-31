"""tests/test_media_draft_repair.py — guard-blocked draft auto-repair (2026-07-18).

Fences the Bug-3 contract:
  * a guard-failing body PASSES after repair (clauses with uncorroborable
    figures are dropped / over-claims replaced per the guard's OWN hints);
  * the guard is NEVER bypassed — if the guard keeps refusing, repair reports
    blocked and nothing is published;
  * the approve endpoint's blocked response is a human-readable page with a
    one-click HMAC repair action (not raw JSON), and the JSON shape keeps a
    repair_url;
  * the /news loader fallback never dead-ends (Flask test-client).

The corroboration guard runs FOR REAL (no DB → it fails closed, which is
exactly the repair-relevant behavior); only Postgres itself is stubbed.
"""
import contextlib

import pytest

flask = pytest.importorskip("flask")
mdr = pytest.importorskip("routes.media_draft_repair")
mpd = pytest.importorskip("routes.media_pending_digest")
pdf = pytest.importorskip("routes.press_digest_fallback")


# ── repair_text: guard-failing body passes after repair ──────────────────
def test_repair_text_fixes_mw_claim(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    body = ("The new Cheyenne facility offers 10.8 MW of capacity. "
            "DC Hub tracks live grid data across major ISOs.")
    from routes.media_fact_check_guard import verify_media_text
    assert not verify_media_text(body).get("ok"), "precondition: guard must block"
    rep = mdr.repair_text(body)
    assert rep["ok"] is True
    assert "10.8 MW" not in rep["text"]
    assert "live grid data" in rep["text"], "unrelated prose must survive"
    assert rep["changes"], "the repair must be explicit about what changed"
    # the final verdict is the real guard on the final text
    assert verify_media_text(rep["text"]).get("ok") is True


def test_repair_never_bypasses_guard(monkeypatch):
    # a guard that ALWAYS refuses → repair must give up blocked, never "ok"
    monkeypatch.setattr(
        "routes.media_fact_check_guard.verify_media_text",
        lambda text: {"ok": False, "checked": 1, "unverified": [
            {"claim": "ghost claim not in text", "found_live": None,
             "expected": "unfixable"}]})
    rep = mdr.repair_text("Anything at all.")
    assert rep["ok"] is False


def test_overclaim_replaced_with_live_figure():
    text = "We track 22,000+ facilities across the globe."
    out = mdr._apply_one_repair(text, {
        "claim": "22,000+ facilities", "found_live": 21405,
        "expected": "<= 21,405 live facilities (claim over-states by >5%)"})
    assert out is not None
    new_text, note = out
    assert "21,405 facilities" in new_text
    assert "22,000" not in new_text
    assert "21,405+" not in new_text, "the exact live figure must not keep the over-claiming '+'"


def test_clause_drop_respects_html_tags():
    text = "<p>Intro stays.</p><p>It reached 225 GW overall — a record run.</p>"
    out = mdr._drop_clause(text, "225 GW")
    assert out is not None
    new_text, _ = out
    assert "225 GW" not in new_text
    assert "<p>Intro stays.</p>" in new_text
    assert new_text.count("<p>") == new_text.count("</p>"), "markup must survive"


# ── auto_repair_press_draft: saves repaired draft, publish only on pass ──
class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.conn.executed.append((s, params))
        if s.startswith("SELECT slug, title, body, published"):
            self.conn._next = self.conn.row
        elif s.startswith("UPDATE press_releases SET title"):
            self.conn.saved = params
            self.conn._next = None
        elif "SET published = TRUE" in s:
            self.conn.published = True
            self.conn._next = (self.conn.row[0],)
        else:
            self.conn._next = None

    def fetchone(self):
        return self.conn._next


class _Conn:
    def __init__(self, row):
        self.row = row
        self.executed = []
        self.saved = None
        self.published = False
        self.autocommit = False
        self._next = None

    def cursor(self):
        return _Cur(self)

    def close(self):
        pass


def test_auto_repair_saves_and_stays_unpublished(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    conn = _Conn(("slug-x", "Title Without Numbers",
                  "The site offers 10.8 MW of capacity. Other prose stays.", False))
    res = mdr.auto_repair_press_draft(conn, 54, publish=False)
    assert res["status"] == "repaired"
    assert conn.saved is not None and "10.8 MW" not in conn.saved[1]
    assert conn.published is False, "publish=False must never flip published"


def test_auto_repair_blocked_publishes_nothing(monkeypatch):
    monkeypatch.setattr(
        "routes.media_fact_check_guard.verify_media_text",
        lambda text: {"ok": False, "checked": 1, "unverified": [
            {"claim": "not present", "found_live": None, "expected": "unfixable"}]})
    conn = _Conn(("slug-x", "T", "Body.", False))
    res = mdr.auto_repair_press_draft(conn, 54, publish=True)
    assert res["status"] == "blocked"
    assert conn.saved is None and conn.published is False


# ── approve endpoint: blocked response is a page with a repair action ────
@pytest.fixture()
def approve_client(monkeypatch):
    app = flask.Flask(__name__)
    app.register_blueprint(mpd.media_pending_digest_bp)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "test-admin-key")
    # press-integrity (2026-08-07): the approve path now runs the COMPLETENESS
    # gate before the fact-check guard, so this body has to be a real one —
    # the old 36-char stub was blocked as a stub and never reached the guard
    # these two tests are about. It still carries the uncorroborated "10.8 MW"
    # claim, which is the thing under test.
    conn = _Conn(("blocked-slug", "Blocked Title",
                  "The site offers 10.8 MW of capacity across a single "
                  "hall, with room to expand as the campus builds out. "
                  "Grid headroom in the surrounding market remains the "
                  "binding constraint on how quickly that expansion can "
                  "be energized, and the interconnection position is the "
                  "detail worth checking before any commitment.", False))
    monkeypatch.setattr(mpd, "_conn", lambda: conn)
    monkeypatch.setattr(
        "routes.media_fact_check_guard.verify_media_text",
        lambda text: {"ok": False, "checked": 2, "unverified": [
            {"claim": "10.8 MW", "found_live": None,
             "expected": "per-facility MW not corroborated — omit"}]})
    return app.test_client(), conn


def test_approve_blocked_html_page(approve_client):
    client, conn = approve_client
    t = mpd._approve_token(54)
    r = client.get(f"/api/v1/media/pending-drafts/approve?id=54&t={t}",
                   headers={"Accept": "text/html,application/xhtml+xml"})
    assert r.status_code == 409
    page = r.get_data(as_text=True)
    assert "10.8 MW" in page
    assert "Auto-repair" in page
    assert f"/api/v1/media/pending-drafts/repair?id=54&t={t}" in page
    assert conn.published is False, "a blocked draft must never publish"


def test_approve_blocked_json_keeps_repair_url(approve_client):
    client, _ = approve_client
    t = mpd._approve_token(54)
    r = client.get(f"/api/v1/media/pending-drafts/approve?id=54&t={t}",
                   headers={"Accept": "application/json"})
    assert r.status_code == 409
    data = r.get_json()
    assert data["blocked_by"] == "fact_check_guard"
    assert "/api/v1/media/pending-drafts/repair?id=54" in data["repair_url"]


def test_approve_blocks_a_blank_draft_before_the_fact_check_guard(monkeypatch):
    """press-integrity (2026-08-07): completeness is a DIFFERENT question from
    corroboration, and the fact-check guard cannot answer it — a body with no
    claims in it has nothing to fail on, so a blank draft sailed through and
    one click published it. That is the perplexity failure, reachable by hand
    even after the composers were gated. Blank in, 409 out, still unpublished."""
    app = flask.Flask(__name__)
    app.register_blueprint(mpd.media_pending_digest_bp)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "test-admin-key")
    conn = _Conn(("blank-slug", "Blank Title Here", "", False))
    monkeypatch.setattr(mpd, "_conn", lambda: conn)
    # The fact-check guard would have WAVED THIS THROUGH: nothing to verify.
    monkeypatch.setattr("routes.media_fact_check_guard.verify_media_text",
                        lambda text: {"ok": True, "checked": 0, "unverified": []})
    client = app.test_client()
    t = mpd._approve_token(77)
    r = client.get(f"/api/v1/media/pending-drafts/approve?id=77&t={t}",
                   headers={"Accept": "application/json"})
    assert r.status_code == 409, r.get_data(as_text=True)
    data = r.get_json()
    assert data["blocked_by"] == "press_integrity"
    assert any(i.get("code") == "body_blank_or_stub"
               for i in data.get("issues") or []), data
    assert conn.published is False, "a blank draft reached published=TRUE"


def test_approve_bad_token_unauthorized(approve_client):
    client, _ = approve_client
    r = client.get("/api/v1/media/pending-drafts/approve?id=54&t=deadbeef")
    assert r.status_code == 401


# ── repair endpoint: one-click repair & retry ────────────────────────────
def test_repair_endpoint_repairs_and_publishes(monkeypatch):
    app = flask.Flask(__name__)
    app.register_blueprint(mdr.media_draft_repair_bp)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "test-admin-key")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    conn = _Conn(("fix-slug", "Fine Title",
                  "The site offers 10.8 MW of capacity. Prose stays.", False))
    monkeypatch.setattr(mpd, "_conn", lambda: conn)
    client = app.test_client()
    t = mpd._approve_token(54)
    r = client.get(f"/api/v1/media/pending-drafts/repair?id=54&t={t}",
                   headers={"Accept": "text/html"})
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    assert "published" in page.lower()
    assert conn.saved is not None and "10.8 MW" not in conn.saved[1]
    assert conn.published is True, "repaired + guard-passing + one-click retry publishes"


def test_repair_endpoint_bad_token(monkeypatch):
    app = flask.Flask(__name__)
    app.register_blueprint(mdr.media_draft_repair_bp)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "test-admin-key")
    client = app.test_client()
    r = client.get("/api/v1/media/pending-drafts/repair?id=54&t=nope")
    assert r.status_code == 401


# ── digest loader fallback: never a dead end (Flask test-client) ─────────
class _DigestCur:
    """announcements with articles on 2026-07-17 only."""

    def __init__(self):
        self._mode = None
        self._rows = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT LEFT(published_date, 10) AS d"):
            self._mode = "latest"
        elif s.startswith("SELECT id, title, summary"):
            d = (params or [""])[0]
            self._mode = "articles"
            self._rows = ([(1, "Story A", "sum", "u", "src", "Grid",
                            "2026-07-17", "")]
                          if d == "2026-07-17" else [])
        elif s.startswith("SELECT 1 FROM press_releases"):
            self._mode = "draft"
            self._rows = ([(1,)] if (params or [""])[0].startswith("partnership-")
                          else [])

    def fetchone(self):
        if self._mode == "latest":
            return ("2026-07-17",)
        if self._mode == "draft":
            return self._rows[0] if self._rows else None
        return None

    def fetchall(self):
        return self._rows if self._mode == "articles" else []


@pytest.fixture()
def news_client():
    """Mirror of main.py's thin views over the fallback helpers, so the
    test-client exercises the exact loader logic without importing main."""
    app = flask.Flask(__name__)

# AUTO-REPAIR: duplicate route '/api/press-releases/digest-<date_slug>' also in main.py:26337 — review and remove one
    @app.route("/api/press-releases/digest-<date_slug>")
    def digest(date_slug):
        d = date_slug[7:] if date_slug.startswith("digest-") else date_slug
        return flask.jsonify(pdf.resolve_digest(_DigestCur(), d))
# AUTO-REPAIR: duplicate route '/api/press-releases/<slug>' also in main.py:37384 — review and remove one

    @app.route("/api/press-releases/<slug>")
    def slug_view(slug):
        return flask.jsonify(pdf.slug_fallback_payload(_DigestCur(), slug))

    return app.test_client()


def test_digest_valid_date_serves_articles(news_client):
    r = news_client.get("/api/press-releases/digest-2026-07-17")
    assert r.status_code == 200
    data = r.get_json()
    assert data["total"] == 1 and "fallback" not in data


def test_digest_expired_date_falls_back_to_latest(news_client):
    r = news_client.get("/api/press-releases/digest-2026-06-01")
    assert r.status_code == 200
    data = r.get_json()
    assert data["fallback"] == "latest-digest"
    assert data["date"] == "2026-07-17" and data["total"] == 1


def test_digest_malformed_id_falls_back(news_client):
    # the id-format-drift case: never a 400 → never the worker's dead end
    r = news_client.get("/api/press-releases/digest-partnership-w23")
    assert r.status_code == 200
    assert r.get_json()["fallback"] == "latest-digest"


def test_draft_slug_falls_back_without_leaking(news_client):
    # THE reported dead end: /news/partnership-partners-2026-w23
    r = news_client.get("/api/press-releases/partnership-partners-2026-w23")
    assert r.status_code == 200
    data = r.get_json()
    assert data["not_found"] is True
    assert data["fallback"] == "latest-digest"
    assert "pending editorial review" in data["note"]
    assert not data.get("body") and not data.get("subheadline"), \
        "no draft content may leak; payload must render as a digest"
    assert data["articles"], "the fallback digest must actually show content"


def test_unknown_slug_falls_back(news_client):
    r = news_client.get("/api/press-releases/totally-unknown-slug")
    assert r.status_code == 200
    data = r.get_json()
    assert data["not_found"] is True and data["articles"]
