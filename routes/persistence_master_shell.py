"""
routes/persistence_master_shell.py — Persistence Master Shell (#41, 2026-07-29).

★ THE SUBJECT OF THIS SHELL IS THE ONLY DURABLE STATE AN AGENT CAN CREATE.

Everything else DC Hub exposes is a read. The shortlist chain — save_to_shortlist,
get_shortlist, set_shortlist_alert, suggest_reallocation, export_dataset — is the
one place an agent writes something that outlives its conversation, and therefore
the only mechanism that can produce a RETURN VISIT rather than a one-off answer.
Day-2 return sits at 5%. That is not a coincidence; it is the same fact measured
from the other end.

In 90 days the whole chain took 44 calls from real external agents and stored ZERO
of their rows. agent_shortlist_sites holds 4 rows and every one is ours
('test-campaign', 'realloc-demo-0707') plus a probe row from building this shell.

WHAT THIS SHELL EXISTS TO CORRECT — five hypotheses died getting here, and the
lanes below are what survived MEASUREMENT rather than what sounded right:

  ✗ "LLMs mute the relay link"        → the link was never emitted (auto-trial
                                        swallowed the paywall). Fixed 07-28.
  ✗ "intent presence can classify"    → intent is on 78 of 57,381 calls (0.136%).
  ✗ "dead tools are badly described"  → meta-frame openers are MORE common among
                                        CALLED tools (39%) than dead ones (29%).
  ✗ "the anon daily cap blocks saves" → a keyless save returns ok:true, id:100007.
  ✗ "the write path is broken"        → it is not; it writes, and reads back.

The survivor is narrower and worse: the write SUCCEEDS, and succeeds into a
shared bucket.

LANES
  1. OWNERSHIP IS A SHARED BUCKET (fires an actuator). _owner() returns the
     literal string 'public' for every keyless caller, so all anonymous shortlists
     occupy ONE global namespace addressed by name — any anonymous agent can read,
     overwrite or collide with any other's list by guessing 'my-sites'. The tool's
     own response text says "Shortlist is scoped to your API key", which is FALSE
     for exactly the callers who most need to be told otherwise. It has not caused
     an incident only because real anonymous saves number one, and that one is
     mine. Making saving EASIER (lane 2) before fixing this would manufacture the
     incident, so this lane is ordered first and gates the others.

     Note what this lane does NOT do: scope anonymous writes to X-MCP-Session.
     The gateway does forward it, so it would compile and the isolation test would
     pass — but session ids rotate per connection (of 7,933 sessions in 30d exactly
     one recurred), so a session-scoped shortlist cannot survive to the next
     conversation. That converts a cross-tenant bug into a silently-useless
     feature, which is harder to detect and no better for the user. Persistence
     requires durable identity. The honest response to a keyless save is the one
     claim_free_key already exists to serve: one call, no email, durable key.

  2. THE ENTRY BARRIER IS THE SCHEMA. save_to_shortlist requires `site` AND
     `objectives`, both UNTYPED (no JSON-Schema `type`), while shortlist_name —
     the only field a human would name — is optional. `site` is documented as
     "metric fields from analyze_site" and `objectives` as the signed-weight map
     "this site was ranked under", so the tool is callable only by an agent that
     already ran analyze_site, retained its full metric object, and constructed a
     weights map. A minimal, obvious call — site_ref + lat + lng + capacity_mw —
     is REJECTED. This is not a hypothesis about model behaviour: our OWN internal
     probes throw invalid_args on the save path 14 times in 90 days. When the
     people who wrote the tool cannot call it from memory, no agent will.

  3. REACHABILITY, RE-MEASURED. Lanes 1 and 2 are changes; this lane is the
     control that says whether they mattered. It pins the pre-change baseline as
     numbers rather than prose (44 external calls, 23 anon_daily_cap, 0 stored
     external rows, 5 dependent tools at zero) so that "we fixed it" has to survive
     contact with a later run. A shell that cannot report FAILED after its own fix
     ships is decoration.

Ordering is a dependency, not a preference: 1 gates 2 (never make an unsafe write
easier), and 3 is meaningless before both.

Read-only except lane 1's actuator, which is a REFUSAL — it removes a write that
should never have been possible, and adds none.

Run:  GET /api/v1/admin/persistence-shell        (admin-gated, read-only)
"""
from __future__ import annotations

