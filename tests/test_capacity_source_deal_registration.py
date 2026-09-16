"""Capacity Source deal registration (2026-09-15): the provider accepts or
declines a registration, and what each side sees before and after.

Exercised as requests against the real blueprint with the harness of
test_pocket_listings_wall_and_leads.py: identity is a signed JWT through the
real tier resolver (`main` stubbed empty), reads sit behind the module's own
_db_* seams, and every register write goes through the real
util/listing_ledger.append against a stand-in cursor that also answers
append's decision guard and fails on any statement it does not expect.

What these pin:
  * POST /api/v1/listings/<slug>/leads/<lead_id>/decision takes the listing's
    operator token in the BODY. registered or operator_notified -> accepted or
    declined; the same decision again is duplicate:true; the other one is 409
    already_decided with the current status; an unconfirmed lead is 409
    not_registered, a withdrawn one 409 lead_withdrawn; an unknown lead, a
    standing requirement or another listing's lead is 404;
  * the admin status route records accepted and declined on the provider's
    behalf through the same function, and introduced needs acceptance first;
  * a decision the ledger already holds is never written twice, even when the
    status was read before it landed;
  * before acceptance the provider sees the company and a scrubbed requirement,
    in its notice and its ledger alike; after, the buyer's name, role, email
    and message;
  * accept emails the buyer the provider, the site and its contact, and the
    provider the buyer; decline emails the buyer with nothing about the
    provider; the admin inbox gets a copy of each;
  * get_listing's `disclosure` for none, pending, declined, accepted,
    introduced and withdrawn; a locked caller gets {"released": false} alone;
  * the public record and the confirmation answer disclose nothing new;
  * co-marketing: the draft names no market, state or site, its search URL
    carries the provider's live regions, and the record stays admin-only;
  * admin writes warn when the title, summary or slug names an undisclosed
    provider.
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

# Built at runtime, so no key-shaped literal sits in a tracked file.
JWT_SECRET = "-".join(("deal", "registration", "jwt", "fixture", "0123456789abcdef"))
INTERNAL_KEY = "-".join(("deal", "registration", "gateway", "fixture"))
ADMIN_KEY = "-".join(("deal", "registration", "admin", "fixture", "0123456789"))
ADMIN = {"X-Admin-Key": ADMIN_KEY}
ADMIN_INBOX = "ops@dchub.example"
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)

PROVIDER = "Quiet Harbor Infrastructure"
OPERATOR_EMAIL = "leasing@quietharbor.example"
NOTICE_EMAIL = "notices@quietharbor.example"
LINKEDIN_POST = "https://www.linkedin.com/posts/quiet-harbor-capacity-7300"
SITE = {"name": "Seagoville Campus", "address": "100 Industrial Blvd", "city": "Seagoville",
        "postal_code": "75159", "parcel_id": "APN 42-17"}
# What identifies the provider or its site, and what identifies the buyer.
PROVIDER_SECRETS = (PROVIDER, "Seagoville", "Industrial Blvd", "APN 42-17", "32.7767", "96.797",
                    "Dana Reyes", "VP Leasing", "quietharbor", "214 555 0100", "QH Holdings",
                    "linkedin.com/posts")
BUYER_SECRETS = ("Jane Doe", "VP Development", "jane@acme.com", "acme.com",
                 "Interested in the shell", "512-555-0199", "7946 0958")

INTRO = {"name": "Jane Doe", "company": "Acme Capital", "role": "VP Development",
         "requirement": {"capacity_mw": 40, "timeline": "Q2 2027 or email jane@acme.com",
                         "use_case": "AI inference, see https://acme.com/rfp",
                         "notes": "Budget approved 2026-2028. Reach me on +44 20 7946 0958 "
                                  "or www.acme.com/team"},
         "message": "Interested in the shell. Call me at 512-555-0199.", "accept_terms": True,
         "terms_version": el.TERMS_VERSION}
SCRUBBED = {"capacity_mw": 40, "timeline": "Q2 2027 or email [email removed]",
            "use_case": "AI inference, see [link removed]",
            "notes": "Budget approved 2026-2028. Reach me on [phone removed] or [link removed]"}

_INSERT_COLS = (
    "lead_id", "event", "listing_id", "listing_slug", "listing_title", "user_ref",
    "email", "name", "role", "company", "message", "requirement", "email_domain",
    "email_verified", "verified_via", "channel", "platform", "client",
    "session_hash", "ip_hash", "user_agent", "terms_version", "meta", "created_at",
    "prev_hash", "entry_json", "entry_hash", "signature", "key_id")
_GUARD_SQL = ("SELECT event FROM listing_lead_ledger WHERE lead_id = %s AND event = ANY(%s) "
              "ORDER BY seq ASC LIMIT 1")


def _listing(**over):
    row = {"id": 1, "slug": "dfw-40", "title": "Powered shell — DFW",
           "summary": "Energized next year.", "status": "pocket",
           "tier_required": "registered", "market": "Dallas", "state": "TX",
           "country": "US", "latitude": 32.776712, "longitude": -96.797012,
           "capacity_mw": 40.0, "asking_price": 1250000, "asking_currency": "USD",
           "detail": {"available": "Q2 2027", "delivery_type": "powered_shell",
                      "update_cadence": "weekly", "provider": {"name": PROVIDER, "disclosed": False},
                      "site": dict(SITE),
                      "power": {"utility": "Oncor", "substation": "Seagoville 345kV",
                                "interconnection_stage": "energized"}},
           "contact": {"name": "Dana Reyes", "title": "VP Leasing", "email": OPERATOR_EMAIL,
                       "phone": "+1 214 555 0100", "notify_email": NOTICE_EMAIL,
                       "auto_notify": False, "company": "QH Holdings LLC",
                       "co_marketing": {"linkedin_post_url": LINKEDIN_POST, "posted_at": "2026-09-10"}},
           "owner_id": "owner-sentinel",
           "created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
           "updated_at": datetime(2026, 9, 2, tzinfo=timezone.utc), "expires_at": None}
    row.update(over)
    return row


def _decode_jsonb(values):
    for key in ("detail", "contact"):
        if isinstance(values.get(key), str):
            values[key] = json.loads(values[key])
    return values


class _Cursor:
    """The register's append statements (guard included) and the admin
    routes' listing INSERT / UPDATE; any other statement fails the test."""

    def __init__(self, env):
        self.env, self._one = env, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        rows = self.env.rows
        if s.startswith("SELECT pg_advisory_xact_lock("):
            self._one = ("",)
        elif s == _GUARD_SQL:
            lead_id, names = params
            self.env.guard_checks.append((lead_id, list(names)))
            found = next((r["event"] for r in rows if r["lead_id"] == lead_id and r["event"] in names),
                         None)
            self._one = (found,) if found else None
        elif s == "SELECT entry_hash FROM listing_lead_ledger ORDER BY seq DESC LIMIT 1":
            self._one = (rows[-1]["entry_hash"],) if rows else None
        elif s.startswith("INSERT INTO listing_lead_ledger"):
            row = dict(zip(_INSERT_COLS, params))
            for key in ("requirement", "client", "meta"):
                if row[key] is not None:
                    row[key] = json.loads(row[key])
            row["created_at"] = datetime.fromisoformat(row["created_at"])
            row["seq"] = len(rows) + 1
            rows.append(row)
            self._one = (row["seq"],)
        elif s.startswith("INSERT INTO exclusive_listings ("):
            cols = s[s.index("(") + 1:s.index(")")].split(", ")
            row = _decode_jsonb(dict(zip(cols, params, strict=True)))
            row.update(id=len(self.env.listings) + 1, created_at=NOW, updated_at=NOW)
            self.env.listings.append(row)
            self._one = (row["id"], row["slug"])
        elif s.startswith("UPDATE exclusive_listings SET "):
            head = "UPDATE exclusive_listings SET "
            assignments = s[len(head):s.index(" WHERE id = %s")].split(", ")
            cols = [a.split(" = ")[0] for a in assignments if "%s" in a]
            *values, lid = params
            row = next((r for r in self.env.listings if r["id"] == lid), None)
            if row is not None:
                row.update(_decode_jsonb(dict(zip(cols, values, strict=True))), updated_at=NOW)
            self._one = (row["id"], row["slug"], row["status"], row["tier_required"]) if row else None
        else:
            raise AssertionError(f"unexpected SQL: {s[:90]}")

    def fetchone(self):
        return self._one


