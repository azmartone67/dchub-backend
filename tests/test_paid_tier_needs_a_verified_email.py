"""Paid MCP tier is never granted on an unverified address (2026-09-11).

THE DEFECT, at the door. mcp_dev_keys has no user_id FK, so `email` is the only
link between a key and a paying account. POST /api/v1/keys/claim took an
optional `email` from its (public, unauthenticated) body, regex-checked it, and
called _inherit_paid_tier — which upgraded the brand-new key on
`LOWER(u.email) = LOWER(k.email)`. Anyone who knew a paying customer's address
got a key at that customer's tier, and the response said so: `paid_plan_applied:
true`, which doubled as an oracle for "is this address a paying customer".
POST /api/v1/keys/identify was the same hole with a re-bind: send your own key
plus any address and the only check was deliverability, which says nothing
about ownership.

These tests drive the REAL handlers through Flask's test client — the
blueprints are imported (never main.py) and `_pool` is replaced by an in-memory
database — so they read exactly what the endpoints return and what they email.

★ The fake applies the verification clause ONLY WHEN THE SHIPPED STATEMENT
CONTAINS IT (see _grants_tier). Delete the clause from flask_mcp_endpoints.py
and these tests go red rather than quietly passing against a fake that is
kinder than Postgres. tests/test_paid_tier_verification_sql.py runs the same
statements against a real Postgres; this file pins the HTTP contract around them.

WHAT THIS PINS
  · claiming with a payer's address returns no paid tier and no paid_plan_applied;
  · that response is byte-identical to claiming with an address nobody pays for,
    so the endpoint is not an oracle for who is a customer;
  · the confirmation link reaches the ADDRESS and never the caller;
  · identify cannot re-bind a free key onto a payer's address and be upgraded;
  · POST /api/v1/keys/confirm with a real token DOES grant — r-coldbuy intact;
  · GET on the same link grants nothing, so a mail scanner that prefetches
    links cannot confirm on the recipient's behalf;
  · a token cannot be replayed onto another key, survive a re-bind, outlive its
    14 days, or be tampered with.
"""
import importlib
import json
import os
import re
import time
import types

import pytest
from flask import Flask

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PAYER = "payer@example.com"
NOBODY = "nobody@example.com"
_KEY_SHAPED = re.compile(r"dch(?:ub)?_(?:live|trial)_[0-9A-Za-z]{8,}")


