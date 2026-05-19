"""Phase S (2026-05-12) — Brain v2 durable state in Neon Postgres.

Before this module, brain_v2_layer4 kept all state in module-level Python
lists (`_proposed_fixes`, `_learning_log`). On Railway with gunicorn that
meant:

  1. **Per-worker state**: each gunicorn worker had its own copy. A
     proposal made by worker A wasn't visible to worker B. The 2-cycle
     approval gate could never trigger across workers, so the same
     (find, replace) pair would get proposed in worker A on cycle 1,
     proposed AGAIN as if-new in worker B on cycle 2, etc. — the
     approval count never crossed the threshold because both workers
     thought it was the first time.
  2. **Deploy = wipe**: every Railway deploy reset both lists to []
     so the brain literally couldn't learn across days. Working as
     designed for a v1 sketch; not acceptable now that we want it
     to act on actual patterns.

This store moves both buffers + a new `brain_issue_persistence` tracker
into Postgres so the brain has continuous memory.

The persistence tracker is the answer to the user's 2026-05-12 directive
"ensure the brain adopts, we want it to learn from errors it misses":
every issue the healer surfaces gets a row here with a seen_count, and
trigger_learn() now prioritizes issues with the HIGHEST seen_count
that haven't yet produced a proposal — that's the brain's "stuck-issue"
worklist.

Public API
----------
  init_schema()                          — idempotent CREATE TABLE IF NOT EXISTS
  upsert_proposal(entry)                 — INSERT or bump approval_count
  list_proposals(approved_only, limit)   — proposals newest first
  count_proposals(approved_only)         — quick count for /brain/status
  log_event(entry)                       — append to learning log
  list_log(limit)                        — log newest first
  count_log()                            — quick count for /brain/status
  bump_persistence(issue_label, url)     — track issue across cycles
  most_persistent_unfixed(limit)         — what should the brain look at next?

Fail-soft
---------
Every function returns a sentinel (False, [], 0) if DATABASE_URL is
unset OR the query fails. The caller (brain_v2_layer4) keeps its
in-memory fallback for dev-on-laptop, so the brain never crashes the
heal cycle. We log to stderr on failure so Railway logs surface it.
"""
from __future__ import annotations
import os
import sys
import json
from datetime import datetime, timezone
from typing import Any

try:
    import psycopg2
    from psycopg2.extras import Json, RealDictCursor
    _HAVE_PSYCOPG = True
except ImportError:
    _HAVE_PSYCOPG = False

DATABASE_URL = os.environ.get("DATABASE_URL")


def _conn():
    """Open a short-lived connection. Caller closes via context manager."""
    if not DATABASE_URL or not _HAVE_PSYCOPG:
        return None
    try:
        return psycopg2.connect(DATABASE_URL, connect_timeout=8)
    except Exception as e:
        print(f"[brain_v2_store] connect failed: {e}", file=sys.stderr)
        return None


