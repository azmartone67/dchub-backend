"""mcp_conversions must carry the session identity its readers join on.

2026-09-17. paid_signal_attribution_30d's relayed_click lane, added by #4684,
could NEVER fire for any row. routes/handoff_definition resolves the paid row's
session as

    coalesce(nullif(p.session_id,''),
             (select pay2.client_reference_id from mcp_checkout_payments pay2
               where pay2.stripe_session_id = p.stripe_session_id ...))

and mcp_conversions had NEITHER session_id NOR stripe_session_id. Both arms
were NULL, so the predicate was never TRUE. #4696 turned the thrown error into
a silent NULL (to_jsonb(c) ->> 'x'), which stopped the whole metric vanishing
but left the lane permanently dead. Measured live: bridged_via_relayed_click 0
of paid_total 3, attribution_rate_pct 0.0.

WHY NO EXISTING GUARD CAUGHT IT: every test in test_paid_signal_relayed_bridge
asserts on the SHAPE OF THE GENERATED SQL STRING. One of them,
test_the_cte_carries_the_columns_the_lane_reads, asserts `"AS session_id" in
cte` — it pins the ALIAS, not the column's existence. A string assertion cannot
see that a column is absent from the table.

★ AND THE REPO CANNOT BE TRUSTED AS THE SCHEMA. mcp_analytics_postgres.py
contains `CREATE TABLE IF NOT EXISTS mcp_conversions (...)`, but that module is
imported NOWHERE — it is referenced only inside comments, and
init_mcp_analytics_tables has no callers. It is documentation that has drifted,
and reading it as the producer is how "stripe_session_id is missing" stayed
invisible. So DECLARED_COLUMNS below is built from the statements that actually
run (routes/schema_repair.SCHEMA_STATEMENTS), plus the columns proven to exist
by the ON CONFLICT target the live webhooks depend on.
"""
import ast
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


# Columns proven present by live behaviour rather than by a DDL statement in
# this repo: the webhooks upsert on stripe_subscription_id and those writes
# demonstrably land (conversions_30d_real is non-zero), so the table has at
# least these. Kept explicit and small — it is an admission, not a schema.
_PROVEN_BY_LIVE_WRITES = {
    "id", "user_email", "stripe_customer_id", "stripe_subscription_id",
    "plan_from", "plan_to", "mrr_cents", "source", "attribution_signal_id",
    "created_at",
}

# ★ UNVERIFIED — written by flask_mcp_endpoints.py's two web-attribution
# INSERTs but declared by NO DDL anywhere in this repo (no ALTER, no migration,
# no schema_repair entry). Either they were added out of band, or those INSERTs
# throw and are swallowed by their own try/except. Listed so this guard is
# honest about what it cannot prove, NOT because they are known-good. Resolving
# them needs a read of the live schema.
KNOWN_UNDECLARED_DEBT = {"web_source", "web_tool"}


def _schema_repair_declared():
    """Columns added to mcp_conversions by statements that actually execute."""
    src = open(os.path.join(REPO, "routes/schema_repair.py")).read()
    return set(re.findall(
        r"ALTER\s+TABLE\s+mcp_conversions\s+ADD\s+COLUMN\s+"
        r"IF\s+NOT\s+EXISTS\s+([a-z_]+)", src, re.I))


def _declared_columns():
    return _PROVEN_BY_LIVE_WRITES | _schema_repair_declared()


def _conversion_insert_sites():
    """(file, column-list, placeholder-count, param-count) per INSERT."""
    out = []
    for rel in ("main.py", "flask_mcp_endpoints.py"):
        src = open(os.path.join(REPO, rel)).read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                    and "INSERT INTO mcp_conversions" in first.value):
                continue
            sql = first.value
            cols_m = re.search(r"INSERT INTO mcp_conversions\s*\(([^)]*)\)", sql, re.S)
            vals_m = re.search(r"VALUES\s*\(([^)]*)\)", sql, re.S)
            if not (cols_m and vals_m):
                continue
            cols = [c.strip() for c in cols_m.group(1).split(",") if c.strip()]
            placeholders = vals_m.group(1).count("%s")
            # the params tuple is the second positional arg, when present
            nparams = None
            if len(node.args) > 1 and isinstance(node.args[1], ast.Tuple):
                nparams = len(node.args[1].elts)
            out.append((rel, node.lineno, cols, placeholders, nparams))
    return out


def test_there_are_conversion_insert_sites_to_check():
    """A scan that can silently find nothing needs a floor."""
    sites = _conversion_insert_sites()
    assert len(sites) >= 4, (
        f"found only {len(sites)} mcp_conversions INSERT site(s) with a "
        f"parseable column list; the walk has gone blind"
    )


def test_every_insert_placeholder_count_matches_its_params():
    """Adding a column to the list without adding its %s (or its param) makes
    the INSERT throw at runtime — and every one of these sites is wrapped in a
    try/except that would swallow it, so conversions would just stop being
    recorded. This is the arithmetic check."""
    bad = []
    for rel, line, cols, ph, nparams in _conversion_insert_sites():
        if nparams is None:
            continue  # params built elsewhere; not checkable here
        if ph != nparams:
            bad.append(f"{rel}:{line} {ph} placeholders vs {nparams} params")
    assert not bad, "placeholder/param mismatch:\n  " + "\n  ".join(bad)