import os
import hashlib

from flask import Blueprint, jsonify, request

persistence_master_shell_bp = Blueprint("persistence_master_shell", __name__)

SHELL_ID = 41
SHELL_NAME = "Persistence Master Shell"

# The chain, in dependency order. save_to_shortlist is the ROOT — every tool after
# it reads state the root creates, so a root that never stores makes four
# downstream tools structurally uncallable regardless of their own quality.
CHAIN_ROOT = "save_to_shortlist"
CHAIN_DEPENDENTS = ("get_shortlist", "set_shortlist_alert",
                    "suggest_reallocation", "export_dataset")
CHAIN = (CHAIN_ROOT,) + CHAIN_DEPENDENTS

# The owner value that means "nobody in particular". Imported by the fix and by
# the tests so all three read ONE definition — the drift class this repo keeps
# hitting is a guard that restates a contract instead of reading it.
SHARED_OWNER = "public"


def _admin_ok() -> bool:
    want = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(want) and got == want


def _disabled() -> bool:
    return (os.environ.get("PERSISTENCE_SHELL_DISABLE") or "0") == "1"


def _conn():
    import psycopg2
    dsn = (os.environ.get("NEON_REPLICA_URL")
           or os.environ.get("DATABASE_URL")
           or os.environ.get("NEON_DATABASE_URL"))
    if not dsn:
        return None
    c = psycopg2.connect(dsn)
    # Read-only: this shell reports, it does not mutate. The actuator lives in
    # routes/shortlists.py where the write it removes actually happens.
    c.set_session(readonly=True, autocommit=True)
    return c


def _scalar(c, sql: str, args=None):
    try:
        cur = c.cursor()
        cur.execute(sql, args)
        r = cur.fetchone()
        return r[0] if r else None
    except Exception:
        return None


def _rows(c, sql: str, args=None) -> list:
    try:
        cur = c.cursor()
        cur.execute(sql, args)
        return list(cur.fetchall())
    except Exception:
        return []


def _check(cid: str, name: str, passed, detail: str, critical: bool = False) -> dict:
    return {"id": cid, "name": name,
            "status": "PASS" if passed is True else ("FAIL" if passed is False else "INDETERMINATE"),
            "detail": detail, "critical": bool(critical)}


def _lane_verdict(checks: list) -> str:
    """INDETERMINATE is never silently a PASS. A lane that could not read its
    evidence must say so — the recurring failure in this repo is a green check
    that ran against nothing."""
    if not checks:
        return "INDETERMINATE"
    if any(c["status"] == "INDETERMINATE" for c in checks):
        return "INDETERMINATE"
    if any(c["status"] == "FAIL" and c["critical"] for c in checks):
        return "FAILED"
    if any(c["status"] == "FAIL" for c in checks):
        return "DEGRADED"
    return "PASSED"


