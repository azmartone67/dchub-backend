"""
Canonical linkedin_posts schema — single source of truth.
=========================================================

Three modules historically ran `CREATE TABLE IF NOT EXISTS linkedin_posts`
with INCOMPATIBLE column sets against the SAME Neon table:

  * linkedin_autopost.py     — content_text NOT NULL, post_id, article_url,
                               published_at, linkedin_response, error_message …
  * linkedin_poster.py       — post_urn, content, error, posted_at  (THIS one
                               is what actually exists in prod — it is the only
                               module whose _ensure_tables runs on a real,
                               DDL-capable connection at boot)
  * intelligence_engine.py   — id TEXT(!), content, source_id, linkedin_post_id,
                               engagement, scheduled_at

Because `CREATE TABLE IF NOT EXISTS` is a no-op once the table exists, whichever
module initialised first won and the others silently failed their INSERT/SELECT
at runtime with `column "..." does not exist` (psycopg2 UndefinedColumn), which
in a shared transaction also cascaded to "current transaction is aborted".

The downstream READERS (routes/media_topic_tuner, multiplatform_amplifier,
dchub_media_accelerator, …) are already written for a SUPERSET — they use
`COALESCE(content, content_text, post_text, '')`,
`COALESCE(linkedin_post_id, post_urn, '')`,
`COALESCE(published_at, posted_at, created_at, NOW())`. Migrating column names
away would BREAK those readers, so the reconciliation is ADDITIVE: one canonical
superset table, with every historical column name preserved, and an idempotent
ADD COLUMN IF NOT EXISTS migration so existing prod rows are never lost.

Live prod columns verified via information_schema before authoring this
(2026-06-26): id(bigint, pk), post_urn, content, post_type, status, error,
posted_at, likes, comments, engagement_fetched_at, impressions, clicks, shares,
unique_impressions, media_topic_tags, autoresponse_triggered_at — i.e. the
poster shape + engagement columns. 235 rows, all written by linkedin_poster.py.

All three modules now route their table-init through reconcile_schema() here.
"""