# ── the fake database ────────────────────────────────────────────────────
class _Cursor:
    def __init__(self, db):
        self.db = db
        self._rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # The two statements that decide a tier. Both are matched on the SHIPPED
    # text, so a clause removed from the source is a clause removed here.
    def _verified(self, row, email, sql):
        if "email_verified_for" not in sql:
            return True          # the pre-fix rule: an address match was enough
        return ((row["metadata"].get("email_verified_for") or "").lower()
                == (email or "").lower())

    def _eligible(self, row, email):
        """Everything both rules share: a paying account, this address, and a
        key not already paid. The verification clause is what differs."""
        u = self.db.users.get((email or "").lower())
        return bool(
            u and u["status"] == "active"
            and u["plan"] in ("developer", "pro", "founding", "enterprise")
            and (row["email"] or "").lower() == (email or "").lower()
            and (row["tier"] or "free") not in ("paid", "enterprise"))

    def _grants_tier(self, row, email, sql):
        return self._eligible(row, email) and self._verified(row, email, sql)

    def _awaits_proof(self, row, email, sql):
        """The offer queries look for the NEGATION: eligible but unconfirmed."""
        return self._eligible(row, email) and not self._verified(row, email, sql)

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self.db.statements.append(s)
        self._rows = []
        self.rowcount = 0

        if s.startswith("INSERT INTO mcp_dev_keys"):
            api_key, dev_id, email = params[0], params[1], params[2]
            meta = json.loads(params[3]) if len(params) > 3 and params[3] else {}
            self.db.keys[api_key] = {"api_key": api_key, "developer_id": dev_id,
                                     "email": email, "tier": "identified",
                                     "status": "active", "metadata": meta}
            return

        if s.startswith("UPDATE mcp_dev_keys AS k SET tier"):       # _inherit_paid_tier
            row = self.db.keys.get(params[0])
            if row and self._grants_tier(row, params[1], s):
                u = self.db.users[(params[1] or "").lower()]
                row["tier"] = "enterprise" if u["plan"] == "enterprise" else "paid"
                self.rowcount = 1
            return

        if s.startswith("SELECT 1 FROM mcp_dev_keys k JOIN users u"):  # pending?
            row = self.db.keys.get(params[0])
            if row and self._awaits_proof(row, params[1], s):
                self._rows = [(1,)]
            return

        if s.startswith("SELECT k.api_key, k.developer_id FROM mcp_dev_keys k JOIN users u"):
            self._rows = [(r["api_key"], r["developer_id"])
                          for r in self.db.keys.values()
                          if self._awaits_proof(r, params[0], s)]
            return

        if s.startswith("SELECT id, LOWER(email) AS email"):      # reconcile: payers
            self._rows = [(i, e, u["plan"]) for i, (e, u)
                          in enumerate(self.db.users.items())
                          if u["plan"] in ("developer", "pro", "founding", "enterprise")
                          and u["status"] in ("active", "trialing", "past_due")]
            return

        if s.startswith("SELECT tier, LOWER(COALESCE(metadata->>'email_verified_for',''))"):
            # (tier, is-this-address-confirmed) per active key on the address
            self._rows = [(r["tier"],
                           (r["metadata"].get("email_verified_for") or "").lower()
                           == (params[0] or "").lower())
                          for r in self.db.keys.values()
                          if (r["email"] or "").lower() == (params[1] or "").lower()
                          and r["status"] == "active"]
            return

        if s.startswith("UPDATE mcp_dev_keys SET tier=%s"):          # reconcile: apply
            for r in self.db.keys.values():
                if ((r["email"] or "").lower() == (params[1] or "").lower()
                        and r["status"] == "active"
                        and (r["tier"] or "") not in ("paid", "enterprise")
                        and self._verified(r, params[2], s)):
                    r["tier"] = params[0]
                    self.rowcount += 1
            return

        if s.startswith("SELECT LOWER(COALESCE(k.email,'')) AS email"):  # legacy audit
            # Clause-aware like the rest: drop the marker filter from the
            # shipped query and this listing stops narrowing, which is what the
            # audit is FOR. A fake that filtered on its own would report the
            # query as selective after the query stopped being so.
            filtered = "email_verified_for" in s
            self._rows = [(r["email"], r["tier"], "claim_api", None)
                          for r in self.db.keys.values()
                          if (r["tier"] or "") in ("paid", "enterprise")
                          and r["status"] == "active" and r["email"]
                          and (not filtered
                               or (r["metadata"].get("email_verified_for") or "").lower()
                               != (r["email"] or "").lower())]
            return

        def _unproven(r):
            return ((r["tier"] or "") in ("paid", "enterprise")
                    and r["status"] == "active" and r["email"]
                    and (r["metadata"].get("email_verified_for") or "").lower()
                    != (r["email"] or "").lower())

        if s.startswith("SELECT COALESCE(k.metadata->>'source',''), LOWER(k.email)"):
            self._rows = [((r["metadata"].get("source") or ""),
                           (r["email"] or "").lower(), r["tier"])
                          for r in self.db.keys.values()
                          if _unproven(r)
                          and (r["metadata"].get("source") or "") in (params[0] or [])]
            return

        if s.startswith("SELECT COALESCE(metadata->>'source',''), COUNT(*)"):
            # Clause-aware: the exclusion only applies when the shipped query
            # carries it. Without that, this fake would report a correct
            # before/after split for a query that no longer makes one.
            excl = set(params[0] or []) if "NOT (COALESCE(metadata->>'source','')" in s else set()
            counts = {}
            for r in self.db.keys.values():
                src = r["metadata"].get("source") or ""
                if _unproven(r) and src not in excl:
                    counts[src] = counts.get(src, 0) + 1
            self._rows = sorted(counts.items(), key=lambda kv: -kv[1])
            return

        if s.startswith("UPDATE mcp_dev_keys AS k SET metadata"):   # the backfill
            for r in self.db.keys.values():
                if _unproven(r) and (r["metadata"].get("source") or "") in (params[0] or []):
                    r["metadata"]["email_verified_for"] = (r["email"] or "").lower()
                    self.rowcount += 1
            return

        if s.startswith("SELECT developer_id FROM mcp_dev_keys"):
            row = self.db.keys.get(params[0])
            self._rows = [(row["developer_id"],)] if row else []
            return

        if s.startswith("SELECT api_key FROM mcp_dev_keys WHERE developer_id"):
            self._rows = [(r["api_key"],) for r in self.db.keys.values()
                          if r["developer_id"] == params[0]
                          and (r["email"] or "").lower() == (params[1] or "").lower()
                          and r["status"] == "active"]
            return

        if s.startswith("UPDATE mcp_dev_keys SET metadata"):        # _mark_verified
            row = self.db.keys.get(params[1])
            if row:
                row["metadata"]["email_verified_for"] = (params[0] or "").lower()
                self.rowcount = 1
            return

        if s.startswith("UPDATE mcp_dev_keys SET email"):           # identify re-bind
            row = self.db.keys.get(params[-1])
            if row:
                row["email"] = params[0]
                self.rowcount = 1
            return

        if s.startswith("SELECT email, tier, status FROM mcp_dev_keys"):
            row = self.db.keys.get(params[0])
            self._rows = [(row["email"], row["tier"], row["status"])] if row else []
            return

        # Everything else on these paths is telemetry / dedupe counting, and
        # every one of them tolerates an empty result.
        return

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _DB:
    def __init__(self, users=(), keys=()):
        self.users = {e.lower(): {"plan": p, "status": s} for e, p, s in users}
        self.keys = {}
        for k in keys:
            self.keys[k["api_key"]] = dict(k)
        self.statements = []
        self.commits = []

    def connection(self):
        db = self

        class _Conn:
            def __enter__(self):
                return types.SimpleNamespace(cursor=lambda: _Cursor(db),
                                             commit=lambda: db.commits.append(1),
                                             rollback=lambda: None)

            def __exit__(self, *exc):
                return False

        return _Conn()


