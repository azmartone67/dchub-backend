"""r-oauth-funnel (2026-07-16) — the CHALLENGE side of the OAuth funnel.

WHY THIS EXISTS
---------------
The WorkOS OAuth 401 challenge is the only retention lever with evidence behind
it: the `oauth_durable` cohort returns at 40% vs 5.2% for `key_only` (Fisher
exact p=0.027). But n=5, so one agent flips significance, and the effect is
confounded (OAuth users self-selected by completing a consent flow). Confirming
or killing the 40% needs ~21 mature OAuth identities. To GROW that cohort you
must be able to manage adoption — and today nothing counts how many challenges
are ISSUED. We know 6 identities completed; we do not know how many were asked.

This module records the denominator-side counts the MCP gateway tallies
in-process and flushes as additive deltas every 60s.

★ WHAT THIS NUMBER IS NOT
-------------------------
It is NOT a conversion rate, and no key here ends in `_pct`. Challenges are
EVENTS that recur for every unconverted caller (one client retrying the
handshake three times = three challenges; a tools/call re-challenges the same
client repeatedly). `dch_oauth_` identities DEDUPE to one row per WorkOS sub
forever (deterministic HMAC + ON CONFLICT DO NOTHING). Numerator and denominator
are DIFFERENT POPULATIONS. Dividing them yields a ratio, not a percentage, and
that ratio is published as an INDEX (`..._challenges_per_new_identity_30d`,
lower is better) precisely so it cannot be quoted as "our OAuth conversion is
X%". A real per-challenge conversion rate is impossible for us: we are the
RESOURCE server, not the authorization server — there are zero shared fields
between an anonymous challenge (no credential, no session, no client IP in
scope) and an authenticated resolve (a WorkOS JWT `sub` arriving on a fresh
connection after an out-of-band browser round trip). The only avenue would be a
nonce round-tripped through the WorkOS authorize `state` param. Deliberately
not built.

Only the TREND is decision-grade. A single reading is not.
"""
from __future__ import annotations

import datetime
import os

from flask import Blueprint, jsonify, request

oauth_challenge_bp = Blueprint("oauth_challenge_ledger", __name__)

# Closed whitelists — the second cardinality fence. The gateway is trusted but
# NOT relied upon: both challenge sites are unauthenticated and fully
# client-controlled, so anything attacker-influenced must be rejected here too.
# This bounds the table at <=12 real rows/day + 1 beat row/day, whatever is POSTed
# (4 kinds x 3 methods; the old "<=6" was written when there were 2 kinds).
# chatgpt_connector_seen (2026-07-17): PASSIVE Phase-0 measurement — the gateway counts
# anonymous, keyless, sessionless ChatGPT-connector inits/calls that WOULD be challenge-
# eligible if the OAuth 401 challenge were widened to ChatGPT. It issues NO challenge; it
# only sizes the cohort denominator so widening is an evidence-based decision (durable
# identity is NOT a proven retention lever — see reference_dchub_oauth_challenge_funnel).
# claude_connector_seen (2026-08-28): the PASSIVE twin of claude_connector, and the
# instrument this family was missing. claude_connector counts challenges WE ISSUED
# (_chBump fires inside the 401 branch), so it measures our own behaviour, not the
# caller's: when r-challenge-after-value stopped challenging `initialize`, that count
# fell ~99% while nothing whatever was known about whether arrivals changed. Worse,
# the better the challenge policy gets at serving callers instead of 401-ing them, the
# SMALLER the challenge count becomes — the metric moves opposite to the outcome. This
# kind counts anonymous, keyless Claude-connector arrivals REGARDLESS of whether the
# challenge fires, so the denominator survives any future change to challenge policy.
_KINDS = {"claude_connector", "invalid_bearer", "chatgpt_connector_seen",
          "claude_connector_seen"}
_METHODS = {"initialize", "tools/call", "other"}
_BEAT_METHODS = {"workos_on", "workos_off"}

_TABLE_READY = False


def _conn():
    """RAW psycopg2 connection.

    ★ NEVER db_utils (get_db / get_read_db / get_bg_db / safe_db / safe_write):
    SKIP_DDL defaults to '1' (db_utils.py:13) and is absent from prod config, so
    every CREATE TABLE routed through db_utils is a silent no-op that RETURNS
    SUCCESS. Do not "simplify" this back to db_utils — the DDL would vanish.
    """
    import psycopg2
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        return None
    c = psycopg2.connect(dsn, sslmode="require", connect_timeout=5)
    # autocommit already isolates statement failures. NO SAVEPOINT anywhere in
    # this file: SAVEPOINT under autocommit raises "can only be used in
    # transaction blocks", gets swallowed, and yields zeros forever
    # (reference_psycopg2_savepoint_autocommit_trap).
    c.autocommit = True
    return c