import os
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical CREATE — full superset. Used on a FRESH DB (whichever module boots
# first creates the complete table; the others become harmless no-ops).
# `id` is BIGSERIAL to match the live bigint identity column.
# Nothing is NOT NULL except the surrogate key: poster writes `content` while
# autopost wrote `content_text`, so a NOT NULL on either would break the other.
# ---------------------------------------------------------------------------
CANONICAL_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS linkedin_posts (
    id                        BIGSERIAL PRIMARY KEY,
    -- LinkedIn-returned identifier (three historical names; readers COALESCE) --
    post_urn                  TEXT,
    post_id                   TEXT,
    linkedin_post_id          TEXT,
    -- post body (content is canonical; content_text/post_text kept for the
    -- defensive COALESCE() readers already in routes/*) --
    content                   TEXT,
    content_text              TEXT,
    post_text                 TEXT,
    -- classification / lifecycle --
    post_type                 TEXT DEFAULT 'manual',
    status                    TEXT DEFAULT 'success',
    -- error reporting (error is canonical; error_message kept for autopost) --
    error                     TEXT,
    error_message             TEXT,
    -- provenance / payload --
    article_url               TEXT,
    linkedin_response         TEXT,
    source_event              TEXT,
    source_data               TEXT,
    source_id                 TEXT,
    engagement                TEXT,
    media_topic_tags          JSONB DEFAULT '[]'::jsonb,
    -- timestamps (readers COALESCE published_at -> posted_at -> created_at) --
    scheduled_at              TIMESTAMPTZ,
    published_at              TIMESTAMPTZ,
    posted_at                 TIMESTAMPTZ DEFAULT NOW(),
    created_at                TIMESTAMPTZ DEFAULT NOW(),
    -- native LinkedIn engagement metrics --
    likes                     INTEGER,
    comments                  INTEGER,
    impressions               BIGINT,
    clicks                    BIGINT,
    shares                    BIGINT,
    unique_impressions        BIGINT,
    engagement_fetched_at     TIMESTAMPTZ,
    autoresponse_triggered_at TIMESTAMPTZ
)
"""

# ---------------------------------------------------------------------------
# Idempotent migration — brings an EXISTING table (the prod poster shape) up to
# the full superset without touching data. ADD COLUMN IF NOT EXISTS is a
# metadata-only operation for nullable / constant-default columns in PG11+,
# so this is fast and safe to run on every boot. Columns that already exist are
# left exactly as-is (their DEFAULT clause here is ignored on a no-op add).
# ---------------------------------------------------------------------------
MIGRATION_SQL = """
ALTER TABLE linkedin_posts
    ADD COLUMN IF NOT EXISTS post_urn                  TEXT,
    ADD COLUMN IF NOT EXISTS post_id                   TEXT,
    ADD COLUMN IF NOT EXISTS linkedin_post_id          TEXT,
    ADD COLUMN IF NOT EXISTS content                   TEXT,
    ADD COLUMN IF NOT EXISTS content_text              TEXT,
    ADD COLUMN IF NOT EXISTS post_text                 TEXT,
    ADD COLUMN IF NOT EXISTS post_type                 TEXT DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS status                    TEXT DEFAULT 'success',
    ADD COLUMN IF NOT EXISTS error                     TEXT,
    ADD COLUMN IF NOT EXISTS error_message             TEXT,
    ADD COLUMN IF NOT EXISTS article_url               TEXT,
    ADD COLUMN IF NOT EXISTS linkedin_response         TEXT,
    ADD COLUMN IF NOT EXISTS source_event              TEXT,
    ADD COLUMN IF NOT EXISTS source_data               TEXT,
    ADD COLUMN IF NOT EXISTS source_id                 TEXT,
    ADD COLUMN IF NOT EXISTS engagement                TEXT,
    ADD COLUMN IF NOT EXISTS media_topic_tags          JSONB DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS scheduled_at              TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS published_at              TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS posted_at                 TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS created_at                TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS likes                     INTEGER,
    ADD COLUMN IF NOT EXISTS comments                  INTEGER,
    ADD COLUMN IF NOT EXISTS impressions               BIGINT,
    ADD COLUMN IF NOT EXISTS clicks                    BIGINT,
    ADD COLUMN IF NOT EXISTS shares                    BIGINT,
    ADD COLUMN IF NOT EXISTS unique_impressions        BIGINT,
    ADD COLUMN IF NOT EXISTS engagement_fetched_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS autoresponse_triggered_at TIMESTAMPTZ
"""

# created_at is added WITHOUT a default above so existing rows are NULL rather
# than stamped with the (misleading) migration time; backfill from the truthful
# posted_at, THEN install the going-forward default for new inserts.
BACKFILL_CREATED_AT_SQL = """
UPDATE linkedin_posts
   SET created_at = COALESCE(created_at, posted_at, NOW())
 WHERE created_at IS NULL
"""

SET_CREATED_AT_DEFAULT_SQL = """
ALTER TABLE linkedin_posts ALTER COLUMN created_at SET DEFAULT NOW()
"""

_STATEMENTS = [
    CANONICAL_CREATE_SQL,
    MIGRATION_SQL,
    BACKFILL_CREATED_AT_SQL,
    SET_CREATED_AT_DEFAULT_SQL,
]

# Run at most once per process — every module's table-init calls this, but the
# DDL only needs to converge once per boot.
_RECONCILED = False


def _conn():
    import psycopg2
    db_url = os.environ.get("NEON_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise RuntimeError("no NEON_DATABASE_URL / DATABASE_URL in env")
    return psycopg2.connect(db_url, connect_timeout=10)


def reconcile_schema(force: bool = False):
    """Create the canonical linkedin_posts table and idempotently migrate an
    existing one to the full superset, on a REAL (DDL-capable) Postgres
    connection.

    Why its own connection: intelligence_engine.py talks to Neon through
    db_utils.get_db(), whose wrapper SKIPs DDL (SKIP_DDL=1), so CREATE/ALTER
    issued there are silent no-ops. Opening a direct psycopg2 connection here
    guarantees the DDL actually runs regardless of caller.

    Idempotent and never raises — safe to call from every module's table-init
    and on every boot.
    """
    global _RECONCILED
    if _RECONCILED and not force:
        return True
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()
        for stmt in _STATEMENTS:
            cur.execute(stmt)
        conn.commit()
        cur.close()
        _RECONCILED = True
        logger.info("[linkedin_posts_schema] canonical schema reconciled")
        return True
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.error(f"[linkedin_posts_schema] reconcile failed: {e}")
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
