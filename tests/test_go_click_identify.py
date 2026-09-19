"""`go_click`: the unlock click that identifies WITHOUT asking a human to type.

THE GAP THIS CLOSES. routes/relay_identify has named 'go_click' in SOURCES
since it shipped (2026-09-17) and nothing ever called it. Its two live sources
both need something extra from the human: the relay page's form needs them to
type an address, and the checkout capture needs them to pay.

A KEYED caller needs neither. Its unlock link carries `pk-`/`k-<sha256(api
key)>` beside its session id, and that key routinely already has an address
bound to it from bind_email / claim_free_key — an identity `identified` cannot
see, because it lives on mcp_dev_keys with no session link. The funnel's own
source says so: "most identity capture happens on the key tables … with NO
session link, so 'identified' structurally undercounts."

Measured 30d to 2026-09-18, before this shipped:
    26 keyed checkout clicks (pk-/k-)
    10 of them carrying a session id
     7 on keys that already had an address bound
     2 sessions this would newly stamp

Small, and stated rather than implied. What it changes is the SHAPE of the
funnel: `identified` can now move on a click, which is what an agent can
actually get a human to do.

★ A MEASUREMENT, NEVER AN ENTITLEMENT — asserted below. bind_email takes
whatever the caller types, so this address is self-declared. That is exactly
what the `identified` STAGE claims. The paid-tier grants require
metadata->>'email_verified_for' ON TOP (2026-09-11), and this lookup must
never be mistaken for that one.
"""
import base64
import hashlib
import hmac
import pathlib
import sys

import pytest
from flask import Flask

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import routes.relay_identify as ri  # noqa: E402

SECRET = "test-internal-key-not-a-real-secret"
SESSION = "1aa6536d-b1d4-24b4-74a8-e89ba266e781"
KEY_HASH = "b" * 64
PACK_REF = "pk-" + KEY_HASH
SUB_REF = "k-" + KEY_HASH


# ── which refs name a key ────────────────────────────────────────────────
class TestKeyRefShape:
    def test_both_durable_key_prefixes_yield_the_hash(self):
        assert ri.key_hash_from_ref(PACK_REF) == KEY_HASH
        assert ri.key_hash_from_ref(SUB_REF) == KEY_HASH

    def test_an_anon_offer_id_is_not_a_key(self):
        # `a-` identifies an OFFER OCCURRENCE, not a person and not a key.
        # Coercing it into a key lookup is how an ephemeral id would start
        # naming somebody.
        assert ri.key_hash_from_ref("a-deadbeef") == ""

    def test_a_bare_session_ref_is_not_a_key(self):
        assert ri.key_hash_from_ref(SESSION) == ""

    @pytest.mark.parametrize("ref,expected", [
        ("", ""), (None, ""),
        ("pk-", ""),
        ("pk-" + "b" * 63, ""),                  # one hex short
        ("pk-" + "b" * 65, ""),                  # one hex long
        ("pk-" + "z" * 64, ""),                  # right length, not hex
        ("xk-" + "b" * 64, ""),                  # not one of our prefixes
        ("pk_" + "b" * 64, ""),                  # underscore, not dash
        ("  PK-" + "B" * 64 + "  ", KEY_HASH),   # trimmed and lowercased
    ])
    def test_only_an_exact_prefixed_sha256_names_a_key(self, ref, expected):
        assert ri.key_hash_from_ref(ref) == expected


# ── the lookup ───────────────────────────────────────────────────────────
class _Cur:
    """A cursor that actually reads the SQL and the parameter it is given.

    A fake that returns a row regardless of what was asked would make the
    lookup untestable: it would pass with the hash matched in Python, with the
    wrong column, or with no WHERE at all.
    """

    def __init__(self, rows_by_hash):
        self._rows = rows_by_hash
        self.sql = None
        self.params = None
        self._out = None

    def execute(self, sql, params=()):
        self.sql, self.params = sql, params
        if "encode(sha256(api_key::bytea), 'hex') = %s" not in sql:
            self._out = None          # not the shipped match -> finds nothing
            return
        self._out = self._rows.get(params[0])

    def fetchone(self):
        return self._out

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur
        self.autocommit = False

    def cursor(self):
        return self._cur

    def close(self):
        pass


