"""The dev-key tier lookup must name columns that actually exist.

`resolve_tier`'s mcp_dev_keys branch — labelled "the historical hot path" —
filtered on `key_hash` and selected `user_id`. **mcp_dev_keys has NEITHER
column.** Live schema is api_key, developer_id, email, tier, status,
created_at, last_used_at, metadata. So the query raised UndefinedColumn on
every call, a bare `except: pass` swallowed it, and every dch_live_ key fell
through to ANONYMOUS. The branch had never matched a key.

It was invisible because the product still worked: the live MCP gate is the
Node server, which reads api_key directly. Only Flask REST routes were
affected — a paying customer using their connector key there was silently
served free-tier depth.

Confirmed end-to-end before the fix: paid keys with 7,831 and 2,715 successful
/mcp calls both resolved `anonymous`.

This is a COUPLING test — the columns the reader names are checked against the
columns the writers actually write. That is what would have caught it.
"""

import ast
import logging
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

# Columns confirmed against information_schema on the live database,
# 2026-08-28. Anything the reader names outside this set cannot resolve.
LIVE_COLUMNS = {"api_key", "developer_id", "email", "tier", "status",
                "created_at", "last_used_at", "metadata"}


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _mcp_dev_keys_queries(src):
    """Every SQL literal in the file that reads FROM mcp_dev_keys."""
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            if "mcp_dev_keys" in v and re.search(r"\bFROM\s+mcp_dev_keys", v):
                out.append(v)
    return out


def test_the_writers_agree_with_this_files_idea_of_the_schema():
    """Guard the guard: if minting starts writing a column not in LIVE_COLUMNS,
    this constant is stale and every assertion below is measured against
    fiction."""
    written = set()
    for rel in ("main.py", "flask_mcp_endpoints.py"):
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        for m in re.finditer(r"INSERT INTO mcp_dev_keys\s*\(([^)]*)\)", _src(rel)):
            # The column list is read from raw source, so a name split across
            # adjacent string literals arrives as '"\n   "tier'. Keep only the
            # identifier characters rather than treating that as a new column.
            for c in m.group(1).split(","):
                c = re.sub(r"[^A-Za-z0-9_]", "", c)
                if c:
                    written.add(c)
    assert written, "found no INSERT INTO mcp_dev_keys — cannot verify the schema"
    assert written <= LIVE_COLUMNS, (
        f"minting writes columns this test does not know about: "
        f"{sorted(written - LIVE_COLUMNS)}. Update LIVE_COLUMNS from "
        f"information_schema before trusting the rest of this file.")


def test_tier_gate_only_reads_columns_that_exist():
    src = _src("util/tier_gate.py")
    queries = _mcp_dev_keys_queries(src)
    assert queries, "resolve_tier no longer queries mcp_dev_keys at all"
    for q in queries:
        body = q[q.index("SELECT"):] if "SELECT" in q else q
        named = set(re.findall(r"\b([a-z_]{3,})\b", body))
        sql_words = {"select", "from", "where", "and", "or", "coalesce", "limit",
                     "order", "desc", "asc", "not", "null", "active", "true",
                     "false", "mcp_dev_keys", "status", "case", "when", "then",
                     "end", "distinct", "left", "join", "on", "as", "by"}
        suspects = {n for n in named - sql_words if "_" in n or n in
                    {"tier", "email", "key", "plan"}}
        bad = suspects - LIVE_COLUMNS
        assert not bad, (
            f"tier_gate reads column(s) mcp_dev_keys does not have: "
            f"{sorted(bad)}. The query will raise UndefinedColumn, the except "
            f"below will swallow it, and every dev key will resolve ANONYMOUS. "
            f"Live columns: {sorted(LIVE_COLUMNS)}")


def test_key_hash_is_not_used_against_mcp_dev_keys():
    """The exact original defect, pinned by name."""
    for q in _mcp_dev_keys_queries(_src("util/tier_gate.py")):
        assert "key_hash" not in q, (
            "mcp_dev_keys has no key_hash column — this filter matches nothing")


def test_the_lookup_failure_is_logged_not_swallowed():
    """A dead gate lookup downgrades paying customers. It must be audible."""
    src = _src("util/tier_gate.py")
    i = src.index("FROM mcp_dev_keys")
    tail = src[i:i + 2200]
    j = tail.index("except Exception")
    handler = tail[j:j + 500]
    assert "logger" in handler, (
        "the mcp_dev_keys lookup still fails silently — that is how this bug "
        "survived: the query was broken from birth and nothing said so")


def test_the_logger_exists_so_the_handler_cannot_nameerror_into_silence():
    """A logging call inside an except that references an undefined name raises
    NameError, which the same except swallows — the identical silence one layer
    deeper. Caught while writing this fix."""
    import util.tier_gate as tg
    assert isinstance(getattr(tg, "logger", None), logging.Logger), (
        "util.tier_gate has no module-level logger")


def test_free_keys_are_not_promoted_by_the_repair():
    """This fix GRANTS tier that was previously denied, so pin the mapping.
    Verified live: free->ANONYMOUS, identified->IDENTIFIED, paid->PRO,
    enterprise->ENTERPRISE, revoked->ANONYMOUS, forged->ANONYMOUS."""
    from util.tier_gate import _PLAN_TO_TIER, Tier
    assert _PLAN_TO_TIER["free"] is Tier.ANONYMOUS, "a free dev key would gain Pro"
    assert _PLAN_TO_TIER["identified"] is Tier.IDENTIFIED
    assert _PLAN_TO_TIER["paid"] is Tier.PRO


def test_revoked_keys_still_have_to_be_active():
    for q in _mcp_dev_keys_queries(_src("util/tier_gate.py")):
        if "api_key" in q:
            assert "status" in q and "active" in q, (
                "the status filter is gone — a revoked key would resolve paid")