def _key(api_key, email, tier="identified", verified_for=None, dev=None):
    meta = {} if verified_for is None else {"email_verified_for": verified_for.lower()}
    return {"api_key": api_key, "developer_id": dev or ("dev_" + api_key[-6:]),
            "email": email, "tier": tier, "status": "active", "metadata": meta}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub:stub@127.0.0.1:1/stub")
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "test-signing-secret")
    fme = importlib.import_module("flask_mcp_endpoints")
    ver = importlib.import_module("routes.mcp_key_email_verification")
    # A stub another test left in sys.modules would let every assertion below
    # pass against a fake. Prove these are the shipped files.
    for mod, rel in ((fme, "flask_mcp_endpoints.py"),
                     (ver, "routes/mcp_key_email_verification.py")):
        assert os.path.realpath(mod.__file__) == os.path.realpath(os.path.join(ROOT, rel))

    sent = []
    monkeypatch.setattr(ver, "_send",
                        lambda to, subj, html: sent.append((to, subj, html)) or True)
    # Shipped, the send runs on a daemon thread so a slow mail provider cannot
    # slow a key claim. Run it inline here so these assertions are not racing it.
    monkeypatch.setattr(ver, "_dispatch", lambda fn, *a: fn(*a))
    # The send budget is a per-PROCESS counter shared with /keys/recover and
    # /api/v1/dev-signup. Reset it per test, or the first few tests spend it and
    # every later one reads as "no email sent" for the wrong reason.
    kr = importlib.import_module("routes.keys_recover")
    monkeypatch.setattr(kr, "_RL_EMAIL", {})
    monkeypatch.setattr(kr, "_RL_IP", {})
    monkeypatch.setattr(kr, "_RL_DAY", {"day": None})
    # Deliverability is not ownership — that confusion IS the defect. Take the
    # address checker out of the way so these tests exercise the ownership rule.
    import routes.email_validation as _ev
    monkeypatch.setattr(_ev, "validate_email",
                        lambda e, **kw: {"valid": True, "deliverable": True})

    flask_app = Flask(__name__)
    flask_app.register_blueprint(fme.mcp_bp)
    ver.register(flask_app)
    client = flask_app.test_client()

    def use(db):
        monkeypatch.setattr(fme, "_pool", db)
        return db

    return types.SimpleNamespace(fme=fme, ver=ver, kr=kr, sent=sent, use=use,
                                 client=client, monkeypatch=monkeypatch)