def test_every_inserted_column_is_declared_or_named_as_debt():
    """The defect class: a writer or reader naming a column the table does not
    have. Fails on anything undeclared that is not explicitly admitted."""
    declared = _declared_columns()
    unknown = {}
    for rel, line, cols, _ph, _np in _conversion_insert_sites():
        for c in cols:
            if c not in declared and c not in KNOWN_UNDECLARED_DEBT:
                unknown.setdefault(c, []).append(f"{rel}:{line}")
    assert not unknown, (
        "column(s) inserted into mcp_conversions but declared by no executing "
        "DDL in this repo: "
        + "; ".join(f"{c} ({', '.join(v)})" for c, v in sorted(unknown.items()))
        + ". Add an ALTER TABLE ... ADD COLUMN IF NOT EXISTS to "
          "routes/schema_repair.SCHEMA_STATEMENTS, or add it to "
          "KNOWN_UNDECLARED_DEBT with the reason."
    )


# Aliases the `paid` CTE produces by RENAMING a base column. They are not
# columns of mcp_conversions and must not be checked as if they were.
_CTE_RENAMES = {"conv_id": "id", "conv_at": "created_at",
                "sig_id": "attribution_signal_id"}


def test_the_relayed_lane_has_at_least_one_resolvable_identity_arm():
    """★ THE GUARD THAT WOULD HAVE CAUGHT #4684.

    The resolver is a coalesce over identity arms. Because the CTE reads them
    TOLERANTLY (to_jsonb(c) ->> 'x'), a missing column does not throw — it
    yields NULL, so an arm naming a non-existent column is silently dead and
    the whole coalesce can resolve NULL for every row forever. That is the
    defect, and "every column must exist" is the wrong assertion for it: the
    honest invariant is that AT LEAST ONE arm resolves to a real column.

    Before the 2026-09-17 fix both arms named absent columns (session_id,
    stripe_session_id) and this failed. It passes once one of them is declared.
    """
    from routes.handoff_definition import paid_signal_relayed_session_sql
    sql = paid_signal_relayed_session_sql()
    read = {_CTE_RENAMES.get(c, c)
            for c in re.findall(r"\bp\.([a-z_]+)", sql)}
    read -= set(_CTE_RENAMES.values())
    assert read, "resolver names no p.<column> at all — the walk is blind"

    declared = _declared_columns()
    live = sorted(c for c in read if c in declared)
    dead = sorted(c for c in read if c not in declared)
    assert live, (
        f"every identity arm of paid_signal_relayed_session_sql names a column "
        f"no executing DDL declares: {dead}. Read tolerantly via to_jsonb, each "
        f"yields NULL, so the coalesce resolves NULL for EVERY row and the lane "
        f"can never fire — measured live as bridged_via_relayed_click 0 of "
        f"paid_total 3. At least one arm must name a declared column."
    )
    # Not an assertion: a dead arm is legal (forward-looking, and harmless
    # under to_jsonb) as long as a live one exists. Surfaced so it is a known
    # fact rather than a discovery.
    if dead:
        print(f"\nNOTE: dead identity arm(s), permanently NULL: {dead}; "
              f"resolving via {live}")


def test_stripe_session_id_is_declared_and_backfilled_and_indexed():
    """The fix is the same ALTER -> backfill -> INDEX trio this file already
    uses for caller_id. A column with no backfill leaves every historical pack
    row unbridgeable; one with no index makes the lane's join a seq scan."""
    src = open(os.path.join(REPO, "routes/schema_repair.py")).read()
    assert "stripe_session_id" in _schema_repair_declared(), (
        "stripe_session_id is not declared by an executing ALTER TABLE"
    )
    assert re.search(
        r"UPDATE mcp_conversions SET stripe_session_id\s*=\s*"
        r"stripe_subscription_id", src), "no backfill from the cs_ rows"
    assert re.search(
        r"left\(COALESCE\(stripe_subscription_id[^)]*\),\s*3\)\s*=\s*'cs_'",
        src), (
        "the backfill must select cs_ rows with left(...) = 'cs_'; LIKE "
        "'cs_%' is wrong because '_' is a LIKE wildcard"
    )
    assert "idx_mcp_conv_stripe_session" in src, "no index on the new column"


def test_the_backfill_does_not_claim_subscription_rows():
    """stripe_subscription_id holds a real sub_... on the subscription paths.
    A backfill that took those would attribute a subscription id as a checkout
    session and join it against mcp_checkout_payments.stripe_session_id, which
    can never match — a fabricated identity, worse than a NULL."""
    src = open(os.path.join(REPO, "routes/schema_repair.py")).read()
    m = re.search(r"UPDATE mcp_conversions SET stripe_session_id.*?\"\"\"",
                  src, re.S)
    assert m, "backfill statement not found"
    stmt = m.group(0)
    assert "'cs_'" in stmt, "backfill is not restricted to checkout sessions"
    assert "sub_" not in stmt, "backfill must never take sub_ ids"
