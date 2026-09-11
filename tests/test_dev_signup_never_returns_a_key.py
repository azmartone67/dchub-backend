"""POST /api/v1/dev-signup must never put a key in its response (2026-09-11).

The endpoint is public and the address unverified. It used to answer an email
that already had a key with THAT key — in `api_key`, and again inside
`upgrade_url` — so anyone who knew or guessed an address received the account's
live key, paid tiers included. Measured live before the fix: a second POST of
one address returned the identical key with is_new:false.

Keys now travel only to the inbox, and every accepted address gets the same
200. These tests drive the REAL handler through Flask's test client: the
blueprint is imported (never main.py) and `_pool` is replaced by an in-memory
database, so they read exactly what the endpoint sends back and what it emails.

WHAT THIS PINS
  · an address with a key gets no key in the body — and still receives its key,
    at that inbox, so the owner is not locked out;
  · a new address is minted exactly one key, which is emailed, not returned;
  · status and body are byte-identical for an address with a key, a new address,
    a paying account, a trial key and a rate-limited address;
  · a paying account with no MCP key gets its recovery email, not a new free key;
  · a lookup that could not run mints nothing — for each of the three lookups;
  · a key the write did not store (a key-string conflict) is never emailed;
  · the emailed HTML escapes the address it echoes;
  · dev-signup and /keys/recover spend one per-address send budget;
  · the signup widget, the endpoint's only UI, no longer expects a key back.
"""
import importlib
import os
import re
import types

import pytest
from flask import Flask

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXISTING = "dch_live_" + "e" * 32
TRIAL = "dch_trial_" + "7" * 32
# Every key shape the platform issues. A key anywhere in the body is the
# defect, whatever the field is called.
_KEY_SHAPED = re.compile(r"dch(?:ub)?_(?:live|trial)_[0-9A-Za-z]{8,}")


class _Cursor:
    def __init__(self, db):
        self.db = db
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self.db.statements.append(s)
        self._row = None
        if s.startswith("INSERT INTO mcp_dev_keys"):
            # api_key is the primary key: model the conflict the way Postgres
            # does, including the statement shapes that handle it differently.
            api_key, email = params[0], params[2]
            taken = set(self.db.dev_keys.values()) | {i["api_key"] for i in self.db.inserts}
            if api_key in taken:
                if "ON CONFLICT (api_key) DO NOTHING" not in s:
                    raise RuntimeError("simulated: duplicate key value violates mcp_dev_keys_pkey")
                self.db.conflicts.append(api_key)
                return
            self.db.inserts.append({"api_key": api_key, "email": email})
            self.db.dev_keys.setdefault(email, api_key)
            if "RETURNING api_key" in s:
                self._row = (api_key,)
            return
        if "FROM users" in s:
            table = "users"
        elif "FROM mcp_dev_keys" in s:
            table = "mcp_dev_keys"
        elif "FROM auto_trial_keys" in s:
            table = "auto_trial_keys"
        else:
            raise AssertionError("unexpected statement on the dev-signup path: " + s[:160])
        if table in self.db.fail_on:
            raise RuntimeError("simulated: %s is unreadable" % table)
        email = params[0]
        found = {"users": "pro" if email in self.db.paid_users else None,
                 "mcp_dev_keys": self.db.dev_keys.get(email),
                 "auto_trial_keys": self.db.trial_keys.get(email)}[table]
        self._row = (found,) if found else None

    def fetchone(self):
        return self._row


class _DB:
    """The three tables the signup path reads and the one it writes, behind
    the `_pool` interface: .connection() -> context manager -> .cursor()."""

    def __init__(self, dev_keys=None, paid_users=(), trial_keys=None, fail_on=()):
        self.dev_keys = dict(dev_keys or {})
        self.paid_users = set(paid_users)
        self.trial_keys = dict(trial_keys or {})
        self.fail_on = set(fail_on)
        self.inserts = []
        self.conflicts = []
        self.statements = []

    def connection(self):
        db = self

        class _Conn:
            def __enter__(self):
                return types.SimpleNamespace(cursor=lambda: _Cursor(db))

            def __exit__(self, *exc):
                return False

        return _Conn()