def _claim(app, email, ip="203.0.113.9"):
    return app.client.post("/api/v1/keys/claim",
                           json={"email": email, "client_name": "agent"},
                           headers={"CF-Connecting-IP": ip})


# ── 1. the claim door ────────────────────────────────────────────────────
def test_claiming_with_a_payers_address_does_not_hand_over_their_tier(app):
    db = app.use(_DB(users=[(PAYER, "pro", "active")]))
    r = _claim(app, PAYER)
    assert r.status_code == 200
    body = r.get_json()
    assert body["tier"] != "paid"
    assert "paid_plan_applied" not in body, (
        "the claim response announced someone else's paid plan — and told the "
        "caller the address belongs to a paying customer")
    [row] = db.keys.values()
    assert row["tier"] == "identified", (
        "a key minted from a typed address must not be born paid")


def test_the_claim_response_does_not_say_whether_the_address_pays(app):
    """Byte-identical bodies: the endpoint must not be an oracle. Key material
    is pinned so the only thing that could differ is the tier reporting."""
    seen = {}
    for email, users in ((PAYER, [(PAYER, "pro", "active")]), (NOBODY, [])):
        app.use(_DB(users=users))
        app.monkeypatch.setattr(app.fme.secrets, "token_hex",
                                lambda n=16: "b" * (2 * n))
        r = _claim(app, email)
        seen[email] = (r.status_code, r.get_data().replace(email.encode(), b"@"))
    assert len(set(seen.values())) == 1, {k: v[1][:200] for k, v in seen.items()}


def test_the_confirmation_goes_to_the_inbox_and_never_to_the_caller(app):
    app.use(_DB(users=[(PAYER, "pro", "active")]))
    r = _claim(app, PAYER)
    assert [to for to, _s, _h in app.sent] == [PAYER], (
        "the only party who may act on this is the address's owner")
    text = r.get_data(as_text=True)
    assert "/keys/confirm" not in text and "token=" not in text, (
        "a confirmation token in the response would hand the caller the very "
        "proof the link exists to withhold")


def test_no_confirmation_is_offered_when_nobody_is_paying(app):
    app.use(_DB(users=[]))
    _claim(app, NOBODY)
    assert app.sent == []


# ── 2. the identify door ─────────────────────────────────────────────────
def test_identify_cannot_re_point_a_free_key_at_a_payers_address(app):
    mine = "dch_live_" + "a" * 32
    db = app.use(_DB(users=[(PAYER, "pro", "active")],
                     keys=[_key(mine, "mine@example.com")]))
    r = app.client.post("/api/v1/keys/identify",
                        json={"api_key": mine, "email": PAYER})
    assert r.status_code == 200
    assert db.keys[mine]["tier"] == "identified", (
        "binding someone else's address to your own key must not upgrade it")
    assert [to for to, _s, _h in app.sent] == [PAYER]


