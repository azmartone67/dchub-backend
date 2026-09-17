"""Pocket listings: the auth wall, the operator-contact rule and the lead
register, exercised as REQUESTS against the real blueprint.

What these pin, and why each matters to the program:
  * teasers are public, detail is walled, operator contact is never served —
    a buyer who has the operator's number never needs DC Hub's introduction;
  * our MCP gateway's X-Internal-Key is not an identity: an anonymous agent
    stays anonymous, and a user credential forwarded through the gateway still
    resolves (map_tier_gating maps the internal key to 'pro' at STEP 1);
  * an identified caller accepts the introduction terms once, recorded in the
    register under the terms version, before a walled listing opens;
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
        elif s == ("SELECT event FROM listing_lead_ledger WHERE lead_id = %s AND event = ANY(%s) "
                   "ORDER BY seq ASC LIMIT 1"):
            # append's decision guard: the first blocking event this lead has, if any.
            lead_id, names = params
            found = next((r["event"] for r in self.store.rows
                          if r["lead_id"] == lead_id and r["event"] in names), None)
            self._one = (found,) if found else None
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
    monkeypatch.setattr(el, "_db_recent_catalogue_read", lambda user_ref, filters_key, since: any(
        r["event"] == "catalogue_read" and r["user_ref"] == user_ref
        and (r.get("meta") or {}).get("filters_key") == filters_key
        and r["created_at"] > since for r in e.rows))
    monkeypatch.setattr(el, "_db_terms_accepted", lambda user_ref, version: any(
        r["user_ref"] == user_ref and r["terms_version"] == version
        and r["event"] in ("terms_accepted", "intro_requested", "interest_registered")
        for r in e.rows))
    monkeypatch.setattr(el, "_db_chain_rows", lambda limit: [dict(r) for r in e.rows][:limit])
    monkeypatch.setattr(el, "_db_verified_via", lambda viewer: e.verified_via)

    def viewer_lead_events(user_ref, listing_id):
        opened = {r["lead_id"] for r in e.rows if r["user_ref"] == user_ref
                  and r["listing_id"] == listing_id and r["event"] == "intro_requested"}
        return [dict(r) for r in e.rows if r["lead_id"] in opened]

    monkeypatch.setattr(el, "_db_viewer_lead_events", viewer_lead_events)
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


def _accept_terms(e, headers):
    """Accept the introduction terms as the caller behind `headers`."""
    r = e.client.post("/api/v1/listings/terms/accept",
                      json={"accept_terms": True, "terms_version": el.TERMS_VERSION}, headers=headers)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r


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
    _accept_terms(env, headers)
    j = env.client.get("/api/v1/listings/dfw-40", headers=headers).get_json()
    assert j["locked"] is False
    assert j["viewer"]["identified"] is True and j["viewer"]["channel"] == "mcp"


def test_a_signed_in_user_opens_the_listing_without_operator_contact(env):
    _accept_terms(env, _bearer())
    r = env.client.get("/api/v1/listings/dfw-40", headers=_bearer())
    body, j = r.get_data(as_text=True), r.get_json()
    listing = j["listing"]
    assert j["locked"] is False and j["access"]["granted"] is True
    # The specs view: no coordinates until the provider accepts a registration (2026-09-15).
    assert (listing["latitude"], listing["longitude"]) == (None, None)
    assert j["disclosure"]["released"] is False and j["disclosure"]["status"] == "none"
    assert listing["asking_price"] == 1250000.0
    # `power` is a reserved detail key (routes/exclusive_listings.py
    # _DETAIL_RESERVED_KEYS). Free text there fails its rules, so it reads as
    # null and the generic detail does not repeat it.
    assert listing["detail"] == {"available": "Q2 2027"} and listing["power"] is None
    assert "contact" not in listing and "owner_id" not in listing
    for secret in (OPERATOR_SENTINEL, OWNER_SENTINEL, "hidden-note"):
        assert secret not in body
    assert j["introduction"]["operator_contact"] == "shared_after_acceptance"
    assert j["viewer"]["email_masked"] == "j***@acme.com"
    assert "jane@acme.com" not in body.lower()


def test_an_identified_view_is_recorded_once_per_day(env):
    _accept_terms(env, _bearer())
    for _ in range(3):
        assert env.client.get("/api/v1/listings/dfw-40", headers=_bearer()).status_code == 200
    views = [r for r in env.rows if r["event"] == "listing_viewed"]
    assert len(views) == 1
    assert (views[0]["listing_id"], views[0]["user_ref"], views[0]["lead_id"]) == (1, "u-jane", None)
    # Two rows in the register, not one: the terms acceptance recorded before
    # the first view, then the single view.
    assert [r["event"] for r in env.rows] == ["terms_accepted", "listing_viewed"]


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
    headers = {"X-API-Key": "dch_live_" + "b" * 32}
    _accept_terms(env, headers)
    j = env.client.get("/api/v1/listings/dfw-40", headers=headers).get_json()
    assert j["locked"] is False
    assert (j["viewer"]["identity_source"], j["viewer"]["channel"]) == ("api_key", "api")


def test_a_pro_listing_asks_a_free_user_to_upgrade(env):
    env.listings[0]["tier_required"] = "pro"
    free = env.client.get("/api/v1/listings/dfw-40", headers=_bearer(plan="free")).get_json()
    assert free["locked"] is True and free["access"]["reason"] == "upgrade_required"
    assert free["access"]["unlock"]["pricing_url"].endswith("/pricing")
    _accept_terms(env, _bearer(plan="pro"))
    pro = env.client.get("/api/v1/listings/dfw-40", headers=_bearer(plan="pro")).get_json()
    assert pro["locked"] is False


def test_drafts_and_reserved_words_are_not_listings(env):
    env.listings.append(_listing(id=2, slug="secret-draft", status="draft"))
    assert env.client.get("/api/v1/listings/secret-draft", headers=_bearer()).status_code == 404
    assert env.client.get("/api/v1/listings/interest").status_code == 404
    assert env.client.get("/api/v1/listings/terms").get_json()["terms"]["version"] == el.TERMS_VERSION


# ── introduction terms ────────────────────────────────────────────────────

def test_a_signed_in_user_who_has_not_accepted_the_terms_gets_the_teaser(env):
    r = env.client.get("/api/v1/listings/dfw-40", headers=_bearer())
    j = r.get_json()
    assert r.status_code == 200 and j["viewer"]["identified"] is True
    assert j["locked"] is True
    access = j["access"]
    assert (access["granted"], access["reason"]) == (False, "terms_acceptance_required")
    assert access["unlock"]["accept"]["path"] == "/api/v1/listings/terms/accept"
    assert access["unlock"]["mcp_steps"] == ["accept_capacity_terms"]
    assert access["unlock"]["terms"]["version"] == el.TERMS_VERSION
    for private in ("latitude", "longitude", "asking_price", "detail"):
        assert private not in j["listing"]
    assert [row["event"] for row in env.rows] == []          # a locked view records no listing_viewed


def test_accepting_the_terms_is_recorded_once_and_opens_the_listing(env):
    j = _accept_terms(env, _bearer()).get_json()
    assert (j["ok"], j["accepted"], j["already_accepted"]) == (True, True, False)
    assert j["terms"]["version"] == el.TERMS_VERSION and j["ledger"]["seq"] == env.rows[0]["seq"]
    detail = env.client.get("/api/v1/listings/dfw-40", headers=_bearer()).get_json()
    assert detail["locked"] is False and detail["listing"]["asking_price"] == 1250000.0
    accepted = [row for row in env.rows if row["event"] == "terms_accepted"]
    assert len(accepted) == 1
    assert (accepted[0]["user_ref"], accepted[0]["lead_id"], accepted[0]["terms_version"],
            accepted[0]["channel"]) == ("u-jane", None, el.TERMS_VERSION, "web")
    # user_ref identifies the account; the entry stores and commits no prospect PII
    assert (accepted[0]["email"], accepted[0]["name"], accepted[0]["company"]) == (None, None, None)
    assert json.loads(accepted[0]["entry_json"])["pii_commitment"] is None


def test_accepting_the_terms_twice_writes_one_entry(env):
    first = _accept_terms(env, _bearer()).get_json()
    second = _accept_terms(env, _bearer()).get_json()
    assert (first["already_accepted"], second["already_accepted"]) == (False, True)
    assert [row["event"] for row in env.rows] == ["terms_accepted"]


def test_an_acceptance_under_an_older_terms_version_does_not_count(env):
    el._append_event(secret=ledger.ledger_secret(), lead_id=None, event="terms_accepted", listing=None,
                     user_ref="u-jane", channel="web", terms_version="1999-01-01")
    j = env.client.get("/api/v1/listings/dfw-40", headers=_bearer()).get_json()
    assert j["locked"] is True and j["access"]["reason"] == "terms_acceptance_required"
    # Control: accepting the current version is a new entry, and then the listing opens.
    assert _accept_terms(env, _bearer()).get_json()["already_accepted"] is False
    assert [row["terms_version"] for row in env.rows] == ["1999-01-01", el.TERMS_VERSION]
    assert env.client.get("/api/v1/listings/dfw-40", headers=_bearer()).get_json()["locked"] is False


@pytest.mark.parametrize("path,body,event", [
    ("/api/v1/listings/dfw-40/intro", INTRO, "intro_requested"),
    ("/api/v1/listings/interest", {**INTRO, "requirement": {"markets": ["Dallas"], "capacity_mw": 20}},
     "interest_registered"),
])
def test_a_request_registered_under_the_current_terms_counts_as_acceptance(env, path, body, event):
    assert env.client.post(path, json=body, headers=_bearer()).status_code == 200
    j = env.client.get("/api/v1/listings/dfw-40", headers=_bearer()).get_json()
    assert j["locked"] is False
    assert [row["event"] for row in env.rows] == [event, "listing_viewed"]    # no terms_accepted row needed


@pytest.mark.parametrize("who,body,status,error", [
    ("anon", {"accept_terms": True, "terms_version": el.TERMS_VERSION}, 401, "identity_required"),
    ("gateway", {"accept_terms": True, "terms_version": el.TERMS_VERSION}, 401, "identity_required"),
    ("user", {"accept_terms": False, "terms_version": el.TERMS_VERSION}, 422, "terms_not_accepted"),
    ("user", {"accept_terms": "yes", "terms_version": el.TERMS_VERSION}, 422, "terms_not_accepted"),
    ("user", {"accept_terms": True, "terms_version": "1999-01-01"}, 409, "terms_version_mismatch"),
])
def test_the_terms_are_not_recorded_without_identity_consent_and_the_current_version(env, who, body,
                                                                                     status, error):
    headers = {"anon": {}, "gateway": {"X-Internal-Key": INTERNAL_KEY}, "user": _bearer()}[who]
    r = env.client.post("/api/v1/listings/terms/accept", json=body, headers=headers)
    assert (r.status_code, r.get_json()["error"]) == (status, error)
    assert env.rows == [] and env.sent == []


def test_the_feed_shows_a_signed_in_user_that_the_terms_keep_a_listing_locked(env):
    item = env.client.get("/api/v1/listings", headers=_bearer()).get_json()["items"][0]
    assert item["locked"] is True and item["lock_reason"] == "terms_acceptance_required"
    # Control: the feed reads the acceptance too, not only the detail view.
    _accept_terms(env, _bearer())
    item = env.client.get("/api/v1/listings", headers=_bearer()).get_json()["items"][0]
    assert item["locked"] is False and item["lock_reason"] is None


def test_access_stays_locked_when_a_caller_does_not_pass_terms_ok(env):
    viewer = {"tier": "free", "identified": True, "email": "jane@acme.com", "user_ref": "u-jane",
              "user_id": "u-jane", "identity_source": "session", "channel": "web",
              "platform": None, "session_hash": None, "api_key": None, "reason": None}
    with env.client.application.test_request_context(headers=_bearer()):
        assert set(viewer) == set(el._viewer())      # the keys _viewer() returns
    # ?l= on purpose: this argument is the SIGN-IN RETURN path, which stays on
    # the index form because /listings/<slug> is a static teaser that never
    # hydrates. tests/test_listing_url_shape.py pins that split.
    access = el._access(_listing(), viewer, "/listings?l=x")
    assert (access["granted"], access["reason"]) == (False, "terms_acceptance_required")
    # Control: the same viewer passing the acceptance is let in.
    assert el._access(_listing(), viewer, "/listings?l=x", terms_ok=True)["granted"] is True


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


def _summary_live(env, monkeypatch):
    """GET /api/v1/listings/summary answering from the fixture's listings.
    cached_listings_summary reads a DSN and a process-wide cache, neither of
    which the env fixture provides, so both are opened here and the REAL
    _build_summary runs over teaser-level rows."""
    monkeypatch.setattr(el, "_dsn", lambda: "postgres://listings-test")
    monkeypatch.setattr(el, "_ensure_schema", lambda: None)
    monkeypatch.setattr(el, "_db_live_listing_facts", lambda: [
        {"market": r["market"], "state": r["state"], "country": r["country"],
         "capacity_mw": r["capacity_mw"], "delivery_type": None,
         "updated_at": r["updated_at"]}
        for r in env.listings if r["status"] in ("pocket", "public")])
    el._SUMMARY_CACHE.update(at=None, value=None)
    r = env.client.get("/api/v1/listings/summary")
    el._SUMMARY_CACHE.update(at=None, value=None)
    return r


def test_the_teaser_is_public_and_the_full_detail_stays_confidential(env, monkeypatch):
    """The licence split (2026-09-16). The MCP gateway stamps CC-BY-4.0 on any
    response that does not carry its own citation, so the backend citation is
    what decides. Teaser-level facts are now on a crawlable /listings/<slug>
    page and are QUOTABLE with attribution; full detail, released identity and
    every registration answer stay confidential. Labelling a fact that search
    engines already index as "not for redistribution" is one surface
    contradicting the other."""
    lead_id = _register_and_confirm(env, headers=_bearer(user_id="u-ann", email="ann@firm.example"))
    accepted = _accept_terms(env, _bearer())
    summary = _summary_live(env, monkeypatch)
    public = {
        "feed": env.client.get("/api/v1/listings"),
        "detail (walled)": env.client.get("/api/v1/listings/dfw-40"),
        "detail (anonymous via our gateway)": env.client.get(
            "/api/v1/listings/dfw-40", headers={"X-Internal-Key": INTERNAL_KEY}),
        "summary": summary,
    }
    confidential = {
        "detail (open)": env.client.get("/api/v1/listings/dfw-40", headers=_bearer()),
        "terms": env.client.get("/api/v1/listings/terms"),
        "terms accepted": accepted,
        "wall": env.client.post("/api/v1/listings/dfw-40/intro", json=INTRO),
        "intro": env.client.post("/api/v1/listings/dfw-40/intro", json=INTRO, headers=_bearer()),
        "interest": env.client.post("/api/v1/listings/interest", headers=_bearer(),
                                    json={**INTRO, "requirement": {"markets": ["Dallas"]}}),
        "record": env.client.get(f"/api/v1/listings/leads/{lead_id}/verify"),
    }
    # ★ NON-VACUITY. The confidential half is trivially satisfied by a response
    #   that never unlocked, and the public half by a summary that failed to
    #   build — so both are proved to be the thing they claim to be first.
    assert confidential["detail (open)"].get_json()["locked"] is False
    assert "asking_price" in confidential["detail (open)"].get_json()["listing"]
    assert public["detail (walled)"].get_json()["locked"] is True
    assert summary.get_json()["ok"] is True and summary.get_json()["live_count"] == 1

    for label, r in public.items():
        j, body = r.get_json(), r.get_data(as_text=True)
        assert j["citation"]["license"] == el.TEASER_LICENSE == "CC-BY-4.0", label
        assert j["citation"]["redistribution"] == "permitted_with_attribution", label
        # Attribution is the CONDITION of the grant, so the string an agent
        # would paste has to name DC Hub.
        assert "DC Hub" in j["citation"]["cite_as"], label
        assert el.LISTING_LICENSE not in body, label
        for secret in (OPERATOR_SENTINEL, OWNER_SENTINEL, "hidden-note"):
            assert secret not in body, label

    for label, r in confidential.items():
        j, body = r.get_json(), r.get_data(as_text=True)
        assert j["citation"]["license"] == el.LISTING_LICENSE, label
        assert j["citation"]["redistribution"] == "not_permitted", label
        assert "CC-BY" not in body, label


def test_an_anonymous_detail_response_carries_no_identity_at_any_licence(env):
    """The teaser page is indexable, so this response is the one a crawler and
    an unidentified agent both read. Quotable is not the same as complete: no
    provider identity, no site, no coordinates, no contact, and a `disclosure`
    that releases nothing."""
    r = env.client.get("/api/v1/listings/dfw-40")
    j, body = r.get_json(), r.get_data(as_text=True)
    assert j["locked"] is True
    listing = j["listing"]
    # ★ The control: the teaser IS being served, so the absences below mean
    #   "withheld", not "empty response".
    assert listing["slug"] == "dfw-40" and listing["market"] == "Dallas"
    assert listing["capacity_mw"] == 40.0 and listing["title"]
    for private in ("latitude", "longitude", "asking_price", "detail",
                    "contact", "owner_id", "verification", "price", "power"):
        assert private not in listing, private
    assert listing["provider"] is None
    assert j["disclosure"] == {"released": False}
    for secret in (OPERATOR_SENTINEL, OWNER_SENTINEL, "hidden-note",
                   "32.776712", "-96.797012", "1250000"):
        assert secret not in body, secret
    # ...and it is the PUBLIC citation that ships with it.
    assert j["citation"]["license"] == el.TEASER_LICENSE


def test_the_operator_ledger_is_scoped_and_hides_the_buyer_until_accepted(env):
    env.listings.append(_listing(id=2, slug="phx-60", title="Phoenix"))
    lead_id = _register_and_confirm(env)
    secret = ledger.ledger_secret()
    wrong = ledger.operator_token(secret, 2)
    assert env.client.get(f"/api/v1/listings/dfw-40/leads?token={wrong}").status_code == 403
    token = ledger.operator_token(secret, 1)
    j = env.client.get(f"/api/v1/listings/dfw-40/leads?token={token}").get_json()
    assert [lead["lead_id"] for lead in j["leads"]] == [lead_id]
    # Deal registration (2026-09-15): company and requirement only until the operator accepts.
    lead = j["leads"][0]
    assert (lead["company"], lead["name"], lead["role"], lead["email_domain"], lead["email"]) == (
        "Acme Capital", None, None, None, None)
    # the operator sees HOW the inbox was proven, so a Google-verified lead does
    # not read as "not yet confirmed" merely because no link was clicked
    assert (lead["email_verified"], lead["verified_via"]) == (True, "email_link")
    # An introduction needs the operator's acceptance first.
    r = env.client.post(f"/api/v1/admin/listings/leads/{lead_id}/status",
                        json={"status": "introduced"}, headers={"X-Admin-Key": ADMIN_KEY})
    assert (r.status_code, r.get_json()["error"]) == (409, "not_accepted")
    r = env.client.post(f"/api/v1/listings/dfw-40/leads/{lead_id}/decision",
                        json={"token": token, "decision": "accept"})
    assert r.status_code == 200, r.get_data(as_text=True)
    r = env.client.post(f"/api/v1/admin/listings/leads/{lead_id}/status",
                        json={"status": "introduced"}, headers={"X-Admin-Key": ADMIN_KEY})
    assert r.status_code == 200
    j = env.client.get(f"/api/v1/listings/dfw-40/leads?token={token}").get_json()
    assert (j["leads"][0]["name"], j["leads"][0]["email"], j["leads"][0]["status"]) == (
        "Jane Doe", "jane@acme.com", "introduced")


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
