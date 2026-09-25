"""The grants #4428 MISSED, and the backfill, against a real Postgres.

#4428 gated three statements that granted paid MCP tier on an email match.
Three more of the same shape were still open, and this file executes each of
them for real:

  * flask_mcp_endpoints.stripe_webhook_mcp — adopted the NEWEST active key on
    the buyer's address and lifted it to paid. `ORDER BY created_at DESC` is
    what made it worth attacking: a key bound to a customer's address shortly
    before they paid took the grant deterministically.
  * stripe_metered._agentic_key_for_email — the same lookup, and its caller
    writes tier='paid' to whatever it returns.
  * main.reconcile_mcp_tiers — the twin of the admin sweep that WAS gated.

In two of the three the gate is on a SELECT rather than on the write, so these
tests run the SELECT and assert which row it hands back. That is the decision
that matters: the UPDATE is only ever as safe as the row handed to it.

★ Every statement is read out of the shipped source, never copied. Two of them
are built by concatenating a module constant, so the reader folds `Constant +
Name` using the module's own constants — the same "resolve the value, don't
trust the name" rule the classification guard uses.

Set TIER_GATES_SQL_DSN to run it. CI passes the db-parity DSN and then asserts
this file did not skip.
"""
import ast
import os
import pathlib

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("TIER_GATES_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="TIER_GATES_SQL_DSN not set — no Postgres to run against")

REPO = pathlib.Path(__file__).resolve().parents[1]
PAYER = "payer@example.com"
OTHER = "someone-else@example.com"


# ── read the shipped statements ──────────────────────────────────────────
def _module_consts(tree):
    out = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node.value.value
    return out


def _fold(node, consts):
    """Evaluate a SQL expression built from string literals and module constants."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left, consts), _fold(node.right, consts)
        return None if left is None or right is None else left + right
    return None


def shipped_sql(rel, func, must_contain):
    """The one SQL expression inside `func` containing `must_contain`."""
    src = (REPO / rel).read_text(encoding="utf-8")
    tree = ast.parse(src)
    consts = _module_consts(tree)
    hits = []
    for fn in ast.walk(tree):
        if (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                and fn.name == func):
            for node in ast.walk(fn):
                sql = _fold(node, consts)
                if sql and "mcp_dev_keys" in sql and must_contain in " ".join(sql.split()):
                    hits.append(sql)
    # A folded BinOp also yields its Constant children; keep the longest.
    hits = [h for h in hits if not any(h != o and h in o for o in hits)]
    assert len(hits) == 1, (
        f"{rel}:{func} — {len(hits)} statements contain {must_contain!r}. "
        f"If the statement was renamed or split, this test is no longer reading "
        f"the shipped one.")
    stmt = hits[0]
    # A statement whose pieces did not all fold comes back as a FRAGMENT — and a
    # WHERE-less UPDATE executed by a test rewrites every row and then reports
    # the guard as broken. Refuse the fragment instead of running it.
    if " ".join(stmt.split()).upper().startswith("UPDATE"):
        assert " WHERE " in " ".join(stmt.split()).upper(), (
            f"{rel}:{func} — the extracted UPDATE has no WHERE clause, so part "
            f"of it did not resolve (a function-local fragment?). Running this "
            f"would touch every row.")
    for h in hits:
        assert "--" not in h, (
            f"{rel}:{func} — a SQL `--` comment inside the statement ends at its "
            f"newline; anything that flattens this SQL comments out the rest.")
    return hits[0]


WEBHOOK_PICK = shipped_sql("flask_mcp_endpoints.py", "stripe_webhook_mcp",
                           "ORDER BY created_at DESC LIMIT 1")
AGENTIC_PICK = shipped_sql("routes/stripe_metered.py", "_agentic_key_for_email",
                           "ORDER BY created_at DESC LIMIT 1")
TWIN_SELECT = shipped_sql("main.py", "reconcile_mcp_tiers", "SELECT k.api_key")
TWIN_UPDATE = shipped_sql("main.py", "reconcile_mcp_tiers", "UPDATE mcp_dev_keys AS k")
# The webhook's email-match write is the one that carries a PLAN CHANGE: unlike
# the two reconcile sweeps it is not upgrade-from-free-only, so it is what moves
# an existing paid key to enterprise.
WEBHOOK_TIER_WRITE = shipped_sql("main.py", "handle_checkout_completed",
                                 "UPDATE mcp_dev_keys SET tier = %s WHERE LOWER(email)")
UNPROVEN = shipped_sql("flask_mcp_endpoints.py",
                       "admin_backfill_verified_bindings",
                       "SELECT COALESCE(metadata->>'source',''), COUNT(*)")
BACKFILL_UPDATE = shipped_sql("flask_mcp_endpoints.py",
                              "admin_backfill_verified_bindings",
                              "UPDATE mcp_dev_keys AS k")


# ── fixture ──────────────────────────────────────────────────────────────
@pytest.fixture
def db():
    conn = psycopg2.connect(DSN, connect_timeout=8)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS mcp_dev_keys, users CASCADE")
        cur.execute("""CREATE TABLE users (
                         id SERIAL PRIMARY KEY, email TEXT, plan TEXT,
                         subscription_status TEXT,
                         stripe_customer_id TEXT DEFAULT 'cus_test')""")
        cur.execute("""CREATE TABLE mcp_dev_keys (
                         api_key TEXT PRIMARY KEY, developer_id TEXT, email TEXT,
                         tier TEXT, status TEXT DEFAULT 'active',
                         metadata JSONB DEFAULT '{}'::jsonb,
                         created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("INSERT INTO users (email, plan, subscription_status) VALUES "
                    "(%s,'pro','active')", (PAYER,))
    yield conn
    conn.close()


def _key(conn, name, email, tier="free", verified_for=None, age_seconds=0,
         source=None):
    import json
    meta = {}
    if verified_for:
        meta["email_verified_for"] = verified_for.lower()
    if source:
        meta["source"] = source
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mcp_dev_keys (api_key, developer_id, email, tier, metadata,"
            " created_at) VALUES (%s,%s,%s,%s,%s::jsonb, NOW() ON CONFLICT DO NOTHING - (%s || ' seconds')::interval)",
            (name, "dev_" + name, email, tier, json.dumps(meta), age_seconds))


def tier_of(conn, name):
    with conn.cursor() as cur:
        cur.execute("SELECT tier FROM mcp_dev_keys WHERE api_key=%s", (name,))
        return cur.fetchone()[0]


# ── 1 + 2. the two SELECTs that choose which key gets upgraded ───────────
@pytest.mark.parametrize("sql,label", [
    (WEBHOOK_PICK, "stripe_webhook_mcp"),
    (AGENTIC_PICK, "_agentic_key_for_email"),
], ids=["subscription-webhook", "metered-agentic"])
def test_the_newest_unconfirmed_key_is_not_the_one_adopted(db, sql, label):
    """The attack this closes, in one fixture: the customer's own confirmed key
    is OLDER than the key an attacker bound to their address. Newest-wins
    ordering handed the grant to the attacker."""
    _key(db, "k_owner", PAYER, verified_for=PAYER, age_seconds=3600)
    _key(db, "k_attacker", PAYER, age_seconds=1)          # newest, unconfirmed
    with db.cursor() as cur:
        cur.execute(sql, (PAYER, PAYER))
        row = cur.fetchone()
    assert row and row[0] == "k_owner", (
        f"{label} adopted {row[0] if row else None} — newest-wins let a key "
        f"bound to the payer's address take their upgrade")


@pytest.mark.parametrize("sql", [WEBHOOK_PICK, AGENTIC_PICK],
                         ids=["subscription-webhook", "metered-agentic"])
def test_an_address_with_only_unconfirmed_keys_adopts_nothing(db, sql):
    """Returning nothing is the SAFE answer: both callers then mint a fresh key
    and email it to the address, which is what an address with no key already
    got. The buyer is never left without one."""
    _key(db, "k_attacker", PAYER)
    with db.cursor() as cur:
        cur.execute(sql, (PAYER, PAYER))
        assert cur.fetchone() is None


# ── 3. the twin admin sweep ──────────────────────────────────────────────
def test_the_twin_sweep_reports_and_upgrades_only_confirmed_bindings(db):
    """#4428 gated /reconcile-keys and missed this one. Its SELECT feeds the
    dry-run report an operator reads before applying, so both carry the clause
    or the report promises what the apply will not do."""
    _key(db, "k_owner", PAYER, verified_for=PAYER)
    _key(db, "k_attacker", PAYER)
    _key(db, "k_rebound", PAYER, verified_for=OTHER)
    with db.cursor() as cur:
        cur.execute(TWIN_SELECT)
        assert [r[0] for r in cur.fetchall()] == ["k_owner"], "the dry-run report"
        cur.execute(TWIN_UPDATE)
        assert cur.rowcount == 1, "the apply"
    assert tier_of(db, "k_owner") == "paid"
    assert tier_of(db, "k_attacker") == "free"
    assert tier_of(db, "k_rebound") == "free"


# ── 4. the backfill: records proof, never grants ─────────────────────────
def _backfill(conn, sources):
    with conn.cursor() as cur:
        cur.execute(BACKFILL_UPDATE, (sources,))
        return cur.rowcount


def test_the_backfill_stamps_a_paid_key_whose_address_came_from_stripe(db):
    _key(db, "k_sub", PAYER, tier="paid", source="stripe_subscription")
    assert _backfill(db, ["stripe_subscription"]) == 1
    with db.cursor() as cur:
        cur.execute("SELECT metadata->>'email_verified_for', "
                    "       metadata->>'email_verified_via' "
                    "  FROM mcp_dev_keys WHERE api_key='k_sub'")
        assert cur.fetchone() == (PAYER, "legacy_backfill")


def test_the_backfill_never_grants_a_tier(db):
    """THE invariant. Stamping a FREE key would not merely record a proof — the
    next reconcile would read that proof and grant paid. So the backfill must
    only ever touch a row whose tier is already in force."""
    _key(db, "k_free", PAYER, tier="free", source="stripe_subscription")
    _key(db, "k_identified", PAYER, tier="identified", source="stripe_subscription")
    assert _backfill(db, ["stripe_subscription"]) == 0
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM mcp_dev_keys "
                    " WHERE metadata ? 'email_verified_for'")
        assert cur.fetchone()[0] == 0, (
            "a not-yet-paid key was stamped — that is a GRANT, one reconcile away")
    assert (tier_of(db, "k_free"), tier_of(db, "k_identified")) == ("free", "identified")


def test_the_backfill_touches_only_the_sources_it_was_given(db):
    """claim_api is the door the defect was in, and `redeem` binds a
    caller-supplied address behind a bearer code. Neither is proof."""
    _key(db, "k_claim", PAYER, tier="paid", source="claim_api")
    _key(db, "k_redeem", PAYER, tier="paid", source="redeem")
    _key(db, "k_none", PAYER, tier="paid")
    _key(db, "k_oauth", PAYER, tier="paid", source="workos_oauth")
    assert _backfill(db, sorted(["stripe_subscription", "workos_oauth"])) == 1
    with db.cursor() as cur:
        cur.execute("SELECT api_key FROM mcp_dev_keys "
                    " WHERE metadata ? 'email_verified_for' ORDER BY 1")
        assert [r[0] for r in cur.fetchall()] == ["k_oauth"]


def test_the_backfill_is_idempotent(db):
    _key(db, "k_sub", PAYER, tier="paid", source="stripe_subscription")
    assert _backfill(db, ["stripe_subscription"]) == 1
    assert _backfill(db, ["stripe_subscription"]) == 0


def test_an_unstamped_legacy_key_misses_a_plan_change_until_it_is_backfilled(db):
    """The reason the backfill is not cosmetic.

    #4428 gated the checkout webhook's tier write, and that write is NOT
    upgrade-only — it is what moves an existing paid key to enterprise when a
    customer changes plan. A legacy key minted before the marker existed
    therefore stops receiving plan changes. Both halves are asserted here, so
    the claim is measured rather than argued: unstamped misses it, stamped
    gets it.
    """
    _key(db, "k_legacy", PAYER, tier="paid", source="stripe_subscription")
    with db.cursor() as cur:
        cur.execute(WEBHOOK_TIER_WRITE, ("enterprise", PAYER, PAYER))
    assert tier_of(db, "k_legacy") == "paid", (
        "control: an unstamped legacy key does not receive the plan change")

    assert _backfill(db, ["stripe_subscription"]) == 1
    with db.cursor() as cur:
        cur.execute(WEBHOOK_TIER_WRITE, ("enterprise", PAYER, PAYER))
    assert tier_of(db, "k_legacy") == "enterprise", (
        "after the backfill records the proof it already had, the plan change "
        "reaches the key again")


# ── 5. the dry run must say what the apply will leave behind ─────────────
def _unproven(conn, exclude_sources):
    with conn.cursor() as cur:
        cur.execute(UNPROVEN, (exclude_sources,))
        return {(r[0] or "(none)"): r[1] for r in cur.fetchall()}


def test_the_dry_run_reports_the_state_the_apply_would_leave(db):
    """The first cut computed this AFTER the write with no source clause, so a
    dry run — which writes nothing — reported the state it STARTS from under a
    name that reads as the state it ends in. Measured live on 52 rows: it said
    52 would remain when the answer was 25.

    The invariant: after == before minus exactly the rows this run stamps, and
    it must hold WITHOUT the write having happened.
    """
    for i, src in enumerate(["stripe_subscription"] * 3 + ["workos_oauth"] * 2
                            + ["redeem"] * 4 + ["claim_api"]):
        _key(db, f"k{i}", PAYER, tier="paid", source=src)
    run_sources = ["stripe_subscription", "workos_oauth"]

    before = _unproven(db, [])
    after = _unproven(db, run_sources)          # no write has happened yet
    assert sum(before.values()) == 10
    assert sum(after.values()) == 5, "after must exclude the 5 rows this run covers"
    assert after == {"redeem": 4, "claim_api": 1}
    assert sum(before.values()) - sum(after.values()) == _count_scope(db, run_sources)

    # ...and the projection was right: applying leaves exactly that set.
    assert _backfill(db, run_sources) == 5
    assert _unproven(db, []) == after, (
        "the dry run's 'after' did not match what the apply actually left")


def _count_scope(conn, sources):
    """How many rows the UPDATE would touch, counted independently of it."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM mcp_dev_keys k"
            " WHERE COALESCE(k.status,'active')='active'"
            "   AND COALESCE(k.tier,'') IN ('paid','enterprise')"
            "   AND COALESCE(k.email,'') <> ''"
            "   AND COALESCE(k.metadata->>'source','') = ANY(%s)"
            "   AND LOWER(COALESCE(k.metadata->>'email_verified_for',''))"
            "       <> LOWER(COALESCE(k.email,''))", (sources,))
        return cur.fetchone()[0]


# ── 6. the CLI's stamp must be the shape the grant predicates accept ─────
def _cli_mint_metadata(email, tier="free", note=None):
    """The metadata the SHIPPED gen_dev_key.py mint writes, obtained by calling
    it — not by restating its keys here. A guard that spells the marker itself
    would pass while the CLI wrote `email_verified_For` and nothing matched."""
    import importlib
    import types as _t
    os.environ.setdefault("NEON_DATABASE_URL",
                          "postgresql://stub:stub@127.0.0.1:1/stub")
    mod = importlib.import_module("gen_dev_key")
    captured = {}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *e): return False
        def execute(self, sql, params=()):
            if "INSERT INTO mcp_dev_keys" in sql:
                captured["params"] = params
        def fetchone(self): return None
        def fetchall(self): return []

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *e): return False
        def cursor(self): return _Cur()

    real = mod._connect
    mod._connect = lambda: _Conn()
    try:
        mod.cmd_mint(_t.SimpleNamespace(email=email, tier=tier, note=note))
    finally:
        mod._connect = real
    return captured["params"]


def test_a_cli_minted_key_satisfies_the_grant_rule_it_was_stamped_for(db):
    """End to end across two artifacts: the CLI writes the marker, and the
    SHIPPED webhook statement is what reads it. Asserting the string in one
    place and the predicate in the other leaves the join between them untested —
    and the join is the only thing that makes the stamp worth writing.

    The webhook write carries a PLAN CHANGE (it is not upgrade-only), which is
    precisely what a hand-minted key could never receive before.
    """
    import json as _json
    api_key, dev_id, email, tier, meta = _cli_mint_metadata(PAYER, tier="paid")[:5]
    assert "email_verified_for" in _json.loads(meta), "the CLI wrote no marker"
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO mcp_dev_keys (api_key, developer_id, email, tier, metadata)"
            " VALUES (%s,%s,%s,%s,%s::jsonb)", (api_key, dev_id, email, tier, meta))
        cur.execute(WEBHOOK_TIER_WRITE, ("enterprise", PAYER, PAYER))
    assert tier_of(db, api_key) == "enterprise", (
        "a key the CLI minted and stamped is still invisible to the grant it "
        "was stamped for — the marker and the predicate disagree")


def test_an_unstamped_cli_mint_is_exactly_what_was_broken(db):
    """The control. Same row without the marker — the state every hand-minted
    key was in, and the reason 8 of the 52 audit rows came from this path."""
    import json as _json
    api_key, dev_id, email, tier, meta = _cli_mint_metadata(PAYER, tier="paid")[:5]
    stripped = {k: v for k, v in _json.loads(meta).items()
                if not k.startswith("email_verified")}
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO mcp_dev_keys (api_key, developer_id, email, tier, metadata)"
            " VALUES (%s,%s,%s,%s,%s::jsonb)",
            (api_key, dev_id, email, tier, _json.dumps(stripped)))
        cur.execute(WEBHOOK_TIER_WRITE, ("enterprise", PAYER, PAYER))
    assert tier_of(db, api_key) == "paid", "control: the plan change must NOT land"