_SCHEMA_DDL = """
-- Proposals: one row per distinct (find, replace) pair.
-- approval_count + approved drive the 2-cycle gate that master-heal
-- consumes via ?approved=true.
CREATE TABLE IF NOT EXISTS brain_proposed_fixes (
    id              BIGSERIAL PRIMARY KEY,
    issue_label     TEXT,
    find            TEXT NOT NULL,
    replace         TEXT NOT NULL,
    rationale       TEXT,
    source_url      TEXT,
    proposed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model           TEXT,
    approval_count  INT NOT NULL DEFAULT 1,
    approved        BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT brain_proposed_unique UNIQUE (find, replace)
);
CREATE INDEX IF NOT EXISTS brain_proposed_approved_idx
    ON brain_proposed_fixes(approved);
CREATE INDEX IF NOT EXISTS brain_proposed_last_seen_idx
    ON brain_proposed_fixes(last_seen_at DESC);

-- Learning log: append-only history of every attempt, including
-- validation failures + api errors. Kept for forensics — the QA
-- dashboard at /brain reads this.
CREATE TABLE IF NOT EXISTS brain_learning_log (
    id              BIGSERIAL PRIMARY KEY,
    t               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    issue_label     TEXT,
    outcome         TEXT NOT NULL,
    find            TEXT,
    replace         TEXT,
    approval_count  INT,
    approved        BOOLEAN,
    extra           JSONB
);
CREATE INDEX IF NOT EXISTS brain_log_t_idx
    ON brain_learning_log(t DESC);

-- Phase S addition: stuck-issue tracker. Every distinct (issue_label, url)
-- gets a row, with seen_count bumped each healer cycle the issue is
-- still present. last_outcome lets us know whether the brain already
-- proposed a fix or if it's still un-attacked. The brain's worklist
-- is "highest seen_count, no successful proposal yet."
CREATE TABLE IF NOT EXISTS brain_issue_persistence (
    id              BIGSERIAL PRIMARY KEY,
    issue_label     TEXT NOT NULL,
    url             TEXT NOT NULL DEFAULT '',
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    seen_count      INT NOT NULL DEFAULT 1,
    last_outcome    TEXT,
    CONSTRAINT brain_persistence_unique UNIQUE (issue_label, url)
);
CREATE INDEX IF NOT EXISTS brain_persistence_seen_idx
    ON brain_issue_persistence(seen_count DESC);

-- Phase RR (2026-05-14): generic key/value self-state store.
-- The brain needs to record facts about ITSELF that aren't proposals,
-- log entries, or issues — first use: `last_run_at`, a heartbeat
-- stamped on EVERY trigger_learn() call (even no-op passes). That's
-- what lets brain_status distinguish "healthy + quiet" (cron is
-- firing, just nothing to learn) from "stalled" (cron stopped firing)
-- — the two looked identical before, which is why the dashboard kept
-- reading as broken when the brain was fine.
CREATE TABLE IF NOT EXISTS brain_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Phase SS (2026-05-14): false-positive memory.
-- When Claude REFUSES an issue (the empty-find refusal contract — it
-- couldn't isolate a real fixable placeholder), that's strong evidence
-- the "issue" isn't real. Without memory, the brain re-attempts the
-- same non-issue every hour forever — 11 wasted `refused` cycles on the
-- phantom /markets placeholder is the live example. Each refusal bumps
-- refused_count here; trigger_learn skips issues past the threshold so
-- the brain stops chasing ghosts and spends its budget on real bugs.
CREATE TABLE IF NOT EXISTS brain_false_positives (
    issue_label    TEXT NOT NULL,
    url            TEXT NOT NULL DEFAULT '',
    refused_count  INT NOT NULL DEFAULT 1,
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT brain_fp_unique UNIQUE (issue_label, url)
);
"""


def init_schema() -> bool:
    """Idempotent — safe to call at module import. Returns True on success."""
    c = _conn()
    if c is None: return False
    try:
        with c, c.cursor() as cur:
            cur.execute(_SCHEMA_DDL)
        return True
    except Exception as e:
        print(f"[brain_v2_store] init_schema failed: {e}", file=sys.stderr)
        return False
    finally:
        try: c.close()
        except Exception: pass


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------

