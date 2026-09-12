"""Pocket listings: the auth wall, the operator-contact rule and the lead
register, exercised as REQUESTS against the real blueprint.

What these pin, and why each matters to the program:
  * teasers are public, detail is walled, operator contact is never served —
    a buyer who has the operator's number never needs DC Hub's introduction;
  * our MCP gateway's X-Internal-Key is not an identity: an anonymous agent
    stays anonymous, and a user credential forwarded through the gateway still
    resolves (map_tier_gating maps the internal key to 'pro' at STEP 1);
  * nothing reaches the register, or anyone's inbox, before identity, valid
    fields and accepted terms;
  * a lead becomes registered only through a token bound to that lead, and the
    admin hears about it exactly once;
  * the public record masks the prospect and names tampering;
  * admin routes open to X-Admin-Key alone.

Imports routes/exclusive_listings.py with the REAL map_tier_gating resolver and
never main.py. `main` is stubbed EMPTY in sys.modules only so the resolver's
optional `from main import …` lookups fail the way they do with no database,
instead of booting the whole app in-process. Storage sits behind the module's
own _db_* seams; register writes go through the real util/listing_ledger.append
SQL against a stand-in cursor that fails on any statement it does not expect.
"""
import json
import re
import sys
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("flask")
import jwt  # noqa: E402 — PyJWT is a runtime dependency (routes/auth_routes.py)
from flask import Flask  # noqa: E402

import routes.exclusive_listings as el  # noqa: E402
from util import listing_ledger as ledger  # noqa: E402

JWT_SECRET = "pocket-listings-test-secret-0123456789abcdef"  # secretscan:allow (test placeholder)
INTERNAL_KEY = "internal-gateway-key-for-tests-only"  # secretscan:allow (test placeholder)
ADMIN_KEY = "admin-key-for-tests-only-0123456789"  # secretscan:allow (test placeholder)
ADMIN_INBOX = "ops@dchub.example"
OPERATOR_SENTINEL = "operator-private@sentinel.example"
OWNER_SENTINEL = "owner-private-sentinel"

_INSERT_COLS = (
    "lead_id", "event", "listing_id", "listing_slug", "listing_title", "user_ref",
    "email", "name", "role", "company", "message", "requirement", "email_domain",
    "email_verified", "verified_via", "channel", "platform", "client",
    "session_hash", "ip_hash", "user_agent", "terms_version", "meta", "created_at",
    "prev_hash", "entry_json", "entry_hash", "signature", "key_id")

INTRO = {"name": "Jane Doe", "company": "Acme Capital", "role": "VP Development",
         "requirement": {"capacity_mw": 40, "timeline": "Q2 2027", "use_case": "AI inference"},
         "message": "Interested in the shell.", "accept_terms": True,
         "terms_version": el.TERMS_VERSION}