def test_a_confirmed_binding_does_not_survive_being_re_pointed(app):
    """Confirm your OWN address, then re-bind the key to a payer's. The proof
    names the address it was made about, so it stops applying."""
    mine = "dch_live_" + "c" * 32
    db = app.use(_DB(users=[(PAYER, "pro", "active")],
                     keys=[_key(mine, "mine@example.com",
                                verified_for="mine@example.com")]))
    app.client.post("/api/v1/keys/identify", json={"api_key": mine, "email": PAYER})
    assert db.keys[mine]["tier"] == "identified", (
        "a confirmation for one address must never unlock another")


# ── 3. the confirm link ──────────────────────────────────────────────────
def _link(app, api_key, dev_id, email, issued=None):
    """The query string of the emailed URL, as a browser would submit it."""
    from urllib.parse import parse_qsl
    url = app.ver.confirm_url(api_key, dev_id, email, issued=issued)
    return dict(parse_qsl(url.split("?", 1)[1]))


def test_the_confirmation_click_applies_the_plan_already_paid_for(app):
    """r-coldbuy: pay first, then claim. The claim grants nothing; the click does."""
    k = "dch_live_" + "d" * 32
    db = app.use(_DB(users=[(PAYER, "pro", "active")], keys=[_key(k, PAYER)]))
    r = app.client.post("/api/v1/keys/confirm",
                        data=_link(app, k, db.keys[k]["developer_id"], PAYER))
    assert r.status_code == 200
    assert "Key confirmed" in r.get_data(as_text=True)
    assert db.keys[k]["tier"] == "paid", (
        "a customer who pays before claiming must still reach their paid tier")


def test_following_the_link_grants_nothing_until_someone_presses_the_button(app):
    """Mail scanners in the recipient's own org follow links in delivered mail.
    A GET that granted the tier would let the victim's security stack confirm
    an attacker's binding for them."""
    k = "dch_live_" + "e" * 32
    db = app.use(_DB(users=[(PAYER, "pro", "active")], keys=[_key(k, PAYER)]))
    from urllib.parse import urlencode
    r = app.client.get("/api/v1/keys/confirm?" + urlencode(
        _link(app, k, db.keys[k]["developer_id"], PAYER)))
    assert r.status_code == 200
    assert db.keys[k]["tier"] == "identified", "a prefetched GET granted the tier"
    assert "<form method=\"POST\"" in r.get_data(as_text=True)
    assert "no-store" in r.headers.get("Cache-Control", "")


@pytest.mark.parametrize("break_it", ["replay", "tamper", "expired", "rebound"])
def test_a_token_that_is_not_for_this_key_and_address_right_now_is_refused(app, break_it):
    mine = "dch_live_" + "1" * 32
    theirs = "dch_live_" + "2" * 32
    db = app.use(_DB(users=[(PAYER, "pro", "active")],
                     keys=[_key(mine, PAYER, dev="dev_mine"),
                           _key(theirs, PAYER, dev="dev_theirs")]))
    if break_it == "replay":
        # A token minted for `mine`, pointed at `theirs` — the signature is
        # over the api_key, so it cannot move to another key.
        form = _link(app, mine, "dev_theirs", PAYER)
    elif break_it == "tamper":
        form = _link(app, mine, "dev_mine", PAYER)
        form["token"] = form["token"][:-1] + ("0" if form["token"][-1] != "0" else "1")
    elif break_it == "expired":
        form = _link(app, mine, "dev_mine", PAYER,
                     issued=int(time.time()) - (app.ver._MAX_AGE_SECONDS + 60))
    else:                       # rebound: proof was made for another address
        form = _link(app, mine, "dev_mine", "someone@example.com")
    r = app.client.post("/api/v1/keys/confirm", data=form)
    assert r.status_code == 200
    assert "Link not valid" in r.get_data(as_text=True)
    assert [v["tier"] for v in db.keys.values()] == ["identified", "identified"]