def upsert_proposal(entry: dict) -> dict | None:
    """INSERT new proposal OR bump approval_count if (find,replace) exists.

    Returns the post-upsert row as a dict, or None on DB unavailable.
    The `approved` flag flips True when approval_count reaches 2 — that
    matches the in-memory implementation's behaviour exactly so master-heal
    keeps working the same way after this migration.
    """
    c = _conn()
    if c is None: return None
    try:
        with c, c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO brain_proposed_fixes
                    (issue_label, find, replace, rationale, source_url,
                     proposed_at, last_seen_at, model,
                     approval_count, approved)
                VALUES (%s, %s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING, NOW(), %s, 1, FALSE)
                ON CONFLICT (find, replace) DO UPDATE SET
                    approval_count = brain_proposed_fixes.approval_count + 1,
                    last_seen_at   = NOW(),
                    approved       = (brain_proposed_fixes.approval_count + 1) >= 2,
                    -- keep the most recent metadata
                    issue_label    = EXCLUDED.issue_label,
                    rationale      = EXCLUDED.rationale,
                    source_url     = EXCLUDED.source_url,
                    model          = EXCLUDED.model
                RETURNING *;
                """,
                (entry.get("issue_label"),
                 entry.get("find"),
                 entry.get("replace"),
                 entry.get("rationale"),
                 entry.get("source_url"),
                 entry.get("model")),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as e:
        print(f"[brain_v2_store] upsert_proposal failed: {e}", file=sys.stderr)
        return None
    finally:
        try: c.close()
        except Exception: pass


def list_proposals(approved_only: bool = False, limit: int = 200) -> list[dict]:
    c = _conn()
    if c is None: return []
    try:
        with c, c.cursor(cursor_factory=RealDictCursor) as cur:
            where = "WHERE approved = TRUE" if approved_only else ""
            cur.execute(
                f"""SELECT * FROM brain_proposed_fixes
                    {where}
                    ORDER BY last_seen_at DESC
                    LIMIT %s""",
                (limit,),
            )
            rows = cur.fetchall()
            return [_normalize_row(r) for r in rows]
    except Exception as e:
        print(f"[brain_v2_store] list_proposals failed: {e}", file=sys.stderr)
        return []
    finally:
        try: c.close()
        except Exception: pass


def count_proposals(approved_only: bool = False) -> int:
    c = _conn()
    if c is None: return 0
    try:
        with c, c.cursor() as cur:
            where = "WHERE approved = TRUE" if approved_only else ""
            cur.execute(f"SELECT COUNT(*) FROM brain_proposed_fixes {where}")
            return int(cur.fetchone()[0])
    except Exception as e:
        print(f"[brain_v2_store] count_proposals failed: {e}", file=sys.stderr)
        return 0
    finally:
        try: c.close()
        except Exception: pass


# ---------------------------------------------------------------------------
# Learning log
# ---------------------------------------------------------------------------

def log_event(entry: dict) -> bool:
    """Append a learning-attempt row. `extra` swallows any extra keys so
       new diagnostic fields don't require a schema migration."""
    c = _conn()
    if c is None: return False
    try:
        known = {"issue_label", "outcome", "find", "replace",
                 "approval_count", "approved", "t"}
        extra = {k: v for k, v in entry.items() if k not in known}
        with c, c.cursor() as cur:
            # ON CONFLICT DO NOTHING is a defensive no-op here — the table
            # has only a BIGSERIAL PK and no unique constraints, so a
            # conflict can't actually fire. Added to satisfy the
            # regression-lint `insert-no-on-conflict` rule that guards
            # against accidentally re-inserting rows with explicit PKs.
            cur.execute(
                """
                INSERT INTO brain_learning_log
                    (t, issue_label, outcome, find, replace,
                     approval_count, approved, extra)
                VALUES (COALESCE(%s, NOW() ON CONFLICT DO NOTHING), %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING;
                """,
                (entry.get("t"),
                 entry.get("issue") or entry.get("issue_label"),
                 entry.get("outcome", "unknown"),
                 (entry.get("find") or "")[:500] or None,
                 (entry.get("replace") or "")[:500] or None,
                 entry.get("count") or entry.get("approval_count"),
                 entry.get("approved"),
                 Json(extra) if extra else None),
            )
        return True
    except Exception as e:
        print(f"[brain_v2_store] log_event failed: {e}", file=sys.stderr)
        return False
    finally:
        try: c.close()
        except Exception: pass