class _Cursor:
    def __init__(self, store):
        self.store, self._one = store, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT pg_advisory_xact_lock("):
            self._one = ("",)
        elif s == "SELECT entry_hash FROM listing_lead_ledger ORDER BY seq DESC LIMIT 1":
            self._one = (self.store.rows[-1]["entry_hash"],) if self.store.rows else None
        elif s.startswith("INSERT INTO listing_lead_ledger"):
            row = dict(zip(_INSERT_COLS, params))
            for key in ("requirement", "client", "meta"):
                if row[key] is not None:
                    row[key] = json.loads(row[key])
            row["created_at"] = datetime.fromisoformat(row["created_at"])
            row["seq"] = len(self.store.rows) + 1
            self.store.rows.append(row)
            self._one = (row["seq"],)
        else:
            raise AssertionError(f"unexpected SQL: {s[:90]}")

    def fetchone(self):
        return self._one


class _Conn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _Cursor(self.store)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _listing(**over):
    row = {"id": 1, "slug": "dfw-40", "title": "Powered shell — DFW",
           "summary": "Energized next year.", "status": "pocket",
           "tier_required": "registered", "market": "Dallas", "state": "TX",
           "country": "US", "latitude": 32.776712, "longitude": -96.797012,
           "capacity_mw": 40.0, "asking_price": 1250000, "asking_currency": "USD",
           "detail": {"available": "Q2 2027", "_internal": "hidden-note",
                      "contact": OPERATOR_SENTINEL, "power": "dual feed"},
           "contact": {"email": OPERATOR_SENTINEL, "notify_email": OPERATOR_SENTINEL,
                       "auto_notify": False},
           "owner_id": OWNER_SENTINEL,
           "created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
           "updated_at": datetime(2026, 9, 2, tzinfo=timezone.utc), "expires_at": None}
    row.update(over)
    return row


class _Env:
    pass


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setitem(sys.modules, "main", types.ModuleType("main"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DCHUB_LEAD_LEDGER_SECRET", raising=False)
    for var in ("DCHUB_SYNC_KEY", "INTERNAL_WORKER_SECRET", "ADMIN_INBOX_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", INTERNAL_KEY)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setenv("DCHUB_ADMIN_EMAIL", ADMIN_INBOX)

    e = _Env()
    e.rows, e.sent, e.listings, e.verified_via = [], [], [_listing()], None

    def get_listing(ident):
        ident = str(ident)
        return next((dict(r) for r in e.listings
                     if str(r["id"]) == ident or r["slug"] == ident), None)

    monkeypatch.setattr(el, "_conn", lambda: _Conn(e))
    monkeypatch.setattr(el, "_db_list_listings", lambda **kw: [
        dict(r) for r in e.listings if r["status"] in ("pocket", "public")])
    monkeypatch.setattr(el, "_db_count_live", lambda: len(
        [r for r in e.listings if r["status"] in ("pocket", "public")]))
    monkeypatch.setattr(el, "_db_count_matching", lambda req: len(e.listings))
    monkeypatch.setattr(el, "_db_get_listing", get_listing)
    monkeypatch.setattr(el, "_db_lead_events", lambda lead_id: [
        dict(r) for r in e.rows if r["lead_id"] == lead_id])
    monkeypatch.setattr(el, "_db_listing_events", lambda listing_id: [
        dict(r) for r in e.rows if r["listing_id"] == listing_id])
    monkeypatch.setattr(el, "_db_user_openings", lambda user_ref, event, listing_id, since: [
        dict(r) for r in e.rows if r["user_ref"] == user_ref and r["event"] == event
        and r["listing_id"] == listing_id and r["created_at"] > since])
    monkeypatch.setattr(el, "_db_recent_view", lambda user_ref, listing_id, since: any(
        r["event"] == "listing_viewed" and r["user_ref"] == user_ref
        and r["listing_id"] == listing_id and r["created_at"] > since for r in e.rows))
    monkeypatch.setattr(el, "_db_chain_rows", lambda limit: [dict(r) for r in e.rows][:limit])
    monkeypatch.setattr(el, "_db_verified_via", lambda viewer: e.verified_via)
    monkeypatch.setattr(el, "_send_email", lambda to, subject, body: e.sent.append(
        {"to": to, "subject": subject, "body": body}) or True)
    monkeypatch.setattr(el, "_dispatch", lambda fn: fn())
    monkeypatch.setattr(el, "_run_bounded", lambda fn, timeout_s: (True, fn()))
    el._RATE.clear()
    el._LAST_CONFIRM_SENT.clear()
    el._CHAIN_CACHE.update(at=0.0, value=None)

    app = Flask(__name__)
    app.register_blueprint(el.exclusive_listings_bp)
    e.client = app.test_client()
    return e


def _bearer(user_id="u-jane", email="Jane@Acme.com", plan="free"):
    token = jwt.encode({"user_id": user_id, "email": email, "plan": plan, "role": "user",
                        "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
                       JWT_SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _confirm_link(html_body):
    m = re.search(r"confirm=(LD-[0-9A-Z]{10})&amp;token=(\d+\.[0-9a-f]{40})", html_body)
    assert m, "no confirmation link in the email"
    return m.group(1), m.group(2)


def _register_and_confirm(e, headers=None):
    lead_id = e.client.post("/api/v1/listings/dfw-40/intro", json=INTRO,
                            headers=headers or _bearer()).get_json()["lead_id"]
    mail = next(m for m in e.sent if lead_id in m["body"] and "confirm=" in m["body"])
    _, token = _confirm_link(mail["body"])
    r = e.client.post("/api/v1/listings/leads/confirm", json={"lead_id": lead_id, "token": token})
    assert r.status_code == 200, r.get_data(as_text=True)
    return lead_id


# ── the wall ──────────────────────────────────────────────────────────────

def test_the_anonymous_feed_is_teasers_only_and_never_carries_operator_contact(env):
    r = env.client.get("/api/v1/listings")
    body, j = r.get_data(as_text=True), r.get_json()
    assert r.status_code == 200 and j["ok"] is True
    assert j["count"] == 1 and j["program"]["status"] == "live"
    item = j["items"][0]
    assert item["slug"] == "dfw-40" and item["locked"] is True and item["capacity_mw"] == 40.0
    for private in ("latitude", "longitude", "asking_price", "detail", "contact", "owner_id"):
        assert private not in item
    for secret in (OPERATOR_SENTINEL, OWNER_SENTINEL, "hidden-note"):
        assert secret not in body


def test_an_empty_program_reads_upcoming(env):
    env.listings = []
    j = env.client.get("/api/v1/listings").get_json()
    assert j["count"] == 0 and j["program"]["status"] == "upcoming" and j["program"]["note"]


def test_our_gateway_key_alone_leaves_an_agent_anonymous(env):
    j = env.client.get("/api/v1/listings/dfw-40", headers={"X-Internal-Key": INTERNAL_KEY}).get_json()
    assert j["locked"] is True and "latitude" not in j["listing"]
    assert j["caller_tier"] == "anonymous"       # provenance, never 'pro'
    assert j["viewer"]["channel"] == "mcp"       # ...but recorded as the channel


def test_a_user_credential_forwarded_through_the_gateway_still_resolves(env):
    headers = {"X-Internal-Key": INTERNAL_KEY, **_bearer()}
    j = env.client.get("/api/v1/listings/dfw-40", headers=headers).get_json()
    assert j["locked"] is False
    assert j["viewer"]["identified"] is True and j["viewer"]["channel"] == "mcp"


def test_a_signed_in_user_opens_the_listing_without_operator_contact(env):
    r = env.client.get("/api/v1/listings/dfw-40", headers=_bearer())
    body, j = r.get_data(as_text=True), r.get_json()
    listing = j["listing"]
    assert j["locked"] is False and j["access"]["granted"] is True
    assert (listing["latitude"], listing["longitude"]) == (32.78, -96.8)
    assert listing["asking_price"] == 1250000.0
    assert listing["detail"] == {"available": "Q2 2027", "power": "dual feed"}
    assert "contact" not in listing and "owner_id" not in listing
    for secret in (OPERATOR_SENTINEL, OWNER_SENTINEL, "hidden-note"):
        assert secret not in body
    assert j["introduction"]["operator_contact"] == "never_shared"
    assert j["viewer"]["email_masked"] == "j***@acme.com"
    assert "jane@acme.com" not in body.lower()


def test_an_identified_view_is_recorded_once_per_day(env):
    for _ in range(3):
        assert env.client.get("/api/v1/listings/dfw-40", headers=_bearer()).status_code == 200
    views = [r for r in env.rows if r["event"] == "listing_viewed"]
    assert len(views) == 1
    assert (views[0]["listing_id"], views[0]["user_ref"], views[0]["lead_id"]) == (1, "u-jane", None)


def test_a_key_without_a_bound_email_is_told_to_bind_one(env, monkeypatch):
    import map_tier_gating
    monkeypatch.setattr(map_tier_gating, "_detect_caller_tier", lambda decode_jwt_func=None: (
        "free", {"email": None, "plan": "free", "source": "mcp_dev_keys"}))
    j = env.client.get("/api/v1/listings/dfw-40",
                       headers={"X-API-Key": "dch_live_" + "a" * 32}).get_json()
    assert j["locked"] is True and j["access"]["reason"] == "email_binding_required"
    assert j["access"]["unlock"]["mcp_steps"] == ["bind_email"]


def test_a_key_with_a_bound_email_opens_the_listing(env, monkeypatch):
    import map_tier_gating
    monkeypatch.setattr(map_tier_gating, "_detect_caller_tier", lambda decode_jwt_func=None: (
        "identified", {"email": "human@firm.example", "plan": "identified", "source": "mcp_dev_keys"}))
    j = env.client.get("/api/v1/listings/dfw-40",
                       headers={"X-API-Key": "dch_live_" + "b" * 32}).get_json()
    assert j["locked"] is False
    assert (j["viewer"]["identity_source"], j["viewer"]["channel"]) == ("api_key", "api")


def test_a_pro_listing_asks_a_free_user_to_upgrade(env):
    env.listings[0]["tier_required"] = "pro"
    free = env.client.get("/api/v1/listings/dfw-40", headers=_bearer(plan="free")).get_json()
    assert free["locked"] is True and free["access"]["reason"] == "upgrade_required"
    assert free["access"]["unlock"]["pricing_url"].endswith("/pricing")
    pro = env.client.get("/api/v1/listings/dfw-40", headers=_bearer(plan="pro")).get_json()
    assert pro["locked"] is False


def test_drafts_and_reserved_words_are_not_listings(env):
    env.listings.append(_listing(id=2, slug="secret-draft", status="draft"))
    assert env.client.get("/api/v1/listings/secret-draft", headers=_bearer()).status_code == 404
    assert env.client.get("/api/v1/listings/interest").status_code == 404
    assert env.client.get("/api/v1/listings/terms").get_json()["terms"]["version"] == el.TERMS_VERSION


# ── registration ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("who,body,status,error", [
    ("anon", INTRO, 401, "identity_required"),
    ("user", {**INTRO, "accept_terms": False}, 422, "terms_not_accepted"),
    ("user", {**INTRO, "accept_terms": "yes"}, 422, "terms_not_accepted"),
    ("user", {**INTRO, "name": ""}, 422, "invalid_request"),
    ("user", {**INTRO, "requirement": {"capacity_mw": 99999}}, 422, "invalid_request"),
    ("user", {**INTRO, "terms_version": "1999-01-01"}, 409, "terms_version_mismatch"),
])
def test_nothing_is_written_or_emailed_before_identity_fields_and_terms(env, who, body, status, error):
    headers = _bearer() if who == "user" else {"X-Internal-Key": INTERNAL_KEY}
    r = env.client.post("/api/v1/listings/dfw-40/intro", json=body, headers=headers)
    assert (r.status_code, r.get_json()["error"]) == (status, error)
    assert env.rows == [] and env.sent == []


@pytest.mark.parametrize("path", ["/api/v1/listings/dfw-40/intro", "/api/v1/listings/interest"])
@pytest.mark.parametrize("headers", [{}, {"X-Internal-Key": INTERNAL_KEY}, {"X-API-Key": "x"}])
def test_an_unidentified_caller_registers_nothing(env, path, headers):
    """/interest has no listing behind it, so no listing-level access check
    stands between an anonymous caller and the register — only identity does."""
    body = {**INTRO, "requirement": {"markets": ["Dallas"], "capacity_mw": 20}}
    r = env.client.post(path, json=body, headers=headers)
    assert (r.status_code, r.get_json()["error"]) == (401, "identity_required")
    assert env.rows == [] and env.sent == []


def test_a_request_is_pending_until_the_inbox_confirms_it(env):
    r = env.client.post("/api/v1/listings/dfw-40/intro", json=INTRO, headers=_bearer())
    body, j = r.get_data(as_text=True), r.get_json()
    assert r.status_code == 200 and j["status"] == "pending_email_confirmation"
    assert ledger.looks_like_lead_id(j["lead_id"]) and j["verify_url"].endswith(j["lead_id"])
    assert [row["event"] for row in env.rows] == ["intro_requested"]
    assert (env.rows[0]["email"], env.rows[0]["email_verified"]) == ("jane@acme.com", False)
    assert "jane@acme.com" not in body
    assert [m["to"] for m in env.sent] == ["jane@acme.com"]
    assert _confirm_link(env.sent[0]["body"])[0] == j["lead_id"]


def test_confirming_registers_the_lead_and_tells_the_admin_once(env):
    r = env.client.post("/api/v1/listings/dfw-40/intro", json=INTRO, headers=_bearer())
    lead_id, token = _confirm_link(env.sent[0]["body"])
    assert lead_id == r.get_json()["lead_id"]
    first = env.client.post("/api/v1/listings/leads/confirm", json={"lead_id": lead_id, "token": token})
    assert first.status_code == 200
    assert (first.get_json()["status"], first.get_json()["already_confirmed"]) == ("registered", False)
    assert [row["event"] for row in env.rows] == ["intro_requested", "email_confirmed"]
    admin = [m for m in env.sent if m["to"] == ADMIN_INBOX]
    assert len(admin) == 1 and lead_id in admin[0]["subject"]
    assert OPERATOR_SENTINEL not in [m["to"] for m in env.sent]     # auto_notify is off
    again = env.client.post("/api/v1/listings/leads/confirm", json={"lead_id": lead_id, "token": token})
    assert again.get_json()["already_confirmed"] is True
    assert len(env.rows) == 2 and len([m for m in env.sent if m["to"] == ADMIN_INBOX]) == 1


def test_a_token_only_confirms_the_lead_it_was_issued_for(env):
    lead_a = env.client.post("/api/v1/listings/dfw-40/intro", json=INTRO,
                             headers=_bearer()).get_json()["lead_id"]
    lead_b = env.client.post("/api/v1/listings/dfw-40/intro", json=INTRO,
                             headers=_bearer(user_id="u-bob", email="bob@other.example")).get_json()["lead_id"]
    _, token_a = _confirm_link(next(m["body"] for m in env.sent if m["to"] == "jane@acme.com"))
    commit_a = json.loads(env.rows[0]["entry_json"])["pii_commitment"]
    stale = ledger.confirm_token(ledger.ledger_secret(), lead_a, commit_a,
                                 issued_at=time.time() - ledger.CONFIRM_TOKEN_TTL_S - 60)
    flipped = token_a[:-1] + ("0" if token_a[-1] != "0" else "1")
    for lead_id, token in ((lead_b, token_a), (lead_a, flipped), (lead_a, stale), (lead_a, "")):
        r = env.client.post("/api/v1/listings/leads/confirm", json={"lead_id": lead_id, "token": token})
        assert (r.status_code, r.get_json()["error"]) == (400, "invalid_token")
    assert [row["event"] for row in env.rows] == ["intro_requested", "intro_requested"]


def test_a_repeat_request_returns_the_same_lead(env):
    first = env.client.post("/api/v1/listings/dfw-40/intro", json=INTRO, headers=_bearer()).get_json()
    second = env.client.post("/api/v1/listings/dfw-40/intro", json=INTRO, headers=_bearer()).get_json()
    assert second["duplicate"] is True and second["lead_id"] == first["lead_id"]
    assert [row["event"] for row in env.rows] == ["intro_requested"] and len(env.sent) == 1


def test_a_google_verified_user_is_registered_at_once(env):
    env.verified_via = "google"
    j = env.client.post("/api/v1/listings/dfw-40/intro", json=INTRO, headers=_bearer()).get_json()
    assert j["status"] == "registered" and j["confirmation"]["required"] is False
    assert (env.rows[0]["email_verified"], env.rows[0]["verified_via"]) == (True, "google")
    assert [m["to"] for m in env.sent] == [ADMIN_INBOX]


def test_an_opted_in_operator_gets_a_notice_quoting_the_register(env):
    env.listings[0]["contact"] = {"notify_email": OPERATOR_SENTINEL, "auto_notify": True}
    lead_id = _register_and_confirm(env)
    notices = [m for m in env.sent if m["to"] == OPERATOR_SENTINEL]
    assert len(notices) == 1 and lead_id in notices[0]["subject"]
    assert env.rows[0]["entry_hash"] in notices[0]["body"]
    assert "jane@acme.com" not in notices[0]["body"]          # email domain only
    assert [row["event"] for row in env.rows] == ["intro_requested", "email_confirmed", "operator_notified"]


def test_a_standing_requirement_needs_something_to_match(env):
    vague = {**INTRO, "requirement": {"timeline": "soon"}}
    r = env.client.post("/api/v1/listings/interest", json=vague, headers=_bearer())
    assert r.status_code == 422 and "requirement" in r.get_json()["fields"]
    good = {**INTRO, "requirement": {"markets": ["Dallas", "Phoenix"], "capacity_mw": 20}}
    j = env.client.post("/api/v1/listings/interest", json=good, headers=_bearer()).get_json()
    assert (j["kind"], j["listing"], j["matching_listings"]) == ("standing_requirement", None, 1)
    assert [(r["event"], r["listing_id"]) for r in env.rows] == [("interest_registered", None)]


def test_without_a_ledger_key_no_lead_is_registered(env, monkeypatch):
    monkeypatch.setattr(el.ledger, "ledger_secret", lambda environ=None: None)
    r = env.client.post("/api/v1/listings/dfw-40/intro", json=INTRO, headers=_bearer())
    assert (r.status_code, r.get_json()["error"]) == (503, "ledger_unavailable")
    assert env.rows == [] and env.sent == []


# ── the record ────────────────────────────────────────────────────────────

def test_the_public_record_masks_the_prospect_and_names_tampering(env):
    lead_id = _register_and_confirm(env)
    r = env.client.get(f"/api/v1/listings/leads/{lead_id}/verify")
    body, j = r.get_data(as_text=True), r.get_json()
    assert r.status_code == 200 and j["status"] == "registered"
    assert j["prospect"] == {"company": "Acme Capital", "email_domain": "acme.com",
                             "email_verified": True, "verified_via": "email_link"}
    assert "jane@acme.com" not in body and "Jane Doe" not in body
    assert j["chain"]["intact"] is True and j["chain"]["entries_checked"] == 2
    assert all(ev["hash_valid"] and ev["signature_valid"] for ev in j["events"])

    entry = json.loads(env.rows[0]["entry_json"])
    entry["created_at"] = "2026-01-01T00:00:00.000000+00:00"        # back-date it
    env.rows[0]["entry_json"] = ledger.canonical_json(entry)
    el._CHAIN_CACHE.update(at=0.0, value=None)
    j2 = env.client.get(f"/api/v1/listings/leads/{lead_id}/verify").get_json()
    assert j2["events"][0]["hash_valid"] is False
    assert j2["chain"]["intact"] is False and j2["chain"]["first_break_seq"] == env.rows[0]["seq"]


def test_every_listing_answer_says_confidential_and_never_cc_by(env):
    """The MCP gateway stamps CC-BY-4.0 on any response that does not carry its
    own citation. Listing data must not reach an agent labelled as
    free to republish — walls included, since the tools relay them as results."""
    lead_id = _register_and_confirm(env, headers=_bearer(user_id="u-ann", email="ann@firm.example"))
    responses = {
        "feed": env.client.get("/api/v1/listings"),
        "detail (walled)": env.client.get("/api/v1/listings/dfw-40"),
        "detail (open)": env.client.get("/api/v1/listings/dfw-40", headers=_bearer()),
        "terms": env.client.get("/api/v1/listings/terms"),
        "wall": env.client.post("/api/v1/listings/dfw-40/intro", json=INTRO),
        "intro": env.client.post("/api/v1/listings/dfw-40/intro", json=INTRO, headers=_bearer()),
        "interest": env.client.post("/api/v1/listings/interest", headers=_bearer(),
                                    json={**INTRO, "requirement": {"markets": ["Dallas"]}}),
        "record": env.client.get(f"/api/v1/listings/leads/{lead_id}/verify"),
    }
    for label, r in responses.items():
        j, body = r.get_json(), r.get_data(as_text=True)
        assert j["citation"]["license"] == el.LISTING_LICENSE, label
        assert j["citation"]["redistribution"] == "not_permitted", label
        assert "CC-BY" not in body, label


def test_the_operator_ledger_is_scoped_and_hides_email_until_introduced(env):
    env.listings.append(_listing(id=2, slug="phx-60", title="Phoenix"))
    lead_id = _register_and_confirm(env)
    secret = ledger.ledger_secret()
    wrong = ledger.operator_token(secret, 2)
    assert env.client.get(f"/api/v1/listings/dfw-40/leads?token={wrong}").status_code == 403
    token = ledger.operator_token(secret, 1)
    j = env.client.get(f"/api/v1/listings/dfw-40/leads?token={token}").get_json()
    assert [lead["lead_id"] for lead in j["leads"]] == [lead_id]
    assert (j["leads"][0]["name"], j["leads"][0]["email_domain"], j["leads"][0]["email"]) == (
        "Jane Doe", "acme.com", None)
    # the operator sees HOW the inbox was proven, so a Google-verified lead does
    # not read as "not yet confirmed" merely because no link was clicked
    assert (j["leads"][0]["email_verified"], j["leads"][0]["verified_via"]) == (True, "email_link")
    r = env.client.post(f"/api/v1/admin/listings/leads/{lead_id}/status",
                        json={"status": "introduced"}, headers={"X-Admin-Key": ADMIN_KEY})
    assert r.status_code == 200
    j = env.client.get(f"/api/v1/listings/dfw-40/leads?token={token}").get_json()
    assert j["leads"][0]["email"] == "jane@acme.com" and j["leads"][0]["status"] == "introduced"


# ── admin ─────────────────────────────────────────────────────────────────

_ADMIN_ROUTES = [
    ("get", "/api/v1/admin/listings"),
    ("post", "/api/v1/admin/listings"),
    ("patch", "/api/v1/admin/listings/1"),
    ("delete", "/api/v1/admin/listings/1"),
    ("get", "/api/v1/admin/listings/leads"),
    ("post", "/api/v1/admin/listings/leads/LD-0000000000/notify-operator"),
    ("post", "/api/v1/admin/listings/leads/LD-0000000000/status"),
    ("post", "/api/v1/admin/listings/1/operator-link"),
    ("get", "/api/v1/admin/listings/ledger/verify"),
]


@pytest.mark.parametrize("method,path", _ADMIN_ROUTES)
def test_admin_routes_open_to_the_admin_header_alone(env, monkeypatch, method, path):
    call = getattr(env.client, method)
    for kwargs in ({}, {"headers": {"X-Internal-Key": INTERNAL_KEY}},
                   {"headers": {"X-Admin-Key": "wrong"}}, {"query_string": {"admin_key": ADMIN_KEY}}):
        assert call(path, **kwargs).status_code == 401, kwargs
    monkeypatch.delenv("DCHUB_ADMIN_KEY")
    assert call(path, headers={"X-Admin-Key": ADMIN_KEY}).status_code == 401
    assert call(path, headers={"X-Admin-Key": ""}).status_code == 401


def test_the_admin_header_does_open_the_admin_routes(env):
    """Control for the test above: without it, a gate that refused everyone
    would pass every one of those 401s."""
    r = env.client.get("/api/v1/admin/listings/ledger/verify", headers={"X-Admin-Key": ADMIN_KEY})
    assert r.status_code == 200 and r.get_json()["signing_key_configured"] is True
    r = env.client.post("/api/v1/admin/listings", json={"title": "x", "slug": "interest"},
                        headers={"X-Admin-Key": ADMIN_KEY})
    assert r.status_code == 400 and "reserved" in r.get_json()["message"]