# ── 4. the confirmation must not become a way to bury someone ────────────
def test_repeated_claims_cannot_flood_the_address_with_confirmations(app):
    """/keys/claim is public and takes any address. Without a shared budget,
    claiming in a loop mails the victim once per request."""
    app.use(_DB(users=[(PAYER, "pro", "active")]))
    for _ in range(app.kr._MAX_PER_EMAIL + 4):
        _claim(app, PAYER)
    assert len(app.sent) == app.kr._MAX_PER_EMAIL, (
        "the confirmation send must spend the same per-address daily budget "
        "/keys/recover and /api/v1/dev-signup share")


def test_no_link_is_minted_or_accepted_without_a_signing_secret(app, monkeypatch):
    """A public repo cannot carry a literal fallback secret: it would be a
    signing key anyone could read. With none configured we mint nothing and
    accept nothing, rather than issuing forgeable grants."""
    k = "dch_live_" + "f" * 32
    db = app.use(_DB(users=[(PAYER, "pro", "active")], keys=[_key(k, PAYER)]))
    good = _link(app, k, db.keys[k]["developer_id"], PAYER)   # signed while set
    for var in ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "DCHUB_SESSION_SECRET"):
        monkeypatch.delenv(var, raising=False)
    assert app.ver.confirm_url(k, "dev", PAYER) == "", "minted an unsignable link"
    r = app.client.post("/api/v1/keys/confirm", data=good)
    assert "Link not valid" in r.get_data(as_text=True)
    assert db.keys[k]["tier"] == "identified"


# ── 5. the daily reconcile: report and apply must agree ──────────────────
# WHICH LAYER PROVES WHAT. The reconcile has two gates, deliberately: a Python
# classification that decides who qualifies, and the WHERE clause on the write
# itself. The Python one short-circuits first, so mutating ONLY the SQL clause
# leaves these tests green — verified, not assumed. That clause is proven by
# tests/test_paid_tier_verification_sql.py, which executes the statement
# directly against Postgres with no Python in front of it. Neither file covers
# both; together they cover each gate once.
@pytest.fixture
def reconcile(app, monkeypatch):
    import routes.funnel_health as fh
    monkeypatch.setattr(fh, "_ADMIN_KEY", "admin-test-key")

    def get(**params):
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return app.client.get("/api/v1/admin/billing/reconcile-keys?" + qs,
                              headers={"X-Admin-Key": "admin-test-key"})
    return get


def test_the_daily_sweep_does_not_regrant_what_the_doors_refused(app, reconcile):
    """It runs with apply=1 every night. Unqualified, it re-granted within 24h
    every promotion /keys/claim and /keys/identify had just been stopped from
    making — which is why gating the doors alone would have been cosmetic."""
    db = app.use(_DB(users=[(PAYER, "pro", "active")],
                     keys=[_key("dch_live_" + "9" * 32, PAYER)]))     # unconfirmed
    r = reconcile(apply=1)
    assert r.status_code == 200
    body = r.get_json()
    assert body["keys_upgraded"] == 0
    assert body["buckets"]["withheld_unverified_binding"] == 1
    assert [v["tier"] for v in db.keys.values()] == ["identified"]


def test_the_dry_run_reports_the_same_split_the_apply_run_performs(app, reconcile):
    """The dry run is how this endpoint is read day to day. If it counted a
    withheld payer as upgradable, the apply run would silently do less than the
    report promised."""
    app.use(_DB(users=[(PAYER, "pro", "active")],
                keys=[_key("dch_live_" + "8" * 32, PAYER)]))
    dry = reconcile(apply=0).get_json()
    assert (dry["buckets"]["key_upgraded"],
            dry["buckets"]["withheld_unverified_binding"]) == (0, 1)


def test_a_confirmed_payer_is_still_reconciled(app, reconcile):
    """The endpoint exists to rescue real payers whose key is stuck. That must
    keep working — the fix narrows who qualifies, not what it does."""
    k = "dch_live_" + "7" * 32
    db = app.use(_DB(users=[(PAYER, "pro", "active")],
                     keys=[_key(k, PAYER, verified_for=PAYER)]))
    body = reconcile(apply=1).get_json()
    assert body["keys_upgraded"] == 1
    assert db.keys[k]["tier"] == "paid"