@pytest.fixture
def signup(monkeypatch):
    # The module refuses to import without a DSN. Nothing connects: `_pool` is
    # swapped before any request and the default psycopg2 path is disabled.
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub:stub@127.0.0.1:1/stub")
    fme = importlib.import_module("flask_mcp_endpoints")
    kr = importlib.import_module("routes.keys_recover")
    # A stub another test left in sys.modules would let every assertion below
    # pass against a fake. Prove these are the shipped files.
    for mod, rel in ((fme, "flask_mcp_endpoints.py"), (kr, "routes/keys_recover.py")):
        assert os.path.realpath(mod.__file__) == os.path.realpath(os.path.join(ROOT, rel))

    sent = []
    monkeypatch.setattr(kr, "_send",
                        lambda to, subject, html: sent.append((to, subject, html)) or True)
    monkeypatch.setattr(kr, "_dsn", lambda: None)
    monkeypatch.setattr(kr, "_RL_EMAIL", {})
    monkeypatch.setattr(kr, "_RL_IP", {})
    monkeypatch.setattr(kr, "_RL_DAY", {"day": None})

    app = Flask(__name__)
    app.register_blueprint(fme.mcp_bp)
    app.register_blueprint(kr.keys_recover_bp)
    client = app.test_client()

    def use(db):
        monkeypatch.setattr(fme, "_pool", db)
        return db

    def post(email, path="/api/v1/dev-signup"):
        return client.post(path, json={"email": email},
                           headers={"X-Forwarded-For": "203.0.113.7"})

    return types.SimpleNamespace(fme=fme, kr=kr, sent=sent, use=use, post=post)


def _no_key_in(resp, *keys):
    text = resp.get_data(as_text=True)
    hit = _KEY_SHAPED.search(text)
    assert hit is None, "a key is in the response body: %s…" % hit.group(0)[:14]
    for k in keys:
        assert k not in text


def test_an_address_with_a_key_gets_no_key_back_but_its_inbox_does(signup):
    db = signup.use(_DB(dev_keys={"alice@example.com": EXISTING}))
    r = signup.post("  Alice@Example.COM ")
    assert r.status_code == 200
    _no_key_in(r, EXISTING)
    assert [(to, EXISTING in html) for to, _s, html in signup.sent] == [
        ("alice@example.com", True)], (
        "the owner must still receive the existing key, at the inbox it is bound to")
    assert db.inserts == [], "an address that already has a key must not be minted a second one"


def test_a_new_address_is_minted_one_key_that_is_emailed_not_returned(signup):
    db = signup.use(_DB())
    r = signup.post("new@example.com")
    assert r.status_code == 200
    assert [i["email"] for i in db.inserts] == ["new@example.com"]
    minted = db.inserts[0]["api_key"]
    _no_key_in(r, minted)
    assert [(to, minted in html) for to, _s, html in signup.sent] == [
        ("new@example.com", True)]


def test_the_response_is_identical_whether_or_not_the_address_is_registered(signup):
    signup.use(_DB(dev_keys={"has-key@example.com": EXISTING},
                   paid_users={"payer@example.com"},
                   trial_keys={"trial@example.com": TRIAL}))
    seen = {}
    for email in ("has-key@example.com", "new@example.com",
                  "payer@example.com", "trial@example.com"):
        r = signup.post(email)
        seen[email] = (r.status_code, r.get_data())
    for _ in range(signup.kr._MAX_PER_EMAIL):
        signup.post("limited@example.com")
    r = signup.post("limited@example.com")          # over the per-address budget
    seen["rate-limited"] = (r.status_code, r.get_data())

    assert seen["new@example.com"][0] == 200
    assert len(set(seen.values())) == 1, {k: v[1][:160] for k, v in seen.items()}