@pytest.fixture
def db(monkeypatch):
    cur = _Cur({KEY_HASH: ("Owner@Example.COM ",)})
    monkeypatch.setattr(ri, "_dsn", lambda: "postgres://stub")
    monkeypatch.setattr(ri, "_pg", type("pg", (), {
        "connect": staticmethod(lambda *a, **k: _Conn(cur))})())
    return cur


class TestKeyEmailLookup:
    def test_it_matches_the_hash_in_sql_and_normalises_the_address(self, db):
        assert ri.email_for_key_ref(PACK_REF) == "owner@example.com"
        assert db.params == (KEY_HASH,)
        assert "mcp_dev_keys" in db.sql
        # the row must have an address; an empty one is not an identity
        assert "email IS NOT NULL AND email <> ''" in db.sql

    def test_an_unknown_key_is_empty_not_an_error(self, db):
        assert ri.email_for_key_ref("pk-" + "c" * 64) == ""

    def test_a_ref_that_names_no_key_never_reaches_the_database(self, db):
        assert ri.email_for_key_ref(SESSION) == ""
        assert db.sql is None

    def test_a_dead_database_is_empty_not_a_raise(self, monkeypatch):
        monkeypatch.setattr(ri, "_dsn", lambda: "postgres://stub")

        def _boom(*a, **k):
            raise RuntimeError("db down")
        monkeypatch.setattr(ri, "_pg", type("pg", (), {
            "connect": staticmethod(_boom)})())
        assert ri.email_for_key_ref(PACK_REF) == ""