# ── Lane 1 — ownership is a shared bucket ────────────────────────────
def _lane_ownership(c) -> list:
    checks = []
    shared = _scalar(c, "SELECT COUNT(*) FROM agent_shortlist_sites WHERE owner=%s",
                     (SHARED_OWNER,))
    if shared is None:
        checks.append(_check("L1.1", "shared-bucket rows readable", None,
                             "agent_shortlist_sites unreadable — cannot judge isolation",
                             critical=True))
        return checks

    checks.append(_check(
        "L1.1", "no rows in the shared 'public' bucket", shared == 0,
        f"{shared} row(s) owned by '{SHARED_OWNER}'. Every keyless caller shares this "
        f"namespace, so shortlists are addressable across tenants by name alone.",
        critical=True))

    # Distinct anonymous shortlist NAMES in the shared bucket. >1 means two
    # different callers' data already coexists in one namespace; a collision is
    # then a matter of name choice, not of luck.
    names = _scalar(c, "SELECT COUNT(DISTINCT shortlist_name) FROM agent_shortlist_sites "
                       "WHERE owner=%s", (SHARED_OWNER,))
    checks.append(_check(
        "L1.2", "no cross-tenant collision surface", (names or 0) <= 1,
        f"{names or 0} distinct shortlist name(s) in the shared bucket. Collision risk "
        f"scales with this number; at 0-1 the bug is latent, not yet realised."))

    # The claim the tool makes to its caller must be true. This is asserted
    # against the shipped source, not restated here.
    try:
        import routes.shortlists as _sl
        src = open(_sl.__file__, encoding="utf-8").read()
        still_shared = f'return "{SHARED_OWNER}"' in src
        checks.append(_check(
            "L1.3", "_owner() no longer returns a shared constant", not still_shared,
            "routes.shortlists._owner() still returns the shared constant for keyless "
            "callers — the response text 'scoped to your API key' remains false."
            if still_shared else
            "_owner() no longer hands keyless callers a shared owner.",
            critical=True))
    except Exception as e:
        checks.append(_check("L1.3", "_owner() source readable", None,
                             f"could not read routes/shortlists.py: {str(e)[:80]}",
                             critical=True))
    return checks


# ── Lane 2 — the entry barrier is the schema ─────────────────────────
def _lane_entry_barrier(c) -> list:
    checks = []
    bad = _scalar(c, """
        SELECT COUNT(*) FROM mcp_call_log
         WHERE timestamp > now() - interval '90 days'
           AND tool = ANY(%s) AND status = 'invalid_args'""", (list(CHAIN) + ["save_site"],))
    total = _scalar(c, """
        SELECT COUNT(*) FROM mcp_call_log
         WHERE timestamp > now() - interval '90 days'
           AND tool = ANY(%s)""", (list(CHAIN) + ["save_site"],))
    if bad is None or not total:
        checks.append(_check("L2.1", "save-path arg failures measurable", None,
                             "mcp_call_log unreadable for the chain", critical=True))
        return checks

    pct = round(100.0 * bad / total, 1)
    # OUR OWN probes are in this count deliberately. An arg contract that the
    # authors get wrong from memory is not one an agent will satisfy by guessing.
    checks.append(_check(
        "L2.1", "save path is callable without prior tool output", bad == 0,
        f"{bad} of {total} chain calls in 90d failed arg validation ({pct}%), "
        f"including our own internal probes. `site` and `objectives` are required "
        f"and untyped; a minimal site_ref+lat+lng+capacity_mw call is rejected.",
        critical=bad > 0))

    root_ok = _scalar(c, """
        SELECT COUNT(*) FROM mcp_call_log
         WHERE timestamp > now() - interval '90 days' AND tool=%s AND status='ok'
           AND platform NOT ILIKE 'dchub%%'
           AND COALESCE(user_agent,'') NOT ILIKE 'dchub-%%'
           AND platform NOT ILIKE '%%probe%%' AND platform NOT ILIKE '%%validator%%'""",
        (CHAIN_ROOT,))
    checks.append(_check(
        "L2.2", "a real external agent has completed a save", (root_ok or 0) > 0,
        f"{root_ok or 0} successful {CHAIN_ROOT} calls from real external agents in 90d.",
        critical=True))
    return checks