class _Conn:
    def __init__(self, env):
        self.env = env

    def cursor(self):
        return _Cursor(self.env)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _live(row):
    return row["status"] in ("pocket", "public") and row["expires_at"] is None


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setitem(sys.modules, "main", types.ModuleType("main"))
    for var in ("DATABASE_URL", "DCHUB_LEAD_LEDGER_SECRET", "DCHUB_SYNC_KEY",
                "INTERNAL_WORKER_SECRET", "ADMIN_INBOX_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", INTERNAL_KEY)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setenv("DCHUB_ADMIN_EMAIL", ADMIN_INBOX)

    e = types.SimpleNamespace(rows=[], sent=[], listings=[_listing()], verified_via=None,
                              guard_checks=[])

    def get_listing(ident):
        return next((dict(r) for r in e.listings if str(r["id"]) == str(ident) or r["slug"] == str(ident)),
                    None)

    def viewer_lead_events(user_ref, listing_id):
        opened = {r["lead_id"] for r in e.rows if r["user_ref"] == user_ref
                  and r["listing_id"] == listing_id and r["event"] == "intro_requested"}
        return [dict(r) for r in e.rows if r["lead_id"] in opened]

    def provider_live_countries(name):
        return [r["country"] for r in e.listings if _live(r)
                and isinstance((r["detail"] or {}).get("provider"), dict)
                and str(r["detail"]["provider"].get("name", "")).lower() == name.lower()]

    monkeypatch.setattr(el, "_now", lambda: NOW)
    monkeypatch.setattr(el, "_conn", lambda: _Conn(e))
    monkeypatch.setattr(el, "_db_list_listings", lambda **kw: [dict(r) for r in e.listings if _live(r)])
    monkeypatch.setattr(el, "_db_count_live", lambda: len([r for r in e.listings if _live(r)]))
    monkeypatch.setattr(el, "_db_count_matching", lambda req: 0)
    monkeypatch.setattr(el, "_db_get_listing", get_listing)
    monkeypatch.setattr(el, "_db_all_listings", lambda: [dict(r) for r in e.listings])
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
    monkeypatch.setattr(el, "_db_terms_accepted", lambda user_ref, version: any(
        r["user_ref"] == user_ref and r["terms_version"] == version
        and r["event"] in ("terms_accepted", "intro_requested", "interest_registered")
        for r in e.rows))
    monkeypatch.setattr(el, "_db_chain_rows", lambda limit: [dict(r) for r in e.rows][:limit])
    monkeypatch.setattr(el, "_db_verified_via", lambda viewer: e.verified_via)
    monkeypatch.setattr(el, "_db_viewer_lead_events", viewer_lead_events)
    monkeypatch.setattr(el, "_db_provider_live_countries", provider_live_countries)
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


# ── helpers ───────────────────────────────────────────────────────────────