def test_the_audit_lists_paid_keys_whose_address_was_never_confirmed(app, reconcile):
    """The rows the old rule already granted. Reported, never auto-demoted:
    demoting a real customer's live key would be worse than the defect."""
    app.use(_DB(users=[(PAYER, "pro", "active")],
                keys=[_key("dch_live_" + "6" * 32, PAYER, tier="paid"),
                      _key("dch_live_" + "5" * 32, PAYER, tier="paid",
                           verified_for=PAYER)]))
    audit = reconcile(apply=0).get_json()["legacy_unverified_paid_keys"]
    assert audit["count"] == 1, "only the unconfirmed paid key belongs in the audit"
    assert audit["samples"][0]["tier"] == "paid"
    assert "@" in audit["samples"][0]["email"] and "***" in audit["samples"][0]["email"]


# ── 6. the link must not sit in a shared edge cache ──────────────────────
def test_the_confirm_token_uses_a_query_name_the_edge_treats_as_a_credential(app):
    """CF rule 2 caches everything under /api/v1/ with mode override_origin,
    which IGNORES the no-store this page sends. Rule 24 bypasses that, but only
    for a fixed list of credential-shaped query-arg NAMES — so the parameter
    name is what decides whether a page carrying a live grant token gets stored
    in a shared cache. Read the name out of the shipped URL builder and check it
    against the ruleset canon, rather than trusting either in isolation.
    """
    import json
    import re as _re
    from urllib.parse import parse_qs, urlparse
    # Ask the shipped builder for a real URL rather than regexing the source —
    # the name that matters is the one it actually emits.
    url = app.ver.confirm_url("dch_live_" + "0" * 32, "dev_x", PAYER)
    args = parse_qs(urlparse(url).query)
    sig = app.ver._sign("dch_live_" + "0" * 32, PAYER,
                        int(args["s"][0]))
    named = [k for k, v in args.items() if v == [sig]]
    assert named == ["token"], (
        f"the signature travels as {named or list(args)} — CF rule 24 bypasses "
        f"cache on a fixed list of arg NAMES, and this one must be on it")

    canon = json.load(open(os.path.join(ROOT, "scripts/cf_cache_ruleset_canon.json")))
    bypass = [r for r in canon["rules"]
              if r.get("description", "").startswith("Bypass cache for CREDENTIALED")]
    assert bypass, "rule 24 is gone from the canon — re-check what caches /api/v1/"
    honoured = set(_re.findall(r'http\.request\.uri\.args\["([^"]+)"\]',
                              bypass[0]["expression"]))
    assert "token" in honoured, (
        "the edge no longer bypasses cache on a `token` query arg, so the "
        "confirmation page — token and all — is cacheable again. Either add "
        "/api/v1/keys/confirm to a bypass rule on the zone, or use an arg name "
        f"the rule still honours: {sorted(honoured)}")


# ── 7. the legacy backfill's source allowlist cannot be widened ──────────
# The allowlist is a standing rule about which channels ESTABLISHED an address
# well enough to count as proof. `?sources=` exists to stage a backfill, so it
# must only ever narrow — a query string that could add `claim_api` would hand
# the rule back to whoever types the URL, which is the shape of the original
# defect one level up.
@pytest.fixture
def backfill(app, monkeypatch):
    import routes.funnel_health as fh
    monkeypatch.setattr(fh, "_ADMIN_KEY", "admin-test-key")

    def get(**params):
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return app.client.get(
            "/api/v1/admin/billing/backfill-verified-bindings?" + qs,
            headers={"X-Admin-Key": "admin-test-key"})
    return get


@pytest.mark.parametrize("asked", ["claim_api", "redeem", "claim_api,redeem",
                                   "", "anything_at_all"])