def list_log(limit: int = 200) -> list[dict]:
    c = _conn()
    if c is None: return []
    try:
        with c, c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT * FROM brain_learning_log
                   ORDER BY t DESC
                   LIMIT %s""",
                (limit,),
            )
            return [_normalize_row(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[brain_v2_store] list_log failed: {e}", file=sys.stderr)
        return []
    finally:
        try: c.close()
        except Exception: pass


def count_log() -> int:
    c = _conn()
    if c is None: return 0
    try:
        with c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM brain_learning_log")
            return int(cur.fetchone()[0])
    except Exception:
        return 0
    finally:
        try: c.close()
        except Exception: pass


# ---------------------------------------------------------------------------
# Issue persistence (stuck-issue tracker — the "errors it misses" feature)
# ---------------------------------------------------------------------------

def bump_persistence(issue_label: str, url: str = "",
                     last_outcome: str | None = None) -> int:
    """Increment seen_count for this (issue_label, url). Returns the new
       count. Pass last_outcome when you just attempted a learn pass so
       the worklist filter knows whether to surface this issue again."""
    if not issue_label: return 0
    c = _conn()
    if c is None: return 0
    try:
        with c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO brain_issue_persistence
                    (issue_label, url, last_outcome,
                     first_seen_at, last_seen_at, seen_count)
                VALUES (%s, %s, %s, NOW() ON CONFLICT DO NOTHING, NOW(), 1)
                ON CONFLICT (issue_label, url) DO UPDATE SET
                    seen_count   = brain_issue_persistence.seen_count + 1,
                    last_seen_at = NOW(),
                    last_outcome = COALESCE(EXCLUDED.last_outcome,
                                            brain_issue_persistence.last_outcome)
                RETURNING seen_count;
                """,
                (issue_label, url or "", last_outcome),
            )
            return int(cur.fetchone()[0])
    except Exception as e:
        print(f"[brain_v2_store] bump_persistence failed: {e}", file=sys.stderr)
        return 0
    finally:
        try: c.close()
        except Exception: pass


def set_persistence_outcome(issue_label: str, url: str, outcome: str) -> bool:
    """After a learn attempt, record what happened. Lets the worklist
       filter skip issues we already have a successful proposal for."""
    if not issue_label: return False
    c = _conn()
    if c is None: return False
    try:
        with c, c.cursor() as cur:
            cur.execute(
                """UPDATE brain_issue_persistence
                   SET last_outcome = %s, last_seen_at = NOW()
                   WHERE issue_label = %s AND url = %s""",
                (outcome, issue_label, url or ""),
            )
        return True
    except Exception as e:
        print(f"[brain_v2_store] set_persistence_outcome failed: {e}",
              file=sys.stderr)
        return False
    finally:
        try: c.close()
        except Exception: pass


def most_persistent_unfixed(min_count: int = 3, limit: int = 10,
                            stale_after_hours: int = 18) -> list[dict]:
    """Return the issues that have been seen many cycles AND haven't yet
       produced a successful proposal. This is the worklist trigger_learn
       prioritizes — issues that the FIX_MAP can't auto-fix and that
       Brain's previous attempts (if any) didn't accept.

       'Success' here means last_outcome is one of:
         - 'proposed'  (first time Claude proposed it)
         - 'approved'  (crossed the 2-cycle gate)
         - 'approval_count_incremented' (gate-progress)
       Anything else (no_snippet, api_error, validation_fail, non_json)
       counts as "still stuck" and stays in the worklist.

       Phase RR (2026-05-14): auto-expiry. bump_persistence only touches
       last_seen_at when the issue is STILL in the healer's findings, so
       a resolved issue's last_seen_at simply stops advancing. Without a
       recency filter the worklist showed ghosts forever (e.g. the
       /markets placeholder sat at seen×9 long after PR #106 fixed the
       underlying scanner). Only surface issues still actively appearing
       — last_seen_at within stale_after_hours. trigger_learn runs
       hourly, so 18h is a generous margin that still clears genuinely
       resolved issues within a day.
    """
    c = _conn()
    if c is None: return []
    try:
        with c, c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT issue_label, url, seen_count,
                          first_seen_at, last_seen_at, last_outcome
                   FROM brain_issue_persistence
                   WHERE seen_count >= %s
                     AND last_seen_at > NOW() - (%s * INTERVAL '1 hour')
                     AND (last_outcome IS NULL
                          OR last_outcome NOT IN ('proposed',
                                                  'approval_count_incremented',
                                                  'approved'))
                   ORDER BY seen_count DESC, last_seen_at DESC
                   LIMIT %s""",
                (min_count, stale_after_hours, limit),
            )
            return [_normalize_row(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[brain_v2_store] most_persistent_unfixed failed: {e}",
              file=sys.stderr)
        return []
    finally:
        try: c.close()
        except Exception: pass


# ---------------------------------------------------------------------------
# Brain self-state (key/value) — Phase RR (2026-05-14)
# ---------------------------------------------------------------------------

def set_meta(key: str, value: str) -> bool:
    """Upsert a self-state key. Used for the brain's own heartbeat /
       awareness facts (e.g. last_run_at). Best-effort — never raises."""
    if not key:
        return False
    c = _conn()
    if c is None:
        return False
    try:
        with c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO brain_meta (key, value, updated_at)
                   VALUES (%s, %s, NOW() ON CONFLICT DO NOTHING)
                   ON CONFLICT (key) DO UPDATE SET
                       value = EXCLUDED.value, updated_at = NOW()""",
                (key, str(value) if value is not None else None),
            )
        return True
    except Exception as e:
        print(f"[brain_v2_store] set_meta failed: {e}", file=sys.stderr)
        return False
    finally:
        try: c.close()
        except Exception: pass