def _bearer(user_id="u-jane", email="Jane@Acme.com", plan="free"):
    token = jwt.encode({"user_id": user_id, "email": email, "plan": plan, "role": "user",
                        "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
                       JWT_SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


BOB = {"user_id": "u-bob", "email": "bob@other.example"}


def _accept_terms(e, headers):
    r = e.client.post("/api/v1/listings/terms/accept",
                      json={"accept_terms": True, "terms_version": el.TERMS_VERSION}, headers=headers)
    assert r.status_code == 200, r.get_data(as_text=True)


def _confirm_token(e, lead_id):
    mail = next(m for m in e.sent if lead_id in m["body"] and "confirm=" in m["body"])
    m = re.search(r"confirm=(LD-[0-9A-Z]{10})&amp;token=(\d+\.[0-9a-f]{40})", mail["body"])
    assert m and m.group(1) == lead_id
    return m.group(2)


def _intro(e, headers=None, slug="dfw-40"):
    r = e.client.post(f"/api/v1/listings/{slug}/intro", json=INTRO, headers=headers or _bearer())
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["lead_id"]


def _confirm(e, lead_id):
    r = e.client.post("/api/v1/listings/leads/confirm",
                      json={"lead_id": lead_id, "token": _confirm_token(e, lead_id)})
    assert r.status_code == 200, r.get_data(as_text=True)


def _registered(e, headers=None, slug="dfw-40"):
    lead_id = _intro(e, headers, slug)
    _confirm(e, lead_id)
    return lead_id


def _standing(e, headers):
    r = e.client.post("/api/v1/listings/interest", headers=headers,
                      json={**INTRO, "requirement": {"markets": ["Dallas"]}})
    assert r.status_code == 200, r.get_data(as_text=True)
    lead_id = r.get_json()["lead_id"]
    _confirm(e, lead_id)
    return lead_id


def _token(listing_id=1):
    return ledger.operator_token(ledger.ledger_secret(), listing_id)


def _decide(e, lead_id, decision, slug="dfw-40", token=None, **extra):
    return e.client.post(f"/api/v1/listings/{slug}/leads/{lead_id}/decision",
                         json={"token": _token() if token is None else token, "decision": decision,
                               **extra})


def _admin_status(e, lead_id, status, **extra):
    return e.client.post(f"/api/v1/admin/listings/leads/{lead_id}/status",
                         json={"status": status, **extra}, headers=ADMIN)


def _lead_rows(e, lead_id):
    return [r for r in e.rows if r["lead_id"] == lead_id]


def _detail(e, headers):
    r = e.client.get("/api/v1/listings/dfw-40", headers=headers)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json(), r.get_data(as_text=True)


def _ledger(e, slug="dfw-40", listing_id=1):
    r = e.client.get(f"/api/v1/listings/{slug}/leads?token={_token(listing_id)}")
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def _absent(texts, body):
    found = [t for t in texts if t in body]
    assert not found, found


# ── the decision route ────────────────────────────────────────────────────

def test_the_provider_accepts_with_its_token_in_the_body_and_a_repeat_is_idempotent(env):
    lead_id = _registered(env)
    r = _decide(env, lead_id, "accept", note="Happy to talk this week.")
    j = r.get_json()
    assert r.status_code == 200, j
    assert (j["ok"], j["lead_id"], j["decision"], j["status"], j["duplicate"]) == (
        True, lead_id, "accept", "accepted", False)
    assert j["listing"] == {"slug": "dfw-40", "title": "Powered shell — DFW"}
    decided = env.rows[-1]
    assert (decided["event"], decided["channel"], decided["listing_id"], decided["user_ref"]) == (
        "registration_accepted", "operator", 1, "u-jane")
    assert decided["meta"] == {"note": "Happy to talk this week."}
    assert (j["ledger"], j["decided_at"]) == (
        {"seq": decided["seq"], "entry_hash": decided["entry_hash"]},
        json.loads(decided["entry_json"])["created_at"])
    assert env.guard_checks[-1] == (lead_id, list(ledger.DECISION_BLOCKING_EVENTS))

    rows, sent = len(env.rows), len(env.sent)
    again = _decide(env, lead_id, "accept").get_json()
    assert (again["status"], again["duplicate"], again["ledger"]) == ("accepted", True, j["ledger"])
    other = _decide(env, lead_id, "decline")
    assert (other.status_code, other.get_json()["error"], other.get_json()["status"]) == (
        409, "already_decided", "accepted")
    assert (len(env.rows), len(env.sent)) == (rows, sent)      # nothing written or sent again
    assert ledger.lead_status(_lead_rows(env, lead_id)) == "accepted"


def test_a_declined_registration_stays_declined(env):
    lead_id = _registered(env)
    r = _decide(env, lead_id, "decline")
    assert (r.status_code, r.get_json()["status"], r.get_json()["duplicate"]) == (200, "declined", False)
    assert env.rows[-1]["meta"] is None
    assert _decide(env, lead_id, "decline").get_json()["duplicate"] is True
    r = _decide(env, lead_id, "accept")
    assert (r.status_code, r.get_json()["error"], r.get_json()["status"]) == (409, "already_decided", "declined")
    for status, error in (("introduced", "lead_declined"), ("withdrawn", "lead_declined"),
                          ("accepted", "already_decided")):
        r = _admin_status(env, lead_id, status)
        assert (r.status_code, r.get_json()["error"]) == (409, error), status
    r = env.client.post(f"/api/v1/admin/listings/leads/{lead_id}/notify-operator", headers=ADMIN)
    assert (r.status_code, r.get_json()["error"]) == (409, "not_registered")
    assert [r["event"] for r in _lead_rows(env, lead_id)] == [
        "intro_requested", "email_confirmed", "registration_declined"]


def test_the_decision_route_refuses_without_writing_anything(env):
    env.listings.append(_listing(id=2, slug="phx-60", title="Phoenix shell", market="Phoenix", state="AZ"))
    confirmed = _registered(env)
    pending = _intro(env, _bearer(**BOB))
    withdrawn = _registered(env, _bearer(user_id="u-cy", email="cy@third.example"))
    assert _admin_status(env, withdrawn, "withdrawn").status_code == 200
    standing = _standing(env, _bearer(user_id="u-dee", email="dee@fourth.example"))
    stale = ledger.operator_token(ledger.ledger_secret(), 1,
                                  issued_at=time.time() - ledger.OPERATOR_TOKEN_TTL_S - 60)
    cases = [
        ("dfw-40", pending, {"decision": "accept"}, 409, "not_registered"),
        ("dfw-40", withdrawn, {"decision": "decline"}, 409, "lead_withdrawn"),
        ("dfw-40", "LD-0000000000", {"decision": "accept"}, 404, "not_found"),
        ("dfw-40", "not-a-lead", {"decision": "accept"}, 404, "not_found"),
        ("dfw-40", standing, {"decision": "accept"}, 404, "not_found"),
        ("phx-60", confirmed, {"decision": "accept", "token": _token(2)}, 404, "not_found"),
        ("dfw-40", confirmed, {"decision": "accept", "token": _token(2)}, 403, "invalid_token"),
        ("dfw-40", confirmed, {"decision": "accept", "token": stale}, 403, "invalid_token"),
        ("dfw-40", confirmed, {"decision": "accept", "token": ""}, 403, "invalid_token"),
        ("no-such-listing", confirmed, {"decision": "accept"}, 403, "invalid_token"),
        ("dfw-40", confirmed, {"decision": "maybe"}, 400, "invalid_request"),
        ("dfw-40", confirmed, {"decision": "accept", "note": "n" * 501}, 400, "invalid_request"),
        ("dfw-40", confirmed, {"decision": "accept", "note": ["not", "text"]}, 400, "invalid_request"),
    ]
    rows, sent = len(env.rows), len(env.sent)
    for slug, lead_id, body, status, error in cases:
        r = env.client.post(f"/api/v1/listings/{slug}/leads/{lead_id}/decision",
                            json={"token": _token(1), **body})
        assert (r.status_code, r.get_json()["error"]) == (status, error), (slug, lead_id, body)
    # A token sent in the query string instead of the body is not read.
    r = env.client.post(f"/api/v1/listings/dfw-40/leads/{confirmed}/decision?token={_token(1)}",
                        json={"decision": "accept"})
    assert (r.status_code, r.get_json()["error"]) == (403, "invalid_token")
    assert (len(env.rows), len(env.sent)) == (rows, sent)
    # Control: the same request with the listing's token in the body is recorded.
    assert _decide(env, confirmed, "accept", note="n" * 500).status_code == 200
    assert len(env.rows) == rows + 1


def test_a_decision_the_ledger_already_holds_is_never_written_twice(env, monkeypatch):
    lead_id = _registered(env)
    assert _decide(env, lead_id, "accept").status_code == 200
    rows, sent = len(env.rows), len(env.sent)
    real, calls = el._db_lead_events, []

    def read_before_the_accept_landed(lead):
        calls.append(lead)
        events = real(lead)
        return [e for e in events if not e["event"].startswith("registration_")] if len(calls) == 1 else events

    monkeypatch.setattr(el, "_db_lead_events", read_before_the_accept_landed)
    r = _decide(env, lead_id, "decline")
    assert (r.status_code, r.get_json()["error"], r.get_json()["status"]) == (409, "already_decided", "accepted")
    assert len(calls) == 2                       # the guard refused, and the answer re-read the lead
    calls.clear()
    r = _decide(env, lead_id, "accept")
    assert (r.status_code, r.get_json()["duplicate"], r.get_json()["status"]) == (200, True, "accepted")
    assert (len(env.rows), len(env.sent)) == (rows, sent)


def test_a_guarded_append_writes_nothing_once_the_lead_has_the_event(env):
    lead_id = _registered(env)
    conn, secret = el._conn(), ledger.ledger_secret()
    ledger.append(conn, secret, lead_id=lead_id, event="withdrawn", channel="admin")
    before = len(env.rows)
    with pytest.raises(ledger.LedgerConflict) as caught:
        ledger.append(conn, secret, lead_id=lead_id, event="registration_accepted",
                      unless_lead_has=ledger.DECISION_BLOCKING_EVENTS)
    assert caught.value.event == "withdrawn" and len(env.rows) == before
    # Control: the same append without the guard lands.
    ledger.append(conn, secret, lead_id=lead_id, event="registration_accepted")
    assert len(env.rows) == before + 1


# ── admin ─────────────────────────────────────────────────────────────────

def test_admin_records_decisions_on_the_providers_behalf_through_the_same_rules(env):
    first = _registered(env)
    r = _admin_status(env, first, "accepted", note="Confirmed by phone.")
    j = r.get_json()
    assert (r.status_code, j["status"], j["decision"], j["duplicate"]) == (200, "accepted", "accept", False)
    assert (env.rows[-1]["event"], env.rows[-1]["channel"], env.rows[-1]["meta"]) == (
        "registration_accepted", "admin", {"note": "Confirmed by phone."})
    assert _admin_status(env, first, "accepted").get_json()["duplicate"] is True
    r = _decide(env, first, "decline")
    assert (r.status_code, r.get_json()["status"]) == (409, "accepted")
    assert any(m["to"] == "jane@acme.com" and "Registration accepted" in m["subject"] for m in env.sent)

    second = _registered(env, _bearer(**BOB))
    assert _decide(env, second, "decline").status_code == 200
    r = _admin_status(env, second, "accepted")
    assert (r.status_code, r.get_json()["error"], r.get_json()["status"]) == (409, "already_decided", "declined")
    assert any(m["to"] == "bob@other.example" and "Update on your registration" in m["subject"]
               for m in env.sent)

    pending = _intro(env, _bearer(user_id="u-cy", email="cy@third.example"))
    r = _admin_status(env, pending, "declined")
    assert (r.status_code, r.get_json()["error"]) == (409, "not_registered")
    standing = _standing(env, _bearer(user_id="u-dee", email="dee@fourth.example"))
    assert _admin_status(env, standing, "accepted").status_code == 404
    # A standing requirement has no provider to accept it, so it can still be marked introduced.
    assert _admin_status(env, standing, "introduced").status_code == 200
    r = _admin_status(env, first, "maybe")
    assert r.status_code == 400 and "accepted" in r.get_json()["message"]


def test_an_introduction_needs_the_providers_acceptance(env):
    lead_id = _registered(env)
    r = _admin_status(env, lead_id, "introduced")
    assert (r.status_code, r.get_json()["error"]) == (409, "not_accepted")
    assert _decide(env, lead_id, "accept").status_code == 200
    r = _admin_status(env, lead_id, "introduced")
    assert (r.status_code, r.get_json()["status"]) == (200, "introduced")
    # An introduced registration still counts as accepted for the provider.
    assert _decide(env, lead_id, "accept").get_json()["duplicate"] is True
    assert _decide(env, lead_id, "decline").status_code == 409


# ── what the provider sees ────────────────────────────────────────────────

def test_before_acceptance_the_provider_sees_the_company_and_a_scrubbed_requirement(env):
    env.listings[0]["contact"]["auto_notify"] = True
    lead_id = _registered(env)
    notices = [m for m in env.sent if m["to"] == NOTICE_EMAIL]
    assert len(notices) == 1
    notice = notices[0]["body"]
    assert lead_id in notice and "Acme Capital" in notice and env.rows[0]["entry_hash"] in notice
    for text in SCRUBBED.values():
        assert str(text) in notice or text == 40, text
    _absent(BUYER_SECRETS, notice)
    link = re.search(r"ledger=dfw-40&amp;token=(1\.\d+\.[0-9a-f]{40})", notice)
    assert link and ledger.check_operator_token(ledger.ledger_secret(), 1, link.group(1))

    j = env.client.get(f"/api/v1/listings/dfw-40/leads?token={link.group(1)}").get_json()
    lead = j["leads"][0]
    assert (lead["company"], lead["status"], lead["requirement"]) == ("Acme Capital", "operator_notified", SCRUBBED)
    assert [lead[k] for k in ("name", "role", "email", "email_domain", "message")] == [None] * 5
    assert lead["decision"] == {"allowed": True, "method": "POST",
                                "path": f"/api/v1/listings/dfw-40/leads/{lead_id}/decision"}
    assert [e["entry_hash"] for e in lead["entries"]] == [r["entry_hash"] for r in _lead_rows(env, lead_id)]
    _absent(BUYER_SECRETS, json.dumps(j))

    assert _decide(env, lead_id, "accept", token=link.group(1)).status_code == 200
    lead = env.client.get(f"/api/v1/listings/dfw-40/leads?token={link.group(1)}").get_json()["leads"][0]
    assert (lead["name"], lead["role"], lead["email"], lead["email_domain"], lead["message"]) == (
        "Jane Doe", "VP Development", "jane@acme.com", "acme.com", INTRO["message"])
    assert lead["requirement"] == INTRO["requirement"]
    assert (lead["status"], lead["decision"]["allowed"]) == ("accepted", False)
    # A notice sent again after acceptance names the buyer too.
    r = env.client.post(f"/api/v1/admin/listings/leads/{lead_id}/notify-operator", headers=ADMIN)
    assert r.status_code == 200 and r.get_json()["sent"] is True
    resent = [m for m in env.sent if m["to"] == NOTICE_EMAIL][-1]["body"]
    assert "Jane Doe" in resent and "jane@acme.com" in resent


def test_the_ledger_offers_a_decision_only_while_one_is_open(env):
    registered = _registered(env)
    declined = _registered(env, _bearer(**BOB))
    assert _decide(env, declined, "decline").status_code == 200
    leads = {lead["lead_id"]: lead for lead in _ledger(env)["leads"]}
    assert (leads[registered]["status"], leads[registered]["decision"]["allowed"]) == ("registered", True)
    assert (leads[declined]["status"], leads[declined]["decision"]["allowed"]) == ("declined", False)
    assert (leads[declined]["name"], leads[declined]["email"]) == (None, None)      # a decline shares nothing


@pytest.mark.parametrize("text,scrubbed", [
    ("Call 512-555-0199 today", "Call [phone removed] today"),
    ("(512) 555-0199", "[phone removed]"),
    ("ring 555 0199", "ring [phone removed]"),
    ("+1 214 555 0100 ext 4", "[phone removed] ext 4"),
    ("mail j.doe+dc@acme.co.uk now", "mail [email removed] now"),
    ("see http://intranet.acme/x and acme.io", "see [link removed] and [link removed]"),
    ("from 2027-06-15, phase 2 by 2026-2028, 12 000 kW, 1000000 USD",
     "from 2027-06-15, phase 2 by 2026-2028, 12 000 kW, 1000000 USD"),
    ("N.Virginia or St.Louis at 40.5 MW", "N.Virginia or St.Louis at 40.5 MW"),
])
def test_the_scrub_takes_out_contact_details_and_keeps_dates_and_sizes(text, scrubbed):
    assert el._scrub_text(text) == scrubbed


# ── decision emails ───────────────────────────────────────────────────────

def test_accepting_sends_each_side_the_others_details_and_the_admin_a_copy(env):
    lead_id = _registered(env)
    before = len(env.sent)
    assert _decide(env, lead_id, "accept", note="Call Dana any morning.").status_code == 200
    mails = env.sent[before:]
    assert [m["to"] for m in mails] == ["jane@acme.com", NOTICE_EMAIL, ADMIN_INBOX]
    buyer, provider, admin = (m["body"] for m in mails)
    for shown in (PROVIDER, "Seagoville Campus", "100 Industrial Blvd", "75159", "APN 42-17",
                  "32.776712", "-96.797012", "Seagoville 345kV", "Dana Reyes", "VP Leasing",
                  OPERATOR_EMAIL, "+1 214 555 0100", "https://dchub.cloud/listings/dfw-40",
                  "Call Dana any morning."):
        assert shown in buyer, shown
    _absent((NOTICE_EMAIL, "QH Holdings", "auto_notify", "linkedin"), buyer)
    for shown in ("Jane Doe", "VP Development", "Acme Capital", "jane@acme.com",
                  "Interested in the shell.", "512-555-0199", "https://acme.com/rfp"):
        assert shown in provider, shown
    assert lead_id in mails[2]["subject"] and "accepted" in mails[2]["subject"]
    assert buyer in admin and provider in admin and "Call Dana any morning." in admin


def test_the_buyer_gets_notify_email_only_when_it_is_the_only_address(env):
    env.listings[0]["contact"] = {"notify_email": NOTICE_EMAIL, "auto_notify": False}
    lead_id = _registered(env)
    assert _decide(env, lead_id, "accept").status_code == 200
    disclosure = _detail(env, _bearer())[0]["disclosure"]
    assert disclosure["contact"] == {"name": None, "email": NOTICE_EMAIL, "phone": None, "title": None}


def test_declining_tells_the_buyer_without_naming_the_provider(env):
    lead_id = _registered(env)
    before = len(env.sent)
    assert _decide(env, lead_id, "decline", note="Committed to another tenant.").status_code == 200
    mails = env.sent[before:]
    assert [m["to"] for m in mails] == ["jane@acme.com", ADMIN_INBOX]
    buyer = mails[0]["subject"] + mails[0]["body"]
    assert "could not take" in buyer
    assert "https://dchub.cloud/listings#listings" in buyer and "https://dchub.cloud/listings#register" in buyer
    _absent(PROVIDER_SECRETS + (NOTICE_EMAIL, "Committed to another tenant"), buyer)
    assert "Committed to another tenant." in mails[1]["body"] and mails[0]["body"] in mails[1]["body"]


# ── what the buyer sees: disclosure ───────────────────────────────────────

LOCKED_VALUES = {"accepted_at": None, "provider": None, "site": None, "latitude": None,
                 "longitude": None, "substation": None, "contact": None}


def test_a_caller_who_cannot_open_the_listing_gets_released_false_alone(env):
    for headers in ({}, {"X-Internal-Key": INTERNAL_KEY}, _bearer()):   # _bearer(): terms not accepted
        j, body = _detail(env, headers)
        assert j["locked"] is True and j["disclosure"] == {"released": False}
        _absent(PROVIDER_SECRETS, body)


def test_a_viewer_without_a_registration_is_shown_how_to_register(env):
    _accept_terms(env, _bearer())
    j, body = _detail(env, _bearer())
    assert j["locked"] is False
    assert j["disclosure"] == {"released": False, "status": "none", "lead_id": None, **LOCKED_VALUES,
                               "how": {"method": "POST", "path": "/api/v1/listings/dfw-40/intro",
                                       "mcp_tool": "request_capacity_intro"}}
    assert j["introduction"]["operator_contact"] == "shared_after_acceptance"
    _absent(PROVIDER_SECRETS, body)


@pytest.mark.parametrize("step,status", [
    ("requested", "pending"), ("confirmed", "pending"), ("notified", "pending"), ("declined", "declined")])
def test_disclosure_stays_locked_until_the_provider_accepts(env, step, status):
    lead_id = _intro(env)
    if step != "requested":
        _confirm(env, lead_id)
    if step == "notified":
        assert env.client.post(f"/api/v1/admin/listings/leads/{lead_id}/notify-operator",
                               headers=ADMIN).get_json()["sent"] is True
    if step == "declined":
        assert _decide(env, lead_id, "decline").status_code == 200
    j, body = _detail(env, _bearer())
    assert j["disclosure"] == {"released": False, "status": status, "lead_id": lead_id, **LOCKED_VALUES}
    _absent(PROVIDER_SECRETS, body)


def test_disclosure_releases_the_site_to_the_accepted_buyer_alone(env):
    lead_id = _registered(env)
    assert _decide(env, lead_id, "accept").status_code == 200
    accepted = next(r for r in env.rows if r["event"] == "registration_accepted")
    j, body = _detail(env, _bearer())
    assert j["disclosure"] == {
        "released": True, "status": "accepted", "lead_id": lead_id,
        "accepted_at": json.loads(accepted["entry_json"])["created_at"],
        "provider": {"name": PROVIDER}, "site": SITE, "latitude": 32.776712, "longitude": -96.797012,
        "substation": "Seagoville 345kV",
        "contact": {"name": "Dana Reyes", "email": OPERATOR_EMAIL, "phone": "+1 214 555 0100",
                    "title": "VP Leasing"}}
    # The listing itself stays the specs view, and the feed never discloses.
    assert (j["listing"]["latitude"], j["listing"]["provider"]) == (None, {"name": None, "disclosed": False})
    _absent((NOTICE_EMAIL, "QH Holdings", "auto_notify", LINKEDIN_POST, "owner-sentinel"), body)
    _absent(PROVIDER_SECRETS, env.client.get("/api/v1/listings", headers=_bearer()).get_data(as_text=True))
    # Another signed-in buyer, with no registration of their own, sees nothing.
    _accept_terms(env, _bearer(**BOB))
    other, other_body = _detail(env, _bearer(**BOB))
    assert (other["disclosure"]["released"], other["disclosure"]["status"]) == (False, "none")
    _absent(PROVIDER_SECRETS, other_body)
    # Introduced keeps it disclosed; a withdrawal takes it away.
    assert _admin_status(env, lead_id, "introduced").status_code == 200
    j, _ = _detail(env, _bearer())
    assert (j["disclosure"]["released"], j["disclosure"]["status"], j["disclosure"]["site"]) == (
        True, "introduced", SITE)
    assert _admin_status(env, lead_id, "withdrawn").status_code == 200
    j, body = _detail(env, _bearer())
    assert (j["disclosure"]["released"], j["disclosure"]["status"], j["disclosure"]["lead_id"]) == (
        False, "none", None)
    _absent(PROVIDER_SECRETS, body)


def test_legacy_address_keys_complete_the_disclosed_site(env):
    detail = dict(env.listings[0]["detail"])
    detail.pop("site")
    detail.update({" City ": "Seagoville", "ADDRESS": "100 Industrial Blvd", "zip": 75159, "apn": "42-17",
                   "facility_name": "SGV-1", "site_name": "Seagoville Campus", "provider_city": "Plano"})
    env.listings[0]["detail"] = detail
    lead_id = _registered(env)
    locked, body = _detail(env, _bearer())
    assert not {"zip", "apn", "ADDRESS", " City ", "site_name"} & set(locked["listing"]["detail"])
    _absent(("SGV-1", "Plano", "42-17"), body)
    assert _decide(env, lead_id, "accept").status_code == 200
    assert _detail(env, _bearer())[0]["disclosure"]["site"] == {
        "name": "Seagoville Campus", "address": "100 Industrial Blvd", "city": "Seagoville",
        "postal_code": "75159", "parcel_id": "42-17"}


def test_the_public_record_and_the_confirmation_disclose_nothing(env):
    lead_id = _intro(env)
    token = _confirm_token(env, lead_id)
    _confirm(env, lead_id)
    assert _decide(env, lead_id, "accept", note="Call Dana any morning.").status_code == 200
    r = env.client.get(f"/api/v1/listings/leads/{lead_id}/verify")
    j, body = r.get_json(), r.get_data(as_text=True)
    assert j["status"] == "accepted"
    assert [e["event"] for e in j["events"]] == ["intro_requested", "email_confirmed", "registration_accepted"]
    assert j["prospect"] == {"company": "Acme Capital", "email_domain": "acme.com",
                             "email_verified": True, "verified_via": "email_link"}
    _absent(PROVIDER_SECRETS + ("Jane Doe", "jane@acme.com", "Call Dana"), body)
    again = env.client.post("/api/v1/listings/leads/confirm", json={"lead_id": lead_id, "token": token})
    assert (again.status_code, again.get_json()["status"], again.get_json()["already_confirmed"]) == (
        200, "accepted", True)
    _absent(PROVIDER_SECRETS + ("Jane Doe", "jane@acme.com"), again.get_data(as_text=True))


# ── co-marketing ──────────────────────────────────────────────────────────

def test_the_operator_ledger_drafts_a_company_level_co_marketing_post(env):
    env.listings += [
        _listing(id=2, slug="fra-12", title="Frankfurt hall", market="Frankfurt", state="HE",
                 country="Germany", detail={"provider": {"name": PROVIDER.lower(), "disclosed": False}}),
        _listing(id=3, slug="sgp-draft", status="draft", country="SG",
                 detail={"provider": {"name": PROVIDER, "disclosed": True}}),
        _listing(id=4, slug="jnb-other", country="ZA",
                 detail={"provider": {"name": "Other Capacity Co", "disclosed": True}}),
    ]
    co = _ledger(env)["co_marketing"]
    url = "https://dchub.cloud/listings?region=north_america,europe"
    assert co == {"required": True, "status": "linkedin_posted", "search_url": url, "draft": (
        f"{PROVIDER} now lists its available data center capacity on DC Hub Capacity Source. "
        "Buyers and their AI agents can search it by size and location, and we update it every week."
        f"\n\nLooking for capacity? Search by kW or MW and region: {url}\n\n"
        "#datacenters #AIinfrastructure")}
    _absent(("Dallas", "TX", "Texas", "Frankfurt", "Germany", "Seagoville", "Industrial", "75159",
             "DFW", "dfw-40", "Powered shell", "345kV", "Singapore"), co["draft"])


@pytest.mark.parametrize("detail,country,contact,company,cadence,url,status", [
    ({"update_cadence": "real_time"}, "BR", {"co_marketing": {"website_url": "https://example.com/capacity"}},
     "[Company]", "in real time", "https://dchub.cloud/listings?region=latin_america", "website_link"),
    ({"update_cadence": "monthly", "provider": {"name": "Solo Provider", "disclosed": True}}, "Atlantis",
     None, "Solo Provider", "every month", "https://dchub.cloud/listings", "missing"),
    (None, "ZA", {"co_marketing": {"linkedin_post_url": "https://evil.example/www.linkedin.com"}},
     "[Company]", "regularly", "https://dchub.cloud/listings?region=middle_east_africa", "missing"),
])
def test_the_draft_falls_back_to_the_listings_own_region_and_cadence(env, detail, country, contact, company,
                                                                     cadence, url, status):
    env.listings[0].update(detail=detail, country=country, contact=contact)
    co = _ledger(env)["co_marketing"]
    assert co["draft"] == (
        f"{company} now lists its available data center capacity on DC Hub Capacity Source. "
        f"Buyers and their AI agents can search it by size and location, and we update it {cadence}."
        f"\n\nLooking for capacity? Search by kW or MW and region: {url}\n\n#datacenters #AIinfrastructure")
    assert (co["required"], co["search_url"], co["status"]) == (True, url, status)


def test_the_co_marketing_record_stays_with_admin_and_the_operator_ledger(env):
    lead_id = _registered(env)
    assert _decide(env, lead_id, "accept").status_code == 200
    public = {
        "feed": env.client.get("/api/v1/listings"),
        "feed signed in": env.client.get("/api/v1/listings", headers=_bearer()),
        "teaser": env.client.get("/api/v1/listings/dfw-40"),
        "disclosed detail": env.client.get("/api/v1/listings/dfw-40", headers=_bearer()),
        "record": env.client.get(f"/api/v1/listings/leads/{lead_id}/verify"),
    }
    for label, r in public.items():
        assert r.status_code == 200, label
        _absent((LINKEDIN_POST, "co_marketing", "2026-09-10"), r.get_data(as_text=True))
    admin = env.client.get("/api/v1/admin/listings", headers=ADMIN).get_json()["listings"][0]
    assert admin["co_marketing_status"] == "linkedin_posted"
    assert admin["contact"]["co_marketing"]["linkedin_post_url"] == LINKEDIN_POST
    assert _ledger(env)["co_marketing"]["status"] == "linkedin_posted"


@pytest.mark.parametrize("record,field", [
    ({"linkedin_post_url": "http://www.linkedin.com/posts/x"}, "contact.co_marketing.linkedin_post_url"),
    ({"linkedin_post_url": "https://linkedin.com.evil.example/posts/x"},
     "contact.co_marketing.linkedin_post_url"),
    ({"linkedin_post_url": "https://www.linkedin.com/posts/two words"}, "contact.co_marketing.linkedin_post_url"),
    ({"website_url": "ftp://example.com/capacity"}, "contact.co_marketing.website_url"),
    ({"website_url": "https://"}, "contact.co_marketing.website_url"),
    ({"posted_at": "2026-02-30"}, "contact.co_marketing.posted_at"),
    ({"posted_at": "10 Sep 2026"}, "contact.co_marketing.posted_at"),
    ({"likes": 12}, "contact.co_marketing.likes"),
    ("posted on LinkedIn", "contact.co_marketing"),
])
def test_admin_writes_refuse_a_malformed_co_marketing_record(env, record, field):
    count = len(env.listings)
    r = env.client.post("/api/v1/admin/listings", headers=ADMIN, json={
        "title": "Dallas shell", "slug": "dallas-shell",
        "contact": {"email": OPERATOR_EMAIL, "co_marketing": record}})
    j = r.get_json()
    assert (r.status_code, j["error"], [e["field"] for e in j["errors"]]) == (400, "invalid_contact", [field])
    r = env.client.patch("/api/v1/admin/listings/1", headers=ADMIN,
                         json={"contact": json.dumps({"co_marketing": record})})
    assert (r.status_code, r.get_json()["error"]) == (400, "invalid_contact")
    assert len(env.listings) == count
    assert env.listings[0]["contact"]["co_marketing"]["linkedin_post_url"] == LINKEDIN_POST


def test_admin_writes_store_a_valid_co_marketing_record(env):
    record = {"linkedin_post_url": "https://linkedin.com/feed/update/urn:li:activity:7300",
              "posted_at": "2026-09-14", "website_url": "https://quietharbor.example/capacity"}
    contact = {**env.listings[0]["contact"], "co_marketing": record}
    r = env.client.patch("/api/v1/admin/listings/1", headers=ADMIN, json={"contact": contact})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert env.listings[0]["contact"]["co_marketing"] == record
    assert env.client.get("/api/v1/admin/listings", headers=ADMIN).get_json()[
        "listings"][0]["co_marketing_status"] == "linkedin_posted"


# ── admin warnings ────────────────────────────────────────────────────────

def test_admin_writes_warn_when_public_text_names_an_undisclosed_provider(env):
    hidden = {"provider": {"name": PROVIDER, "disclosed": False}}
    r = env.client.post("/api/v1/admin/listings", headers=ADMIN,
                        json={"title": "Quiet Harbor Infrastructure — DFW shell", "detail": hidden})
    j = r.get_json()
    assert (r.status_code, j["slug"]) == (200, "quiet-harbor-infrastructure-dfw-shell")
    assert [w["field"] for w in j["warnings"]] == ["title", "slug"]
    assert all("provider" in w["message"] for w in j["warnings"])
    named_id = j["id"]
    r = env.client.post("/api/v1/admin/listings", headers=ADMIN, json={
        "title": "DFW shell", "slug": "dfw-shell-b", "summary": "Offered by QUIET HARBOR  infrastructure.",
        "detail": hidden})
    assert [w["field"] for w in r.get_json()["warnings"]] == ["summary"]
    # Controls: a disclosed provider may be named, and a listing that names no provider warns about nothing.
    r = env.client.post("/api/v1/admin/listings", headers=ADMIN, json={
        "title": "Quiet Harbor Infrastructure — Reno", "detail": {"provider": {"name": PROVIDER, "disclosed": True}}})
    assert (r.status_code, r.get_json()["warnings"]) == (200, [])
    r = env.client.post("/api/v1/admin/listings", headers=ADMIN, json={"title": "Reno shell", "detail": hidden})
    assert (r.status_code, r.get_json()["warnings"]) == (200, [])
    # A PATCH is judged on the listing as stored after it, whatever the body touched.
    r = env.client.patch(f"/api/v1/admin/listings/{named_id}", headers=ADMIN, json={"status": "pocket"})
    assert [w["field"] for w in r.get_json()["warnings"]] == ["title", "slug"]
    r = env.client.patch(f"/api/v1/admin/listings/{named_id}", headers=ADMIN, json={"title": "DFW shell"})
    assert [w["field"] for w in r.get_json()["warnings"]] == ["slug"]