def test_a_source_outside_the_allowlist_is_never_backfilled(app, backfill, asked):
    db = app.use(_DB())
    r = backfill(**({"sources": asked} if asked else {}))
    assert r.status_code == 200
    body = r.get_json()
    for bad in ("claim_api", "redeem"):
        assert bad not in body["sources"], (
            f"{bad!r} reached the backfill through the query string — "
            f"the allowlist must be an intersection, never a union")
    if asked:
        assert set(body["sources"]) <= set(app.fme._BACKFILL_PROVEN_SOURCES)
        assert body["rejected_sources"] == sorted(
            set(x for x in asked.split(",") if x)
            - set(app.fme._BACKFILL_PROVEN_SOURCES))
        assert db.statements == [], "an all-rejected request must not query at all"


def test_the_allowlist_excludes_the_two_bearer_shaped_sources(app):
    """Kept as an assertion, not only a comment: `redeem` binds a caller-typed
    address behind a bearer session id, and `claim_api` is the door the defect
    was in. Neither is evidence the inbox belongs to the key holder."""
    proven = app.fme._BACKFILL_PROVEN_SOURCES
    for never in app.fme._BACKFILL_NEVER:
        assert never not in proven, f"{never!r} is not proof of anything"
    assert "stripe_subscription" in proven and "workos_oauth" in proven, (
        "the allowlist lost the channels that DO establish an address")


def test_the_backfill_requires_the_admin_gate(app):
    app.use(_DB())
    r = app.client.get("/api/v1/admin/billing/backfill-verified-bindings?apply=1")
    assert r.status_code in (401, 503)


# ── 8. the dry run must report the state the apply would LEAVE ───────────
def test_the_dry_run_reports_what_would_remain_not_what_is_there_now(app, backfill):
    """The defect this fixes, at the wiring rather than in the SQL.

    The query can be perfectly correct and still be CALLED with the wrong
    source list — which is what shipped: the after-count was computed with no
    exclusion, so a dry run (which writes nothing) reported its starting state
    under a name that reads as its ending state. Measured live on 52 rows: it
    said 52 would remain when the answer was 25.

    tests/test_tier_grant_gates_sql.py executes the query directly and cannot
    see this: it supplies its own parameters. Only driving the handler does.
    """
    db = app.use(_DB(keys=[
        _key("dch_live_" + "a" * 32, PAYER, tier="paid"),          # source ""
        _key("dch_live_" + "b" * 32, PAYER, tier="paid"),
    ]))
    for k, src in zip(db.keys.values(), ["stripe_subscription", "redeem"]):
        k["metadata"]["source"] = src

    body = backfill(apply=0).get_json()
    assert body["unproven_before"]["count"] == 2
    assert body["unproven_after"]["count"] == 1, (
        "the dry run reported its starting state as what would remain")
    assert body["unproven_after"]["by_source"] == {"redeem": 1}
    assert sum(body["by_source"].values()) == 1
    assert body["stamped"] == 0 and db.keys[list(db.keys)[0]]["tier"] == "paid"
    # the alias the endpoint shipped with now means what its name claimed
    assert body["still_unproven"] == body["unproven_after"]


def test_the_apply_leaves_exactly_what_the_dry_run_predicted(app, backfill):
    """The projection is only worth anything if applying agrees with it."""
    db = app.use(_DB(keys=[
        _key("dch_live_" + "c" * 32, PAYER, tier="paid"),
        _key("dch_live_" + "d" * 32, PAYER, tier="paid"),
        _key("dch_live_" + "e" * 32, PAYER, tier="paid"),
    ]))
    for k, src in zip(db.keys.values(),
                      ["stripe_subscription", "workos_oauth", "claim_api"]):
        k["metadata"]["source"] = src

    predicted = backfill(apply=0).get_json()["unproven_after"]
    applied = backfill(apply=1).get_json()
    assert applied["stamped"] == 2
    assert applied["unproven_before"]["count"] == 1, (
        "after the write, 'before' is measured on the new state — the two "
        "stamped rows are proven now")
    assert applied["unproven_after"] == predicted, (
        "the apply left a different set than the dry run promised")