# ── the capture ──────────────────────────────────────────────────────────
class TestCaptureFromKeyRef:
    def test_it_captures_under_the_go_click_source(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(ri, "email_for_key_ref", lambda r: "a@b.com")
        monkeypatch.setattr(ri, "capture",
                            lambda sid, email, source="relay_page", **kw:
                            seen.update(sid=sid, email=email, source=source,
                                        kw=kw) or {"ok": True})
        assert ri.capture_from_key_ref(PACK_REF, SESSION, tool="analyze_site")["ok"]
        assert seen["sid"] == SESSION
        assert seen["email"] == "a@b.com"
        assert seen["source"] == "go_click"
        assert seen["kw"]["tool"] == "analyze_site"

    def test_go_click_is_a_real_source_not_relabelled_other(self):
        # capture() rewrites an unknown source to 'other'. If this label were
        # dropped from SOURCES the captures would still be written and would
        # become indistinguishable from every other stray source.
        assert "go_click" in ri.SOURCES

    def test_no_session_means_nothing_to_bind_to(self, monkeypatch):
        monkeypatch.setattr(ri, "email_for_key_ref", lambda r: "a@b.com")
        monkeypatch.setattr(ri, "capture", lambda *a, **k:
                            pytest.fail("captured with no session"))
        assert ri.capture_from_key_ref(PACK_REF, "")["skipped"] == "no_session"

    def test_a_key_with_no_address_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(ri, "email_for_key_ref", lambda r: "")
        monkeypatch.setattr(ri, "capture", lambda *a, **k:
                            pytest.fail("captured with no email"))
        got = ri.capture_from_key_ref(PACK_REF, SESSION)
        assert got["skipped"] == "key_has_no_email"

    def test_it_never_invents_an_address_for_a_keyless_ref(self, monkeypatch):
        monkeypatch.setattr(ri, "capture", lambda *a, **k:
                            pytest.fail("captured from a ref with no key"))
        # real lookup, no DB configured -> '' -> no capture
        monkeypatch.setattr(ri, "_dsn", lambda: "")
        assert ri.capture_from_key_ref(SESSION, SESSION)["ok"] is False
        assert ri.capture_from_key_ref("a-1234", SESSION)["ok"] is False


# ── the two call sites, driven through their real routes ─────────────────
def _go_token(plan, ref, sid):
    payload = base64.urlsafe_b64encode(
        ("%s|%s|%s" % (plan, ref, sid)).encode()).decode().rstrip("=")
    sig = hmac.new(SECRET.encode(), payload.encode(),
                   hashlib.sha256).hexdigest()[:32]
    return payload + "." + sig


def _relay_token(sid, tool, tier, ts, kref=""):
    raw = "%s|%s|%s|%d" % (sid, tool, tier, ts)
    if kref:
        raw += "|" + kref
    payload = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    sig = hmac.new(SECRET.encode(), payload.encode(),
                   hashlib.sha256).hexdigest()[:32]
    return payload + "." + sig


@pytest.fixture
def calls(monkeypatch):
    """Spy on the capture, with both writers' own DB paths disabled."""
    seen = []
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", SECRET)
    monkeypatch.setattr(ri, "capture_from_key_ref",
                        lambda ref, sid, tool="": seen.append((ref, sid, tool)))
    return seen


@pytest.fixture
def go_client(monkeypatch, calls):
    import routes.checkout_click_tracker as cct
    monkeypatch.setattr(cct, "_log_click", lambda *a, **k: None)
    app = Flask(__name__)
    app.register_blueprint(cct.checkout_click_bp)
    return app.test_client()


@pytest.fixture
def relay_client(monkeypatch, calls):
    import routes.human_relay as hr
    monkeypatch.setattr(hr, "_log_open", lambda *a, **k: None)
    app = Flask(__name__)
    app.register_blueprint(hr.human_relay_bp)
    return app.test_client()


class TestTheClick:
    def test_a_keyed_click_identifies_the_session_that_made_it(
            self, go_client, calls):
        r = go_client.get("/go/c/" + _go_token("metered", PACK_REF, SESSION))
        assert r.status_code == 302
        assert "buy.stripe.com" in r.headers["Location"], "the sale must still happen"
        assert calls == [(PACK_REF, SESSION, "")]

    def test_a_click_with_no_session_captures_nothing(self, go_client, calls):
        r = go_client.get("/go/c/" + _go_token("metered", PACK_REF, ""))
        assert r.status_code == 302
        assert calls == []

    def test_an_unsigned_click_never_reaches_the_lookup(self, go_client, calls):
        # We did not mint it, so it names no key of ours. It still lands the
        # human somewhere payable.
        r = go_client.get("/go/c/" + _go_token("metered", PACK_REF, SESSION)[:-4] + "0000")
        assert r.status_code == 302
        assert calls == []

    def test_a_raising_capture_cannot_cost_the_sale(self, go_client, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("identify blew up")
        monkeypatch.setattr(ri, "capture_from_key_ref", _boom)
        r = go_client.get("/go/c/" + _go_token("metered", PACK_REF, SESSION))
        assert r.status_code == 302
        assert "buy.stripe.com" in r.headers["Location"]


class TestTheOpen:
    def test_a_keyed_relay_open_identifies_before_any_typing(
            self, relay_client, calls):
        import time
        tok = _relay_token(SESSION, "analyze_site", "free", int(time.time()),
                           kref=PACK_REF)
        r = relay_client.get("/upgrade/h/" + tok)
        assert r.status_code == 200
        assert calls == [(PACK_REF, SESSION, "analyze_site")]

    def test_a_keyless_open_leaves_it_to_the_form(self, relay_client, calls):
        import time
        tok = _relay_token(SESSION, "analyze_site", "free", int(time.time()))
        r = relay_client.get("/upgrade/h/" + tok)
        assert r.status_code == 200
        assert b"<form" in r.data, "the keyless human's only path must still render"
        assert calls == []

    def test_a_raising_capture_still_renders_the_payment_page(
            self, relay_client, monkeypatch):
        import time

        def _boom(*a, **k):
            raise RuntimeError("identify blew up")
        monkeypatch.setattr(ri, "capture_from_key_ref", _boom)
        tok = _relay_token(SESSION, "analyze_site", "free", int(time.time()),
                           kref=PACK_REF)
        r = relay_client.get("/upgrade/h/" + tok)
        assert r.status_code == 200
        assert b"Unlock full data" in r.data
