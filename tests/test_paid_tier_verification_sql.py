"""The three paid-tier grants, executed against a real Postgres (2026-09-11).

THE DEFECT. mcp_dev_keys has no user_id FK, so `email` is the only link between
a key and a paying account — and three statements granted paid MCP tier on that
link alone:

  1. _inherit_paid_tier            — called by POST /api/v1/keys/claim and
                                     POST /api/v1/keys/identify, both public,
                                     both taking the address from a request body
  2. admin_reconcile_keys          — run daily with apply=1 by
                                     .github/workflows/billing-reconcile-daily.yml
  3. handle_checkout_completed     — the Stripe checkout webhook

Anyone who knew a paying customer's address could claim a key with it and be
handed tier 'paid'. Fixing only (1) would have been cosmetic: (2) re-granted it
within 24 hours, and (3) granted it to whatever key happened to be bound to the
address when the customer paid.

★ EACH STATEMENT IS READ OUT OF THE SHIPPED SOURCE BY ast, NEVER COPIED HERE.
A copy would drift, and a drifted copy passing is worse than no test — it would
report that the shipped grant behaves in a way the shipped grant no longer does.
The fixtures below are the same rows for all three, so one table of expectations
covers every door.

THE ROWS. Every one of them fails a different clause:

  k_owner      bound + CONFIRMED for the payer's address     -> upgrades
  k_attacker   bound to the payer's address, never confirmed -> WITHHELD  ← the defect
  k_rebound    confirmed for its OLD address, re-bound to
               the payer's (what /keys/identify lets anyone
               do to their own key)                          -> WITHHELD  ← the bypass
  k_upper      confirmation stored in a different case        -> upgrades
  k_free       confirmed, but the account is on the free plan -> no grant
  k_cancelled  confirmed, but the subscription is not active  -> no grant
  k_paid       already paid                                   -> untouched (upgrade-only)
  k_ent        confirmed, enterprise plan                      -> 'enterprise', not 'paid'

Set PAID_TIER_SQL_DSN to run it. CI passes the db-parity service DSN and then
asserts this file did not skip — a skipped proof is not a proof.
"""
import ast
import os
import pathlib

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("PAID_TIER_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="PAID_TIER_SQL_DSN not set — no Postgres to run against")

REPO = pathlib.Path(__file__).resolve().parents[1]

PAYER = "payer@example.com"
OTHER = "someone-else@example.com"


# ── read the shipped statements ──────────────────────────────────────────
def shipped_updates(rel, func):
    """Every `UPDATE mcp_dev_keys` string literal inside `func`, from source.

    ast, not an import: flask_mcp_endpoints and main pull psycopg2, Flask and
    most of the app at module scope. Parsing gets the real strings without
    importing the world. Implicitly-concatenated literals (and the comments
    between them) are already one Constant by the time ast sees them.
    """
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
    found = []
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == func:
            for n in ast.walk(fn):
                if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                        and "UPDATE mcp_dev_keys" in n.value):
                    found.append(n.value)
    assert found, f"{rel}:{func} — no UPDATE mcp_dev_keys found (renamed?)"
    return found


def shipped_update(rel, func, must_contain=None, expect=1):
    """The one statement in `func` matching `must_contain`.

    `expect` is a ratchet on the TOTAL number of grants in that function: a
    newly added way to write mcp_dev_keys.tier fails here rather than shipping
    ungated, because every grant in these three functions needs a row in the
    table of expectations below.
    """
    all_stmts = shipped_updates(rel, func)
    assert len(all_stmts) == expect, (
        f"{rel}:{func} has {len(all_stmts)} UPDATE mcp_dev_keys statements, "
        f"expected {expect}. A new tier grant needs its own case in this test — "
        f"decide whether it may run on an email match alone.")
    # Match on a whitespace-normalised copy, EXECUTE the raw statement. A `--`
    # comment only ends at its newline, so a flattened statement would comment
    # out every clause after it — which is exactly how the first draft of this
    # test reported the fix as broken. Nothing may hide a predicate that way.
    for s in all_stmts:
        assert "--" not in s, (
            f"{rel}:{func} — a SQL `--` comment inside the statement. It ends at "
            f"a newline, so anything that logs or normalises this SQL turns the "
            f"rest of the WHERE clause into comment text. Keep it in Python.")
    hits = ([s for s in all_stmts if must_contain in " ".join(s.split())]
            if must_contain else all_stmts)
    assert len(hits) == 1, (
        f"{rel}:{func} — {len(hits)} statements contain {must_contain!r}")
    return hits[0]


