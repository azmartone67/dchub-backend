"""
mcp_mint_cliff.py — WHY do 41.3% of minted keys never make a single call?

2026-08-12. Measured over 30d: 748 keys minted by `claim_free_key`, **309
(41.3%) never made one call**. That is the largest absolute loss anywhere in
the funnel — larger than the paywall, larger than checkout — and nothing
anywhere measured *why*. The admin waterfall has a `key_issued -> first_call`
step, but it is a COUNT: it can tell you 309 vanished and nothing else.

This module does NOT guess the cause. It builds the measurement that lets the
next look answer, per key, five questions that imply completely different
fixes:

    how far did they get   ->  cohort (below)
    what did they last see ->  last_status / last_tool in the minting session
    did they error         ->  last_status in the error classes
    did they never retry   ->  silent_no_return
    did the key ever reach
    the client at all      ->  presented_never_logged vs session_continued_keyless

★ WHY THE COHORTS ARE ORDERED THIS WAY. They are mutually exclusive and
evaluated top-down, most-specific-cause first, so every never-called key lands
in exactly one bucket and the buckets sum. The order encodes what would
INVALIDATE a simpler story:

  1 born_gated              The key was minted ALREADY at/over the unbound
                            call cap (metadata.gate_carried_from_identity —
                            the re-mint escape closed 2026-07-10 deliberately
                            carries the meter onto the new key). Its first
                            full call is refused by design. This is a
                            MECHANICAL non-start we cause, not agent
                            behaviour, and it must be counted before anything
                            else or it hides inside "agents just leave".

  2 presented_never_logged  last_used_at or validate_calls > 0, but zero
                            mcp_call_log rows. The key DID reach the client
                            and DID come back to us at the validate gate —
                            so this is not a delivery failure. Something
                            between auth and the track callback ate it.

  3 superseded_by_remint    Another claim_api key from the SAME metadata.ip
                            made calls. ★THE ARTIFACT DETECTOR. The funnel
                            has measured ~15-19 re-mints per distinct agent
                            (0727, 0807). If one agent mints 19 keys and uses
                            the last, 18 keys "never call" — and that is ONE
                            working agent, not 18 lost ones. Until this bucket
                            is sized, 41.3% cannot be read as a loss rate at
                            all. It may be mostly this.

  4 session_continued_keyless  The minting session kept making tool calls
                            AFTER the mint, but never under this key. The key
                            never reached the client, or the client ignored
                            it — while the agent carried on working. A
                            delivery/plumbing bug, and the one cohort where
                            the agent demonstrably still wanted the data.

  5 silent_no_return        Session known, zero further activity anywhere.
                            The agent stopped at the mint. Only THIS bucket
                            supports "the agent left".

  6 unattributable_no_session  No session_id stamped and no other signal —
                            we genuinely cannot tell. Reported as its own
                            bucket, never folded into 5, because inflating
                            "the agent left" with rows we simply cannot see
                            is exactly the flattering-story failure mode.

★ THREE-VALUED, and no flattering zeros. Every count is PASS/FAIL/UNMEASURED.
If a column this reads does not exist on the live schema (repo DDL and live
DDL have diverged before), the affected dimension returns None and names
itself in `unmeasured`, rather than returning 0 — a 0 here would read as
"nobody hit that case", which is the opposite of "we could not look".

★ It publishes a `definitions` block. Every count is divisible only by the
population its definition names; `called` is defined as "has >= 1 mcp_call_log
row", which is the same grain the waterfall's first_call step uses.

Route:
  GET /api/v1/admin/mcp/mint-cliff?days=30   — admin/internal keyed
"""
import os
import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

mcp_mint_cliff_bp = Blueprint("mcp_mint_cliff", __name__)

# Per-probe ceiling. Matches funnel_health._PROBE_TIMEOUT_MS — this builder runs
# inside that page, so a different ceiling here would just be a second number
# to keep in sync.
_PROBE_TIMEOUT_MS = 8000

# The unbound free-call cap. A key minted carrying >= this many calls is born
# past the gate. Read from the same env the gatekeeper uses so the two can
# never disagree silently.
def _bind_gate_calls() -> int:
    try:
        from routes.auto_trial import TRIAL_FREE_CALLS_UNBOUND as _g
        return int(_g)
    except Exception:
        try:
            return int(os.environ.get("TRIAL_FREE_CALLS_UNBOUND", "10") or 10)
        except Exception:
            return 10