def _ensure_table(cur):
    """Lazy-create on FIRST WRITE, once per process.

    ★ NEVER at import/boot: the 2026-07-01 failed deploy had boot DDL hang ~15s
    each against locks held by the still-serving old replica and blow the 5-min
    Railway healthcheck window. A brand-new table has no live table for the old
    replica to lock, which is the safest DDL shape in this codebase.
    """
    global _TABLE_READY
    if _TABLE_READY:
        return
    # `day` is a PLAIN DATE COLUMN carrying the gateway's UTC day — NOT a
    # ((ts AT TIME ZONE 'UTC')::date) expression index (bare timestamptz::date is
    # only STABLE and Postgres rejects it as non-IMMUTABLE).
    # The key is a table-level PRIMARY KEY: a real, PLAIN, non-partial constraint,
    # so ON CONFLICT (day, kind, method) is inference-safe (a partial index would
    # not match unless the INSERT repeated its predicate verbatim).
    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mcp_oauth_challenges (
                day       DATE NOT NULL,
                kind      TEXT NOT NULL,
                method    TEXT NOT NULL,
                n         BIGINT NOT NULL DEFAULT 0,
                first_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (day, kind, method)
            )
            """
        )
    except Exception as e:
        print(f"[oauth-funnel] create table failed (non-fatal): {e}", flush=True)
    try:
        # NOTE: CREATE INDEX IF NOT EXISTS matches by NAME ONLY. If this
        # definition is ever revised, a DROP INDEX IF EXISTS must precede it.
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_mcp_oauth_challenges_day "
            "ON mcp_oauth_challenges (day DESC)"
        )
    except Exception as e:
        print(f"[oauth-funnel] create index failed (non-fatal): {e}", flush=True)
    _TABLE_READY = True


def _clamp_day(raw):
    """Accept the gateway's UTC day; clamp to [today-2, today+1].

    The day is stamped at the SOURCE so a flush crossing midnight (or delayed by
    a breaker backoff) cannot smear counts into the wrong day. Clamping bounds
    cardinality against a buggy or compromised gateway.
    """
    today = datetime.datetime.now(datetime.timezone.utc).date()
    try:
        d = datetime.date.fromisoformat(str(raw)[:10])
    except Exception:
        return today
    if d < (today - datetime.timedelta(days=2)) or d > (today + datetime.timedelta(days=1)):
        return today
    return d


@oauth_challenge_bp.route("/api/v1/mcp/oauth-challenge/emit", methods=["POST"])
def oauth_challenge_emit():
    """Additive delta upsert of 401-challenge counts from the MCP gateway.

    Returns 503 (NOT 200) on write failure so the gateway's circuit breaker can
    actually trip. Safe here precisely because the gateway NEVER retries or
    re-merges — it clears its window before POSTing — so a 503 cannot produce a
    retry storm against an additive upsert. It just backs a sick backend off to
    1 req/10min/replica after 3 strikes.
    """
    try:
        from internal_auth import is_valid_internal_key
    except Exception:
        is_valid_internal_key = lambda _v: False  # noqa: E731 — fail-closed
    if not is_valid_internal_key(request.headers.get("X-Internal-Key", "")):
        return jsonify({"error": "internal_only"}), 403

    body = request.get_json(silent=True) or {}
    day = _clamp_day(body.get("day"))
    counts = body.get("counts") or {}
    if not isinstance(counts, dict):
        counts = {}
    workos_enabled = bool(body.get("workos_enabled"))

    rows = []
    for key, raw_n in counts.items():
        # Split on the LAST ':' — methods contain '/', not ':'.
        k = str(key)
        if ":" not in k:
            continue
        kind, _, method = k.rpartition(":")
        if kind not in _KINDS or method not in _METHODS:
            continue  # skip, never store, an unrecognised dimension
        try:
            n = int(raw_n)
        except Exception:
            continue
        if n <= 0:
            continue
        rows.append((day, kind, method, min(n, 1_000_000)))

    c = None
    try:
        c = _conn()
        if c is None:
            return jsonify({"error": "no_dsn"}), 503
        with c.cursor() as cur:
            _ensure_table(cur)
            for (d, kind, method, n) in rows:
                cur.execute(
                    """
                    INSERT INTO mcp_oauth_challenges (day, kind, method, n)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (day, kind, method) DO UPDATE
                      SET n = mcp_oauth_challenges.n + EXCLUDED.n, last_at = NOW()
                    """,
                    (d, kind, method, n),
                )
            # Heartbeat on EVERY request, including a counts-empty beat. This is
            # what makes "zero challenges" distinguishable from "gateway never
            # shipped" / "WorkOS off" / "feature dormant" — without it, an idle
            # gateway never materializes the table and the read below reports
            # oauth_funnel_error, which reads as BROKEN when the truth is DORMANT.
            cur.execute(
                """
                INSERT INTO mcp_oauth_challenges (day, kind, method, n)
                VALUES (%s, '_beat', %s, 1)
                ON CONFLICT (day, kind, method) DO UPDATE
                  SET n = mcp_oauth_challenges.n + EXCLUDED.n, last_at = NOW()
                """,
                (day, "workos_on" if workos_enabled else "workos_off"),
            )
        return jsonify({"ok": True, "day": day.isoformat(), "rows": len(rows)}), 200
    except Exception as e:
        print(f"[oauth-funnel] emit failed: {e}", flush=True)
        return jsonify({"error": "write_failed"}), 503
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass


@oauth_challenge_bp.route("/api/v1/mcp/oauth-challenge/state", methods=["GET"])
def oauth_challenge_state():
    """Read-only view of the ledger. Admin/debug; the engines read the
    /api/v1/mcp/retention summary instead."""
    c = None
    try:
        c = _conn()
        if c is None:
            return jsonify({"error": "no_dsn"}), 503
        import psycopg2.extras as _extras
        with c.cursor(cursor_factory=_extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT day, kind, method, n, first_at, last_at
                FROM mcp_oauth_challenges
                WHERE day >= ((NOW() AT TIME ZONE 'UTC')::date - 30)
                ORDER BY day DESC, kind, method
                """
            )
            rows = [dict(r) for r in (cur.fetchall() or [])]
        return jsonify({
            "rows": rows,
            "note": (
                "Challenge EVENTS, not people. An unconverted caller is re-challenged on "
                "every initialize and every tools/call, so this is not a distinct-user count "
                "and cannot be divided by identities to get a conversion rate."
            ),
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)[:200], "hint": "table may not exist yet (dormant)"}), 200
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