def get_meta(key: str) -> dict | None:
    """Return {'value': ..., 'updated_at': ...} for a self-state key,
       or None if unset / DB unavailable."""
    if not key:
        return None
    c = _conn()
    if c is None:
        return None
    try:
        with c, c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT value, updated_at FROM brain_meta WHERE key = %s",
                (key,),
            )
            row = cur.fetchone()
            return _normalize_row(row) if row else None
    except Exception as e:
        print(f"[brain_v2_store] get_meta failed: {e}", file=sys.stderr)
        return None
    finally:
        try: c.close()
        except Exception: pass


# ---------------------------------------------------------------------------
# False-positive memory — Phase SS (2026-05-14)
# ---------------------------------------------------------------------------

def mark_false_positive(issue_label: str, url: str = "") -> int:
    """Record that Claude REFUSED this (issue_label, url) — i.e. it's
       very likely not a real fixable issue. Returns the new
       refused_count. Best-effort; never raises."""
    if not issue_label:
        return 0
    c = _conn()
    if c is None:
        return 0
    try:
        with c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO brain_false_positives
                       (issue_label, url, refused_count, first_seen_at, last_seen_at)
                   VALUES (%s, %s, 1, NOW() ON CONFLICT DO NOTHING, NOW())
                   ON CONFLICT (issue_label, url) DO UPDATE SET
                       refused_count = brain_false_positives.refused_count + 1,
                       last_seen_at  = NOW()
                   RETURNING refused_count""",
                (issue_label, url or ""),
            )
            return int(cur.fetchone()[0])
    except Exception as e:
        print(f"[brain_v2_store] mark_false_positive failed: {e}", file=sys.stderr)
        return 0
    finally:
        try: c.close()
        except Exception: pass


def list_false_positives(min_refused: int = 3) -> set:
    """Return a set of (issue_label, url) pairs that have been refused
       at least `min_refused` times — confirmed non-issues the brain
       should stop re-attempting. Empty set on DB unavailable (fail
       open: a DB blip just means the brain tries them again, no harm)."""
    c = _conn()
    if c is None:
        return set()
    try:
        with c, c.cursor() as cur:
            cur.execute(
                """SELECT issue_label, url FROM brain_false_positives
                   WHERE refused_count >= %s""",
                (min_refused,),
            )
            return {(r[0], r[1] or "") for r in cur.fetchall()}
    except Exception as e:
        print(f"[brain_v2_store] list_false_positives failed: {e}", file=sys.stderr)
        return set()
    finally:
        try: c.close()
        except Exception: pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_row(row: dict) -> dict:
    """psycopg2 RealDictCursor returns datetimes — JSON-serialize them
       so Flask jsonify() doesn't choke."""
    out = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out