# ── Lane 3 — reachability, re-measured ───────────────────────────────
def _lane_reachability(c) -> list:
    checks = []
    ext = _rows(c, """
        SELECT tool,
               COUNT(*),
               COUNT(*) FILTER (WHERE status='anon_daily_cap'),
               COUNT(*) FILTER (WHERE status='ok')
          FROM mcp_call_log
         WHERE timestamp > now() - interval '90 days' AND tool = ANY(%s)
           AND platform NOT ILIKE 'dchub%%'
           AND COALESCE(user_agent,'') NOT ILIKE 'dchub-%%'
           AND platform NOT ILIKE '%%probe%%' AND platform NOT ILIKE '%%validator%%'
           AND platform NOT ILIKE '%%certifier%%'
         GROUP BY 1""", (list(CHAIN),))
    if not ext:
        checks.append(_check("L3.1", "chain reachability measurable", None,
                             "no external chain rows readable — cannot baseline",
                             critical=True))
        return checks

    calls = sum(r[1] for r in ext)
    capped = sum(r[2] for r in ext)
    cap_pct = round(100.0 * capped / calls, 1) if calls else 0.0
    checks.append(_check(
        "L3.1", "chain is not disproportionately rate-capped", cap_pct < 10.0,
        f"{capped}/{calls} external chain calls hit anon_daily_cap ({cap_pct}%) vs a "
        f"~1.3% baseline across all other tools. NOTE: the cap is NOT why saves fail "
        f"(a keyless save returns ok) — it is a symptom of these tools sitting late "
        f"in a session, and is tracked here so a later fix cannot claim it."))

    live = {r[0] for r in ext if r[3] > 0}
    dead = [t for t in CHAIN_DEPENDENTS if t not in live]
    checks.append(_check(
        "L3.2", "dependent tools are reachable", not dead,
        f"{len(dead)} of {len(CHAIN_DEPENDENTS)} dependent tools have never succeeded "
        f"for a real external agent: {', '.join(dead) or 'none'}. These cannot be fixed "
        f"directly — they read state {CHAIN_ROOT} must create first.",
        critical=bool(dead)))

    stored = _scalar(c, "SELECT COUNT(*) FROM agent_shortlist_sites WHERE owner NOT LIKE 'k\\_%%'")
    checks.append(_check(
        "L3.3", "stored rows trace to identified owners", (stored or 0) == 0,
        f"{stored or 0} stored row(s) have no key-derived owner. Rows without a durable "
        f"owner cannot be retrieved in a later conversation, which is the entire "
        f"purpose of the subsystem."))
    return checks


LANES = (
    ("ownership", "Ownership is a shared bucket", _lane_ownership),
    ("entry_barrier", "The entry barrier is the schema", _lane_entry_barrier),
    ("reachability", "Reachability, re-measured", _lane_reachability),
)


def run_persistence_shell() -> dict:
    if _disabled():
        return {"shell": SHELL_NAME, "id": SHELL_ID, "status": "DISABLED"}
    c = _conn()
    if c is None:
        return {"shell": SHELL_NAME, "id": SHELL_ID, "status": "INDETERMINATE",
                "error": "no database URL configured — nothing was measured"}
    out, verdicts = [], []
    try:
        for key, title, fn in LANES:
            try:
                checks = fn(c)
            except Exception as e:
                checks = [_check(f"{key}.err", "lane executed", None,
                                 f"lane raised: {str(e)[:150]}", critical=True)]
            v = _lane_verdict(checks)
            verdicts.append(v)
            out.append({"lane": key, "title": title, "verdict": v, "checks": checks})
    finally:
        try:
            c.close()
        except Exception:
            pass
    overall = ("INDETERMINATE" if "INDETERMINATE" in verdicts
               else "FAILED" if "FAILED" in verdicts
               else "DEGRADED" if "DEGRADED" in verdicts else "PASSED")
    return {"shell": SHELL_NAME, "id": SHELL_ID, "overall": overall, "lanes": out}


@persistence_master_shell_bp.route("/api/v1/admin/persistence-shell", methods=["GET"])
def persistence_shell_endpoint():
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(run_persistence_shell())


def register_persistence_master_shell(app) -> None:
    try:
        app.register_blueprint(persistence_master_shell_bp)
    except Exception:
        pass