def _internal_ok(req) -> bool:
    """X-Internal-Key OR X-Admin-Key OR ?admin_key= — mirrors
    mcp_high_intent_claim._internal_ok exactly (one auth shape for the whole
    admin funnel family)."""
    sent = (req.headers.get("X-Internal-Key")
            or req.headers.get("X-Admin-Key")
            or req.args.get("admin_key") or "").strip()
    if not sent:
        return False
    expected_admin = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    expected_internal = (os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    return sent in (expected_admin, expected_internal) and bool(sent)


def _conn():
    try:
        import psycopg2
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if not dsn:
            return None
        c = psycopg2.connect(dsn, sslmode="require", connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[mint_cliff] DB connect failed: %s", e)
        return None


def _has_columns(cur, table: str, cols) -> bool:
    """True only if EVERY named column exists on the live table.

    ★ Live DDL has diverged from repo DDL before (power_plants), so a query is
    never issued against a column we have not confirmed. A miss makes the
    dependent dimension UNMEASURED, not 0."""
    try:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s", (table,))
        live = {r[0] for r in (cur.fetchall() or [])}
        return all(c in live for c in cols)
    except Exception as e:
        logger.debug("[mint_cliff] column probe failed on %s: %s", table, e)
        return False


# ── the classifier ───────────────────────────────────────────────────────────
# One pass, one CASE, evaluated top-down. Mutually exclusive by construction:
# CASE returns on first match, so the buckets sum to never_called exactly.
# That summation is asserted at read time (see `sums_ok` below) — if it ever
# fails, the endpoint says so rather than publishing a breakdown that silently
# drops rows.
_COHORT_CASE = """
CASE
  WHEN k.metadata->>'gate_carried_from_identity' = 'true'
       OR COALESCE(NULLIF(k.metadata->>'validate_calls','')::int, 0) >= %(gate)s
    THEN 'born_gated'
  WHEN k.last_used_at IS NOT NULL
       OR COALESCE(NULLIF(k.metadata->>'validate_calls','')::int, 0) > 0
    THEN 'presented_never_logged'
  WHEN COALESCE(k.metadata->>'ip','') <> '' AND EXISTS (
         SELECT 1 FROM mcp_dev_keys k2
          WHERE k2.api_key <> k.api_key
            AND k2.metadata->>'source' = 'claim_api'
            AND k2.metadata->>'ip' = k.metadata->>'ip'
            AND EXISTS (SELECT 1 FROM mcp_call_log l2
                         WHERE l2.api_key = k2.api_key))
    THEN 'superseded_by_remint'
  WHEN COALESCE(k.metadata->>'session_id','') <> '' AND EXISTS (
         SELECT 1 FROM mcp_call_log l3
          WHERE l3.session_id = k.metadata->>'session_id'
            AND COALESCE(l3.api_key,'') <> k.api_key
            AND l3.timestamp >= k.created_at)
    THEN 'session_continued_keyless'
  WHEN COALESCE(k.metadata->>'session_id','') <> ''
    THEN 'silent_no_return'
  ELSE 'unattributable_no_session'
END
"""

_COHORT_META = {
    "born_gated": (
        "Minted already at/over the unbound call cap — first full call refused by design",
        "MECHANICAL: we caused this. Not agent behaviour.",
        "Should the re-mint meter carry onto a key we then advertise as usable?"),
    "presented_never_logged": (
        "Key was presented to us (validated) but produced zero tool-call rows",
        "The key REACHED the client. Loss is between auth and the track callback.",
        "Is the call failing after auth, or is the track callback dropping rows?"),
    "superseded_by_remint": (
        "Another claim_api key from the same metadata.ip did make calls",
        "ARTIFACT, not a lost agent — one agent re-minting. Deduct before reading 41.3% as loss.",
        "What is the never-called rate per distinct AGENT rather than per key?"),
    "session_continued_keyless": (
        "The minting session kept calling AFTER the mint, but never under this key",
        "DELIVERY failure: key never reached the client, yet the agent kept working.",
        "Why did the key not attach — auto-apply, or the client dropping the header?"),
    "silent_no_return": (
        "Session known; zero further activity anywhere after the mint",
        "The only cohort that supports 'the agent left'.",
        "What was the last thing they saw before stopping?"),
    "unattributable_no_session": (
        "No session_id stamped and no other signal",
        "UNKNOWN — we cannot see these. Never fold into 'the agent left'.",
        "Why is session_id missing at mint for these callers?"),
}



def build_mint_cliff(cur, days: int = 30) -> dict:
    """Compute the mint->first-call cliff breakdown against an OPEN cursor.

    ★ ONE BUILDER, TWO SURFACES. The admin funnel-health card and the
    /mint-cliff endpoint both call this, so they agree by construction. The
    step-drop waterfall next door had the SAME correctness fix applied TWICE
    (r-claim-honest 06-27) precisely because each surface carried its own copy
    — not repeating that here.

    The caller owns the cursor and any rollback. Returns the dict both
    surfaces publish verbatim.
    """
    gate = _bind_gate_calls()
    unmeasured: list = []
    out = {
        "ok": True,
        "window_days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Confirm every column this reads actually exists on the LIVE schema.
    need_keys = ("api_key", "created_at", "last_used_at", "metadata", "email")
    need_log = ("api_key", "session_id", "timestamp", "status", "tool")
    keys_ok = _has_columns(cur, "mcp_dev_keys", need_keys)
    log_ok = _has_columns(cur, "mcp_call_log", need_log)
    if not keys_ok:
        unmeasured.append("mcp_dev_keys: required columns absent on live schema")
    if not log_ok:
        unmeasured.append("mcp_call_log: required columns absent on live schema")
    if not (keys_ok and log_ok):
        out.update(ok=False, unmeasured=unmeasured, population=None, cohorts=None,
                   note=("UNMEASURED, not zero — the live schema does not expose "
                         "the columns this diagnosis reads."))
        return out

    # ★ BOUND THE WORK. The re-mint arm correlates mcp_dev_keys against itself
    # on metadata->>'ip' (a JSON extraction, so no index to lean on), and this
    # builder also runs inside the admin funnel-health page — which sits behind
    # a 15s edge ROUTE_TIMEOUT that turns a slow query into a 503 for the WHOLE
    # board. A statement_timeout makes the worst case "this block reports
    # UNMEASURED" instead of "the admin funnel goes down".
    #
    # ★ THE FORM MATTERS, and both obvious choices are wrong here:
    #
    #   `SET LOCAL` alone   — DISCARDED. Both callers run autocommit, so each
    #                         statement is its own implicit transaction and the
    #                         setting dies on return. Looks like a guard, bounds
    #                         nothing.
    #   session-level `SET` — does not stick on Neon's POOLED endpoint. Under
    #                         pgbouncer transaction mode the SET lands on a
    #                         DIFFERENT backend connection than the queries
    #                         (verified live 2026-07-01, see funnel_health._bounded).
    #
    # The form that actually holds on this infrastructure is an EXPLICIT
    # transaction per probe: BEGIN / SET LOCAL / query / COMMIT. That is exactly
    # what funnel_health._bounded does and why — reusing the proven shape rather
    # than inventing a third one. See _bounded() below.
    _run_cliff_queries(cur, out, days, gate, unmeasured)
    return out


def _bounded(cur, sql: str, args, fetch: str):
    """Run ONE probe inside its own explicit transaction with SET LOCAL
    statement_timeout — the only form that sticks on Neon's pooled endpoint.

    Mirrors routes/funnel_health._bounded (same reason, same infrastructure).
    ROLLBACK on any error so a timed-out probe never poisons the next one with
    "current transaction is aborted"."""
    cur.execute("BEGIN")
    try:
        cur.execute("SET LOCAL statement_timeout = %d" % _PROBE_TIMEOUT_MS)
        cur.execute(sql, args)
        result = cur.fetchone() if fetch == "one" else cur.fetchall()
        cur.execute("COMMIT")
        return result
    except Exception:
        try:
            cur.execute("ROLLBACK")
        except Exception:
            pass
        raise


def _run_cliff_queries(cur, out: dict, days: int, gate: int, unmeasured: list) -> None:
    """The measurement itself. Every probe goes through _bounded()."""
    win = ("k.metadata->>'source' = 'claim_api' "
           "AND k.created_at >= NOW() - make_interval(days => %(d)s)")
    args = {"d": days, "gate": gate}

    # ── population ────────────────────────────────────────────────────────
    row = _bounded(cur,
        f"""SELECT COUNT(*)::int,
                   COUNT(*) FILTER (
                     WHERE EXISTS (SELECT 1 FROM mcp_call_log l
                                    WHERE l.api_key = k.api_key))::int
              FROM mcp_dev_keys k
             WHERE {win}""", args, "one") or (0, 0)
    minted, called = int(row[0] or 0), int(row[1] or 0)
    never = minted - called
    out["population"] = {
        "minted": minted, "called": called, "never_called": never,
        "never_called_pct": (round(100.0 * never / minted, 2) if minted else None),
    }

    # ── cohorts ───────────────────────────────────────────────────────────
    _rows = _bounded(cur,
        f"""SELECT {_COHORT_CASE} AS cohort, COUNT(*)::int
              FROM mcp_dev_keys k
             WHERE {win}
               AND NOT EXISTS (SELECT 1 FROM mcp_call_log l
                                WHERE l.api_key = k.api_key)
             GROUP BY 1 ORDER BY 2 DESC""", args, "all")
    cohorts, total = [], 0
    for code, n in (_rows or []):
        n = int(n or 0)
        total += n
        label, means, nxt = _COHORT_META.get(code, (code, "unclassified", ""))
        cohorts.append({
            "code": code, "label": label, "n": n,
            "pct_of_never_called": (round(100.0 * n / never, 2) if never else None),
            "means": means, "next_question": nxt,
        })
    out["cohorts"] = cohorts
    # The buckets MUST sum to never_called. If they ever do not, say so loudly
    # rather than publishing a breakdown that silently drops rows.
    out["sums_ok"] = (total == never)
    if total != never:
        out["sums_note"] = (f"BREAKDOWN INCOMPLETE: cohorts sum to {total} but "
                            f"never_called is {never}. Do not read the percentages.")

    # ── what did they LAST see ────────────────────────────────────────────
    # Final logged call in the SAME session that minted the key, for
    # never-called keys whose session we can see. Answers "what did they last
    # see" and "did they error" without guessing.
    _last = _bounded(cur,
        f"""WITH nc AS (
              SELECT k.api_key, k.metadata->>'session_id' AS sid
                FROM mcp_dev_keys k
               WHERE {win}
                 AND COALESCE(k.metadata->>'session_id','') <> ''
                 AND NOT EXISTS (SELECT 1 FROM mcp_call_log l
                                  WHERE l.api_key = k.api_key)),
            last AS (
              SELECT DISTINCT ON (nc.api_key)
                     nc.api_key, l.status, l.tool
                FROM nc JOIN mcp_call_log l ON l.session_id = nc.sid
               ORDER BY nc.api_key, l.timestamp DESC)
            SELECT COALESCE(status,'(null)'), COALESCE(tool,'(null)'), COUNT(*)::int
              FROM last GROUP BY 1,2 ORDER BY 3 DESC LIMIT 15""", args, "all")
    out["last_seen_in_minting_session"] = [
        {"status": s, "tool": t, "n": int(n or 0)}
        for s, t, n in (_last or [])
    ]
    out["last_seen_note"] = (
        "The final logged call in the SAME session that minted the key, for "
        "never-called keys with a visible session. These are calls made "
        "WITHOUT the new key. An empty list means no never-called key had "
        "visible session activity — a finding (nothing to see), not an error.")

    # ── how long did we actually watch them ───────────────────────────────
    # A key minted 20 minutes ago has not had a fair chance to call. Without
    # this, a fresh cohort inflates the cliff and the number flatters nobody.
    r2 = _bounded(cur,
        f"""SELECT COUNT(*) FILTER (
                     WHERE k.created_at >= NOW() - INTERVAL '1 hour')::int,
                   COUNT(*) FILTER (
                     WHERE k.created_at >= NOW() - INTERVAL '24 hours')::int
              FROM mcp_dev_keys k
             WHERE {win}
               AND NOT EXISTS (SELECT 1 FROM mcp_call_log l
                                WHERE l.api_key = k.api_key)""", args, "one") or (0, 0)
    out["immaturity"] = {
        "never_called_minted_last_1h": int(r2[0] or 0),
        "never_called_minted_last_24h": int(r2[1] or 0),
        "note": ("Keys too young to have had a fair chance to call. Subtract "
                 "before reading the cliff as settled — a fresh mint inflates it."),
    }

    out["definitions"] = {
        "population": ("mcp_dev_keys where metadata.source='claim_api' and "
                       f"created_at within {days}d — keys minted by claim_free_key."),
        "called": ("has >= 1 mcp_call_log row for that api_key — the same grain "
                   "the admin waterfall's first_call step uses."),
        "cohorts": ("mutually exclusive, evaluated most-specific-cause first; "
                    "they sum to never_called (see sums_ok)."),
        "bind_gate_calls": gate,
        "read_this_first": (
            "superseded_by_remint is an ARTIFACT bucket. The funnel has measured "
            "~15-19 re-mints per distinct agent, so a large share of 'never "
            "called' keys may be duplicate keys belonging to ONE working agent. "
            "Deduct it before quoting the never-called rate as a loss."),
    }
    out["unmeasured"] = unmeasured


@mcp_mint_cliff_bp.route("/api/v1/admin/mcp/mint-cliff", methods=["GET"])
def mint_cliff():
    """Diagnose the mint->first-call cliff. Admin/internal keyed."""
    if not _internal_ok(request):
        return jsonify(ok=False, error="forbidden"), 403
    try:
        days = max(1, min(365, int(request.args.get("days") or "30")))
    except Exception:
        days = 30

    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_db",
                       note="UNMEASURED — no database handle. Not zero."), 503
    try:
        with c.cursor() as cur:
            return jsonify(build_mint_cliff(cur, days=days)), 200
    except Exception as e:
        logger.warning("[mint_cliff] failed: %s", e)
        return jsonify(ok=False, error="mint_cliff_failed",
                       detail=f"{type(e).__name__}: {str(e)[:200]}",
                       note="UNMEASURED — the query failed. Not zero."), 200
    finally:
        try:
            c.close()
        except Exception:
            pass
