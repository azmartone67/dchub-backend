"""recipe_lifecycle.py — first-class recipe/plan lifecycle logging (2026-07-30).

Perplexity's round-5 ask, three rounds running: today we log individual tool
calls, so "recipe completion" and "second-recipe take-up" in the Agent Success
Report are INFERENCES over call rows (a pseudo-call named `execute_plan_steps`
plus intent-string grouping). This module makes the lifecycle itself a fact:

  a recipe execution STARTED           → row with started_at
  it COMPLETED / FAILED                → completed_at + outcome
  it was ABANDONED                     → derived at read time: a started row
                                         with no terminal outcome after
                                         ABANDONED_AFTER_MINUTES

The gateway (dchub-mcp-server server.mjs) runs the whole execute_plan graph
server-side, so it emits both events from one place, each carrying the same
gateway-minted `recipe_execution_id`, POSTed to /api/v1/mcp/track with
`event: "recipe_lifecycle"` (the track endpoint dispatches here BEFORE any
call-table write — lifecycle events must never appear as tool calls, or they
would pollute the episode metrics they exist to replace).

IDENTITY — the agent-day unit, never session_id
  session_id rotates per MCP connection (~1.2 calls/session, measured
  2026-07-01), so it is stored for forensics ONLY. The identity columns are
  the same raw components the planner-bypass episode model reads
  (durable api_key first, session as fallback); readers derive the agent-day
  episode key at read time with the same COALESCE — nothing is precomputed,
  so an identity-rule change never strands historical rows.

OUTCOME SEMANTICS (writer side)
  completed  the envelope returned with ≥1 step executed or gated_preview —
             the caller got usable results (gated previews ARE working
             results at that tier).
  failed     the envelope returned but no step produced a result.
  abandoned  NEVER written by the v1 gateway — the column accepts it for
             future explicit markers, but the canonical read derives
             abandonment from a missing completion (outcome IS NULL past the
             threshold). A crash between start and completion is
             indistinguishable from abandonment and is counted as abandoned;
             both mean "the graph did not deliver an envelope".

DDL — applied via CI only (.github/workflows/apply-recipe-lifecycle.yml,
workflow_dispatch + confirm=APPLY), rendered from THIS module by
scripts/render_recipe_lifecycle_ddl.py so the repo migration and the applied
DDL cannot drift (the LIVE≠repo-DDL class). Never applied from a local shell
— DATABASE_URL lives in repo secrets and Railway only. Until the migration is
applied, record_lifecycle_event fails soft (ok:false, HTTP 200) and the
report metric reads UNAVAILABLE/UNMEASURED — never 0%.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

TABLE = "recipe_executions"

SOURCE_EXECUTE_PLAN = "execute_plan"

# The run budget is 40s; 15 minutes is generous enough that no legitimate
# in-flight run is ever misread as abandoned, and short enough that the
# weekly report never waits on stale rows.
ABANDONED_AFTER_MINUTES = 15

VALID_OUTCOMES = ("completed", "abandoned", "failed")
VALID_PHASES = ("started", "completed")

# One statement per entry; CREATE-only by design — the apply workflow refuses
# DROP/DELETE/TRUNCATE/ALTER/GRANT, so schema evolution happens as new
# rendered migrations, never in-place mutation.
TABLE_DDL = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
    -- gateway-minted UUID; both lifecycle events carry it, so either event
    -- arriving first (or alone) still yields exactly one row
    recipe_execution_id TEXT PRIMARY KEY,
    source              TEXT NOT NULL DEFAULT '{SOURCE_EXECUTE_PLAN}',
    intent              TEXT,
    intent_class        TEXT,
    planner_version     TEXT,
    -- identity components of the agent-day unit (durable api_key first).
    -- session_id is forensics/fallback ONLY — it rotates per connection and
    -- must never be used as the identity key (measured 2026-07-01).
    api_key             TEXT,
    session_id          TEXT,
    platform            TEXT,
    client_name         TEXT,
    user_agent          TEXT,
    ip_address          TEXT,
    tier                TEXT,
    started_at          TIMESTAMPTZ NOT NULL,
    completed_at        TIMESTAMPTZ,
    -- NULL while in flight; a NULL older than the abandonment threshold
    -- reads as 'abandoned' (derived at read time, never backfilled)
    outcome             TEXT CHECK (outcome IN ('completed','abandoned','failed')),
    duration_ms         INTEGER,
    steps_planned       INTEGER,
    steps_executed      INTEGER,
    steps_gated         INTEGER,
    steps_failed        INTEGER,
    steps_not_run       INTEGER,
    steps_skipped       INTEGER,
    -- raw per-status counts from the gateway — survives new step statuses
    -- without a schema change (the derived columns above just miss them)
    status_counts       JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
)"""

INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_started_at "
    f"ON {TABLE} (started_at)",
    f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_api_key_started "
    f"ON {TABLE} (api_key, started_at) WHERE api_key IS NOT NULL",
)


def all_ddl_statements() -> tuple:
    return (TABLE_DDL,) + INDEX_DDL