INHERIT_SQL = shipped_update("flask_mcp_endpoints.py", "_inherit_paid_tier")
RECONCILE_SQL = shipped_update("flask_mcp_endpoints.py", "admin_reconcile_keys")
# The webhook holds TWO grants. Only the email match is a guess about identity.
WEBHOOK_SQL = shipped_update("main.py", "handle_checkout_completed",
                             must_contain="LOWER(email) = LOWER(%s)", expect=2)
WEBHOOK_KREF_SQL = shipped_update("main.py", "handle_checkout_completed",
                                  must_contain="sha256(api_key", expect=2)


# ── fixture ──────────────────────────────────────────────────────────────
KEYS = {
    #  api_key        email    tier           verified_for
    "k_owner":     (PAYER, "identified", PAYER),
    "k_attacker":  (PAYER, "identified", None),
    "k_rebound":   (PAYER, "identified", OTHER),
    "k_upper":     (PAYER, "identified", PAYER.upper()),
    "k_free":      ("free-user@example.com", "identified", "free-user@example.com"),
    "k_cancelled": ("lapsed@example.com", "identified", "lapsed@example.com"),
    "k_paid":      (PAYER, "paid", PAYER),
    "k_ent":       ("ent@example.com", "identified", "ent@example.com"),
}

USERS = [
    (PAYER, "pro", "active"),
    ("free-user@example.com", "free", "active"),
    ("lapsed@example.com", "pro", "canceled"),
    ("ent@example.com", "enterprise", "active"),
]