def test_a_paying_account_without_an_mcp_key_is_not_minted_a_free_one(signup):
    db = signup.use(_DB(paid_users={"payer@example.com"}))
    r = signup.post("payer@example.com")
    assert r.status_code == 200
    _no_key_in(r)
    assert db.inserts == [], "a paying account must get its recovery email, not a second, free key"
    assert [to for to, _s, _h in signup.sent] == ["payer@example.com"]


@pytest.mark.parametrize("unreadable", [
    ("users",), ("mcp_dev_keys",), ("auto_trial_keys",),
    ("users", "mcp_dev_keys", "auto_trial_keys"),
], ids=["users", "mcp_dev_keys", "auto_trial_keys", "all"])
def test_a_lookup_that_could_not_run_mints_nothing(signup, unreadable):
    # Control: with every table readable, the same request does mint.
    control = signup.use(_DB())
    ok = signup.post("control@example.com")
    assert len(control.inserts) == 1

    db = signup.use(_DB(fail_on=unreadable))
    r = signup.post("maybe@example.com")
    assert (r.status_code, r.get_data()) == (ok.status_code, ok.get_data())
    assert db.inserts == [], (
        "absence was never established — minting could give an address a second "
        "key, or a paying account a free one")
    assert [to for to, _s, _h in signup.sent] == ["control@example.com"]


def test_a_key_the_write_did_not_store_is_never_emailed(signup, monkeypatch):
    # Force the generated key string to collide with Alice's row. The write
    # stores nothing, and emailing the generated string would hand Alice's key
    # to Bob — the same disclosure this endpoint was fixed for.
    db = signup.use(_DB(dev_keys={"alice@example.com": EXISTING}))
    monkeypatch.setattr(signup.fme.secrets, "token_hex", lambda n=32: "e" * (2 * n))
    r = signup.post("bob@example.com")
    assert r.status_code == 200
    _no_key_in(r, EXISTING)
    assert db.conflicts == [EXISTING], "the collision was not exercised"
    assert signup.sent == [], "a key the write did not store was emailed to another address"


@pytest.mark.parametrize("bad", [
    "", "alice", "alice@example", "a b@example.com", "x" * 250 + "@example.com",
])
def test_a_malformed_address_is_rejected_before_any_lookup(signup, bad):
    db = signup.use(_DB(dev_keys={"alice@example.com": EXISTING}))
    r = signup.post(bad)
    assert r.status_code == 400
    _no_key_in(r, EXISTING)
    assert db.statements == [] and signup.sent == []


def test_the_emailed_page_escapes_the_address_it_echoes(signup):
    signup.use(_DB())
    address = "<script>alert(1)</script>@example.com"
    assert signup.post(address).status_code == 200
    [(to, _s, html)] = signup.sent
    assert to == address
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_dev_signup_and_keys_recover_share_one_send_budget(signup):
    signup.use(_DB(dev_keys={"alice@example.com": EXISTING}))
    for _ in range(signup.kr._MAX_PER_EMAIL - 1):
        signup.post("alice@example.com", path="/api/v1/keys/recover")
    signup.post("alice@example.com")        # the last slot: sends
    signup.post("alice@example.com")        # over budget: must not send
    assert len(signup.sent) == 1, (
        "alternating the two endpoints must not double an address's send budget")


def test_the_signup_widget_does_not_expect_a_key_in_the_response():
    with open(os.path.join(ROOT, "static", "signup-widget.html"), encoding="utf-8") as f:
        page = f.read()
    script = page[page.index("<script>"):page.index("</script>")]
    code = "\n".join(line for line in script.splitlines()
                     if not line.strip().startswith("//"))
    assert "api_key" not in code, "the widget still reads a key out of the dev-signup response"
    assert "d.ok" in code and "d.message" in code, "the widget must render the neutral reply"