# ── writers ────────────────────────────────────────────────────────────────
# No literal % anywhere in these strings: they always execute WITH params,
# where psycopg2 %-substitution would eat a lone percent (the 2026-07-17
# /api/v1/map outage class). Tests emulate the substitution to hold this.

_INSERT_STARTED = f"""
INSERT INTO {TABLE}
    (recipe_execution_id, source, intent, intent_class, planner_version,
     api_key, session_id, platform, client_name, user_agent, ip_address,
     tier, started_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (recipe_execution_id) DO NOTHING
"""

# Completion is an UPSERT so a lost `started` event (track is fire-and-forget)
# still yields a complete row. On conflict the terminal fields update and the
# started row's identity + started_at WIN — completion finalizes, it never
# rewrites who started the recipe or when.
_UPSERT_COMPLETED = f"""
INSERT INTO {TABLE}
    (recipe_execution_id, source, intent, intent_class, planner_version,
     api_key, session_id, platform, client_name, user_agent, ip_address,
     tier, started_at, completed_at, outcome, duration_ms,
     steps_planned, steps_executed, steps_gated, steps_failed,
     steps_not_run, steps_skipped, status_counts)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s::jsonb)
ON CONFLICT (recipe_execution_id) DO UPDATE SET
    completed_at    = EXCLUDED.completed_at,
    outcome         = EXCLUDED.outcome,
    duration_ms     = EXCLUDED.duration_ms,
    steps_planned   = EXCLUDED.steps_planned,
    steps_executed  = EXCLUDED.steps_executed,
    steps_gated     = EXCLUDED.steps_gated,
    steps_failed    = EXCLUDED.steps_failed,
    steps_not_run   = EXCLUDED.steps_not_run,
    steps_skipped   = EXCLUDED.steps_skipped,
    status_counts   = EXCLUDED.status_counts,
    planner_version = COALESCE(EXCLUDED.planner_version,
                               {TABLE}.planner_version)
"""


def _ts(value, fallback=None):
    """Parse an ISO-8601 timestamp (Z-suffixed ok); fallback on anything else."""
    if not value:
        return fallback
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return fallback


def _trunc(value, n):
    if value is None:
        return None
    s = str(value).strip()
    return s[:n] if s else None


def _int_or_none(value):
    try:
        return int(value)
    except Exception:
        return None


def record_lifecycle_event(conn, body: dict):
    """Record one lifecycle event. Returns (ok, error).

    Telemetry contract: NEVER raises on bad payloads — validation failures
    return (False, why) and the endpoint answers 200, same as the track
    handler's missing-tool path. A DB error (e.g. the table not yet applied)
    propagates to the caller's except, which also answers 200.
    """
    if conn is None:
        return False, "no database connection"
    if not isinstance(body, dict):
        return False, "malformed payload"

    rid = _trunc(body.get("recipe_execution_id"), 64)
    if not rid:
        return False, "missing recipe_execution_id"
    phase = (str(body.get("phase") or "").strip().lower())
    if phase not in VALID_PHASES:
        return False, f"unknown phase: {phase or '(empty)'}"

    source = _trunc(body.get("source"), 40) or SOURCE_EXECUTE_PLAN
    now = datetime.now(timezone.utc)
    identity = (
        _trunc(body.get("api_key"), 200),
        _trunc(body.get("session_id"), 200),
        _trunc(body.get("platform"), 80),
        _trunc(body.get("client_name"), 200),
        _trunc(body.get("user_agent"), 300),
        _trunc(body.get("ip_address"), 64),
        _trunc(body.get("tier"), 40),
    )
    head = (
        rid, source,
        _trunc(body.get("intent"), 500),
        _trunc(body.get("intent_class"), 80),
        _trunc(body.get("planner_version"), 40),
    ) + identity

    if phase == "started":
        with conn.cursor() as cur:
            cur.execute(_INSERT_STARTED,
                        head + (_ts(body.get("started_at"), now),))
        return True, None

    # phase == "completed"
    outcome = (str(body.get("outcome") or "").strip().lower())
    if outcome not in VALID_OUTCOMES:
        # A mislabeled outcome is worse than a dropped event — refuse, never
        # coerce. The started row (if any) stays and reads as abandoned.
        return False, f"invalid outcome: {outcome or '(empty)'}"

    counts = body.get("status_counts")
    counts = counts if isinstance(counts, dict) else {}

    def _n(key):
        try:
            return int(counts.get(key) or 0)
        except Exception:
            return 0

    with conn.cursor() as cur:
        cur.execute(_UPSERT_COMPLETED, head + (
            _ts(body.get("started_at"), now),
            _ts(body.get("completed_at"), now),
            outcome,
            _int_or_none(body.get("duration_ms")),
            _int_or_none(body.get("steps_planned")),
            _n("executed"),
            _n("gated_preview"),
            _n("failed"),
            _n("not_run"),
            _n("skipped_meta") + _n("skipped_unresolved"),
            json.dumps(counts, default=str) if counts else None,
        ))
    return True, None