@pytest.fixture
def db():
    conn = psycopg2.connect(DSN, connect_timeout=8)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS mcp_dev_keys, users CASCADE")
        cur.execute("""CREATE TABLE users (
                         id SERIAL PRIMARY KEY,
                         email TEXT,
                         plan TEXT,
                         subscription_status TEXT,
                         stripe_customer_id TEXT DEFAULT 'cus_test')""")
        cur.execute("""CREATE TABLE mcp_dev_keys (
                         api_key TEXT PRIMARY KEY,
                         developer_id TEXT,
                         email TEXT,
                         tier TEXT,
                         status TEXT DEFAULT 'active',
                         metadata JSONB DEFAULT '{}'::jsonb,
                         created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.executemany(
            "INSERT INTO users (email, plan, subscription_status) VALUES (%s,%s,%s)",
            USERS)
        for name, (email, tier, verified) in KEYS.items():
            meta = ({} if verified is None
                    else {"email_verified_for": verified.lower(),
                          "email_verified_via": "confirm_link"})
            import json
            cur.execute(
                "INSERT INTO mcp_dev_keys (api_key, developer_id, email, tier, metadata)"
                " VALUES (%s,%s,%s,%s,%s::jsonb)",
                (name, "dev_" + name, email, tier, json.dumps(meta)))
    yield conn
    conn.close()


def tier_of(conn, name):
    with conn.cursor() as cur:
        cur.execute("SELECT tier FROM mcp_dev_keys WHERE api_key = %s", (name,))
        return cur.fetchone()[0]


# ── 1. the helper both public doors call ─────────────────────────────────
@pytest.mark.parametrize("key,email,expect", [
    ("k_owner",     PAYER, "paid"),
    ("k_attacker",  PAYER, "identified"),
    ("k_rebound",   PAYER, "identified"),
    ("k_upper",     PAYER, "paid"),
    ("k_free",      "free-user@example.com", "identified"),
    ("k_cancelled", "lapsed@example.com", "identified"),
    ("k_paid",      PAYER, "paid"),
    ("k_ent",       "ent@example.com", "enterprise"),
])
def test_inherit_grants_only_a_confirmed_binding(db, key, email, expect):
    with db.cursor() as cur:
        cur.execute(INHERIT_SQL, (key, email, email))
    assert tier_of(db, key) == expect


def test_the_attack_and_the_paying_customer_differ_only_in_the_confirmation(db):
    """k_owner and k_attacker are the SAME row but for the marker. One is the
    customer who paid; the other is anyone who typed their address."""
    with db.cursor() as cur:
        cur.execute(INHERIT_SQL, ("k_owner", PAYER, PAYER))
        cur.execute(INHERIT_SQL, ("k_attacker", PAYER, PAYER))
    assert (tier_of(db, "k_owner"), tier_of(db, "k_attacker")) == ("paid", "identified")


# ── 2. the daily reconcile ───────────────────────────────────────────────
def test_reconcile_upgrades_only_confirmed_bindings(db):
    """It sweeps by address, so ALL FOUR keys on the payer's address are in
    scope of one statement — exactly why an unqualified sweep re-granted the
    tier every night after the public doors were gated."""
    with db.cursor() as cur:
        cur.execute(RECONCILE_SQL, ("paid", PAYER, PAYER))
        assert cur.rowcount == 2, "k_owner + k_upper, and nothing else"
    assert tier_of(db, "k_owner") == "paid"
    assert tier_of(db, "k_upper") == "paid"
    assert tier_of(db, "k_attacker") == "identified"
    assert tier_of(db, "k_rebound") == "identified"


# ── 3. the Stripe checkout webhook ───────────────────────────────────────
def test_the_webhook_does_not_promote_a_key_someone_else_bound(db):
    """The customer's OWN payment used to promote every active key on their
    address — including one an attacker bound there before they paid."""
    with db.cursor() as cur:
        cur.execute(WEBHOOK_SQL, ("paid", PAYER, PAYER))
    assert tier_of(db, "k_owner") == "paid"
    assert tier_of(db, "k_attacker") == "identified"
    assert tier_of(db, "k_rebound") == "identified"


# ── 4. r-coldbuy, the flow that must keep working ────────────────────────
def test_pay_first_then_confirm_still_reaches_the_paid_tier(db):
    """A customer who paid BEFORE ever claiming a key: the claim grants
    nothing (no proof yet), the confirmation click writes the marker, and the
    same helper then applies the tier they already bought."""
    import json
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO mcp_dev_keys (api_key, developer_id, email, tier, metadata)"
            " VALUES ('k_cold','dev_cold',%s,'identified','{}'::jsonb)", (PAYER,))
        cur.execute(INHERIT_SQL, ("k_cold", PAYER, PAYER))
        assert tier_of(db, "k_cold") == "identified", "no proof yet — nothing granted"

        # what POST /api/v1/keys/confirm writes
        cur.execute("UPDATE mcp_dev_keys SET metadata = metadata || %s::jsonb "
                    "WHERE api_key = 'k_cold'",
                    (json.dumps({"email_verified_for": PAYER}),))
        cur.execute(INHERIT_SQL, ("k_cold", PAYER, PAYER))
    assert tier_of(db, "k_cold") == "paid", (
        "r-coldbuy: a customer who pays before claiming must still reach their "
        "paid MCP tier after confirming the address they paid with")


# ── 5. the grant that is CORRECTLY ungated, and why ──────────────────────
def test_the_checkout_reference_grant_needs_no_email_at_all(db):
    """The webhook's other grant matches on encode(sha256(api_key),'hex') taken
    from the checkout's client_reference_id — the caller opened the checkout
    holding that exact key. That is possession of the credential, not a guess
    about who owns an address, so it is deliberately NOT gated on a confirmed
    binding. This test exists so the distinction is deliberate and stays so: it
    fails if that statement ever starts matching on email instead.
    """
    import hashlib
    assert "email" not in WEBHOOK_KREF_SQL, (
        "the checkout-reference grant must identify the key by its own hash, "
        "never by an address — an address is the thing that cannot be trusted")
    with db.cursor() as cur:
        cur.execute("UPDATE mcp_dev_keys SET email = NULL WHERE api_key = 'k_attacker'")
        khash = hashlib.sha256(b"k_attacker").hexdigest()
        cur.execute(WEBHOOK_KREF_SQL, ("paid", khash, "paid"))
    assert tier_of(db, "k_attacker") == "paid", (
        "possession of the key is sufficient on its own — gating this path too "
        "would break the keyed upgrade flow for no security gain")
