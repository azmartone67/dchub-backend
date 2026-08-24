"""
Content Publishing Pipeline
Manages draft social media posts and press releases through an approval workflow.
Supports LinkedIn auto-publishing via LINKEDIN_ACCESS_TOKEN.
"""

import os
import sqlite3
import logging
import time
import requests
import threading
from contextlib import contextmanager, ExitStack
import json
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify
from utils.anthropic_helper import anthropic_messages_url
from linkedin_text import escape_li_commentary  # /rest/posts commentary escaping
from routes._swallowed_writes import note_swallowed_write

# phase57_landing — daily landing URL helper for LinkedIn rich-card preview
def _phase30c_landing_url(d=None):
    """Return canonical /api/v1/social/posts/<date> URL for LinkedIn OG card."""
    import datetime
    if d is None:
        d = datetime.date.today()
    return f"https://dchub.cloud/api/v1/social/posts/{d.isoformat()}"


logger = logging.getLogger(__name__)

content_bp = Blueprint('content_publisher', __name__)

DB_PATH = 'dc_nexus.db'

def _get_db(retries=3):
    """Phase RRR-content-publisher-neon (2026-05-18) — MIGRATED from
    sqlite3 to psycopg2/Neon. The SQL queries in this module already
    use %s placeholders (PG style), so they work as-is once the
    connection is PG. On Railway, dc_nexus.db doesn't exist — the old
    sqlite3.connect() was hanging in 30s timeouts and broke 8
    downstream blueprints. Falls back to SQLite ONLY if no Neon URL
    is set (local-dev shim)."""
    last_error = None
    neon_url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
    if neon_url:
        try:
            import psycopg2
            import psycopg2.extras
            # Use RealDictCursor so cur.fetchone() returns dict (the
            # auto-publish loops reference row['id'], row['content'])
            conn = psycopg2.connect(neon_url, connect_timeout=10,
                                    cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                from db_utils import cap_lock_wait
                cap_lock_wait(conn)   # boot-DDL fail-fast on lock contention
            except Exception:
                pass
            return conn
        except Exception as e:
            logger.warning(f"Neon connect failed, falling back to sqlite: {e}")
            last_error = e
    # Local dev only: SQLite fallback. On Railway this will fail because
    # dc_nexus.db doesn't exist + sqlite3.connect with timeout hangs.
    for attempt in range(retries):
        try:
            conn = sqlite3.connect(DB_PATH, timeout=10)
            return conn
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
            last_error = e
            if attempt < retries - 1:
                logger.warning(f"SQLite connect attempt {attempt+1}/{retries} failed: {e}")
                time.sleep(2 * (attempt + 1))
            else:
                logger.error(f"SQLite connect failed after {retries} attempts: {e}")
    raise last_error

@contextmanager
def _db_conn():
    """Yields a _get_db() connection; guarantees close on every exit path
    (the conn-leak class behind the 2026-05-19 Neon-pool outage)."""
    conn = _get_db()
    try:
        yield conn
    finally:
        try: conn.close()
        except Exception: pass

def _check_admin(req):
    admin_key = req.headers.get('X-Admin-Key') or req.args.get('admin_key') or req.args.get('key')
    valid_keys = [k for k in [os.environ.get('DCHUB_ADMIN_KEY', '')] if k]
    return admin_key in valid_keys


def _utc_day_bounds(now=None):
    """(day, next_day) as 'YYYY-MM-DD' for a half-open [day, next_day) filter.

    This is the replacement for `WHERE <col> LIKE 'YYYY-MM-DD%'`, which was
    wrong twice over: a prefix LIKE cannot use a b-tree range scan, and it
    raises `operator does not exist` the moment the column stops being TEXT.
    The range form is correct against BOTH types, because the stored values are
    ISO-8601 and lexicographic order on ISO-8601 IS chronological order — so a
    column can be migrated TEXT -> timestamptz without editing its readers
    again. Verified against the live table: LIKE and this range returned
    identical counts for all 24 days of 2026-08 (0 mismatches).
    """
    d = (now or datetime.utcnow()).date()
    return d.isoformat(), (d + timedelta(days=1)).isoformat()


def _scalar(cur, key='n', default=0):
    """First value of the fetched row, for a RealDict *or* tuple cursor.

    _get_db() sets cursor_factory=RealDictCursor, so `cur.fetchone()[0]` raises
    KeyError: 0 — the fault that made /api/admin/content/stats return HTTP 500.
    """
    row = cur.fetchone()
    if not row:
        return default
    if hasattr(row, 'get'):
        val = row.get(key)
        if val is None:
            vals = list(row.values())
            val = vals[0] if vals else None
    else:
        val = row[0]
    return default if val is None else val


def stage_draft(content, platform='linkedin', priority=0):
    """Persist a REVIEW-FIRST announcement draft into social_media_posts as status='draft'.

    Deduped by content_hash so re-staging identical copy is a no-op. Does NOT publish:
    the auto-publisher only drains status='approved', so an operator must review the row
    (e.g. GET /api/admin/content-queue?status=draft) and approve it before anything ships.
    This is the persistence the pillars/announcement shells were missing — they generated
    drafts in-memory only, so nothing could ever ship them.
    Returns {'action': 'inserted'|'dupe'|'skipped', 'post_id': int|None, ...}.
    """
    import hashlib
    content = (content or '').strip()
    if len(content) < 20:
        return {'action': 'skipped', 'reason': 'content too short (<20 chars)', 'post_id': None}
    platform = (platform or 'linkedin').strip().lower()
    chash = hashlib.sha256(content.encode('utf-8')).hexdigest()
    with _db_conn() as conn:
        cur = conn.cursor()
        # content_hash dedup — never enqueue the same body twice (in any status)
        cur.execute("SELECT id FROM social_media_posts WHERE content_hash = %s LIMIT 1", (chash,))
        row = cur.fetchone()
        if row:
            existing = row['id'] if hasattr(row, 'get') else row[0]
            return {'action': 'dupe', 'post_id': existing}
        cur.execute(
            "INSERT INTO social_media_posts (content, platform, status, content_hash, priority, created_at) "
            "VALUES (%s, %s, 'draft', %s, %s, NOW()) RETURNING id",
            (content, platform, chash, priority),
        )
        r = cur.fetchone()
        new_id = r['id'] if hasattr(r, 'get') else (r[0] if r else None)
        conn.commit()
        return {'action': 'inserted', 'post_id': new_id}

def init_content_tables():
    """Phase RRR-content-publisher-neon (2026-05-18) — Neon-compatible
    table bootstrap. Creates social_media_posts if missing, then adds
    any missing columns. press_releases is already managed elsewhere
    (routes/press_queue.py etc.) so we leave it alone."""
    with _db_conn() as conn:
        cur = conn.cursor()
        try:
            # social_media_posts — needed by auto-publish loops
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS social_media_posts (
                        id              SERIAL PRIMARY KEY,
                        content         TEXT NOT NULL,
                        platform        TEXT,
                        status          TEXT NOT NULL DEFAULT 'draft',
                        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        approved_at     TEXT,
                        posted_at       TEXT,
                        published_at    TEXT,
                        publish_platform TEXT,
                        bluesky_uri     TEXT,
                        twitter_id      TEXT,
                        linkedin_urn    TEXT
                    )
                """)
                try: conn.commit()
                except Exception: pass
            except Exception as e:
                logger.warning(f"social_media_posts CREATE skipped: {e}")
            # Add missing columns idempotently (cheap on PG with IF NOT EXISTS).
            # accelerate + viral_score added 2026-06-05 for the DC Hub Media
            # accelerator (routes/dchub_media_accelerator.py): when a LinkedIn
            # post outperforms the 30d baseline by 2x+ within 6h of publish we
            # auto-enqueue Twitter + Bluesky cross-posts and flag the row.
            for col_def in [
                "approved_at TEXT",
                "publish_platform TEXT",
                "published_at TEXT",
                "bluesky_uri TEXT",
                "twitter_id TEXT",
                "linkedin_urn TEXT",
                "accelerate BOOLEAN DEFAULT FALSE",
                "viral_score NUMERIC DEFAULT NULL",
                # Item 17 (2026-06-30): queue-drain priority + content dedupe.
                # priority — higher drains first (fresh wins/citations can jump
                #   ahead of a stale backlog instead of waiting behind FIFO).
                #   Default 0 so every existing row keeps today's created_at order.
                # content_hash — sha256 of the body; lets the drain skip a row whose
                #   text already published (near-dup floods were shipping twice).
                "priority INTEGER DEFAULT 0",
                "content_hash TEXT",
                # 2026-07-16: the INTENDED branded card for this row (the
                # data/data_brutal card compose_story_post built). The drain
                # attaches it directly as the LinkedIn image so drumbeat posts
                # carry the same good card as the quad. NULL -> prior behaviour.
                "og_image TEXT",
                # 2026-07-17 X-editorial fix: which editorial-desk lead this row
                # was composed from. This IS the X anti-repeat ledger — the desk's
                # linkedin_quad_posts ledger only sees quad posts, so X had no
                # (kind, entity) memory and shipped the same city on repeat.
                "lead_kind TEXT",
                "lead_entity TEXT",
            ]:
                col = col_def.split()[0]
                try:
                    cur.execute(f"ALTER TABLE social_media_posts ADD COLUMN IF NOT EXISTS {col_def}")
                    try: conn.commit()
                    except Exception: pass
                except Exception:
                    pass
            # Item 17 (2026-06-30): supporting indexes for the priority-ordered,
            # dedupe-aware drain. Both IF NOT EXISTS (idempotent, cheap on PG).
            #  * (status, priority DESC, created_at ASC) matches the new drain
            #    ORDER BY so the queue picks the highest-priority oldest row.
            #  * (content_hash) speeds the "did this exact body already publish?"
            #    lookup the drain uses to skip near-dup floods.
            for _idx_sql in (
                "CREATE INDEX IF NOT EXISTS social_media_posts_drain_idx "
                "ON social_media_posts(status, priority DESC, created_at ASC)",
                "CREATE INDEX IF NOT EXISTS social_media_posts_content_hash_idx "
                "ON social_media_posts(content_hash)",
            ):
                try:
                    cur.execute(_idx_sql)
                    try: conn.commit()
                    except Exception: pass
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
            logger.info("Content publishing tables initialized (Neon)")
        finally:
            try: conn.close()
            except Exception: pass
    # 2026-06-06: piggy-back the Feedback Forum schema bootstrap onto
    # init_content_tables so it runs on the same boot path (already wired
    # in main.py). Defensive — if feedback_forum import or table init
    # fails, we log and continue so content_publisher stays up.
    try:
        from routes.feedback_forum import init_feedback_tables as _ift
        _ift()
    except Exception as _fbe:
        logger.warning("feedback_forum table init skipped: %s", _fbe)

    # market_verdict_post_log table — same boot-init pattern.
    try:
        from routes.market_verdict_shifts import init_verdict_shift_tables as _ivst
        _ivst()
    except Exception as _vse:
        logger.warning("market_verdict_shifts table init skipped: %s", _vse)

    # widget_embeds table — embeddable Market Brief widget (2026-06-06).
    # Defensive ALTER pattern; idempotent CREATE + ADD COLUMN IF NOT EXISTS.
    try:
        from routes.market_brief import init_widget_embed_tables as _iwet
        _iwet()
    except Exception as _wee:
        logger.warning("widget_embeds table init skipped: %s", _wee)

    # Watchlist tables (user_watchlists + watchlist_alerts_sent +
    # browser_push_subscriptions) — same boot-init pattern. 2026-06-06:
    # Real-time Watchlist + Verdict-Shift Alerts. Without this hook the
    # first /api/v1/watchlist/add call would have to lazily create the
    # tables on the request path; this is the clean path.
    try:
        from routes.watchlist import init_watchlist_tables as _iwlt
        _iwlt()
    except Exception as _wle:
        logger.warning("watchlist table init skipped: %s", _wle)

    # Media topic tuner tables (2026-06-07): media_topic_mix + media_link_clicks
    # + media_link_shortcodes + media_themed_series, plus the
    # media_topic_tags JSONB column added to social_media_posts +
    # linkedin_posts. Same boot-init pattern. Required for the daily
    # 14:00 UTC tuner cron + the /li/<short> click attribution proxy.
    try:
        from routes.media_topic_tuner import init_topic_tuner_tables as _itt
        _itt()
    except Exception as _tte:
        logger.warning("media topic tuner table init skipped: %s", _tte)

    # Media ROUND 2 — spike responder (2026-06-07): media_autoresponse_log
    # table + linkedin_posts.autoresponse_triggered_at column. Required
    # for the twice-daily spike detector cron at 10/22 UTC and the
    # /api/v1/admin/media/spikes/* endpoints.
    try:
        from routes.media_spike_responder import init_spike_responder_tables as _isr
        _isr()
    except Exception as _isre:
        logger.warning("media spike responder table init skipped: %s", _isre)

    # State of 2026 LIVING document tables (state_of_2026_pageviews,
    # state_of_2026_clicks, state_of_2026_claim_proposals). 2026-06-07:
    # boot-init so the /state-of-2026 landing + /r/<token> attribution
    # proxy + daily claims evolver cron can write without lazy creation
    # on the hot path.
    try:
        from routes.state_of_2026_live import init_state_of_2026_tables as _is26t
        _is26t()
    except Exception as _s26te:
        logger.warning("state_of_2026 table init skipped: %s", _s26te)

    # Media comment engagement loop (2026-06-07): media_comment_engagement_log
    # table. Required for the LinkedIn comment poll cron at 9/21 UTC + flush
    # at 13/1 UTC + the /api/v1/admin/media/comment-engagement/* endpoints.
    # CRITICAL for Monday's State of 2026 launch comment storm.
    try:
        from routes.media_comment_engagement import init_comment_engagement_tables as _ice
        _ice()
    except Exception as _icee:
        logger.warning("media comment engagement table init skipped: %s", _icee)

    # Multi-platform amplifier (2026-06-07): multiplatform_amplifier_log table.
    # Required by the master fan-out dispatcher (LinkedIn → Bluesky + Twitter
    # + Mastodon + HN semi-auto) for the State of 2026 launch and any future
    # cross-posts. Same defensive boot-init pattern as the other media-loop
    # modules — fail-soft so a missing brand-new table never blanks
    # content_publisher.
    try:
        from routes.multiplatform_amplifier import init_amplifier_tables as _ima
        _ima()
    except Exception as _imae:
        logger.warning("multiplatform amplifier table init skipped: %s", _imae)

    # Brain feature proposer (2026-06-07): adds feedback_submissions.
    # brain_proposal_pr_url + brain_proposal_cluster_id columns +
    # creates brain_feature_proposal_log table. Idempotent
    # ALTER ADD COLUMN IF NOT EXISTS + CREATE TABLE IF NOT EXISTS.
    # Required by the twice-daily proposer cron at 15/3 UTC that pulls
    # feedback_submissions (last 30d, MEDIUM/HIGH), clusters by theme,
    # asks Claude for a 200-word feature spec, and opens a DRAFT PR
    # stub for each cluster of 3+ users. Same defensive boot-init
    # pattern — feature_submissions table already created by the
    # feedback_forum hook earlier in this function, so this hook just
    # extends it.
    try:
        from routes.brain_feature_proposer import init_feature_proposer_columns as _ifpc
        _ifpc()
    except Exception as _ifpe:
        logger.warning("brain feature proposer table init skipped: %s", _ifpe)

    # Brain HOURLY micro-decision loop (2026-06-07): brain_micro_decisions
    # + brain_micro_budget_state tables. Companion to the L6 weekly
    # strategic synthesis — runs Haiku-backed 24/day inside a daemon
    # thread spawned from main.py. Daily $5 budget cap + context-hash
    # dedup. Same defensive boot-init pattern.
    try:
        from routes.brain_micro_cycle import init_micro_tables as _imct
        _imct()
    except Exception as _imcte:
        logger.warning("brain micro-cycle table init skipped: %s",
                       _imcte)

def _media_block_category(reason: str) -> str:
    r = (reason or "").lower()
    if "disclaimer" in r:                       return "ai_disclaimer_as_validation"
    if "duplicate" in r or "hook" in r:         return "duplicate_post"
    if "zero-stat" in r:                        return "zero_stat"
    if "stub" in r or "deal" in r:              return "value_less_deal_stub"
    if "low quality" in r or "editor rejected" in r: return "thin_or_offbrand"
    return "other"


@content_bp.route('/api/v1/media/self-critique', methods=['GET'])
def media_self_critique():
    """r66 EVOLVING-MEDIA visibility: what the publish gate REJECTED in the last
    7 days (by category), the reject rate vs what shipped, and the exact lessons
    now fed back into the generator's prompt. This is how the brain (and you) can
    SEE DC Hub Media learning from its own mistakes. Open read — quality
    telemetry, no secrets."""
    _labels = {
        "ai_disclaimer_as_validation": "quoting an AI disclaiming knowledge as 'validation'",
        "duplicate_post": "duplicating a recent post (same hook/story)",
        "zero_stat": "a headline stat that is 0/empty",
        "value_less_deal_stub": "a value-less deal stub (no $/MW)",
        "thin_or_offbrand": "thin/low-signal or off-brand content",
        "other": "other quality issues",
    }
    out = {"window_days": 7, "blocked_total": 0, "blocked_by_category": {},
           "published_7d": 0, "reject_rate": None, "recent_blocks": [],
           "lessons_fed_to_generator": []}
    with _db_conn() as conn:
        if conn is None:
            out["error"] = "no_db"
            return jsonify(out), 200
        try:
            with conn.cursor() as cur:
                rows = []
                try:
                    cur.execute("""SELECT reason, created_at FROM media_review_log
                        WHERE decision = 'blocked'
                          AND created_at > NOW() - INTERVAL '7 days'
                        ORDER BY created_at DESC LIMIT 500""")
                    rows = cur.fetchall() or []
                except Exception:
                    rows = []  # table may not exist until the first block
                cats = {}
                for row in rows:
                    reason = row.get('reason') if hasattr(row, 'get') else row[0]
                    cats[_media_block_category(reason)] = cats.get(_media_block_category(reason), 0) + 1
                out["blocked_total"] = len(rows)
                out["blocked_by_category"] = dict(sorted(cats.items(), key=lambda x: -x[1]))
                out["recent_blocks"] = [
                    {"category": _media_block_category(r.get('reason') if hasattr(r, 'get') else r[0]),
                     "reason": ((r.get('reason') if hasattr(r, 'get') else r[0]) or "")[:140]}
                    for r in rows[:8]]
                _smp = 0
                try:
                    cur.execute("""SELECT COUNT(*) FROM social_media_posts
                        WHERE status='published' AND publish_platform='linkedin'
                          AND published_at >= (NOW() - INTERVAL '7 days')""")
                    pr = cur.fetchone()
                    _smp = int((pr.get('count') if hasattr(pr, 'get') else pr[0]) or 0)
                except Exception:
                    pass
                # r-quad-visibility (2026-06-28): the 4x/day LinkedIn quad cron records
                # its successes ONLY to linkedin_quad_posts (linkedin_quad_daily._record),
                # never backfilling a social_media_posts row — so the query above was
                # structurally BLIND to the dominant LinkedIn publisher and pinned
                # published_7d=0 / reject_rate=1.0 even while ~11 posts/7d actually shipped.
                # Add the quad successes so this panel reflects reality. Disjoint tables:
                # social_media_posts = Bluesky/other; linkedin_quad_posts = the quad arm.
                _quad = 0
                try: conn.rollback()          # clear any aborted tx from the probe above
                except Exception: pass
                try:
                    cur.execute("""SELECT COUNT(*) FROM linkedin_quad_posts
                        WHERE success = TRUE
                          AND posted_at >= (NOW() - INTERVAL '7 days')""")
                    qr = cur.fetchone()
                    _quad = int((qr.get('count') if hasattr(qr, 'get') else qr[0]) or 0)
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
                out["published_7d"] = _smp + _quad
        finally:
            try: conn.close()
            except Exception: pass
    _tot = out["blocked_total"] + out["published_7d"]
    out["reject_rate"] = round(out["blocked_total"] / _tot, 3) if _tot else None
    out["lessons_fed_to_generator"] = [
        f"{n}× {_labels.get(k, k)}" for k, n in out["blocked_by_category"].items()]
    return jsonify(out), 200


@content_bp.route('/api/admin/content/stats', methods=['GET'])
def content_stats():
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    with _db_conn() as conn:
        cur = conn.cursor()
        stats = {'draft': 0, 'approved': 0, 'published': 0, 'rejected': 0, 'published_today': 0}
        # 2026-08-24: this endpoint 500'd on EVERY call. Measured live, three
        # independent faults, all of which had to go together:
        #   (1) `cur.fetchone()[0]` — _get_db() hands out a RealDictCursor, so
        #       subscripting by 0 raises KeyError: 0. That is verbatim the live
        #       response body, {"error":"0"}. It blew up on the FIRST query, so
        #       nothing after it had ever executed.
        #   (2) press_releases has no `status` column at all — it carries
        #       `published` (boolean) — so every status query in that table's
        #       iteration raised UndefinedColumn.
        #   (3) press_releases.published_at is ALREADY timestamptz, and
        #       LIKE/ILIKE on timestamptz is `operator does not exist`. The
        #       shared loop could not have worked for both tables.
        # Each table is now asked only what its own schema can answer.
        day, next_day = _utc_day_bounds()
        for status_val in ['draft', 'approved', 'published', 'rejected']:
            cur.execute("SELECT COUNT(*) AS n FROM social_media_posts WHERE status = %s",
                        (status_val,))
            stats[status_val] += _scalar(cur)
        cur.execute("""SELECT COUNT(*) AS n FROM social_media_posts
                        WHERE status = 'published'
                          AND published_at >= %s AND published_at < %s""",
                    (day, next_day))
        stats['published_today'] += _scalar(cur)
        cur.execute("SELECT COUNT(*) AS n FROM press_releases WHERE published IS TRUE")
        stats['published'] += _scalar(cur)
        cur.execute("""SELECT COUNT(*) AS n FROM press_releases
                        WHERE published IS TRUE
                          AND published_at >= %s AND published_at < %s""",
                    (day, next_day))
        stats['published_today'] += _scalar(cur)
        # 2026-07-31: DB-first like the publish paths — the badge used to read
        # only the env var and said "disconnected" while the refresh-cron's DB
        # token was posting fine (and vice versa: stale env showed connected).
        linkedin_connected = bool(_li_access_token())
    return jsonify({'stats': stats, 'linkedin_connected': linkedin_connected})

@content_bp.route('/api/admin/content-queue', methods=['GET'])
def content_queue():
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    status_filter = request.args.get('status', 'draft')
    content_type = request.args.get('type', 'social')
    platform_filter = request.args.get('platform', '')
    page = max(1, int(request.args.get('page', 1)))
    limit = min(50, max(1, int(request.args.get('limit', 10))))
    offset = (page - 1) * limit
    with _db_conn() as conn:
        cur = conn.cursor()
        if content_type == 'press':
            # 2026-08-24: this branch returned HTTP 500 on EVERY call —
            #   {"error": "column \"status\" does not exist"}
            # It named FOUR columns press_releases does not have: status,
            # content, publish_platform, approved_at. The live columns,
            # measured on the replica the same day, are: id, title, summary,
            # source, source_url, category, published_date, featured,
            # created_at, slug, date, subheadline, body, meta_description,
            # published (boolean), published_at (timestamptz).
            #
            # The queue speaks four statuses; this table can express two, so
            # the mapping is STATED rather than guessed:
            #   published -> published IS TRUE       (153 of 196 rows today)
            #   draft     -> published IS NOT TRUE   (43; the column is
            #                nullable, so NOT TRUE rather than = FALSE)
            #   approved  -> press_releases has no approval step at all
            #   rejected  -> nor a rejection one
            # approved/rejected return an EMPTY page, not a plausible one.
            # Listing drafts under "approved" would mis-populate an approval
            # queue and listing published rows under "rejected" would invert
            # its meaning; an empty page is the honest answer to a question
            # this table cannot be asked.
            if status_filter == 'published':
                base_query = "FROM press_releases WHERE published IS TRUE"
            elif status_filter == 'draft':
                base_query = "FROM press_releases WHERE published IS NOT TRUE"
            else:
                base_query = "FROM press_releases WHERE FALSE"
            if platform_filter:
                # No platform column and no platform concept — a press release
                # ships to the site, not to a social account. Any platform
                # filter therefore selects nothing.
                base_query += " AND FALSE"
            cur.execute(f"SELECT COUNT(*) AS n {base_query}")
            total = _scalar(cur)   # RealDictCursor: [0] raises KeyError: 0
            # approved_at and og_image are deliberately NOT selected: the row
            # loop below already reads both through an `in r.keys()` guard and
            # yields None, which is the truth for press. E'...' (not '...') so
            # the separator is two real newlines rather than two backslash-n
            # literals, which is what the old string produced under
            # standard_conforming_strings. COALESCE because `||` with a NULL
            # operand yields NULL, which would blank the title too.
            cur.execute(
                "SELECT id, 'press' AS type,"
                " title || E'\\n\\n' || COALESCE(body, '') AS content,"
                " CASE WHEN published IS TRUE THEN 'published'"
                "      ELSE 'draft' END AS status,"
                " '' AS publish_platform, created_at, published_at "
                f"{base_query} ORDER BY created_at DESC LIMIT %s OFFSET %s",
                [limit, offset])
        else:
            base_query = "FROM social_media_posts WHERE status = %s"
            params = [status_filter]
            if platform_filter:
                base_query += " AND platform = %s"
                params.append(platform_filter)
            cur.execute(f"SELECT COUNT(*) {base_query}", params)
            total = _scalar(cur)   # RealDictCursor: [0] raises KeyError: 0
            # 2026-08-24: social_media_posts carries FOUR timestamp types —
            # approved_at timestamptz, created_at/posted_at/scheduled_at
            # `timestamp`, published_at text. Coalescing posted_at with
            # published_at raises `COALESCE types timestamp without time
            # zone and text cannot
            # be matched` and always was; it only became visible once the
            # RealDictCursor KeyError above it was fixed, because that failed
            # first. ::text on both arms is correct whatever either column
            # becomes, so a later tranche cannot break it again.
            cur.execute(f"SELECT id, 'social' as type, content, status, platform as publish_platform, created_at, COALESCE(posted_at::text, published_at::text) as published_at, approved_at, og_image {base_query} ORDER BY created_at DESC LIMIT %s OFFSET %s", params + [limit, offset])
        rows = cur.fetchall()
        items = []
        for r in rows:
            # DCHUB_LI_CARDS (2026-07-31): surface the branded card in the
            # approval loop. og_image = a producer's explicit card (wins at
            # publish); card_url = what will actually attach — og_image if
            # set, else the on-demand stat card rendered from this row's own
            # headline metric (routes/media_card.py). None → the post ships
            # on the r64/ARTICLE/text path, exactly as before.
            _og_img = r['og_image'] if 'og_image' in r.keys() else None
            _card_url = _og_img
            # type guard: the card endpoint reads social_media_posts by id —
            # a press_releases id must never be composed into that URL.
            if not _card_url and r['type'] == 'social':
                try:
                    if _media_card_lead(r['content']):
                        _card_url = f"/api/v1/media/card/{r['id']}.png"
                except Exception:
                    _card_url = None
            items.append({
                'id': r['id'],
                'type': r['type'],
                'content': r['content'],
                'status': r['status'],
                'publish_platform': r['publish_platform'],
                'created_at': r['created_at'],
                'published_at': r['published_at'],
                'approved_at': r['approved_at'] if 'approved_at' in r.keys() else None,
                'og_image': _og_img,
                'card_url': _card_url,
            })
    return jsonify({'items': items, 'total': total})

# ── Content actions: the table is chosen by the CALLER'S DECLARED TYPE ──
#
# `press_releases.id` and `social_media_posts.id` are INDEPENDENT sequences over
# the same integers. Measured on the live replica 2026-08-24: 87 of 196 press
# ids also exist as social ids. So the old default — `request.args.get('type',
# 'social')` — did not mean "social", it meant "whatever row happens to carry
# this integer in social_media_posts". Approving press id 117 ("86 AI agents
# queried DC Hub's live power data…") ran
#     UPDATE social_media_posts SET status='approved' WHERE id=117
# against an unrelated PUBLISHED linkedin post, and the UI reported success.
# The 109 press ids with no social twin failed the other way: rowcount 0 -> 404
# -> "Failed to approve content", with the real row untouched and unexplained.
_CONTENT_TABLES = {'social': 'social_media_posts', 'press': 'press_releases'}

# Live press_releases columns (measured 2026-08-24): id, title, summary, source,
# source_url, category, published_date, featured, created_at, slug, date,
# subheadline, body, meta_description, published, published_at. There is no
# status, no approved_at and no content column — a press release is published
# via the `published` boolean, never approved. So this queue's approve/reject/
# edit model does not describe a press release at all. Aimed at press_releases
# the social UPDATE raises UndefinedColumn (a 500); aimed at social_media_posts
# because `type` was omitted it corrupts an unrelated post. Say so instead of
# doing either.
_PRESS_NOT_ACTIONABLE_DETAIL = (
    "press_releases has no status/approved_at/content column — a press release "
    "is published via the `published` boolean, not approved. This queue's "
    "approve/reject/edit model does not apply to it."
)


def _resolve_content_table(cur, item_id, declared_type):
    """Return (table, error_response); exactly one of the two is None.

    The caller's DECLARED type is authoritative — an action never touches a
    table other than the one matching it. With no declared type we resolve by
    which table actually holds the id, and REFUSE when both do: there is no safe
    way to guess which row the operator was looking at.
    """
    if declared_type:
        table = _CONTENT_TABLES.get(declared_type)
        if table is None:
            return None, (jsonify({
                'success': False,
                'error': f"unknown content type {declared_type!r}",
                'expected': sorted(_CONTENT_TABLES),
            }), 400)
        return table, None

    holders = []
    for kind in sorted(_CONTENT_TABLES):
        cur.execute(f"SELECT 1 FROM {_CONTENT_TABLES[kind]} WHERE id = %s", (item_id,))
        if cur.fetchone() is not None:
            holders.append(kind)
    if len(holders) > 1:
        return None, (jsonify({
            'success': False,
            'error': 'ambiguous id — pass ?type= to say which row you mean',
            'id': item_id,
            'found_in': holders,
        }), 409)
    if not holders:
        return None, (jsonify({'success': False, 'error': 'Not found'}), 404)
    return _CONTENT_TABLES[holders[0]], None


def _press_not_actionable(action):
    return jsonify({
        'success': False,
        'type': 'press',
        'error': f"press releases have no {action} step",
        'detail': _PRESS_NOT_ACTIONABLE_DETAIL,
    }), 400


@content_bp.route('/api/admin/content/<int:item_id>/approve', methods=['POST'])
def content_approve(item_id):
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    now = datetime.utcnow().isoformat() + 'Z'
    with _db_conn() as conn:
        cur = conn.cursor()
        table, err = _resolve_content_table(cur, item_id, request.args.get('type'))
        if err is not None:
            return err
        if table == 'press_releases':
            return _press_not_actionable('approve')
        # Table named LITERALLY, never interpolated: the only row this branch
        # can reach is a social one, whatever the id collides with.
        cur.execute("UPDATE social_media_posts SET status = 'approved', approved_at = %s WHERE id = %s",
                    (now, item_id))
        if cur.rowcount == 0:
            return jsonify({'success': False, 'error': 'Not found'}), 404
        conn.commit()
    return jsonify({'success': True})


@content_bp.route('/api/admin/content/<int:item_id>/reject', methods=['POST'])
def content_reject(item_id):
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    with _db_conn() as conn:
        cur = conn.cursor()
        table, err = _resolve_content_table(cur, item_id, request.args.get('type'))
        if err is not None:
            return err
        if table == 'press_releases':
            return _press_not_actionable('reject')
        cur.execute("UPDATE social_media_posts SET status = 'rejected' WHERE id = %s", (item_id,))
        if cur.rowcount == 0:
            return jsonify({'success': False, 'error': 'Not found'}), 404
        conn.commit()
    return jsonify({'success': True})


@content_bp.route('/api/admin/content/<int:item_id>/edit', methods=['POST'])
def content_edit(item_id):
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(force=True)
    new_content = data.get('content', '')
    auto_approve = data.get('auto_approve', False)
    now = datetime.utcnow().isoformat() + 'Z'
    with _db_conn() as conn:
        cur = conn.cursor()
        table, err = _resolve_content_table(cur, item_id, request.args.get('type'))
        if err is not None:
            return err
        if table == 'press_releases':
            # Beyond the missing columns: the press queue composes
            # `title || E'\n\n' || body` into one `content` string, so writing an
            # edited blob back to `body` duplicates the title into the body. A
            # press editor needs its own route with separate title/subheadline/
            # body fields; that route does not exist yet, and guessing the
            # inverse of another module's composition silently corrupts a
            # published document.
            return _press_not_actionable('edit')
        if auto_approve:
            cur.execute("UPDATE social_media_posts SET content = %s, status = 'approved', approved_at = %s WHERE id = %s",
                        (new_content, now, item_id))
        else:
            cur.execute("UPDATE social_media_posts SET content = %s WHERE id = %s", (new_content, item_id))
        if cur.rowcount == 0:
            return jsonify({'success': False, 'error': 'Not found'}), 404
        conn.commit()
    # `approved` is reported, not assumed: the UI used to say "edited & approved"
    # on every success, including the paths that approve nothing.
    return jsonify({'success': True, 'approved': bool(auto_approve)})

def _extract_og_image_url(page_url):
    """r51 (2026-05-29): scrape <meta property="og:image"> from a URL.

    Returns the absolute og:image URL string or None. Best-effort —
    any failure (network, HTML parse, missing tag) returns None so the
    caller falls back to the ARTICLE-share path (LinkedIn does its own
    OG scrape downstream).

    We need this because the LinkedIn /v2/ugcPosts ARTICLE share has
    a flaky og:image scrape (5 recent posts shipped without any image
    despite valid og:image tags on dchub.cloud/dcpi/<slug>). The fix
    is to FETCH the image server-side and ATTACH the binary directly
    via /rest/images, which LinkedIn renders 100% of the time.
    """
    if not page_url:
        return None
    try:
        import re as _re
        from urllib.parse import urljoin as _urljoin
        r = requests.get(page_url,
                          headers={'User-Agent': 'DCHub-LinkedInPublisher/1.0'},
                          timeout=10, allow_redirects=True)
        if r.status_code != 200:
            return None
        html = r.text[:200_000]  # cap to first 200KB; meta tags are in <head>
        # Match either property="og:image" or name="og:image", and either
        # attribute order (content="..." first vs property="..." first).
        m = _re.search(
            r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            html, _re.IGNORECASE)
        if not m:
            m = _re.search(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
                html, _re.IGNORECASE)
        if not m:
            return None
        img_url = m.group(1).strip()
        if not img_url.startswith(('http://', 'https://')):
            img_url = _urljoin(page_url, img_url)
        return img_url
    except Exception:
        return None


# 2026-07-11 (LinkedIn queue-drain audit): valid image magic numbers. The CF
# worker's failover layer was serving og-card PNGs whose bytes had been
# text-decoded upstream — every non-ASCII byte replaced with the UTF-8
# replacement char (b'\xef\xbf\xbd'), so the PNG signature read
# b'\xef\xbf\xbdPNG' instead of b'\x89PNG'. Those bytes pass the size check,
# upload fine (PUT 201), then die ASYNC in LinkedIn's processor
# (PROCESSING_FAILED), burning 3 full re-init/re-PUT/poll retries (~40-60s and
# LinkedIn image-API quota) per attempt — verified live against
# /dcpi/og/papillion.png on 2026-07-11. Reject non-images BEFORE upload so the
# publisher falls straight through to the next (valid) image source.
_IMAGE_MAGIC_PREFIXES = (
    b'\x89PNG\r\n\x1a\n',   # PNG
    b'\xff\xd8\xff',        # JPEG
    b'GIF87a', b'GIF89a',   # GIF
)


def _looks_like_image_bytes(data) -> bool:
    """True when `data` starts with a PNG/JPEG/GIF/WebP signature."""
    if not data or len(data) < 12:
        return False
    if any(data.startswith(m) for m in _IMAGE_MAGIC_PREFIXES):
        return True
    # WebP: RIFF....WEBP
    return data[:4] == b'RIFF' and data[8:12] == b'WEBP'


def _fetch_image_bytes_for_linkedin(image_url):
    """r51 (2026-05-29): fetch image bytes for LinkedIn asset upload.

    Returns bytes (or None). Defensive size cap — LinkedIn rejects
    images <1KB (transparent gif fallbacks) and >5MB. Pattern lifted
    from routes/linkedin_quad_daily.py:_fetch_image_bytes (proven
    working in the 4×/day quad publisher).

    2026-07-11: also validates magic bytes — a 200 image/png response whose
    body is NOT a real image (the worker-failover mojibake corruption) is
    rejected here instead of dying async in LinkedIn's image processor.
    """
    if not image_url:
        return None
    try:
        r = requests.get(image_url,
                          headers={'User-Agent': 'DCHub-LinkedInPublisher/1.0'},
                          timeout=15, allow_redirects=True)
        if r.status_code != 200:
            return None
        data = r.content
        if not (1000 < len(data) < 5_000_000):
            return None
        if not _looks_like_image_bytes(data):
            logger.warning(
                "image bytes from %s are not a valid PNG/JPEG/GIF/WebP "
                "(first bytes=%r) — likely text-decoded/corrupt upstream "
                "(worker-failover mojibake); skipping upload", image_url,
                data[:8])
            return None
        return data
    except Exception:
        return None


def _upload_image_to_linkedin(image_bytes, access_token, org_id):
    """r51 (2026-05-29): upload binary image to LinkedIn, return image URN.

    Uses the MODERN /rest/images?action=initializeUpload flow (the
    same flow linkedin_poster.post_to_linkedin r50 uses for the
    /rest/posts endpoint). Returns urn:li:image:* on success, or None
    on any failure (caller falls back to legacy ARTICLE share — image
    upload is best-effort, never blocks the post).

    Two-step:
      1) POST /rest/images?action=initializeUpload → {uploadUrl, image URN}
      2) PUT bytes to uploadUrl
    """
    if not image_bytes or not access_token or not org_id:
        return None
    import urllib.parse as _up
    author = f"urn:li:organization:{org_id}"
    init_headers = {
        'Authorization': f'Bearer {access_token}',
        'LinkedIn-Version': '202601',
        'X-Restli-Protocol-Version': '2.0.0',
        'Content-Type': 'application/json',
    }
    s_headers = {'Authorization': f'Bearer {access_token}',
                 'LinkedIn-Version': '202601',
                 'X-Restli-Protocol-Version': '2.0.0'}

    def _one_attempt():
        """init + PUT + poll-to-AVAILABLE. Returns one of:
           ('OK', urn)      — image is AVAILABLE, safe to attach
           ('RETRY', None)  — transient failure (PUT hiccup / PROCESSING_FAILED /
                              processing timeout); a fresh re-upload may succeed
           ('GIVEUP', None) — config/auth/shape error; retrying won't help
        """
        try:
            init_resp = requests.post(
                'https://api.linkedin.com/rest/images?action=initializeUpload',
                headers=init_headers,
                json={'initializeUploadRequest': {'owner': author}},
                timeout=15,
            )
            if init_resp.status_code not in (200, 201):
                logger.warning("r51 image initializeUpload failed: %s %s",
                               init_resp.status_code, init_resp.text[:200])
                return ('GIVEUP', None)
            v = (init_resp.json() or {}).get('value', {})
            upload_url = v.get('uploadUrl')
            image_urn = v.get('image')
            if not (upload_url and image_urn):
                logger.warning("r51 init response missing uploadUrl/image: %s", v)
                return ('GIVEUP', None)
            put_resp = requests.put(
                upload_url,
                headers={'Authorization': f'Bearer {access_token}'},
                data=image_bytes, timeout=30,
            )
            if put_resp.status_code not in (200, 201):
                logger.warning("r51 image PUT failed: %s %s",
                               put_resp.status_code, put_resp.text[:200])
                return ('RETRY', None)
            # WAIT for AVAILABLE — LinkedIn won't PUBLISH a post whose image is
            # still WAITING_UPLOAD/PROCESSING (it returns a share urn that 404s
            # and never hits the feed — this silently killed content_publisher's
            # posts for weeks, racing image processing ~2s to AVAILABLE).
            status_url = ("https://api.linkedin.com/rest/images/"
                          + _up.quote(image_urn, safe=''))
            for _ in range(10):  # ≤~10s
                try:
                    sr = requests.get(status_url, headers=s_headers, timeout=10)
                    st = (sr.json() or {}).get('status') if sr.status_code == 200 else None
                except Exception:
                    st = None
                if st == 'AVAILABLE':
                    return ('OK', image_urn)
                if st in ('PROCESSING_FAILED', 'FAILED'):
                    # NOT a format problem — LinkedIn's processor fails valid
                    # PNGs intermittently; a fresh re-upload usually succeeds.
                    logger.warning("r-img-ready: image %s %s — will retry",
                                   image_urn, st)
                    return ('RETRY', None)
                time.sleep(1)
            logger.warning("r-img-ready: image %s not AVAILABLE after wait — "
                           "will retry", image_urn)
            return ('RETRY', None)
        except Exception as e:
            logger.warning("r51 image upload exception: %s", e)
            return ('GIVEUP', None)

    # Retry transient PROCESSING_FAILED/timeouts up to 3x (each retry re-inits +
    # re-PUTs — a failed image asset can't be reused). On persistent failure
    # return None so the caller falls back to a text/article share, which
    # publishes reliably (and still gets a rich card via LinkedIn's OG-scrape).
    for _try in range(3):
        outcome, urn = _one_attempt()
        if outcome == 'OK':
            return urn
        if outcome == 'GIVEUP':
            return None
    logger.warning("r-img-ready: image upload failed after 3 attempts — "
                   "falling back to text/article share")
    return None


def _og_today_slug_for(article_url):
    """r64 (2026-05-30): derive a slug for the guaranteed OG fallback card
    https://dchub.cloud/api/v1/og/today/<slug>.png.

    The card endpoint (routes/og_cards.py:og_card) NEVER 404s — an unknown
    slug renders the branded _draw_fallback card — so any slug yields a
    valid 1200x630 PNG. We still try to reuse the post's real slug (last
    path segment of article_url, e.g. /news/<slug>, /dcpi/<slug>,
    /markets/<slug>) so a matching press_releases row produces the richer
    per-story card. With no URL we use a stable constant.
    """
    default = 'dchub-intelligence'
    if not article_url:
        return default
    try:
        import re as _re_slug
        from urllib.parse import urlparse as _urlparse
        path = (_urlparse(article_url).path or '').rstrip('/')
        seg = path.rsplit('/', 1)[-1] if path else ''
        # Strip a trailing .png/.html etc. and keep only slug-safe chars.
        seg = seg.split('?', 1)[0].split('#', 1)[0]
        if '.' in seg:
            seg = seg.rsplit('.', 1)[0]
        seg = _re_slug.sub(r'[^a-zA-Z0-9\-]+', '-', seg).strip('-').lower()
        return seg or default
    except Exception:
        return default


def _is_recent_linkedin_duplicate(content_text, days=7):
    """True if this EXACT post body already shipped to LinkedIn within `days`.
    Catches the repeat-post bug (same content fired 3-4x in a day — the
    class-based dedup misses it) regardless of which generator/path produced it.
    Matches on the whitespace-normalized first 80 chars (what distinguishes a
    post; the date IS in daily-intelligence openers so different days differ).
    Fail-OPEN — a check error never blocks a legit post."""
    try:
        norm = ' '.join((content_text or '').split())
        if len(norm) < 25:
            return False
        key = norm[:80]
        with _db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM linkedin_posts "
                " WHERE posted_at > NOW() - make_interval(days => %s) "
                "   AND COALESCE(status,'') = 'success' "
                "   AND LEFT(regexp_replace(btrim(COALESCE(content,'')), '\\s+', ' ', 'g'), 80) = %s "
                " LIMIT 1",
                (int(days), key))
            return cur.fetchone() is not None
    except Exception as e:
        logger.warning("[LinkedIn] dup-check failed (fail-open): %s", e)
        return False


def _li_access_token():
    """LinkedIn access token for every content_publisher publish path —
    DB-FIRST via linkedin_poster._get_valid_token() (the self-sustaining token
    the proactive refresh cron maintains; routes/linkedin_token_reset.py),
    falling back to the LINKEDIN_ACCESS_TOKEN env var.

    2026-07-31: these paths read ONLY the env var, which goes stale silently —
    the drain 401'd post 105426 (EXPIRED_ACCESS_TOKEN) while a healthy DB
    token sat 13 days from expiry with refresh_token + cron in place.
    linkedin_poster's own scheduled posts never hit this because they already
    source DB-first. Lazy import (linkedin_poster lazy-imports FROM this
    module, so a top-level import would be circular) and fail-open to the env
    var, so a poster-module problem can never make publishing darker than the
    old env-only behaviour."""
    try:
        from linkedin_poster import _get_valid_token as _gvt
        tok = (_gvt() or '').strip()
        if tok:
            return tok
    except Exception as e:
        logger.warning("[LinkedIn] DB-first token lookup failed (%s) — "
                       "falling back to LINKEDIN_ACCESS_TOKEN env var", e)
    return os.environ.get('LINKEDIN_ACCESS_TOKEN', '').strip()


# 2026-07-11 (LinkedIn queue-drain audit): _post_to_linkedin has TWO distinct
# failure shapes — (False, {'error': <gate>, 'reason': ...}) for its own
# editorial-gate refusals (quality / policy / duplicate), and (False, "LinkedIn
# API error ...") strings for real transport/API errors. The drain used to
# mark BOTH 'failed', so the queue-flood repeats that the duplicate gate
# correctly refused showed up as 21 "publish failures" in 7d (all
# market-verdict posts) while auth/API were perfectly healthy. Gate refusals
# are content-intrinsic — retrying the same row can NEVER succeed — so they
# belong in the r78 terminal 'rejected' state, not 'failed'.
_LI_GATE_ERRORS = ('content_quality_gate', 'editorial_policy_gate',
                   'duplicate_gate')


def _li_gate_refusal(result):
    """Return 'gate: reason' when a (False, result) from _post_to_linkedin is
    an editorial-gate refusal (content-intrinsic; never passes on retry),
    else None (a real API/transport error that deserves status='failed')."""
    if isinstance(result, dict) and result.get('error') in _LI_GATE_ERRORS:
        return f"{result.get('error')}: {str(result.get('reason') or '')[:200]}"
    return None


def _post_rest_image_share(content_text, access_token, org_id, image_urn,
                           title=None, alt=None):
    """POST a modern /rest/posts IMAGE share for an already-uploaded (and
    AVAILABLE) /rest/images asset. Returns (ok, urn_or_error_string).

    Used by the DCHUB_LI_CARDS stat-card block in _post_to_linkedin; the r51 /
    r64 blocks predate this helper and keep their proven inline copies. The
    commentary goes through escape_li_commentary exactly like those blocks —
    a card never changes the text that ships."""
    headers = {
        'Authorization': f'Bearer {access_token}',
        'LinkedIn-Version': '202601',
        'X-Restli-Protocol-Version': '2.0.0',
        'Content-Type': 'application/json',
    }
    payload = {
        'author': f'urn:li:organization:{org_id}',
        'commentary': escape_li_commentary(content_text),
        'visibility': 'PUBLIC',
        'distribution': {
            'feedDistribution': 'MAIN_FEED',
            'targetEntities': [],
            'thirdPartyDistributionChannels': [],
        },
        'lifecycleState': 'PUBLISHED',
        'content': {
            'media': {
                'id': image_urn,
                'title': (title or 'DC Hub')[:200],
                'altText': (alt or title
                            or 'DC Hub data center intelligence')[:300],
            }
        },
    }
    try:
        r = requests.post('https://api.linkedin.com/rest/posts',
                          json=payload, headers=headers, timeout=20)
        if r.status_code in (200, 201):
            return True, (r.headers.get('x-restli-id')
                          or r.headers.get('X-LinkedIn-Id')
                          or 'posted-with-image')
        return False, f'/rest/posts {r.status_code}: {r.text[:200]}'
    except Exception as e:
        return False, f'/rest/posts exception: {e}'


def _post_to_linkedin(content_text, access_token, article_url=None,
                       article_title=None, article_description=None,
                       article_thumbnail_url=None):
    """Post to DC Hub LinkedIn Company Page (org ID: 110894959).

    Phase HH (2026-05-13): added optional article params. When supplied,
    LinkedIn renders a rich link-card preview with thumbnail + title +
    description (much higher engagement than plain text). Without them,
    falls back to text-only share (legacy behaviour).

    LinkedIn extracts the link-card image from the URL's og:image meta
    tag — buildPressReleaseHtml in _worker.js points that at the
    /api/v1/og/today/<slug>.png dynamic card endpoint.

    r62 (2026-05-29): respects LINKEDIN_PUBLISHER_DRY_RUN env flag.
    When set to "1"/"true"/"yes", the LinkedIn API call is skipped and
    a synthetic urn:li:share:DRY_RUN:<ts> is returned. The caller
    treats this as success (marks the post 'published') so dry-run
    fully exercises the pipeline including dedup classification + row
    state transitions WITHOUT firing an actual share. Useful for:
      • verifying queue draining behaves correctly after a fix
      • previewing the rewritten content of legacy short posts
      • local-dev smoke tests
    Disable by unsetting the env var.

    r51 (2026-05-29): IMAGE-FIRST path. When article_url is present,
    we now try to fetch its og:image and UPLOAD it directly as a
    LinkedIn /rest/images asset, then attach via /rest/posts. That
    renders a proper image-card post (much higher engagement than the
    text-only or scraped-article shares user was seeing — 5 recent
    posts shipped imageless despite valid og:image tags). On ANY
    image failure we fall back to the previous /v2/ugcPosts ARTICLE
    share, which leans on LinkedIn's own OG scrape. So images are
    always best-effort — they never block the post going out.

    Kill-switch: LINKEDIN_ATTACH_IMAGES=0 disables image upload
    entirely (returns to pre-r51 behaviour). Default = enabled.
    """
    # ── Content-quality gate (2026-06-30) ─────────────────────────────────
    # NEVER ship an incomplete/amateur post. A publish-path bug once posted just
    # "Guam " (the market name, truncated before the analyst blurb) to the
    # company page. Refuse word-poor / fragment commentary — DC Hub Media is an
    # analyst, not a one-word headline. False return = non-fatal skip (logged),
    # so a bad payload is DROPPED, never shared.
    _ct = (content_text or '').strip()
    _wc = len(_ct.split())
    if _wc < 6 or len(_ct) < 25:
        logger.error("LINKEDIN QUALITY GATE: refused too-short post "
                     "(%d chars / %d words): %r", len(_ct), _wc, _ct[:120])
        return False, {"error": "content_quality_gate",
                       "reason": f"too short: {len(_ct)} chars / {_wc} words"}
    # ── Editorial-policy gate (2026-07-02, operator directive) ────────────
    # Runs HERE — at the single HTTP choke point — because guards that live
    # only in the queue-drain path (_should_skip_publish) are bypassed by the
    # direct posters (that's how a partner-bashing post escaped on 06-22).
    # Two hard rules, no env knob:
    #   1. Never disparage a peer AI platform (they are partners AND our
    #      distribution — their agents query DC Hub).
    #   2. Never LEAD with a downgrade. AVOID/CAUTION verdicts live on the
    #      product surfaces and subscriber alerts; the media feed messages
    #      positive results and enhancements, not doom commentary.
    _snippet = _disparages_partner(_ct)
    if _snippet:
        logger.error("LINKEDIN POLICY GATE: refused partner-disparaging post "
                     "(%r)", _snippet)
        try:
            _record_media_block('linkedin',
                                f'partner_disparage: {_snippet[:80]}', _ct)
        except Exception:
            pass
        return False, {"error": "editorial_policy_gate",
                       "reason": f"partner disparage: {_snippet[:80]}"}
    _lead = _ct[:200]
    if (_re_legacy.search(r'\bAVOID\b', _lead)
            or _re_legacy.search(
                r'(?i)\b(shifted to avoid|moved to avoid|downgraded to|'
                r'falls? to avoid|drops? to avoid)\b', _lead)):
        logger.error("LINKEDIN POLICY GATE: refused downgrade-lead post: %r",
                     _lead[:120])
        try:
            _record_media_block('linkedin', 'downgrade_lead', _ct)
        except Exception:
            pass
        return False, {"error": "editorial_policy_gate",
                       "reason": "leads with a downgrade (AVOID)"}
    # Forensic trail (2026-06-30): the "Guam " incident stored the FULL blurb but
    # rendered one word, with NO backend truncation found. Log the EXACT
    # commentary we hand to LinkedIn so the next occurrence is captured with
    # proof (rules backend in/out vs the LinkedIn render layer).
    logger.info("[LinkedIn] SENDING commentary: %d chars / %d words | %r",
                len(_ct), _wc, _ct[:110])
    # Exact-duplicate gate (2026-06-29): the class-based dedup misses same-day
    # repeats (e.g. the same blurb fired 3-4x in one day). Refuse content whose
    # normalized opening already shipped to LinkedIn within the window. Env knob
    # DCHUB_LINKEDIN_DUP_DAYS (default 7); set 0 to disable.
    try:
        _dup_days = int(os.environ.get('DCHUB_LINKEDIN_DUP_DAYS', '7') or '7')
    except Exception:
        _dup_days = 7
    if _dup_days > 0 and _is_recent_linkedin_duplicate(_ct, days=_dup_days):
        logger.warning("[LinkedIn] DUPLICATE GATE: skipping repost (same content "
                       "shipped within %dd): %r", _dup_days, _ct[:80])
        return False, {"error": "duplicate_gate",
                       "reason": f"already posted within {_dup_days} days"}
    _dry = (os.environ.get('LINKEDIN_PUBLISHER_DRY_RUN', '') or '').strip().lower()
    if _dry in ('1', 'true', 'yes', 'on'):
        _preview = (content_text or '')[:240].replace('\n', ' / ')
        # r51: surface what the image-attach path WOULD have done, so dry-run
        # also exercises the og:image resolution (catches "page has no
        # og:image" before the cron actually fires for real).
        _attach_dry = os.environ.get('LINKEDIN_ATTACH_IMAGES', '1').strip() != '0'
        if not _attach_dry:
            _img_preview = "image=skip(LINKEDIN_ATTACH_IMAGES=0)"
        else:
            # r64 (2026-05-30): mirror the live image-attach decision so dry-run
            # surfaces which source the real post WOULD use:
            #   1. og:image on article_url (existing image-first path), else
            #   2. the GUARANTEED /api/v1/og/today/<slug>.png branded card.
            # An image is now MANDATORY (no text-only NONE) unless even the
            # fallback card can't be fetched.
            _og = ((article_thumbnail_url or _extract_og_image_url(article_url))
                   if article_url else None)
            _card_lead_dry = (_media_card_lead(content_text)
                              if os.environ.get('DCHUB_LI_CARDS', '1').strip() != '0'
                              else None)
            if _og:
                _img_preview = f"image=would-attach-og({_og})"
            elif _card_lead_dry:
                # DCHUB_LI_CARDS (2026-07-31): the locally-rendered stat card
                # now outranks the fetched fallback card — mirror that here.
                _img_preview = ("image=would-attach-stat-card(headline="
                                f"{_card_lead_dry['headline']})")
            else:
                _slug = _og_today_slug_for(article_url)
                _fallback = f"https://dchub.cloud/api/v1/og/today/{_slug}.png"
                _img_preview = f"image=would-attach-fallback-card({_fallback})"
        logger.warning(
            "LINKEDIN_PUBLISHER_DRY_RUN active — NOT posting (would have sent: %s%s · %s)",
            _preview,
            "..." if len(content_text or '') > 240 else "",
            _img_preview,
        )
        return True, f"urn:li:share:DRY_RUN:{int(time.time())}"
    DCHUB_ORG_ID = (os.environ.get('LINKEDIN_ORG_ID', '110894959') or '110894959').strip()

    # r51: IMAGE-FIRST attempt (modern /rest/posts + /rest/images). Gated by
    # env so operator can disable instantly if LinkedIn API misbehaves.
    _attach_images = os.environ.get('LINKEDIN_ATTACH_IMAGES', '1').strip() != '0'
    if _attach_images and article_url:
        # 1. Resolve OG image — caller can pass article_thumbnail_url to skip
        #    the OG-scrape round-trip (used by press-release publisher which
        #    already knows /api/v1/og/today/<slug>.png).
        _og_url = (article_thumbnail_url
                    or _extract_og_image_url(article_url))
        if _og_url:
            _img_bytes = _fetch_image_bytes_for_linkedin(_og_url)
            if _img_bytes:
                _image_urn = _upload_image_to_linkedin(
                    _img_bytes, access_token, DCHUB_ORG_ID)
                if _image_urn:
                    # Modern /rest/posts shape — IMAGE attached, article_url
                    # also appears as a clickable hyperlink in the body text
                    # (LinkedIn linkifies URLs in `commentary` automatically).
                    _h_post = {
                        'Authorization': f'Bearer {access_token}',
                        'LinkedIn-Version': '202601',
                        'X-Restli-Protocol-Version': '2.0.0',
                        'Content-Type': 'application/json',
                    }
                    _payload = {
                        'author': f'urn:li:organization:{DCHUB_ORG_ID}',
                        'commentary': escape_li_commentary(content_text),
                        'visibility': 'PUBLIC',
                        'distribution': {
                            'feedDistribution': 'MAIN_FEED',
                            'targetEntities': [],
                            'thirdPartyDistributionChannels': [],
                        },
                        'lifecycleState': 'PUBLISHED',
                        'content': {
                            'media': {
                                'id': _image_urn,
                                'title': (article_title or 'DC Hub')[:200],
                                'altText': (article_description
                                              or article_title
                                              or 'DC Hub data center intelligence')[:300],
                            }
                        },
                    }
                    try:
                        _r = requests.post(
                            'https://api.linkedin.com/rest/posts',
                            json=_payload, headers=_h_post, timeout=20)
                        if _r.status_code in (200, 201):
                            _urn = (_r.headers.get('x-restli-id')
                                     or _r.headers.get('X-LinkedIn-Id')
                                     or 'posted-with-image')
                            logger.info(
                                "r51 LinkedIn IMAGE post succeeded: urn=%s "
                                "(article=%s og=%s)", _urn, article_url, _og_url)
                            return True, _urn
                        logger.warning(
                            "r51 /rest/posts (image) failed: %s %s — "
                            "falling through to ARTICLE share",
                            _r.status_code, _r.text[:200])
                    except Exception as _e:
                        logger.warning(
                            "r51 /rest/posts exception: %s — falling through", _e)
                else:
                    logger.info(
                        "r51: image upload returned no URN, falling through "
                        "to ARTICLE share for %s", article_url)
            else:
                logger.info(
                    "r51: couldn't fetch image bytes from %s, falling through",
                    _og_url)
        else:
            logger.info(
                "r51: no og:image found on %s, falling through to ARTICLE share",
                article_url)

    # ── DCHUB_LI_CARDS (2026-07-31): locally-rendered branded STAT CARD ────
    # Operator: "the linkedin posts are all texts." Bare drafts carry no
    # article_url, so the r51 image-first path above never fires for them, and
    # the r64 fallback below fetches its card from https://dchub.cloud — any
    # CF/origin hiccup ships the post bare. Render the 1200x627 stat card
    # IN-PROCESS instead (routes/media_card.py) from this post's own headline
    # metric: _media_card_lead reads the same _METRIC_PATTERNS the gate
    # scores, so card numbers are the text's numbers verbatim, never
    # recomputed. Runs AFTER every text gate above and sends the identical
    # escaped commentary, so text scoring/dedup cannot shift. Kill-switch
    # DCHUB_LI_CARDS=0 (LINKEDIN_ATTACH_IMAGES=0 still kills all images).
    # ANY failure — no metric in the text, render error, upload error, POST
    # error — falls through to r64 → ARTICLE → text-only: a card can never
    # block a post.
    if _attach_images and (os.environ.get('DCHUB_LI_CARDS', '1').strip() != '0'):
        _mc_lead = _media_card_lead(content_text)   # None on any parse issue
        if _mc_lead:
            _mc_bytes = None
            try:
                from routes.media_card import render_stat_card as _mc_render
                _mc_bytes = _mc_render(_mc_lead)
            except Exception as _mc_e:
                logger.warning("DCHUB_LI_CARDS: stat-card render failed (%s) "
                               "— falling through", _mc_e)
            if _mc_bytes and _looks_like_image_bytes(_mc_bytes) \
                    and 1000 < len(_mc_bytes) < 5_000_000:
                _mc_urn = _upload_image_to_linkedin(
                    _mc_bytes, access_token, DCHUB_ORG_ID)
                if _mc_urn:
                    _mc_alt = ("DC Hub stat card: " + _mc_lead['headline']
                               + (f" {_mc_lead['unit']}" if _mc_lead.get('unit') else ''))
                    _mc_ok, _mc_res = _post_rest_image_share(
                        content_text, access_token, DCHUB_ORG_ID, _mc_urn,
                        title=(article_title or _mc_lead.get('label') or 'DC Hub'),
                        alt=_mc_alt)
                    if _mc_ok:
                        logger.info(
                            "DCHUB_LI_CARDS stat-card post succeeded: urn=%s "
                            "(headline=%s)", _mc_res, _mc_lead['headline'])
                        return True, _mc_res
                    logger.warning("DCHUB_LI_CARDS: %s — falling through",
                                   _mc_res)
                else:
                    logger.warning("DCHUB_LI_CARDS: card upload returned no "
                                   "URN — falling through")

    # r64 (2026-05-30): MANDATORY-IMAGE fallback. Reaching here means the
    # image-first path did not attach an image (no article_url, no scrape-able
    # og:image, image fetch/upload/POST failed, OR the page — e.g.
    # /news/<slug> and bare DCPI/digest posts — simply has no og:image). Before
    # r64 those all fell to the text-only shareMediaCategory:NONE branch, which
    # is exactly the imageless LinkedIn posts the operator flagged. We now build
    # a GUARANTEED branded card from /api/v1/og/today/<slug>.png (og_cards.py
    # renders a valid 1200x630 PNG for ANY slug — unknown slugs get the DC Hub
    # _draw_fallback card, never a 404) and attach it via the SAME modern
    # /rest/posts media flow as the image-first block above. Only if even this
    # fallback can't be fetched/uploaded/posted do we fall through to NONE.
    #
    # Gated by the same LINKEDIN_ATTACH_IMAGES kill-switch (=0 → skip straight
    # to the legacy path, pre-r51 behaviour). DRY_RUN already returned above.
    if _attach_images:
        _fb_slug = _og_today_slug_for(article_url)
        # 2026-07-16: content-aware fallback card. Capability / platform / news
        # posts (e.g. the error-contract /news post the operator flagged) get the
        # branded DC Hub LEDGER data-card (live canonical stats, on-brand) instead
        # of the generic ai_hero stock photo. Market pages (/dcpi, /markets) keep
        # their DCPI card. Only hit when no better card was threaded (quad +
        # drumbeat carry their own content-specific data-cards and never reach here).
        _au = (article_url or '').lower()
        if (not _au) or any(seg in _au for seg in
                            ('/news/', '/whats-new', '/capabilities', '/docs/', '/connect', '/platforms')):
            _fallback = "https://dchub.cloud/api/v1/og/dynamic.png?style=data_card&kind=weekly_ledger"
        else:
            _fallback = f"https://dchub.cloud/api/v1/og/today/{_fb_slug}.png"
        _fb_bytes = _fetch_image_bytes_for_linkedin(_fallback)
        if _fb_bytes:
            _fb_urn = _upload_image_to_linkedin(
                _fb_bytes, access_token, DCHUB_ORG_ID)
            if _fb_urn:
                _h_post = {
                    'Authorization': f'Bearer {access_token}',
                    'LinkedIn-Version': '202601',
                    'X-Restli-Protocol-Version': '2.0.0',
                    'Content-Type': 'application/json',
                }
                _payload = {
                    'author': f'urn:li:organization:{DCHUB_ORG_ID}',
                    'commentary': escape_li_commentary(content_text),
                    'visibility': 'PUBLIC',
                    'distribution': {
                        'feedDistribution': 'MAIN_FEED',
                        'targetEntities': [],
                        'thirdPartyDistributionChannels': [],
                    },
                    'lifecycleState': 'PUBLISHED',
                    'content': {
                        'media': {
                            'id': _fb_urn,
                            'title': (article_title or 'DC Hub')[:200],
                            'altText': (article_description
                                          or article_title
                                          or 'DC Hub data center intelligence')[:300],
                        }
                    },
                }
                try:
                    _r = requests.post(
                        'https://api.linkedin.com/rest/posts',
                        json=_payload, headers=_h_post, timeout=20)
                    if _r.status_code in (200, 201):
                        _urn = (_r.headers.get('x-restli-id')
                                 or _r.headers.get('X-LinkedIn-Id')
                                 or 'posted-with-fallback-image')
                        logger.info(
                            "r64 LinkedIn FALLBACK-CARD post succeeded: urn=%s "
                            "(article=%s fallback=%s)",
                            _urn, article_url, _fallback)
                        return True, _urn
                    logger.warning(
                        "r64 /rest/posts (fallback card) failed: %s %s — "
                        "falling through to text-only NONE",
                        _r.status_code, _r.text[:200])
                except Exception as _e:
                    logger.warning(
                        "r64 /rest/posts (fallback card) exception: %s — "
                        "falling through to text-only NONE", _e)
            else:
                logger.warning(
                    "r64: fallback-card upload returned no URN (%s), "
                    "falling through to text-only NONE", _fallback)
        else:
            logger.warning(
                "r64: couldn't fetch fallback card bytes from %s, "
                "falling through to text-only NONE", _fallback)
    else:
        logger.info(
            "r64: LINKEDIN_ATTACH_IMAGES=0 — skipping mandatory-image "
            "fallback, using legacy text/ARTICLE path")

    # LEGACY PATH (pre-r51 behaviour, also r51 fallback when image upload
    # fails / is disabled / no URL in body). Builds an ARTICLE share that
    # leans on LinkedIn's own OG:image scrape — flaky but valid.
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
        'X-Restli-Protocol-Version': '2.0.0',
    }
    if article_url:
        # Rich link-card share — LinkedIn scrapes the URL for OG tags
        # (title, description, image) and renders a click-through card.
        # We provide hints; LinkedIn uses our values if og:* is missing.
        media_block = {
            "status": "READY",
            "originalUrl": article_url,
        }
        if article_title:
            media_block["title"] = {"text": article_title[:200]}
        if article_description:
            media_block["description"] = {"text": article_description[:300]}
        if article_thumbnail_url:
            # If we know the exact image, hint at it. LinkedIn still
            # scrapes the URL for og:image but this can be a tiebreaker.
            media_block["thumbnails"] = [{"url": article_thumbnail_url}]
        share_content = {
            "shareCommentary": {"text": content_text},
            "shareMediaCategory": "ARTICLE",
            "media": [media_block],
        }
    else:
        # Text-only fallback
        share_content = {
            "shareCommentary": {"text": content_text},
            "shareMediaCategory": "NONE",
        }
    post_body = {
        "author": f"urn:li:organization:{DCHUB_ORG_ID}",
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": share_content
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
        }
    }
    resp = requests.post('https://api.linkedin.com/v2/ugcPosts',
                          json=post_body, headers=headers, timeout=15)
    if resp.status_code in (200, 201):
        return True, resp.json().get('id', 'posted')
    return False, f"LinkedIn API error {resp.status_code}: {resp.text[:300]}"


def _delete_linkedin_share(share_urn, access_token):
    """Phase HH (2026-05-13): delete a previously-posted share. Used by
    the /repost-now endpoint when republishing today's press release
    with a new visual card. LinkedIn requires URL-encoding the URN."""
    import urllib.parse as _up
    encoded = _up.quote(share_urn, safe='')
    resp = requests.delete(
        f'https://api.linkedin.com/v2/ugcPosts/{encoded}',
        headers={
            'Authorization': f'Bearer {access_token}',
            'X-Restli-Protocol-Version': '2.0.0',
        },
        timeout=15,
    )
    if resp.status_code in (200, 204):
        return True, "deleted"
    return False, f"LinkedIn delete error {resp.status_code}: {resp.text[:300]}"


def _li_post_type_for(article_url):
    """Classify linkedin_posts.post_type from the article-URL section.

    The old code hardcoded 'auto_press' for EVERY mirror insert, which
    mislabeled DCPI / market-movement alerts (which link to /dcpi/<city> or
    /markets/<metro>) as press releases — that's why "auto_press" rows were
    showing market slugs like 'boise'/'calgary'. Still 'auto_'-prefixed so the
    existing `post_type LIKE 'auto_%'` filters (e.g. linkedin_poster's
    scheduled-poster dedupe) keep matching."""
    if not article_url:
        return 'auto_share'
    try:
        from urllib.parse import urlparse as _up
        path = (_up(article_url).path or '').lower()
    except Exception:
        return 'auto_share'
    if path.startswith('/dcpi/'):
        return 'auto_dcpi'
    if path.startswith('/markets/'):
        return 'auto_market'
    if path.startswith(('/news/', '/press/')):
        return 'auto_press'
    if '/facilit' in path:
        return 'auto_facility'
    return 'auto_share'


def _verify_linkedin_render_drift(urn, sent_text, access_token=None):
    """Render-drift probe (2026-06-29) — closes the "elusive root cause" gap
    flagged in commit dab60cc1.

    The "Guam " incident: the company page rendered ONE word ("Guam ") while
    BOTH the linkedin_posts row and the social_media_posts row held the FULL
    analyst sentence ("Guam (GPA) just shifted to AVOID …") plus a real share
    URN (urn:li:share:7477514527392047105). Every Python code path passes the
    full text to LinkedIn's `commentary`/`shareCommentary`, and no truncation
    was ever found in this backend — so the loss happened OUTSIDE Python, at
    LinkedIn's API/render layer or an out-of-repo worker.

    This probe fetches the just-created share BACK from LinkedIn by its URN and
    compares the rendered `commentary` char-for-char against what we sent. On a
    mismatch (rendered is typically a prefix/fragment of sent) it logs an ERROR
    with both strings and writes a row to linkedin_render_drift — so the NEXT
    occurrence is captured with hard proof instead of going cold.

    Design contract (all four matter):
      • OFF by default — runs only when LINKEDIN_RENDER_DRIFT_PROBE is truthy.
      • FAIL-SOFT — never raises; a successful post is never blocked or undone
        by a probe failure (caller ignores the return value).
      • ISOLATED DB write — uses its OWN short-lived connection so a bad INSERT
        cannot poison the caller's (already-successful) post transaction.
      • Reuses LINKEDIN_ACCESS_TOKEN + the LinkedIn-Version header exactly as the
        poster paths do.

    Returns a small dict for tests/observability (never consumed in prod).
    """
    out = {"ran": False, "drift": None, "reason": None}
    try:
        _flag = (os.environ.get('LINKEDIN_RENDER_DRIFT_PROBE', '') or '').strip().lower()
        if _flag not in ('1', 'true', 'yes', 'on'):
            out["reason"] = "disabled"
            return out
        # Only real shares are fetchable — skip DRY_RUN + non-urn sentinels
        # (caller already filters these, but double-guard so the probe is safe
        # to call from anywhere).
        if (not urn or not isinstance(urn, str)
                or not urn.startswith('urn:li:') or 'DRY_RUN' in urn):
            out["reason"] = "non_real_urn"
            return out
        token = (access_token
                 or _li_access_token() or '').strip()
        if not token:
            out["reason"] = "no_token"
            return out
        # Optional settle delay — LinkedIn's render can lag the create by a beat.
        # Default 0 (lightweight, non-blocking); operator can set a couple of
        # seconds while actively hunting the drift. Capped at 10s.
        try:
            _delay = float(os.environ.get('LINKEDIN_RENDER_DRIFT_PROBE_DELAY', '0') or '0')
        except (ValueError, TypeError):
            _delay = 0.0
        if _delay > 0:
            time.sleep(min(_delay, 10.0))
        import urllib.parse as _up
        api_ver = (os.environ.get('LINKEDIN_API_VERSION', '202601') or '202601').strip()
        headers = {
            'Authorization': f'Bearer {token}',
            'X-Restli-Protocol-Version': '2.0.0',
            'LinkedIn-Version': api_ver,
        }
        enc = _up.quote(urn, safe='')
        rendered = None
        # Primary: GET /rest/posts/{urn} — returns the post's `commentary`,
        # i.e. exactly what LinkedIn rendered from what we sent.
        try:
            resp = requests.get(f'https://api.linkedin.com/rest/posts/{enc}',
                                headers=headers, timeout=10)
            if resp.status_code == 200:
                rendered = (resp.json() or {}).get('commentary')
            else:
                out["reason"] = f"posts_status_{resp.status_code}: {resp.text[:160]}"
        except Exception as _e:
            out["reason"] = f"posts_exc: {str(_e)[:160]}"
        # Fallback: some surfaces answer on /rest/socialActions for the share's
        # social object. Commentary isn't guaranteed there, so best-effort only,
        # and only when /rest/posts gave us nothing to compare.
        if rendered is None:
            try:
                resp2 = requests.get(
                    f'https://api.linkedin.com/rest/socialActions/{enc}',
                    headers=headers, timeout=10)
                if resp2.status_code == 200:
                    d2 = resp2.json() or {}
                    rendered = (d2.get('commentary')
                                or (d2.get('message') or {}).get('text'))
            except Exception:
                pass
        if rendered is None:
            out["reason"] = out["reason"] or "no_rendered_commentary"
            logger.info("[LinkedIn drift probe] could not read back commentary "
                        "for %s (%s)", urn, out["reason"])
            return out
        out["ran"] = True
        sent = sent_text or ''
        rendered = rendered or ''
        # We compare against the RAW sent text, but LinkedIn returns commentary
        # in "little" format (hashtags as {hashtag|\#|tag}, reserved chars
        # escaped), so an exact mismatch is EXPECTED and benign. Only flag the
        # signature we actually care about: TRUNCATION — rendered is a strict,
        # meaningfully-shorter PREFIX of what we sent (the "Guam "/"(CAUTION)"
        # cut). Format-only differences are not drift.
        _is_prefix = bool(rendered) and sent.startswith(rendered)
        _is_trunc = _is_prefix and (len(sent) - len(rendered) > 8)
        if not _is_trunc:
            out["drift"] = False
            logger.info("[LinkedIn drift probe] OK — no truncation "
                        "(sent=%d rendered=%d, prefix=%s) for %s",
                        len(sent), len(rendered), _is_prefix, urn)
            return out
        # ── TRUNCATION DETECTED ──────────────────────────────────────────────
        out["drift"] = True
        logger.error(
            "[LinkedIn RENDER DRIFT] urn=%s sent_len=%d rendered_len=%d prefix=%s\n"
            "  SENT:     %r\n"
            "  RENDERED: %r",
            urn, len(sent), len(rendered), _is_prefix, sent, rendered)
        # Persist on an ISOLATED connection so a write error can never roll back
        # the caller's already-committed-or-about-to-commit successful post.
        try:
            with _db_conn() as _c:
                _cur = _c.cursor()
                _cur.execute("""
                    CREATE TABLE IF NOT EXISTS linkedin_render_drift (
                        id            SERIAL PRIMARY KEY,
                        urn           TEXT,
                        sent_text     TEXT,
                        rendered_text TEXT,
                        sent_len      INT,
                        rendered_len  INT,
                        detected_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )""")
                _cur.execute("""
                    INSERT INTO linkedin_render_drift
                        (urn, sent_text, rendered_text, sent_len, rendered_len)
                    VALUES (%s, %s, %s, %s, %s)""",
                    (urn, sent, rendered, len(sent), len(rendered)))
                _c.commit()
            logger.info("[LinkedIn drift probe] drift row written for %s", urn)
        except Exception as _e:
            logger.warning("[LinkedIn drift probe] drift detected but row-write "
                           "failed (urn=%s): %s", urn, _e)
        return out
    except Exception as _e:
        # Absolute backstop — the probe must NEVER surface to the post path.
        try:
            logger.warning("[LinkedIn drift probe] unexpected error "
                           "(fail-soft): %s", _e)
        except Exception:
            pass
        out["reason"] = f"exc: {str(_e)[:160]}"
        return out


def _persist_linkedin_urn(cur, post_id, urn, content_text, slug=None, article_url=None):
    """r72 (2026-06-05): close the URN capture gap that broke the engagement
    measure→learn loop.
       (1) UPDATE social_media_posts.linkedin_urn = <urn>  (column existed but
           was never populated; the publisher loop dropped the API return value).
       (2) INSERT INTO linkedin_posts (post_urn, content, post_type, status,
           posted_at) so fetch_linkedin_engagement(days=21) in linkedin_poster.py
           — which iterates linkedin_posts.post_urn, NOT
           social_media_posts.linkedin_urn — picks them up on the next sync.

    FAIL-SOFT: never raises. A URN-persist failure cannot un-do a successful
    LinkedIn share, so we log + return rather than blowing up the caller. Only
    persists real URNs (filters DRY_RUN + sentinel strings 'posted',
    'posted-with-image', 'posted-with-fallback-image')."""
    if not urn or not isinstance(urn, str):
        return False
    if 'DRY_RUN' in urn:
        return False
    if not urn.startswith('urn:li:'):
        # _post_to_linkedin fallback sentinels — useful as a status signal but
        # the engagement API needs a real urn:li:share:* / urn:li:ugcPost:*.
        logger.info("r72 skip URN-persist (non-urn sentinel): %s", urn)
        return False
    # Derive the press-release slug from the article URL when not supplied, so
    # marketing_engine / og_cards (which JOIN auto_press_releases ON a.slug =
    # linkedin_posts.slug) can correlate this post's engagement back to its
    # story. _og_today_slug_for returns the last path segment, which equals the
    # auto_press_releases.slug for /news/<slug> etc.; a non-press URL yields a
    # non-matching slug (harmless — the joins simply find no match).
    if not slug and article_url:
        try:
            slug = _og_today_slug_for(article_url)
        except Exception:
            slug = None
    # Accurate content-type label (replaces the old hardcoded 'auto_press' —
    # see _li_post_type_for). For /dcpi/ and /markets/ posts the slug above is
    # the market slug, correlated to engagement by marketing_engine's
    # _market_performance(); for /news/ press posts it stays the press slug.
    post_type = _li_post_type_for(article_url)
    try:
        cur.execute(
            "UPDATE social_media_posts SET linkedin_urn = %s WHERE id = %s",
            (urn, post_id),
        )
    except Exception as e:
        logger.warning("r72 social_media_posts.linkedin_urn UPDATE failed: %s", e)
    # Mirror into linkedin_posts so the existing engagement reader sees it.
    # We use the SAME conn/cur (already committed by the caller's UPDATE wrapper)
    # to keep this atomic with the row state transition to 'published'.
    try:
        # 2026-07-17: store the FULL text (was [:500]) — the column is TEXT and
        # the truncation broke verbatim output audits (post 100292's 500-char
        # cut); LinkedIn caps commentary ~3,000 chars so this stays bounded.
        cur.execute(
            """INSERT INTO linkedin_posts (post_urn, content, post_type, status,
                                            slug, posted_at)
               VALUES (%s, %s, %s, %s, %s, NOW())""",
            (urn, (content_text or ''), post_type, 'success', slug),
        )
    except Exception as e:
        # Table may not exist in some dev DBs; linkedin_poster._ensure_tables
        # creates it on backend boot — log + continue (engagement sync will
        # no-op for that row, but the share itself was already posted).
        logger.warning("r72 linkedin_posts INSERT failed (urn=%s): %s", urn, e)
    # Render-drift probe — off unless LINKEDIN_RENDER_DRIFT_PROBE=1. Fetches this
    # share back by URN and compares LinkedIn's rendered commentary vs what we
    # sent, to catch a recurrence of the "Guam " truncation at the render layer
    # (dab60cc1). Fully fail-soft + isolated DB write; cannot affect this
    # successful post. Uses the FULL content_text (not the [:500] truncation
    # above) so the char-for-char compare is exact.
    try:
        _verify_linkedin_render_drift(urn, content_text)
    except Exception:
        pass
    return True


def _post_to_twitter(content_text):
    """Post to DC Hub X/Twitter account.

    Phase FF+3 (2026-05-13): added X/Twitter publish path.
    Uses the v2 tweets endpoint with OAuth 2.0 bearer token. Requires:
        TWITTER_BEARER_TOKEN   — user-context OAuth 2.0 token (needs
                                  tweet.write scope, not just read).
    OR for OAuth 1.0a User Context (preferred for posting on behalf
    of @dchubcloud), set all four:
        TWITTER_API_KEY, TWITTER_API_SECRET,
        TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET
    The OAuth 1.0a path uses requests_oauthlib if available; falls
    back to OAuth 2.0 bearer if only the simpler token is set.
    """
    # Try OAuth 1.0a first (the path the X dev platform recommends
    # for posting from a confirmed account).
    # .strip() — env vars pasted via dashboards routinely carry a trailing
    # newline; an unstripped credential silently fails auth (OAuth1 sig
    # mismatch / malformed Bearer header).
    api_key = os.environ.get('TWITTER_API_KEY', '').strip()
    api_sec = os.environ.get('TWITTER_API_SECRET', '').strip()
    acc_tok = os.environ.get('TWITTER_ACCESS_TOKEN', '').strip()
    acc_sec = os.environ.get('TWITTER_ACCESS_SECRET', '').strip()
    if all([api_key, api_sec, acc_tok, acc_sec]):
        try:
            from requests_oauthlib import OAuth1
            auth = OAuth1(api_key, api_sec, acc_tok, acc_sec)
            resp = requests.post(
                'https://api.twitter.com/2/tweets',
                json={'text': as_published(content_text, 'twitter')},
                auth=auth,
                timeout=15,
            )
            if resp.status_code in (200, 201):
                data = resp.json().get('data', {})
                return True, data.get('id', 'posted')
            return False, f"X API error {resp.status_code}: {resp.text[:300]}"
        except ImportError:
            # requests_oauthlib isn't installed — fall through to bearer.
            pass
        except Exception as e:
            return False, f"X OAuth1 error: {str(e)[:200]}"

    bearer = os.environ.get('TWITTER_BEARER_TOKEN', '')
    if not bearer:
        return False, "no_twitter_credentials"
    resp = requests.post(
        'https://api.twitter.com/2/tweets',
        json={'text': as_published(content_text, 'twitter')},
        headers={'Authorization': f'Bearer {bearer}',
                 'Content-Type': 'application/json'},
        timeout=15,
    )
    if resp.status_code in (200, 201):
        data = resp.json().get('data', {})
        return True, data.get('id', 'posted')
    return False, f"X API error {resp.status_code}: {resp.text[:300]}"

# Phase PP (2026-05-17) — Bluesky AT Protocol publishing.
# DC Hub Media currently amplifies via LinkedIn + email only. Bluesky
# is the fastest-growing dev/research community and has zero competitor
# presence in the data-center-intelligence niche — first-mover advantage.
#
# Auth: BLUESKY_HANDLE + BLUESKY_APP_PASSWORD (app password is generated
# at https://bsky.app/settings/app-passwords — never use account password).
# Free to post unlimited via the public AT Protocol. No approval delay.
def _post_to_bluesky(content_text):
    """Post to DC Hub Bluesky account via AT Protocol.

    Two-step flow:
      1. POST /xrpc/com.atproto.server.createSession with handle + app
         password → returns accessJwt + did
      2. POST /xrpc/com.atproto.repo.createRecord with the jwt + did →
         creates the post in the bsky.feed.post collection

    Bluesky post length cap is 300 graphemes (we truncate to be safe).
    """
    handle  = os.environ.get('BLUESKY_HANDLE', '').strip()
    app_pwd = os.environ.get('BLUESKY_APP_PASSWORD', '').strip()
    if not handle or not app_pwd:
        return False, "no_bluesky_credentials"

    # Step 1 — create session
    try:
        session_resp = requests.post(
            'https://bsky.social/xrpc/com.atproto.server.createSession',
            json={'identifier': handle, 'password': app_pwd},
            timeout=12,
        )
        if session_resp.status_code != 200:
            return False, f"Bluesky session failed {session_resp.status_code}: {session_resp.text[:200]}"
        session = session_resp.json()
        jwt = session.get('accessJwt')
        did = session.get('did')
        if not jwt or not did:
            return False, "Bluesky session missing accessJwt or did"
    except Exception as e:
        return False, f"Bluesky session error: {str(e)[:200]}"

    # Step 2 — create the post record
    try:
        from datetime import datetime as _dt, timezone as _tz
        now_iso = _dt.now(_tz.utc).isoformat().replace('+00:00', 'Z')
        # Bluesky: 300 grapheme limit. Truncate by chars (close enough).
        # Single source of truth with the publish gate — see as_published().
        text = as_published(content_text, 'bluesky')
        record_resp = requests.post(
            'https://bsky.social/xrpc/com.atproto.repo.createRecord',
            json={
                'repo':       did,
                'collection': 'app.bsky.feed.post',
                'record':     {
                    'text':       text,
                    'createdAt':  now_iso,
                    '$type':      'app.bsky.feed.post',
                    'langs':      ['en'],
                },
            },
            headers={
                'Authorization': f'Bearer {jwt}',
                'Content-Type':  'application/json',
            },
            timeout=15,
        )
        if record_resp.status_code in (200, 201):
            data = record_resp.json()
            return True, data.get('uri', 'posted')
        return False, f"Bluesky post failed {record_resp.status_code}: {record_resp.text[:200]}"
    except Exception as e:
        return False, f"Bluesky post error: {str(e)[:200]}"


@content_bp.route('/api/admin/publish/bluesky', methods=['POST'])
def publish_bluesky():
    """Admin endpoint: manually push a social_media_posts row to Bluesky.
    Phase PP (2026-05-17) — companion to publish_linkedin / publish_twitter."""
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(force=True) or {}
    post_id = data.get('post_id')
    raw_text = data.get('text', '').strip()

    # Allow either {post_id} (lookup row) OR {text} (one-shot post)
    content_text = raw_text
    conn = None
    with ExitStack() as _conn_stack:
        if post_id and not raw_text:
            try:
                conn = _conn_stack.enter_context(_db_conn())
                cur = conn.cursor()
                cur.execute("SELECT content FROM social_media_posts WHERE id = %s",
                            (post_id,))
                row = cur.fetchone()
                if not row:
                    return jsonify({'success': False, 'error': 'post_not_found'}), 404
                content_text = row[0] or ""
            except Exception as e:
                return jsonify({'success': False, 'error': f'db:{str(e)[:120]}'}), 500
        if not content_text:
            return jsonify({'success': False, 'error': 'post_id_or_text_required'}), 400

        ok, result = _post_to_bluesky(content_text)
        if ok and post_id and conn is not None:
            try:
                cur = conn.cursor()
                from datetime import datetime as _dt2
                now = _dt2.utcnow()
                cur.execute("""UPDATE social_media_posts
                                  SET status = %s,
                                      posted_at = %s, published_at = %s,
                                      publish_platform = %s
                                WHERE id = %s""",
                            ('published', now, now, 'bluesky', post_id))
                conn.commit()
            except Exception:
                note_swallowed_write("social_media_posts", where="content_publisher.publish_bluesky")
                pass
        if conn is not None:
            try: conn.close()
            except Exception: pass
        return jsonify({
            'success':  ok,
            'platform': 'bluesky',
            'post_id':  post_id,
            'uri':      result if ok else None,
            'error':    None if ok else result,
        }), (200 if ok else 502)


@content_bp.route('/api/admin/publish/linkedin', methods=['POST'])
def publish_linkedin():
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(force=True)
    post_id = data.get('post_id')
    if not post_id:
        return jsonify({'success': False, 'error': 'post_id required'}), 400
    access_token = _li_access_token()   # DB-first, env fallback (2026-07-31)
    if not access_token:
        return jsonify({'success': False, 'error': 'no LinkedIn token (DB empty and LINKEDIN_ACCESS_TOKEN not set)'}), 500
    with _db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, content, status, platform FROM social_media_posts WHERE id = %s", (post_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Post not found'}), 404
        if row['status'] not in ('approved', 'draft'):
            conn.close()
            return jsonify({'success': False, 'error': f"Post status is '{row['status']}', must be approved or draft"}), 400
        content_text = row['content']
        # r51 (2026-05-29): extract URL from body so manual publishes also get
        # the IMAGE-attach path (otherwise this endpoint sent imageless text-only
        # posts even after the auto-publisher started attaching images).
        _art_url = None
        _art_title = None
        try:
            import re as _re_url
            _m = _re_url.search(r'https?://[^\s)>\]]+', content_text or '')
            if _m:
                _art_url = _m.group(0).rstrip('.,')
                _first_line = (content_text or '').strip().split('\n', 1)[0].strip()
                _art_title = _first_line[:180] or None
        except Exception:
            _art_url = None
            _art_title = None
        success, result = _post_to_linkedin(content_text, access_token,
                                              article_url=_art_url,
                                              article_title=_art_title)
        now = datetime.utcnow().isoformat() + 'Z'
        if success:
            cur.execute("UPDATE social_media_posts SET status = 'published', posted_at = %s, published_at = %s, publish_platform = 'linkedin' WHERE id = %s", (now, now, post_id))
            # r72: capture the URN so we can later fetch engagement
            # (likes/comments/impressions) for this post.
            _persist_linkedin_urn(cur, post_id, result, content_text, article_url=_art_url)
            conn.commit()
            conn.close()
            logger.info(f"Published post {post_id} to LinkedIn: {result}")
            return jsonify({'success': True, 'linkedin_post_id': result})
        else:
            conn.close()
            logger.warning(f"LinkedIn publish failed for post {post_id}: {result}")
            return jsonify({'success': False, 'error': result})

# r62 (2026-05-29): legacy short-DCPI-post detector + auto-rewriter.
# Even after r47.38 fixed the generator, the queue still contains rows
# enqueued before the fix. Publisher drains 1 per 6h, so without a
# rewrite gate those legacy posts can keep landing on LinkedIn for
# weeks. Operator flagged seeing "📍 Coeur d'Alene · WECC · DCPI
# verdict: CAUTION / Excess Power: 44.8/100 · Constraint: 41.1/100 /
# Live page: <link>" on dchub-media's LinkedIn — that's the pre-r47.38
# shape draining out. Fix:
#   1. detect the shape (3-5 lines, 'DCPI verdict:' + 'Excess Power:'
#      + 'Live page:' markers).
#   2. parse market/iso/verdict/excess/constraint out of the body.
#   3. rebuild via _shape_linkedin (the post-r47.38 rich shape) so the
#      post that lands on LinkedIn matches what content_enqueue would
#      produce TODAY for the same DCPI signal.
# This rewrite is persisted back to social_media_posts.content before
# publish so the dchub-media admin queue + audit logs reflect the real
# post that went out.
import re as _re_legacy

_LEGACY_SHORT_DCPI = _re_legacy.compile(
    r'^[^\S\r\n]*📍.+·.+·\s*DCPI verdict:\s*(BUILD|CAUTION|AVOID|HOLD|LOW_SIGNAL)',
    _re_legacy.IGNORECASE | _re_legacy.MULTILINE,
)


def _is_legacy_short_dcpi_shape(text: str) -> bool:
    """Detect the pre-r47.38 short DCPI verdict post shape.

    Pattern (rendered):
      📍 Coeur d'Alene · WECC · DCPI verdict: CAUTION
      Excess Power: 44.8/100 · Constraint: 41.1/100
      Live page: https://dchub.cloud/dcpi/<slug>

    Heuristic: matches the pin+verdict header AND contains
    'Excess Power:' (colon-form is legacy; new shape uses
    'Excess Power N/100' without the colon).
    """
    if not text:
        return False
    if not _LEGACY_SHORT_DCPI.search(text):
        return False
    if 'Excess Power:' not in text:
        return False
    # New rich shape is >= 600 chars; legacy is <= 250. If it's long,
    # it's already been rewritten or was always rich.
    return len(text) <= 400


_LEGACY_HEADER = _re_legacy.compile(
    r'📍\s*(.+?)\s*·\s*(.+?)\s*·\s*DCPI verdict:\s*(BUILD|CAUTION|AVOID|HOLD|LOW_SIGNAL)',
    _re_legacy.IGNORECASE,
)
_LEGACY_SCORES = _re_legacy.compile(
    r'Excess Power:\s*([\d.]+)\s*/\s*100\s*·\s*Constraint:\s*([\d.]+)\s*/\s*100',
    _re_legacy.IGNORECASE,
)
_LEGACY_LINK = _re_legacy.compile(
    r'https?://dchub\.cloud/dcpi/([a-z0-9\-]+)',
    _re_legacy.IGNORECASE,
)


def _parse_legacy_short_dcpi(text: str) -> dict | None:
    """Extract market/iso/verdict/excess/constraint/slug from legacy text.
    Returns None if parsing fails (caller should leave post untouched).
    """
    if not text:
        return None
    hdr = _LEGACY_HEADER.search(text)
    if not hdr:
        return None
    name = (hdr.group(1) or '').strip()
    iso = (hdr.group(2) or '').strip()
    verdict = (hdr.group(3) or '').strip().upper()

    excess = 0.0
    constr = 0.0
    sc = _LEGACY_SCORES.search(text)
    if sc:
        try:
            excess = float(sc.group(1) or 0)
            constr = float(sc.group(2) or 0)
        except (TypeError, ValueError):
            pass

    slug = ''
    lk = _LEGACY_LINK.search(text)
    if lk:
        slug = (lk.group(1) or '').strip().lower()
    if not slug:
        # fall back to a slugified market name
        slug = _re_legacy.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')

    return {
        'name': name or '?',
        'slug': slug or 'unknown',
        'verdict': verdict or 'HOLD',
        'iso': iso or '?',
        'excess': excess,
        'constraint': constr,
    }


def _rewrite_legacy_to_rich(text: str) -> str | None:
    """Take a legacy short DCPI post, parse it, and return the rich
    shape from routes/content_enqueue._shape_linkedin (single source of
    truth for the DCPI narrative template).

    Returns the new content string on success, None on failure
    (publisher then falls back to the legacy text — never worse than
    before).
    """
    mover = _parse_legacy_short_dcpi(text)
    if not mover:
        return None
    try:
        # Lazy import to avoid circular dep at module load.
        from routes.content_enqueue import _shape_linkedin
    except Exception:
        return None
    try:
        return _shape_linkedin(mover, None)
    except Exception:
        return None


# r42v (2026-05-26): content classification for per-class daily dedup.
# Without this, the publisher could fire 3 "DCPI verdict" posts in a row
# (operator caught Chantilly/Edison/Buffalo AVOID posts at 3-4m apart).
# Classification is lightweight pattern-match on the first ~200 chars.
# ---------------------------------------------------------------------------
# 2026-07-28 — THE WIRE TRANSFORM. What each platform actually receives.
#
# The posters have always truncated (X hard-cuts at 280; Bluesky cuts at 297 and
# appends an ellipsis), but the publish gate scored the untruncated DRAFT. So
# the gate was judging a different artifact than the one that shipped, and the
# gap was invisible to every measurement — measured 2026-07-28 on the pillars X
# card: the 359-char draft scored 0.600 while the 280-char tweet that actually
# published scored 0.150 and had lost its link entirely to the cut.
#
# Both the posters and the gate now go through as_published(), so they cannot
# drift. If a platform's limit changes, change it HERE — not in the poster.
# LinkedIn is absent on purpose: its poster does not truncate, so its wire text
# IS the draft.
_WIRE_LIMITS = {
    "twitter": {"limit": 280, "suffix": ""},
    # Bluesky counts GRAPHEMES, not chars; the poster approximates with chars
    # and this mirrors it exactly rather than being more correct than the thing
    # it models — the point is to predict what the poster will send.
    "bluesky": {"limit": 300, "suffix": "...", "cut": 297},
}


def as_published(content_text: str, platform: str) -> str:
    """The text the platform will actually receive, after its own truncation.

    This is what the publish gate must judge — scoring the draft instead is how
    a tweet shipped with its link cut off while the gate recorded 0.600. Returns
    the text unchanged for platforms that do not truncate. Never raises."""
    try:
        spec = _WIRE_LIMITS.get((platform or "").strip().lower())
        if not spec or not content_text:
            return content_text or ""
        if len(content_text) <= spec["limit"]:
            return content_text
        return content_text[:spec.get("cut", spec["limit"])] + spec["suffix"]
    except Exception:
        # Fail-open to the draft: never let this helper block a post.
        return content_text or ""


def _classify_post_for_dedup(text: str) -> str:
    """Return a coarse class tag so we can rate-limit per-class daily.
    Goal: max 1 'dcpi_verdict' per day, max 1 'partnership_invite' per
    day, etc. Returns 'other' for posts that don't match any known class."""
    if not text:
        return "other"
    t = text[:300].lower()
    # 1. DCPI verdict pin posts (📍 X · ISO · DCPI verdict: AVOID...)
    if "dcpi verdict:" in t or ("📍" in text[:30] and "dcpi" in t):
        return "dcpi_verdict"
    # 2. Partnership-track posts (Switzerland model, open invitation)
    if "switzerland model" in t or "open invitation" in t or "partnerships@dchub.cloud" in t:
        return "partnership_invite"
    # 3. Daily intelligence digest
    if "daily intelligence" in t or "daily digest" in t or "🗞" in text[:30]:
        return "daily_digest"
    # 4. MCP / AI-agent integration pitch
    if "mcp server" in t or "mcp api" in t or "ai agent" in t or "score_facility" in t:
        return "mcp_pitch"
    # 5. Per-tool / per-feature press
    if "tony bishop" in t:
        return "tony_bishop"
    # 6. Capacity/coverage milestones
    if "added" in t and "markets" in t:
        return "coverage_milestone"
    return "other"


def _x_source_class(press_release_id, lead_kind, text: str) -> str:
    """SOURCE class for the X drain's one-per-class-per-day rule (2026-07-31).

    X publishes at most 2/day, so diversity there has to operate on where a
    row CAME FROM, not just its copy: every press-release distribution row is
    one class ("press" — the headline+link+hashtags shape reads as the same
    template regardless of story), each editorial-desk lead kind is its own
    class ("lead:deal", "lead:dcpi_build", …), and unstamped rows fall back to
    the copy classifier the LinkedIn drain uses. The 2026-07-17 verbatim audit
    measured X at 100% one template — the press class taking every slot; the
    14d re-measure (07-31) still had it at 22 of 27 posts."""
    if press_release_id is not None:
        return "press"
    lk = (lead_kind or "").strip().lower()
    if lk:
        return "lead:" + lk
    return _classify_post_for_dedup(text or "")


# ---------------------------------------------------------------------------
# r63 (2026-05-29) — pre-publish media-judgment guard.
#
# WHY: DC Hub Media posted near-duplicate LinkedIn posts that the existing
# per-CLASS dedup (_classify_post_for_dedup + _seen_classes_today) could not
# catch, because:
#   (1) ENTITY-BLINDNESS — "Montréal 65.2 Excess Power BUILD" and "MCP ~142k
#       tool calls" both fall through to the catch-all "other" class, and two
#       "other" posts never collide. The class tag is too coarse.
#   (2) TODAY-ONLY WINDOW — _seen_classes_today is rebuilt every loop from only
#       posts with published_at LIKE today%, so a 2nd Montréal post 13h later
#       crossed the UTC-midnight boundary and saw an empty seen-set.
#   (3) NO ZERO-STAT GUARD — "DC Hub MCP served 0 AI tool calls" was eligible
#       to publish (embarrassing "0 MCP requests" zero-stat post).
#
# This guard is ENTITY-level and time-windowed: it looks at the actual
# market+verdict and the headline metric of each candidate, compares against a
# rolling N-day window of already-published posts (crosses midnight), and
# hard-blocks zero/null headline stats. It is a pre-publish FILTER, not a
# rewrite — fail-open on any error so it can never make distribution worse.
# ---------------------------------------------------------------------------

# Lookback window for entity-level dedup. 5 days sits inside the spec's
# 3-7d band: long enough to stop the "same Montréal BUILD twice this week"
# repeats, short enough that a genuinely-changed verdict can re-post within
# the week.
_DEDUP_LOOKBACK_DAYS = 5

# Headline-metric extractor. Matches the handful of quotable stats the press
# engine leads with (marketing_engine._pick_daily_topic).
#
# 2026-07-28 — each entry is now (label, regex, DEDUP_MODE). The third field is
# the fix for this list's double duty. It used to be a 2-tuple, and membership
# implicitly meant BOTH "this counts as a headline stat" (quality score) AND
# "two posts sharing this label are the same story" (entity dedup) — two
# unrelated jobs that want opposite breadth. Score wants every real measurement;
# dedup wants only labels where collapsing is actually right.
#
# The cost of that coupling was visible in the code: SIX of the thirteen labels
# had to be opted back out via a separate _NO_METRIC_DEDUP set, i.e. they were
# added to this list purely for score credit and then excluded from the dedup
# they had silently joined. Worse, the default was DANGEROUS — a new pattern
# added for scoring would start collapsing posts unless someone remembered the
# opt-out set two screens away. "7 of 7 ISOs" would have suppressed "5 of 6
# markets" for the whole 5-day window.
#
# DEDUP_MODE values, declared per pattern so the decision is made where the
# pattern is written, and adding a pattern FORCES the choice:
#   DEDUP_NONE   — score credit only; never collapses posts. For per-market
#                  values (dcpi_score dedups via market_verdict instead) and for
#                  DC Hub's own coverage stats, whose rotation is the editorial
#                  (kind, entity) cooldown's job.
#   DEDUP_LABEL  — same label = same story. For named platform metrics.
#   DEDUP_VALUE  — same label AND ~same number = same story. For generic units
#                  where the label alone over-collapses distinct stories, but
#                  the same figure re-leading IS the repeat (the "427 GW x 6
#                  posts" case).
DEDUP_NONE, DEDUP_LABEL, DEDUP_VALUE = None, "label", "label+value"

_METRIC_PATTERNS = [
    # "DC Hub MCP served 142,318 AI tool calls in the last 24h"
    ("mcp_tool_calls",
     _re_legacy.compile(r'MCP\s+served\s+([\d,]+)\s+(?:AI\s+)?tool\s+calls', _re_legacy.I),
     DEDUP_LABEL),
    # generic "<N> AI tool calls" / "<N> tool calls" fallback (surge posts)
    ("mcp_tool_calls",
     _re_legacy.compile(r'([\d,]+)\s+(?:AI\s+)?tool\s+calls', _re_legacy.I),
     DEDUP_LABEL),
    # "<N> MCP requests" / "<N> MCP API requests" (the 0-stat case)
    ("mcp_requests",
     _re_legacy.compile(r'([\d,]+)\s+MCP(?:\s+API)?\s+requests', _re_legacy.I),
     DEDUP_LABEL),
    # "added 1,204 facilities in the last 7 days" / coverage milestones
    ("coverage_added",
     _re_legacy.compile(r'added\s+([\d,]+)\s+\w+\s+in\s+the\s+last', _re_legacy.I),
     DEDUP_LABEL),
    # "<N> unique (AI )callers/agents"
    ("unique_callers",
     _re_legacy.compile(r'([\d,]+)\s+unique\s+(?:AI\s+)?(?:callers|agents)', _re_legacy.I),
     DEDUP_LABEL),
    # 2026-06-10: DCPI score — DC Hub's #1 content type was INVISIBLE to the
    # quality scorer (no pattern), so DCPI-led posts ("Cheyenne hit DCPI score
    # 69.5 (BUILD)") scored ~0.55 and were auto-rejected below CONTENT_QUALITY_MIN
    # — the root cause of near-zero social posting. Matches "DCPI 69.5", "DCPI
    # score 69.5", "DCPI score of 69.5", "Excess Power 69.5"; value bounded 0-999
    # so it can't grab an unrelated big number. NOTE: dcpi_score is PER-MARKET
    # (value differs per market) so it does NOT dedup on the shared label —
    # DCPI posts dedup on market_verdict instead.
    ("dcpi_score",
     _re_legacy.compile(r'(?:DCPI|Excess[\s\-]?Power)(?:\s+score)?(?:\s+of)?\s+([\d]{1,3}(?:\.\d{1,2})?)\b', _re_legacy.I),
     DEDUP_NONE),
    # 2026-07-02 (operator "it repeats itself"): generic power/capital figures.
    # The ERCOT "427 GW" queue post shipped ~6 times in a week because no
    # pattern matched it → metric_label stayed None → entity dedup never saw
    # it. DEDUP_VALUE so the same figure can't re-lead within the lookback
    # while genuinely different GW/$B/MW stories still can. Keep these LAST —
    # first match wins, so the specific labels above take priority.
    ("gw_figure",
     _re_legacy.compile(r'([\d,]+(?:\.\d+)?)\s*GW\b', _re_legacy.I),
     DEDUP_VALUE),
    ("usd_billion",
     _re_legacy.compile(r'\$\s*([\d,]+(?:\.\d+)?)\s*(?:B\b|billion)', _re_legacy.I),
     DEDUP_VALUE),
    ("mw_figure",
     _re_legacy.compile(r'([\d,]+(?:\.\d+)?)\s*MW\b', _re_legacy.I),
     DEDUP_VALUE),
    # 2026-07-15: DC Hub's OWN coverage stats. Capability / platform posts
    # (provenance, ledger, tool catalog, grid, memory — the editorial cap_*
    # kinds) LEAD with these, but the scorer only knew market/deal/DCPI/MCP
    # metrics, so a "4,923 facilities" provenance post scored ~0.55 and was gated
    # out — silently blocking the whole capability-content push at publish time.
    # These give the stat + novelty credit so those posts clear CONTENT_QUALITY_MIN
    # honestly. Placed LAST so the specific labels above always win (e.g. an
    # "18 MW facility" post stays mw_figure). Require >=2 leading digits so an
    # incidental "5 markets" can't grab a label. DEDUP_NONE — capability
    # rotation is the editorial (kind,entity) cooldown's job, not a shared
    # coverage number (mirrors dcpi_score).
    ("facilities_count",
     _re_legacy.compile(r'([\d,]{2,})\s+(?:[a-z-]+\s+){0,2}(?:data\s+centers?|facilit\w*)', _re_legacy.I),
     DEDUP_NONE),
    ("markets_count",
     _re_legacy.compile(r'([\d,]{2,})\s+(?:DCPI[\s-]?)?(?:scored\s+)?markets?\b', _re_legacy.I),
     DEDUP_NONE),
    ("countries_count",
     _re_legacy.compile(r'([\d,]{2,})\+?\s+countries\b', _re_legacy.I),
     DEDUP_NONE),
    ("tools_count",
     _re_legacy.compile(r'([\d,]{2,})\s+(?:live\s+)?(?:agent\s+)?tools?\b', _re_legacy.I),
     DEDUP_NONE),
    # 2026-07-28: analyst COVERAGE-COMPLETENESS ratios — "7 of 7 US ISOs",
    # "5 of 6 markets". This construction IS the headline metric of a coverage
    # post, but no pattern saw it, so moat_live_telemetry — a substantive post
    # about live per-ISO telemetry carrying a documented +9.9% / -3.1%
    # correction — scored 0.550 against QUALITY_MIN 0.60 and was REFUSED at
    # publish. It had never been publishable. The post was not thin; the
    # recogniser was blind to its metric TYPE.
    #
    # Deliberately narrow, matching "<n> of <n>" and NOT a bare percentage. A
    # percentage appears in almost any prose ("up 5%", "-3.1%"), so a pct rule
    # would hand the 0.35 stat credit to genuinely thin posts and quietly gut
    # the gate — the opposite of the problem being fixed here. "N of N" is a
    # distinctive analyst construction that a low-signal post does not stumble
    # into. Both sides bounded to 3 digits so it cannot grab an unrelated pair;
    # value = the NUMERATOR (what is actually covered). Placed LAST, so every
    # specific label above still wins first.
    # DEDUP_NONE: a completeness statement ("7 of 7 ISOs") is not a story.
    # Deduping on the label would let one coverage post lock out every other for
    # the whole lookback window — "7 of 7 ISOs" suppressing "5 of 6 markets".
    # 2026-07-28: the lookarounds stop it reading ACROSS a thousands separator.
    # \b let "4,923 of 12,650 analyst-verified" match as "923 of 12" and record
    # metric_value 923 — a nonsense headline metric off a perfectly ordinary
    # sentence. Score-only, so it never mis-deduped anything, but the recorded
    # metric was wrong. "7 of 7" / "18 of 22" are unaffected.
    ("coverage_ratio",
     _re_legacy.compile(r'(?<![\d,])(\d{1,3})\s+of\s+(\d{1,3})(?![\d,])'),
     DEDUP_NONE),
]

# Fail fast if a pattern is added without declaring its dedup behaviour — the
# whole point of the third field is that the choice cannot be forgotten.
for _lbl, _pat, _mode in _METRIC_PATTERNS:
    if _mode not in (DEDUP_NONE, DEDUP_LABEL, DEDUP_VALUE):
        raise ValueError(
            "_METRIC_PATTERNS[%s]: dedup mode must be DEDUP_NONE, DEDUP_LABEL "
            "or DEDUP_VALUE, got %r" % (_lbl, _mode))
del _lbl, _pat, _mode

# label -> dedup mode, derived from the patterns so the two can never drift.
_METRIC_DEDUP_MODE = {lbl: mode for lbl, _p, mode in _METRIC_PATTERNS}


def _post_headline_signature(text: str) -> dict:
    """Extract the entity signature of a post for dedup + zero-stat checks.

    Returns a dict:
      {
        "market_verdict": "montreal|build" | None,   # market slug + verdict
        "metric_label":   "mcp_tool_calls" | None,    # headline stat kind
        "metric_value":   142318.0 | None,            # parsed numeric value
        "zero_stat":      True | False,               # headline stat is 0/null
        "dedup_label":    "mcp_tool_calls" | None,    # ONLY if it dedups
        "dedup_mode":     "label"|"label+value"|None, # how it dedups
      }
    2026-07-28: dedup_label/dedup_mode are what the entity-dedup reads, and they
    are populated ONLY for patterns declaring DEDUP_LABEL/DEDUP_VALUE. metric_*
    still describes every recognised quantity, so a score-only pattern lifts the
    quality score without ever collapsing two posts. Callers must not infer
    "these are the same story" from metric_label — that is what dedup_label is
    for; the two used to be the same field, which is how six labels ended up
    needing an opt-out set.

    Robust across BOTH the structured "📍 X · ISO · DCPI verdict: BUILD" shape
    AND the free-text "Montréal leads the BUILD ranking ..." shape. Never
    raises — returns an all-None signature on any parse failure (fail-open)."""
    sig = {"market_verdict": None, "metric_label": None,
           "metric_value": None, "zero_stat": False,
           "dedup_label": None, "dedup_mode": None}
    if not text:
        return sig
    try:
        # --- market + verdict ------------------------------------------------
        # 1) structured pin header (reuses the legacy DCPI parser regex)
        m = _LEGACY_HEADER.search(text)
        if m:
            name = (m.group(1) or "").strip().lower()
            verdict = (m.group(3) or "").strip().upper()
            slug = _re_legacy.sub(r'[^a-z0-9]+', '-', name).strip('-')
            if slug and verdict:
                sig["market_verdict"] = f"{slug}|{verdict}"
        # 2) /dcpi/<slug> link + a verdict word anywhere in the body
        if not sig["market_verdict"]:
            lk = _LEGACY_LINK.search(text)
            vd = _re_legacy.search(r'\b(BUILD|AVOID|CAUTION|HOLD)\b', text)
            if lk and vd:
                slug = (lk.group(1) or "").strip().lower()
                if slug:
                    sig["market_verdict"] = f"{slug}|{vd.group(1).upper()}"
        # 3) free-text "<Market> leads the BUILD ranking" / "<Market> flagged
        #    AVOID" (the marketing_engine dcpi_leader / dcpi_warning shapes)
        if not sig["market_verdict"]:
            ft = _re_legacy.search(
                r'^\s*([A-Z][\w .\'\-éÉ]{2,40}?)\s+(?:leads the\s+(BUILD|AVOID)\b'
                r'|flagged\s+(BUILD|AVOID)\b)',
                text, _re_legacy.M)
            if ft:
                name = (ft.group(1) or "").strip().lower()
                verdict = (ft.group(2) or ft.group(3) or "").strip().upper()
                slug = _re_legacy.sub(r'[^a-z0-9]+', '-', name).strip('-')
                if slug and verdict:
                    sig["market_verdict"] = f"{slug}|{verdict}"

        # --- headline metric -------------------------------------------------
        for label, pat, dedup_mode in _METRIC_PATTERNS:
            mm = pat.search(text)
            if not mm:
                continue
            try:
                val = float((mm.group(1) or "0").replace(",", ""))
            except (TypeError, ValueError):
                continue
            sig["metric_label"] = label
            sig["metric_value"] = val
            # Only patterns that DECLARE a dedup mode can collapse two posts.
            # A score-only pattern leaves dedup_label None, so it cannot.
            if dedup_mode is not DEDUP_NONE:
                sig["dedup_label"] = label
                sig["dedup_mode"] = dedup_mode
            if val <= 0:
                sig["zero_stat"] = True
            break
    except Exception:
        # Fail-open: a parse failure must never block legitimate posts.
        return {"market_verdict": None, "metric_label": None,
                "metric_value": None, "zero_stat": False,
                "dedup_label": None, "dedup_mode": None}
    return sig


# ── Branded stat cards (2026-07-31, DCHUB_LI_CARDS) ─────────────────────────
# Card copy for routes/media_card.render_stat_card. Reads the SAME
# _METRIC_PATTERNS objects the quality gate scores — the one rule here is that
# a card never shows a number its post doesn't say (and never recomputes one
# from the DB). Unit nouns per label; None → the headline carries its own unit
# ("$4.2B", "7 of 7").
_CARD_UNIT_FOR_LABEL = {
    'mcp_tool_calls': 'AI tool calls',
    'mcp_requests': 'MCP requests',
    'coverage_added': 'added',
    'unique_callers': 'unique AI callers',
    'dcpi_score': 'DCPI score',
    'gw_figure': 'GW',
    'usd_billion': None,
    'mw_figure': 'MW',
    'facilities_count': 'facilities',
    'markets_count': 'markets',
    'countries_count': 'countries',
    'tools_count': 'tools',
    'coverage_ratio': None,
}

# Trend PHRASE, taken verbatim from the post ("up 18% week-over-week",
# "+4.2%"). The card only adds a direction glyph; it never invents a period
# or a figure.
_CARD_TREND_RE = _re_legacy.compile(
    r'\b(?:up|down)\s+\d+(?:\.\d+)?\s*%'
    r'(?:\s+(?:week[- ]over[- ]week|month[- ]over[- ]month|year[- ]over[- ]year|WoW|MoM|YoY))?'
    r'|[+−]\d+(?:\.\d+)?\s*%',
    _re_legacy.I)


def _media_card_lead(text):
    """Build the stat-card lead dict from a post body, or None for "no card".

    None (never an exception) when the text has no recognisable headline
    metric, the metric is a zero-stat, or the winning pattern here would
    disagree with _post_headline_signature — the card is best-effort and must
    never wobble the publish path. The headline is the text's own matched
    substring VERBATIM (e.g. "142,318"), so card numbers == post numbers by
    construction."""
    if not text:
        return None
    try:
        sig = _post_headline_signature(text)
        if not sig.get('metric_label') or sig.get('zero_stat'):
            return None
        mm_win, label_win = None, None
        for label, pat, _mode in _METRIC_PATTERNS:   # same order ⇒ same winner
            mm = pat.search(text)
            if not mm:
                continue
            try:
                float((mm.group(1) or '0').replace(',', ''))
            except (TypeError, ValueError):
                continue
            mm_win, label_win = mm, label
            break
        if mm_win is None or label_win != sig['metric_label']:
            return None
        raw = (mm_win.group(1) or '').strip()
        if label_win == 'usd_billion':
            headline, unit = f'${raw}B', None
        elif label_win == 'coverage_ratio':
            headline, unit = f'{raw} of {(mm_win.group(2) or "").strip()}', None
        else:
            headline, unit = raw, _CARD_UNIT_FOR_LABEL.get(label_win)
        trend = None
        tm = _CARD_TREND_RE.search(text)
        if tm:
            phrase = ' '.join(tm.group(0).split())
            down = (phrase.lower().startswith('down')
                    or phrase.startswith(('-', '−')))
            trend = ('▼ ' if down else '▲ ') + phrase
        first = ''
        for ln in text.splitlines():
            ln = ln.strip()
            if ln:
                first = ln
                break
        first = _re_legacy.sub(r'https?://\S+', '', first)
        first = _re_legacy.sub(r'\s+', ' ', first).strip(' —–-·:;,')
        if len(first) > 160:
            first = first[:157].rstrip() + '…'
        return {'headline': headline, 'unit': unit, 'label': first,
                'trend': trend}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# B3 (2026-05-31) — pre-publish QUALITY score.
#
# WHY: the publisher only had EXISTENCE-style dedup (per-class + entity-level
# + zero-stat) before this. That stops *repeats* and *empty* posts, but a
# thin-but-novel post (no number, no link, no recency cue) still sailed
# through. B3 adds a positive QUALITY signal so we publish FEWER, HIGHER-signal
# posts: a post must clear CONTENT_QUALITY_MIN (default 0.5) on a 0..1 scale.
#
# The score reuses signals this module already computes — there is no rich
# "post" object in this pipeline, a post IS its content text (article_url,
# metric and market are all parsed out of the body), so _quality_score(post)
# takes the content string:
#   (a) concrete non-zero stat  → _post_headline_signature().metric_value
#   (b) freshness               → recency phrases / a current-ish year
#   (c) novelty                 → _names_concrete_subject(): does the post name
#                                 an operator/market/company/place, or carry a
#                                 market or metric signature? (2026-07-28: this
#                                 used to read the dedup CLASS, which answers a
#                                 scheduling question, not a substance one)
#   (d) real article_url/link   → an http(s) URL or a scheme-less domain+path
#
# This is an ADDITIONAL conservative gate layered into _should_skip_publish;
# it does NOT touch the daily caps. Fail-OPEN for scoring *errors* (if scoring
# itself throws we log and allow — distribution must never dark-hold on a
# bug), but fail-CLOSED for a confidently-computed low score.
# ---------------------------------------------------------------------------
# 2026-06-08: raised 0.5 -> 0.72 (quality over quantity). At 0.5 roughly half of
# generated posts shipped; the operator wants fewer, sharper posts. 0.72 means a
# post must clear a clearly-above-average bar (data specificity + freshness +
# hook) to publish. Override with CONTENT_QUALITY_MIN env if you want to retune.
# r-qa (2026-06-27): 0.72 default lowered to 0.60. At 0.72 the LinkedIn quad fed
# 0 posts/wk (500 blocked) — the heuristic _quality_score rarely awards the
# literal link(0.20)+freshness(0.20) points for the composer's output, so even a
# good stat+novelty post tops out ~0.60. 0.60 = stat + (novelty|freshness|link):
# substantive without demanding all four signals. Env CONTENT_QUALITY_MIN still
# overrides (set it on Railway to retune without a deploy).
QUALITY_MIN = float(os.environ.get('CONTENT_QUALITY_MIN', '0.60'))

# Phrases that signal the post references something recent (freshness). Kept
# in sync with the cadence language marketing_engine leads with ("in the last
# 24h / 7 days", "today", "this week"). A current-or-recent 4-digit year also
# counts so dated stats ("2026 interconnection queue") score as fresh.
_FRESHNESS_RE = _re_legacy.compile(
    r'\b(?:today|this week|this month|right now|just|latest|breaking|'
    r'in the last\s+\d+\s*(?:h|hr|hrs|hours|d|day|days|week|weeks)|'
    r'last\s+\d+\s*(?:h|hr|hrs|hours|days|weeks)|'
    r'(?:24h|48h|7\s*days|7d|30\s*days|30d))\b',
    _re_legacy.IGNORECASE)
_URL_RE = _re_legacy.compile(r'https?://[^\s)>\]]+', _re_legacy.IGNORECASE)
# 2026-07-28: SCHEME-LESS links. The X/Twitter drafts sign off with a bare
# "→ dchub.cloud/connect" (X auto-links bare domains, and the house style for
# a 280-char post omits the scheme), so _URL_RE — which requires https?:// —
# scored a REAL link as no link at all. Measured: the pillars X card lost the
# 0.20 link credit purely to a missing "https://" and sat at 0.150, refused;
# its LinkedIn sibling has the same bare link and only survived because other
# signals carried it. This is a false NEGATIVE in the gate, not thin copy.
#
# Requires a PATH on purpose. "dchub.cloud/connect" is somewhere to go;
# a bare "cite as DC Hub (dchub.cloud)" is a citation, not a call to action,
# and should not earn link credit. Requiring the path also drops the whole
# false-positive family this could otherwise open up — "main.py",
# "content_publisher.py", "v2.9.3", "e.g." — none of which carry one. The TLD
# list is curated rather than a generic [a-z]{2,} for the same reason: a
# generic one matches every module filename these posts mention by name.
_BARE_LINK_RE = _re_legacy.compile(
    r'(?<![\w@.])'                                   # not mid-word, not an email
    r'(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+'        # domain labels
    r'(?:cloud|com|org|net|io|ai|dev|app|gov|edu|co)'  # curated TLD (longest first)
    r'/[^\s)>\]]+',                                  # REQUIRED path
    _re_legacy.IGNORECASE)
_RECENT_YEAR_RE = _re_legacy.compile(r'\b(20[2-3]\d)\b')
# r65-qa (#6): the value-less M&A stub template the user flagged — a bullet line
# that's just "Deal - Google" / "→ Deal — Blackstone" with NO $ or MW. Matches
# →/•/-/* bullets and em-dash/hyphen/colon separators.
_DEAL_STUB_RE = _re_legacy.compile(
    r'(?mi)^\s*(?:[→••*\-]\s*)?deal\s*[—:\-]\s*[A-Za-z][\w&.\' ]{1,40}\s*$')
# r65-qa (citation self-own): an LLM DISCLAIMING knowledge is NOT a citation/
# endorsement — showcasing "I don't have specific current information about these
# two services" as "third-party AI validation" is a credibility own-goal. Block
# any post quoting these. (User-flagged 2026-06-01: two "Claude cited DC Hub"
# posts quoted Claude saying it had no info on DCHawk vs dchub.cloud.)
_LLM_DISCLAIMER_RE = _re_legacy.compile(
    r"(?i)("
    r"I(?:'m| am)?\s+(?:don'?t|do not|cannot|can'?t|not able|unable|not familiar|not aware|not sure)\b[^.\n]{0,60}"
    r"(?:specific|current|real[- ]?time|access|enough|accurate|information|compare|comparison|provide|verify|confirm|familiar)"
    r"|don'?t have (?:specific|current|real[- ]?time|enough|access|any)\b"
    r"|lack(?:ed)?\s+(?:current\s+)?specifics"
    r"|no (?:specific|current|reliable)\s+(?:current\s+)?information"
    r"|as of my (?:last|knowledge|training)\b"
    r"|knowledge cut[- ]?off"
    r"|to give you an accurate comparison"
    r"|without (?:more|current|additional)\s+(?:information|context|data)"
    r")")


def _opening_hook(text: str) -> str:
    """r65-qa: normalized opening line of a post for near-dup detection. Two
    variants that share the same hook (e.g. both open 'ChatGPT just called DC
    Hub the most purpose-built platform...') collapse to the same key, even when
    their bodies diverge and they carry no market/metric signature (which is why
    the entity-dedup missed the 4 duplicate citation posts)."""
    if not text:
        return ""
    first = ""
    for ln in str(text).splitlines():
        ln = ln.strip()
        if len(ln) >= 12:
            first = ln
            break
    if not first:
        first = str(text).strip()[:160]
    norm = _re_legacy.sub(r'[^a-z ]+', ' ', first.lower())
    words = [w for w in norm.split() if w]
    return " ".join(words[:10])


# ---------------------------------------------------------------------------
# 2026-07-28 — SPECIFICITY, the novelty signal, decoupled from dedup.
#
# _quality_score used to take its novelty signal from _classify_post_for_dedup.
# That function exists to answer a SCHEDULING question ("max 1 dcpi_verdict per
# day") and its buckets are campaign types, so it was a bad proxy for substance
# in both directions: specific posts that fit no bucket scored zero novelty,
# and boilerplate that fit one scored full novelty. This asks the substance
# question directly — does the post name a concrete SUBJECT? — and touches
# nothing the dedup path uses.
#
# Two things are deliberately NOT subjects:
#   • DC Hub itself. Every post names the publisher; that cannot be what makes
#     a post specific, or "100% free access to DC Hub" would score as specific.
#   • Units and generic abbreviations (MW, GW, API, PDF, US, EU). They are the
#     vocabulary these posts are written in, not things they are ABOUT.
# The stop-set is therefore units + generic abbreviations + self-reference — a
# small, stable list. It is NOT a topic taxonomy, which is the thing that has
# needed patching five times (see _METRIC_PATTERNS); no new market, operator or
# protocol ever needs to be added here to be recognised.
_SUBJECT_STOPWORDS = frozenset({
    # DC Hub referring to itself
    "DC", "HUB", "DCHUB", "DCPI",
    # units
    "MW", "GW", "KW", "TW", "KWH", "MWH", "GWH", "TWH", "KV", "MVA", "HZ",
    "USD", "EUR", "GBP", "SQFT",
    # generic abbreviations / boilerplate
    "AI", "API", "APIS", "PDF", "PDFS", "CEO", "CTO", "COO", "CFO", "FAQ",
    "URL", "HTTP", "HTTPS", "JSON", "CSV", "XML", "RSS", "SLA", "QA", "OK",
    "CC", "BY", "SA", "ND", "NC", "MIT", "TBD", "ETA", "FYI", "AKA",
    # too coarse to be the subject of a post
    "US", "USA", "EU", "UK", "GB", "NA", "APAC", "EMEA", "GLOBAL",
    # sentence-initial words that survive the position filter after a bullet
    "THE", "THIS", "THAT", "THESE", "THOSE", "AND", "BUT", "FOR", "NOT",
    "NEW", "NOW", "MOST", "EVERY", "WHERE", "WHEN", "WHAT", "WHY", "HOW",
    "THREE", "TWO", "ONE", "FOUR", "FIVE", "BOTH", "EACH", "LOAD", "GEN",
})
# Uppercase tokens: ERCOT, PJM, CAISO, NYISO, ONS, KPX, MCP, ISO-NE, CC-BY.
_SUBJECT_ACRONYM_RE = _re_legacy.compile(r'\b[A-Z][A-Z0-9]{1,6}(?:-[A-Z0-9]{1,4})?\b')
# Capitalised words: Ashburn, Edison, Brazil, Bishop. Sentence-initial ones are
# filtered out by POSITION below (not by a word list) — "Three things shipped"
# must not count "Three" as a named subject.
_SUBJECT_PROPER_RE = _re_legacy.compile(r'\b[A-Z][a-z]{2,}\b')
_SENTENCE_SPLIT_RE = _re_legacy.compile(r'(?:[.!?;:]|\n|[•·→*]|\s[-–—]\s)\s*')


def _names_concrete_subject(text: str) -> bool:
    """True when the post names a specific subject — an operator, market,
    company, protocol, place or person — rather than only describing itself.

    Position-aware: the first word of every sentence/bullet is skipped before
    looking for proper nouns, so an ordinary capitalised opener is not mistaken
    for a named entity. Never raises."""
    if not text:
        return False
    try:
        for chunk in _SENTENCE_SPLIT_RE.split(str(text)):
            chunk = chunk.strip()
            if not chunk:
                continue
            # An acronym is a named thing wherever it appears — capitalisation
            # is not positional for ERCOT the way it is for "Three".
            for m in _SUBJECT_ACRONYM_RE.finditer(chunk):
                tok = m.group(0)
                if tok.replace("-", "") not in _SUBJECT_STOPWORDS \
                        and tok.split("-")[0] not in _SUBJECT_STOPWORDS:
                    return True
            # Proper nouns: skip the chunk's first word, which is capitalised
            # by grammar rather than by being a name.
            words = chunk.split()
            for w in words[1:]:
                mm = _SUBJECT_PROPER_RE.match(w)
                if mm and mm.group(0).upper() not in _SUBJECT_STOPWORDS:
                    return True
    except Exception:
        # Fail-open to "not specific": a parse failure may cost credit,
        # never invent it.
        return False
    return False


def _quality_score(post) -> float:
    """Score a candidate post 0.0–1.0 on publish-worthiness. B3 (2026-05-31).

    `post` is the content text (this pipeline has no richer post object —
    see module note above). Four weighted signals, all reusing existing
    extractors so the score stays consistent with the dedup layer:

      (a) concrete non-zero stat   0.35  — _post_headline_signature parses a
                                           numeric headline metric > 0
      (b) freshness                0.20  — references something recent
      (c) novelty                  0.25  — names a concrete SUBJECT (an
                                           operator, market, company, place)
                                           or carries a market/metric
                                           signature. Independent of the
                                           dedup class since 2026-07-28.
      (d) real article_url/link    0.20  — a dchub.cloud (or any http) link,
                                           with or without the scheme

    Returns a float in [0,1]. A short/empty body floors low. Designed to be
    called inside a try/except by the gate so a raising input fails OPEN."""
    text = (post or "").strip()
    if not text:
        return 0.0

    # r65-qa (#6): hard-floor the broken M&A stub template. A "Deal - Google"
    # line with named companies but zero $/MW is exactly the value-less stub the
    # user flagged — it slipped through before because the date in the URL slug
    # faked freshness + number credit. Refuse it outright.
    if _DEAL_STUB_RE.search(text) and not _re_legacy.search(r'\$|\bMW\b', text):
        return 0.1

    # r65-qa (#6): strip URLs before the number/freshness checks so a post can't
    # earn "concrete stat" + "fresh" credit purely from the YYYY-MM-DD in its own
    # /news/<slug> link — the real number/year must be in the human-readable body.
    # 2026-07-28: strip scheme-less links for the SAME reason — a bare
    # "dchub.cloud/news/2026-07-28-foo" faked exactly the number+year credit
    # r65-qa closed for the https:// form. _URL_RE runs FIRST so a full URL is
    # consumed whole and _BARE_LINK_RE never re-matches its interior.
    text_nourl = _BARE_LINK_RE.sub(' ', _URL_RE.sub(' ', text))

    sig = _post_headline_signature(text)
    score = 0.0

    # (a) concrete, non-zero stat. A parsed headline metric > 0 is the
    # strongest signal; a bare number elsewhere in the body is a weaker
    # partial credit (so "added 1,204 facilities" without a recognised
    # metric label still isn't treated as statless).
    mv = sig.get("metric_value")
    if mv is not None and mv > 0:
        score += 0.35
    elif _re_legacy.search(r'\b\d[\d,]*(?:\.\d+)?\b', text_nourl):
        score += 0.15

    # (b) freshness — recency phrase or a recent year (URL-stripped: the slug
    # date no longer counts).
    if _FRESHNESS_RE.search(text_nourl) or _RECENT_YEAR_RE.search(text_nourl):
        score += 0.20

    # (c) novelty — does the post name a concrete SUBJECT?
    # 2026-07-28: this used to read `_classify_post_for_dedup(text) != "other"`,
    # which asked the wrong question. That function answers "which daily
    # rate-limit bucket is this post in?" — a SCHEDULING question, keyed on
    # campaign types ("switzerland model", "tony bishop", "open invitation")
    # that say nothing about substance, and looking at only the first 300 chars
    # because a bucket is about a post's LEAD. Using it as the novelty signal
    # meant: a post naming ERCOT, PJM and three countries scored ZERO novelty
    # for falling in no rate-limit bucket, while a boilerplate partnership
    # invite scored full marks for being in one. The pillars X card is the
    # measured case — three real figures, no bucket, no novelty credit.
    # The two now move independently: adding a rate-limit bucket no longer
    # changes anyone's score, and making a post more specific no longer
    # silently changes which daily cap it competes under.
    if _names_concrete_subject(text) or sig.get("market_verdict") \
            or sig.get("metric_label"):
        score += 0.25

    # (d) real article_url / link. 2026-07-28: a scheme-less "dchub.cloud/connect"
    # counts too — it is a real destination, and the X drafts write links that way.
    if _URL_RE.search(text) or _BARE_LINK_RE.search(text):
        score += 0.20

    # Clamp (defensive — weights sum to 1.0 but partial-credit paths could
    # in principle nudge over).
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return round(score, 3)


_EDITOR_MODEL = os.environ.get("EDITOR_REVIEW_MODEL", "claude-haiku-4-5-20251001")
try:
    _EDITOR_TIMEOUT = float(os.environ.get("EDITOR_REVIEW_TIMEOUT", "8") or 8)
except (TypeError, ValueError):
    _EDITOR_TIMEOUT = 8.0


def _editor_review(content_text: str):
    """r66 'knows better' layer — a final editor-in-chief LLM pass that reads the
    draft like a sharp B2B comms director and BLOCKS anything embarrassing that
    the deterministic gates didn't hard-code: an AI disclaiming knowledge dressed
    up as 'validation', fabricated/unverifiable specifics, cringe self-
    congratulation, off-brand or thin content. Generalizes the hard-coded guards
    to catch NOVEL embarrassment.

    Returns (publish: bool, reason: str). FAIL-OPEN: no key / any error / non-200
    / unparseable → (True, "editor-skip:...") so a flaky LLM never dark-holds the
    queue — the deterministic gate already blocks every KNOWN-bad class, this is
    an additional judgment layer on top, not the floor."""
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    text = (content_text or "").strip()
    if not key or not text:
        return True, "editor-skip:no-key-or-empty"
    # r-qa (2026-06-27): the editor was rejecting DC Hub's OWN canonical platform
    # numbers (15,000+ facilities / 230+ markets / 4,000+ deals) as "unverifiable"
    # under rule 3 — a false positive that dark-held the entire LinkedIn feed
    # (every post bounced here after clearing quality + number-lead). Tell the
    # editor those figures are verified ground truth (pulled live so they track
    # honest-numbers / canonical_stats).
    try:
        from canonical_stats import get_canonical_stats as _gcs
        _cs = _gcs() or {}
    except Exception:
        _cs = {}
    try:
        import datetime as _dt
        _today = _dt.datetime.utcnow().strftime("%B %d, %Y")
    except Exception:
        _today = "2026"
    _canon = (
        f"CONTEXT — treat as ground truth (do NOT call any of this fictional, "
        f"future, or unverifiable): TODAY IS {_today}. Dates in 2025-2026 are "
        "CURRENT/recent, never future or made-up. DC Hub's OWN platform metrics "
        "are VERIFIED from its live database, and its MCP tools (search_facilities, "
        "get_grid_intelligence, rank_markets, hyperscaler_deals, etc.) are REAL "
        "shipped product features. NEVER reject a post for citing DC Hub's own "
        "numbers/tools as 'unverifiable' or for a 2025-2026 date being 'future/"
        "fictional'. Canonical (rounded): "
        f"~{int(_cs.get('facilities', 21000)):,}+ tracked facilities, "
        f"{int(_cs.get('countries', 178))}+ countries, "
        f"{int(_cs.get('markets', 230))}+ markets (DCPI), 4,000+ tracked "
        "M&A deals, 7 live US ISOs. A post citing these (or consistent figures) is "
        "accurate, not fabricated.\n"
        "DC Hub Media ALSO ships and announces PLATFORM / CAPABILITY UPDATES, not "
        "only market-movement insight — new MCP tools, a provenance/citation "
        "envelope (per-record source+method+as-of + CC-BY), an in-band versioned "
        "error contract (error_version:1, published at /docs/error-codes), agent "
        "memory (save_site / get_changes), and tools like get_retirement_headroom "
        "and cluster_sites_by_latency. These are REAL, shipped features. A clear, "
        "number-led product/platform-update post is a VALID, on-brand DC Hub Media "
        "post: do NOT reject it merely for being a capability announcement rather "
        "than a market insight, and do NOT call these named features 'internal "
        "jargon', 'demo copy', or 'unverifiable'. Still hold it to the same bar "
        "below (no fabrication, no cringe/over-claim, no thin/templated copy).\n"
    )
    sys_prompt = (
        "You are the Editor-in-Chief of DC Hub, a serious data-center & energy "
        "intelligence company. Approve or REJECT one draft social post before it "
        "ships to LinkedIn. " + _canon + "REJECT (publish=false) if ANY is true:\n"
        "1. It quotes/paraphrases an AI or LLM DISCLAIMING knowledge or hedging "
        "(e.g. 'I don't have specific current information', 'I'm not sure', 'as of "
        "my last update', 'lacked current specifics'). Framing that as a citation "
        "or 'validation' is a humiliating self-own.\n"
        "2. It presents a non-endorsement, refusal, or generic mention AS an "
        "endorsement/citation.\n"
        "3. It states specific numbers, quotes, company names, or facts that look "
        "fabricated, internally inconsistent, or unverifiable.\n"
        "4. It is cringe/desperate/over-claiming, or off-brand for a credible B2B "
        "data company.\n"
        "5. It is thin, empty, or a broken template (placeholder text, bare "
        "'Deal - Company' with no value/MW, zero-value stats).\n"
        "Otherwise APPROVE. The deterministic gates already block every known-bad "
        "class (disclaimers, partner attacks, thin/templated, missing number-lead), "
        "so you are a LIGHT final check for NOVEL embarrassment only — when "
        "genuinely uncertain, APPROVE. Reject ONLY for a clear, specific problem "
        "from the list above. Reply with STRICT JSON only, no prose: "
        '{"publish": true|false, "reason": "<=12 words"}.'
    )
    try:
        import requests as _rq
        import json as _json
        r = _rq.post(
            anthropic_messages_url(),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": _EDITOR_MODEL, "max_tokens": 120,
                  "system": sys_prompt,
                  "messages": [{"role": "user",
                                "content": "DRAFT POST:\n\n" + text[:2000]}]},
            timeout=_EDITOR_TIMEOUT)
        if r.status_code != 200:
            return True, f"editor-skip:http{r.status_code}"
        blocks = (r.json() or {}).get("content") or []
        out = "".join(b.get("text", "") for b in blocks
                      if b.get("type") == "text").strip()
        m = _re_legacy.search(r'\{.*\}', out, _re_legacy.DOTALL)
        if not m:
            return True, "editor-skip:noparse"
        verdict = _json.loads(m.group(0))
        if verdict.get("publish") is False:
            return False, ("editor rejected — "
                           + str(verdict.get("reason", "no reason"))[:90])
        return True, "editor-ok"
    except Exception as e:
        return True, f"editor-skip:{type(e).__name__}"


_media_review_ready = False


def _record_media_block(platform: str, reason: str, content: str = ""):
    """r66 EVOLVING-MEDIA LOOP: persist every gate REJECTION (with reason) so the
    generator can learn from its own mistakes (lessons fed back into the prompt
    by marketing_engine._inject_editorial_lessons) and the brain can see the
    quality trend (/api/v1/media/self-critique). Fail-soft — telemetry must NEVER
    break or block publishing."""
    global _media_review_ready
    try:
        with _db_conn() as conn:
            if conn is None:
                return
            try:
                with conn.cursor() as cur:
                    if not _media_review_ready:
                        cur.execute("""CREATE TABLE IF NOT EXISTS media_review_log (
                            id BIGSERIAL PRIMARY KEY, platform TEXT,
                            decision TEXT DEFAULT 'blocked', reason TEXT,
                            content_excerpt TEXT,
                            created_at TIMESTAMPTZ DEFAULT NOW())""")
                        cur.execute("CREATE INDEX IF NOT EXISTS media_review_log_ts "
                                    "ON media_review_log(created_at DESC)")
                        conn.commit()
                        _media_review_ready = True
                    cur.execute(
                        "INSERT INTO media_review_log (platform, decision, reason, content_excerpt) "
                        "VALUES (%s,'blocked',%s,%s)",
                        (platform, (reason or "")[:300], (content or "")[:280]))
                    conn.commit()
            finally:
                try: conn.close()
                except Exception: pass
    except Exception:
        note_swallowed_write("media_review_log", where="content_publisher._record_media_block")
        pass


# r-no-disparage (2026-06-23): DC Hub's traffic comes FROM these platforms — their
# agents query us. A media post must NEVER knock them. We block any post that puts a
# peer AI platform within ~70 chars of a disparaging term (either order). Root cause
# of the incident: the hyperscaler_drama news pull fed an "Anthropic Mythos mess"
# headline into a "contrarian take" prompt → a published partner-bashing LinkedIn post.
_DISPARAGE_PARTNER_RE = _re_legacy.compile(
    r"\b(anthropic|claude|openai|chatgpt|gemini|deepmind|copilot|perplexity|"
    r"xai|grok|mistral|deepseek|cohere)\b", _re_legacy.IGNORECASE)
_DISPARAGE_NEG_RE = _re_legacy.compile(
    r"\b(mess|messes|fiasco|debacle|scandal|lawsuit|sued|fails?|failed|failure|"
    r"failing|fumbl\w+|stumbl\w+|flounder\w*|struggl\w+|woes|crisis|chaos|"
    r"botch\w*|blunder\w*|controvers\w+|backlash|meltdown|drags?\s+on|"
    r"falls?\s+behind|strangl\w+|can'?t|cannot|delays?|delayed|outage)\b",
    _re_legacy.IGNORECASE)


# 2026-07-17: the retired DCPI status template (killed at the composer
# 2026-07-15; this regex kills the QUEUED backlog + any reintroduction at
# publish time). Matches the invariant clause every shaped variant carried.
_RETIRED_TEMPLATE_RE = _re_legacy.compile(
    r"rates\s+BUILD\s+on\s+the\s+DC\s+Hub\s+Power\s+Index", _re_legacy.IGNORECASE)


def _disparages_partner(text: str, window: int = 70):
    """Return an offending snippet if a peer AI platform sits within `window`
    chars of a disparaging term (either order); else None. Used to hard-block
    media copy that knocks a partner."""
    if not text:
        return None
    parts = [m.start() for m in _DISPARAGE_PARTNER_RE.finditer(text)]
    if not parts:
        return None
    negs = [m.start() for m in _DISPARAGE_NEG_RE.finditer(text)]
    for p in parts:
        for n in negs:
            if abs(p - n) <= window:
                lo = max(0, min(p, n) - 12)
                hi = max(p, n) + 24
                return text[lo:hi].replace("\n", " ").strip()
    return None


def _run_claim_breaker(text: str, kind: str):
    """Indirection to the claim-breaker gate (Claim Loop step 3).

    Isolated so it can be stubbed in tests and so an import failure of the
    breaker module never touches the publisher's own import graph. Returns the
    breaker's decision dict, or None if the gate is unavailable (caller ships).
    """
    from routes.claim_breaker import breaker
    return breaker(text, kind)


def _should_skip_publish(cur, content_text: str, platform: str):
    """Pre-publish media-judgment filter. Returns (skip: bool, reason: str).

    Decision order (skip on the FIRST hit):
      (q) QUALITY GATE (B3) — skip if _quality_score(content) is below
          CONTENT_QUALITY_MIN (default 0.5). An ADDITIONAL conservative gate
          so we publish fewer, higher-signal posts. Fail-OPEN if scoring
          itself raises (log + allow), fail-CLOSED on a confident low score.
      (b) ZERO-STAT GUARD — never publish a post whose headline metric parses
          to 0/null ("DC Hub MCP served 0 AI tool calls"). These read as
          "the platform did nothing today" and damage credibility.
      (a) ENTITY DEDUP — skip if the SAME market+verdict OR the SAME headline
          metric was already published (this platform) within the last
          _DEDUP_LOOKBACK_DAYS. Uses a rolling time window queried fresh from
          the DB, so it catches near-dupes posted hours apart ACROSS the UTC
          midnight boundary (the bug the today-only seen-set missed).

    FAIL-OPEN: any DB / parse error returns (False, "") so a transient blip
    never dark-holds the publisher. The caller logs the reason when skip=True.

    2026-07-28: every check below judges the text AS PUBLISHED, not the draft.
    X hard-cuts at 280 and Bluesky at 297+ellipsis, and scoring the draft meant
    the gate was rating an artifact nobody would ever see — the pillars X card
    scored 0.600 as a 359-char draft while the 280-char tweet that actually
    shipped scored 0.150 with its link cut off. Truncating ONCE here (rather
    than only inside the quality score) keeps every downstream signal honest:
    a stat, an entity or a link that falls past the cut is not in the published
    post, so it must not earn credit, satisfy the zero-stat guard, or drive
    dedup.
    """
    _draft = content_text or ""
    content_text = as_published(_draft, platform)
    _text = content_text
    if _text != _draft:
        # Visible, not silent: a post that only passes as a draft is a COPY bug
        # (the tail is being thrown away on the wire), and the operator should
        # see which posts are in that state rather than discovering it in the feed.
        logger.warning(
            "[wire] %s draft is %d chars, over the wire limit — the gate is "
            "judging the %d chars that will actually publish; %d chars "
            "(ending %r) are discarded by the poster",
            platform, len(_draft), len(_text), len(_draft) - len(_text),
            _draft[len(_text):][:60])
    # (d) r65-qa: NEVER showcase a post that quotes an LLM DISCLAIMING knowledge
    # ("I don't have specific current information about these two services... to
    # give you an accurate comparison"). That reads as the opposite of an
    # endorsement — a credibility self-own. Hard block, ahead of every other
    # signal. User-flagged 2026-06-01.
    if _LLM_DISCLAIMER_RE.search(_text):
        return True, ("LLM-disclaimer quote — refusing to showcase an "
                      "'I don't have current info' LLM response as AI validation")

    # (d2) r-no-disparage (2026-06-23): NEVER publish copy that knocks a peer AI
    # platform — they are DC Hub's traffic source, not a target. Hard block, ahead
    # of the quality gate. (The Anthropic-"Mythos mess" LinkedIn post is why.)
    _disp = _disparages_partner(_text)
    if _disp:
        return True, ("partner-disparagement — refusing to publish copy that "
                      f"knocks a peer AI platform: …{_disp[:120]}…")

    # (d2b) RETIRED-TEMPLATE GATE (2026-07-17): the '<City> (<ISO>) rates BUILD
    # on the DC Hub Power Index' template was retired 2026-07-15 (composer-first,
    # silence-beats-template), but rows shaped by it were still QUEUED as
    # status='approved' and kept draining to X for days after the retirement —
    # all 7 X posts in the 14d audit were this exact template. Content-intrinsic
    # hard block on every platform: the drain's skip path terminal-rejects the
    # row, so the legacy backlog burns off instead of publishing.
    if _RETIRED_TEMPLATE_RE.search(_text):
        return True, ("retired-template — the 'rates BUILD on the DC Hub Power "
                      "Index' template was retired 2026-07-15; composed analyst "
                      "posts only (legacy queued row, reject to advance queue)")

    # (d2c) ENTITY-SCOPE GATE (2026-07-17): a queue stat paired with the wrong
    # geography is a WRONG PUBLIC NUMBER (post 100292: '609 GW ... NESO's
    # interconnection queue, 35% of all US queued load' — NESO is the GB
    # operator and the denominator mixed GB+US). Always-on hard block, all
    # platforms, independent of the MEDIA_CLAIM_VERIFY warn/block mode —
    # same class as the agent-count honesty gate. Fail-OPEN on import error.
    try:
        from routes.media_claim_verify import check_entity_scope
        _es = check_entity_scope(_text)
        if _es:
            return True, (f"entity-scope mismatch: {'; '.join(_es)[:220]} "
                          "— operator geography must match the scope the "
                          "sentence claims")
    except Exception as _ese:
        logger.warning("entity-scope gate unavailable (%s) — failing OPEN", _ese)

    # (d2) DOWNGRADE-LEAD GATE (2026-07-02, operator directive): the media
    # feed messages positive results and enhancements. A post that LEADS
    # with an AVOID verdict / downgrade is doom commentary — those belong
    # on the product surfaces and in subscriber alerts, not the feed. All
    # platforms (the queued backlog still holds pre-policy AVOID posts).
    _dl = (_text or "")[:200]
    if (_re_legacy.search(r"\bAVOID\b", _dl)
            or _re_legacy.search(
                r"(?i)\b(shifted to avoid|moved to avoid|downgraded to|"
                r"falls? to avoid|drops? to avoid)\b", _dl)):
        return True, ("downgrade-lead — feed posts message positive results "
                      "and enhancements, never AVOID/downgrade commentary")

    # (d3) AGENT-COUNT HONESTY GATE (r-reach-canonical-views, 2026-07-01): any
    # "N AI agents / N unique callers" claim must corroborate against the
    # canonical identity view mcp_calls_identity (agent = md5 of first public
    # XFF token, real external only — NEVER session_id / raw ip_address). The
    # "86 AI agents … up 41% week-over-week" post shipped from an unfiltered
    # COUNT(DISTINCT ip_address) when the honest count was ~14/wk. Hard block
    # on an over-claim; a text WITH agent claims that we cannot corroborate
    # (view unreadable) is ALSO blocked — omit-or-prove. Fail-OPEN only when
    # the helper itself can't import.
    try:
        from routes.media_fact_check_guard import check_agent_count_claims
        _ac = check_agent_count_claims(_text)
        if _ac["claims"] and _ac["over"]:
            _c0 = _ac["over"][0]
            return True, (f"agent-count over-claim: '{_c0['raw']}' vs "
                          f"{_ac['live'] if _ac['live'] is not None else 'unverifiable'} "
                          f"real external agents/30d (mcp_calls_identity) — agents are "
                          f"never session_id or raw ip_address counts")
    except Exception as _ae:
        logger.warning("agent-count gate unavailable (%s) — failing OPEN", _ae)

    # (q) QUALITY GATE (B3, 2026-05-31). Computed FIRST so a thin post is
    # filtered before the dedup DB round-trip. Wrapped so a scoring bug
    # NEVER blocks a post (fail-open); a successfully-computed low score
    # DOES block (fail-closed) — that's the whole point of the gate.
    try:
        _q = _quality_score(content_text or "")
    except Exception as _qe:
        logger.warning(
            "B3 quality-score raised (%s) — failing OPEN, allowing post",
            _qe)
        _q = None
    if _q is not None and _q < QUALITY_MIN:
        return True, (f"low quality score {_q:.3f} < {QUALITY_MIN:.3f} "
                      f"(CONTENT_QUALITY_MIN) — refusing thin/low-signal post")

    # (n) r86c NUMBER-LEAD GATE (LinkedIn): an analyst post LEADS with a real
    # metric. Reject any LinkedIn post that opens with a brand claim instead of
    # a number+trend — this is what stops "DC Hub is the authority" marketing
    # and the self-citation filler from publishing. Fail-OPEN if the helper is
    # unavailable so a transient import issue never dark-holds the feed.
    if (platform or "").lower() == "linkedin":
        try:
            from routes.media_editorial import leads_with_number
            if not leads_with_number(_text):
                return True, ("no number-lead — analyst posts must open with a "
                              "metric+trend, not a brand claim (r86c gate)")
        except Exception as _ng:
            logger.warning("r86c number-gate unavailable (%s) — failing OPEN", _ng)

    # (v) CLAIM-VERIFY GATE (r-claimverify, 2026-06-19; hoisted 2026-08-08).
    # leads_with_number checks a number is PRESENT; this checks the number is
    # TRUE against canonical_stats — catching runtime-hallucinated over-claims
    # (50,000 facilities, $324B, 190 countries) the static honest-numbers
    # fence never sees because they're composed, not committed.
    # ★Hoisted OUT of the LinkedIn-only branch (audit SH52-063): X and Bluesky
    # posts carried the same composed numbers with NO verification — an
    # over-claim blocked on LinkedIn shipped verbatim on the other two
    # platforms. The gate now runs for EVERY platform; only the number-LEAD
    # style rule above stays LinkedIn-scoped. Behind MEDIA_CLAIM_VERIFY:
    # 'block' fails the publish; anything else (default 'warn') logs and
    # ships. Fail-OPEN if the verifier is unavailable so a transient import
    # never dark-holds.
    try:
        _cv_mode = str(os.environ.get("MEDIA_CLAIM_VERIFY", "warn")).strip().lower()
        from routes.media_claim_verify import verify_claims
        _cv = verify_claims(_text)
        if _cv.get("blocks"):
            _reason = "; ".join(_cv["blocks"])[:240]
            if _cv_mode == "block":
                return True, (f"claim-verify: {_reason} (r-claimverify gate)")
            logger.warning("[claim_verify] WARN-only (would block): %s", _reason)
        for _w in (_cv.get("warns") or []):
            logger.warning("[claim_verify] warn: %s", _w)
    except Exception as _cve:
        logger.warning("claim-verify gate unavailable (%s) — failing OPEN", _cve)

    # (cb) CLAIM-BREAKER GATE (Claim Loop step 3, 2026-08-21). The one gate that
    # replays the five lie classes, on the AS-PUBLISHED text (`content_text`) so
    # a claim past the wire cut cannot earn a pass. Placed after the content-truth
    # gates but BEFORE the DB dedup round-trip so it is reachable with cur=None.
    # FAIL CLOSED for posts — refuse only when the gate is TRUSTED (its
    # must-stay-green control passed) AND says the post is not ok. When the gate
    # is UNTRUSTED (its own control failed, or the kill switch is on) LOG and
    # SHIP — a gate that cannot pass its own control must not block on its own
    # say-so. Fail-OPEN on any import/exception, like every other gate here.
    try:
        _cb = _run_claim_breaker(content_text or "", "post")
        if _cb is not None:
            if _cb.get("trusted") and not _cb.get("ok"):
                _cls = ", ".join(
                    sorted({v.get("cls", "?") for v in (_cb.get("violations") or [])}))
                return True, ("claim-breaker: refusing post — lie class(es) "
                              f"[{_cls}]: "
                              + "; ".join(v.get("detail", "")
                                          for v in (_cb.get("violations") or [])[:3])[:260])
            if not _cb.get("trusted"):
                logger.warning(
                    "[claim_breaker] UNTRUSTED (control failed/disabled) — "
                    "allowing %s post; violations=%s",
                    platform, [v.get("cls") for v in (_cb.get("violations") or [])])
    except Exception as _cbe:
        logger.warning("claim-breaker gate unavailable (%s) — failing OPEN", _cbe)

    sig = _post_headline_signature(content_text or "")

    # (b) zero / null headline stat — hard block, no DB needed.
    if sig.get("zero_stat"):
        return True, (f"zero-stat headline ({sig.get('metric_label')}="
                      f"{sig.get('metric_value')}) — refusing to publish a "
                      f"'platform did nothing' post")

    # Fetch recent published for BOTH the opening-hook dedup (ALL posts) and the
    # entity dedup (posts with a market/metric signature). r65-qa: this block
    # previously bailed with (False,"") for posts lacking an entity signature —
    # so citation posts ("ChatGPT/Claude cited DC Hub...") skipped dedup
    # ENTIRELY, which is exactly how 4 near-identical citation variants shipped
    # at once. Now every post is hook-deduped.
    _hook = _opening_hook(_text)
    try:
        cutoff = (datetime.utcnow()
                  - timedelta(days=_DEDUP_LOOKBACK_DAYS)).isoformat()
        cur.execute(
            "SELECT content FROM social_media_posts "
            "WHERE status = 'published' AND publish_platform = %s "
            "AND published_at >= %s "
            "ORDER BY published_at DESC LIMIT 80",
            (platform, cutoff))
        rows = cur.fetchall() or []
    except Exception:
        # Fail-open on any DB error.
        return False, ""

    for r in rows:
        prev = r.get('content') if hasattr(r, 'get') else (r[0] if r else '')
        # (c) opening-hook dedup — catches near-identical VARIANTS (same hook,
        # divergent body) even when they carry no market/metric signature.
        if _hook and _opening_hook(prev or "") == _hook:
            return True, (f'duplicate opening hook ("{_hook[:48]}…") already '
                          f"posted to {platform} within {_DEDUP_LOOKBACK_DAYS}d")
        # (a) entity-level dedup — same market+verdict OR same headline metric.
        # 2026-07-28: reads dedup_label, NOT metric_label. A pattern only
        # collapses posts if it declared DEDUP_LABEL/DEDUP_VALUE next to itself;
        # score-only patterns leave dedup_label None and cannot reach here. This
        # replaces the _NO_METRIC_DEDUP opt-out set, whose default was backwards
        # — a pattern added for scoring silently started deduping unless someone
        # remembered to exclude it two screens away.
        if sig.get("market_verdict") or sig.get("dedup_label"):
            psig = _post_headline_signature(prev or "")
            if (sig.get("market_verdict")
                    and psig.get("market_verdict") == sig.get("market_verdict")):
                return True, (f"duplicate market+verdict '{sig['market_verdict']}' "
                              f"already posted to {platform} within "
                              f"{_DEDUP_LOOKBACK_DAYS}d")
            if (sig.get("dedup_label")
                    and psig.get("dedup_label") == sig.get("dedup_label")):
                # DEDUP_VALUE labels (GW/$B/MW) require the VALUE to match too —
                # same label with a different figure is a different story; the
                # same figure re-leading is the repeat (the "427 GW × 6 posts"
                # case).
                _lbl = sig.get("dedup_label")
                _vals_match = True
                if sig.get("dedup_mode") == DEDUP_VALUE:
                    try:
                        _a, _b = float(sig.get("metric_value") or 0), \
                                 float(psig.get("metric_value") or 0)
                        _vals_match = abs(_a - _b) <= max(abs(_a), abs(_b)) * 0.01
                    except (TypeError, ValueError):
                        _vals_match = False
                if _vals_match:
                    return True, (f"duplicate headline metric '{_lbl}' "
                                  f"already posted to {platform} within "
                                  f"{_DEDUP_LOOKBACK_DAYS}d")

    # (e) r66 EDITOR-IN-CHIEF — final holistic judgment on the survivors. Only
    # posts that already cleared every deterministic guard reach here, so the
    # LLM cost is bounded. Fail-OPEN inside _editor_review, so this can only ever
    # ADD blocks, never dark-hold on an LLM blip.
    try:
        _ok, _why = _editor_review(_text)
        if not _ok:
            return True, _why
    except Exception:
        pass
    return False, ""


# r42z admin: enqueue a custom-authored post and (optionally) publish
# it immediately. Used when operator writes a specific post (e.g. an
# updated press-release announcement) and wants it on the wire NOW,
# bypassing the 6h cron + per-class dedup. The 'publish_now' flag
# triggers /api/admin/publish/linkedin inline so the post hits LinkedIn
# in one round-trip instead of two.
@content_bp.route('/api/admin/publish/enqueue-custom', methods=['POST'])
def enqueue_custom():
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(force=True) or {}
    content = (data.get('content') or '').strip()
    platform = (data.get('platform') or 'linkedin').strip().lower()
    publish_now = bool(data.get('publish_now', False))
    if not content or len(content) < 20:
        return jsonify({'success': False,
                        'error': 'content required (min 20 chars)'}), 400
    if platform not in ('linkedin', 'twitter', 'bluesky'):
        return jsonify({'success': False,
                        'error': "platform must be linkedin|twitter|bluesky"}), 400

    with _db_conn() as conn:
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO social_media_posts (content, platform, status, created_at)
                VALUES (%s, %s, 'approved', NOW())
                RETURNING id
            """, (content, platform))
            row = cur.fetchone()
            new_id = row['id'] if hasattr(row, 'get') else (row[0] if row else None)
            conn.commit()
        except Exception as e:
            try: conn.rollback()
            except Exception: pass
            try: conn.close()
            except Exception: pass
            return jsonify({'success': False, 'error': str(e)[:200]}), 500

        out = {'success': True, 'post_id': new_id, 'platform': platform,
               'status': 'approved'}

        if publish_now and platform == 'linkedin':
            access_token = _li_access_token()   # DB-first, env fallback (2026-07-31)
            if not access_token:
                out['published'] = False
                out['publish_error'] = 'no LinkedIn token (DB empty and LINKEDIN_ACCESS_TOKEN not set)'
                try: conn.close()
                except Exception: pass
                return jsonify(out), 200
            # Pull URL hint for rich link-card share (LinkedIn scrapes og:image)
            try:
                import re as _re_url
                _m = _re_url.search(r'https?://[^\s)>\]]+', content)
                _art_url = _m.group(0).rstrip('.,') if _m else None
                _art_title = (content.strip().split('\n', 1)[0].strip())[:180] or None
            except Exception:
                _art_url = None
                _art_title = None
            ok, result = _post_to_linkedin(content, access_token,
                                             article_url=_art_url,
                                             article_title=_art_title)
            now = datetime.utcnow().isoformat() + 'Z'
            if ok:
                cur.execute("""UPDATE social_media_posts
                                  SET status = 'published',
                                      posted_at = %s, published_at = %s,
                                      publish_platform = 'linkedin'
                                WHERE id = %s""", (now, now, new_id))
                # r72: persist URN for engagement loop.
                _persist_linkedin_urn(cur, new_id, result, content, article_url=_art_url)
                conn.commit()
                out['published'] = True
                out['linkedin_post_id'] = result
            else:
                out['published'] = False
                out['publish_error'] = result
        try: conn.close()
        except Exception: pass
        return jsonify(out), 200


# r42v admin: bulk-reject queued posts matching a content pattern.
# Used to clean up stale auto-generated content (Tony Bishop reposts,
# targeted partner-attack posts, etc.) without dropping the whole queue.
@content_bp.route('/api/admin/publish/purge-queue', methods=['POST'])
def purge_queue():
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(force=True) or {}
    pattern = (data.get('pattern') or '').strip()
    platform = (data.get('platform') or '').strip().lower() or None
    if not pattern:
        return jsonify({'success': False,
                        'error': 'pattern required (text substring, case-insensitive)'}), 400
    if len(pattern) < 3:
        return jsonify({'success': False,
                        'error': 'pattern too short (min 3 chars)'}), 400
    with _db_conn() as conn:
        cur = conn.cursor()
        try:
            if platform:
                cur.execute("""UPDATE social_media_posts
                                  SET status = 'rejected'
                                WHERE status IN ('approved', 'draft')
                                  AND platform = %s
                                  AND content ILIKE %s
                                RETURNING id""",
                            (platform, f'%{pattern}%'))
            else:
                cur.execute("""UPDATE social_media_posts
                                  SET status = 'rejected'
                                WHERE status IN ('approved', 'draft')
                                  AND content ILIKE %s
                                RETURNING id""",
                            (f'%{pattern}%',))
            rejected_ids = [r['id'] if hasattr(r, 'get') else r[0] for r in (cur.fetchall() or [])]
            conn.commit()
            return jsonify({
                'success': True,
                'pattern': pattern,
                'platform': platform or 'all',
                'rejected_count': len(rejected_ids),
                'rejected_ids': rejected_ids[:50],  # cap for response size
            }), 200
        except Exception as e:
            try: conn.rollback()
            except Exception: pass
            return jsonify({'success': False, 'error': str(e)[:200]}), 500
        finally:
            try: conn.close()
            except Exception: pass


# r62 (2026-05-29): admin tools for the legacy short-DCPI cleanup.

@content_bp.route('/api/admin/publish/sanitize-queue', methods=['POST'])
def sanitize_queue():
    """Bulk-rewrite all queued (approved/draft) LinkedIn posts that
    match the pre-r47.38 short DCPI shape, in-place to the rich shape.

    Without this, the auto-publisher only rewrites posts as it drains
    them (1 per 6h). With 30+ legacy rows queued, the operator would
    still see ugly posts on dchub-media for weeks. This endpoint
    drains the legacy bucket in one shot.

    Set ?dry_run=1 to preview which rows WOULD be rewritten without
    persisting changes. Returns count + first 5 before/after samples.
    """
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    dry_run = (request.args.get('dry_run', '0') or '0').strip().lower() in ('1', 'true', 'yes')
    with _db_conn() as conn:
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT id, content FROM social_media_posts
                 WHERE status IN ('approved', 'draft')
                   AND (platform = 'linkedin' OR platform IS NULL)
                 ORDER BY created_at ASC
                 LIMIT 500
            """)
            rows = cur.fetchall() or []
            samples = []
            rewritten_count = 0
            skipped_count = 0
            for r in rows:
                rid = r['id'] if hasattr(r, 'get') else r[0]
                text = r['content'] if hasattr(r, 'get') else r[1]
                if not _is_legacy_short_dcpi_shape(text or ''):
                    continue
                new_text = _rewrite_legacy_to_rich(text or '')
                if not new_text or len(new_text) <= len(text or ''):
                    skipped_count += 1
                    continue
                if len(samples) < 5:
                    samples.append({
                        'id': rid,
                        'before_chars': len(text or ''),
                        'after_chars': len(new_text),
                        'before_preview': (text or '')[:160],
                        'after_preview': new_text[:240],
                    })
                if not dry_run:
                    try:
                        cur.execute(
                            "UPDATE social_media_posts SET content = %s WHERE id = %s",
                            (new_text, rid),
                        )
                    except Exception:
                        skipped_count += 1
                        continue
                rewritten_count += 1
            if not dry_run:
                conn.commit()
            return jsonify({
                'success':         True,
                'dry_run':         dry_run,
                'scanned':         len(rows),
                'rewritten_count': rewritten_count,
                'skipped_count':   skipped_count,
                'samples':         samples,
            }), 200
        except Exception as e:
            try: conn.rollback()
            except Exception: pass
            return jsonify({'success': False, 'error': str(e)[:200]}), 500
        finally:
            try: conn.close()
            except Exception: pass


@content_bp.route('/api/admin/publish/preview-rewrite', methods=['GET', 'POST'])
def preview_rewrite():
    """Preview the legacy-to-rich rewrite for a single queued post or
    for arbitrary text. Never writes. Useful for verifying the new
    template renders before flipping the env flag off.

    Usage:
      GET  /api/admin/publish/preview-rewrite?id=123
      POST /api/admin/publish/preview-rewrite  body: {"content": "..."}
    """
    if not _check_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    raw = None
    rid = request.args.get('id') or (request.get_json(silent=True) or {}).get('id')
    if rid:
        try:
            rid_int = int(rid)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'id must be integer'}), 400
        with _db_conn() as conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT content FROM social_media_posts WHERE id = %s", (rid_int,))
                r = cur.fetchone()
                if not r:
                    return jsonify({'success': False, 'error': 'not_found'}), 404
                raw = r['content'] if hasattr(r, 'get') else r[0]
            finally:
                try: conn.close()
                except Exception: pass
    else:
        body = request.get_json(silent=True) or {}
        raw = body.get('content') or ''

    is_legacy = _is_legacy_short_dcpi_shape(raw or '')
    parsed = _parse_legacy_short_dcpi(raw or '') if is_legacy else None
    rewritten = _rewrite_legacy_to_rich(raw or '') if is_legacy else None
    return jsonify({
        'success':      True,
        'is_legacy':    is_legacy,
        'parsed':       parsed,
        'original':     raw,
        'original_chars':  len(raw or ''),
        'rewritten':    rewritten,
        'rewritten_chars': len(rewritten or '') if rewritten else 0,
        'dry_run_env':  (os.environ.get('LINKEDIN_PUBLISHER_DRY_RUN', '') or '').strip().lower() in ('1','true','yes','on'),
    }), 200


# =============================================================================
# Publisher operational state (2026-06-05) — pure in-memory, no secrets.
# Surfaced via the PUBLIC GET /api/v1/dchub-media/publisher-status endpoint
# (routes/publisher_status.py). Each loop calls _record_boot() once at start
# (or when the env-gate short-circuits the start) and _record_attempt() once
# per publish-attempt path. Reads are lock-free single-dict-key copies; the
# Python interpreter's GIL makes the simple field assignments safe enough
# for a status panel (no correctness invariants depend on cross-field reads).
# =============================================================================
_PUBLISHER_STATE = {
    "linkedin": {
        "boot_started":        False,
        "boot_started_at":     None,
        "boot_disabled_reason": None,
        "last_attempt_at":     None,
        "last_attempt_result": None,  # ok | error | skipped_cap | no_queued
        "last_error_class":    None,
        "attempts_24h":        0,
        "successes_24h":       0,
        "errors_24h":          0,
        "consecutive_auth_failures": 0,  # ITEM 6: dead-token circuit breaker
        "_counter_day_utc":    None,  # YYYY-MM-DD; reset trigger
    },
    "twitter": {
        "boot_started":        False,
        "boot_started_at":     None,
        "boot_disabled_reason": None,
        "last_attempt_at":     None,
        "last_attempt_result": None,
        "last_error_class":    None,
        "attempts_24h":        0,
        "successes_24h":       0,
        "errors_24h":          0,
        "consecutive_auth_failures": 0,  # ITEM 6: dead-token circuit breaker
        "_counter_day_utc":    None,
    },
    "bluesky": {
        "boot_started":        False,
        "boot_started_at":     None,
        "boot_disabled_reason": None,
        "last_attempt_at":     None,
        "last_attempt_result": None,
        "last_error_class":    None,
        "attempts_24h":        0,
        "successes_24h":       0,
        "errors_24h":          0,
        "consecutive_auth_failures": 0,  # ITEM 6: dead-token circuit breaker
        "_counter_day_utc":    None,
    },
}


def _utcnow_iso() -> str:
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')


def _maybe_reset_24h_counters(platform: str) -> None:
    """Reset the per-platform attempts/successes/errors counters at UTC
    midnight. Cheap: one date comparison per attempt. We keep last_* fields
    untouched so the diagnostic still shows what last happened across the
    midnight boundary."""
    today = datetime.utcnow().strftime('%Y-%m-%d')
    st = _PUBLISHER_STATE.get(platform)
    if not st:
        return
    if st.get('_counter_day_utc') != today:
        st['attempts_24h']     = 0
        st['successes_24h']    = 0
        st['errors_24h']       = 0
        st['_counter_day_utc'] = today


def _record_boot(platform: str, started: bool, reason: str = None) -> None:
    """Record whether a publisher loop actually booted.

    Called once per process from each start_*_publisher() function:
      * started=True  → loop thread spawned
      * started=False → env gate / config short-circuit (reason explains why)
    Safe to call before _record_attempt; never raises."""
    try:
        st = _PUBLISHER_STATE.get(platform)
        if st is None:
            return
        st['boot_started']         = bool(started)
        st['boot_started_at']      = _utcnow_iso() if started else st['boot_started_at']
        st['boot_disabled_reason'] = None if started else (reason or 'disabled')
    except Exception:
        pass  # status tracking must never break the publisher


def _record_attempt(platform: str, result: str, error_class: str = None) -> None:
    """Record one publish-attempt outcome.

    result is one of: 'ok' | 'error' | 'skipped_cap' | 'no_queued'.
    Increments the 24h counters (auto-reset at UTC midnight) and stamps
    last_attempt_at + last_attempt_result. For 'error', also stamps the
    last_error_class (e.g. 'auth_failed', 'rate_limit', 'network'). Never
    raises — wrap defensively because publisher loops should not crash on a
    status-tracker hiccup."""
    try:
        st = _PUBLISHER_STATE.get(platform)
        if st is None:
            return
        _maybe_reset_24h_counters(platform)
        st['last_attempt_at']     = _utcnow_iso()
        st['last_attempt_result'] = result
        st['attempts_24h']        = st.get('attempts_24h', 0) + 1
        if result == 'ok':
            st['successes_24h'] = st.get('successes_24h', 0) + 1
            st['last_error_class'] = None
            # ITEM 6 (2026-06-14): any success clears the auth-failure
            # circuit breaker (token was re-authed / transient blip ended).
            st['consecutive_auth_failures'] = 0
        elif result == 'error':
            st['errors_24h'] = st.get('errors_24h', 0) + 1
            st['last_error_class'] = error_class or 'unknown'
            # ITEM 6: count consecutive auth/forbidden failures so the loop
            # can circuit-break a dead token instead of retrying it forever
            # (the X 132-silent-403 pattern). Only auth-class errors trip
            # the breaker; rate-limit / network / server errors are transient
            # and should keep retrying, so they reset the counter.
            if (error_class or '') in ('auth_failed', 'forbidden'):
                st['consecutive_auth_failures'] = (
                    st.get('consecutive_auth_failures', 0) + 1)
            else:
                st['consecutive_auth_failures'] = 0
        # 'skipped_cap' + 'no_queued' do not bump success/error and leave the
        # auth-failure breaker untouched (no publish was attempted).
    except Exception:
        pass


# ITEM 6 (2026-06-14): consecutive-auth-failure circuit breaker. After
# this many back-to-back auth/forbidden failures we PAUSE a publisher's
# retries (it's a dead/expired token — a human must re-auth, mirroring the
# LinkedIn token-reset flow) and raise a clear owner re-auth action ONCE
# rather than churning a 403 every cycle. The breaker auto-clears the
# moment any publish succeeds (see _record_attempt 'ok' path), so a re-auth
# self-heals with no human un-pause step.
_AUTH_FAILURE_BREAKER_THRESHOLD = 3


def _auth_breaker_tripped(platform: str) -> bool:
    """True when `platform` has hit the consecutive-auth-failure threshold —
    its token is dead and the loop should stop retrying until re-auth."""
    try:
        st = _PUBLISHER_STATE.get(platform) or {}
        return int(st.get('consecutive_auth_failures', 0) or 0) >= \
            _AUTH_FAILURE_BREAKER_THRESHOLD
    except Exception:
        return False


def _classify_publish_error(exc_or_msg) -> str:
    """Best-effort error-class string for the diagnostic. We do NOT leak the
    raw message (it can contain tokens). Just a coarse tag the operator can
    use to know what to fix."""
    try:
        msg = str(exc_or_msg).lower() if exc_or_msg is not None else ''
    except Exception:
        msg = ''
    if not msg:
        return 'unknown'
    if 'unauthorized' in msg or '401' in msg or 'invalid_token' in msg or 'auth' in msg:
        return 'auth_failed'
    if '403' in msg or 'forbidden' in msg:
        return 'forbidden'
    if '429' in msg or 'rate' in msg or 'too many' in msg:
        return 'rate_limit'
    if 'timeout' in msg or 'timed out' in msg:
        return 'timeout'
    if 'connection' in msg or 'dns' in msg or 'network' in msg or 'resolve' in msg:
        return 'network'
    if '5' in msg and ('500' in msg or '502' in msg or '503' in msg or '504' in msg):
        return 'server_error'
    if 'duplicate' in msg or 'dup' in msg:
        return 'duplicate'
    return 'other'


def get_publisher_status_snapshot() -> dict:
    """Public read of the per-platform state, sanitized. Used by
    routes/publisher_status.py. We strip the leading-underscore internal
    fields (e.g. _counter_day_utc) so the JSON is clean."""
    out = {}
    for platform, st in _PUBLISHER_STATE.items():
        # Refresh counters before reading so a status check after UTC
        # midnight doesn't show yesterday's tallies.
        _maybe_reset_24h_counters(platform)
        out[platform] = {k: v for k, v in st.items() if not k.startswith('_')}
    # ITEM deadman (2026-07-02): surface the DB-durable 72h-silence watchdog
    # (see run_publisher_deadman_check below). Pure in-memory read of the
    # cache the loop ticks maintain — no DB call on the request path.
    # Per-platform: last_db_success_at + silent_hours + fired flag.
    try:
        out["deadman"] = {
            "silence_threshold_hours": _DEADMAN_SILENCE_HOURS,
            "check_interval_hours": _DEADMAN_CHECK_INTERVAL_SECONDS / 3600.0,
            "platforms": {p: dict(v) for p, v in _DEADMAN_STATE.items()},
        }
        if not _DEADMAN_STATE:
            out["deadman"]["note"] = (
                "no check has run yet this process (first publisher loop "
                "tick lands ~2-3 min after boot)")
    except Exception:
        pass  # status snapshot must never break on the watchdog cache
    return out


_auto_publisher_running = False

def _is_publish_leader() -> bool:
    """r66: only the LEADER replica auto-publishes — kills the 2-replica
    double-post (the same story shipped twice within a minute, e.g. the 4
    citation dups). Deferred import (main imports this module, so no top-level
    import) + FAIL-OPEN: if leadership can't be determined, return True so we
    never silence publishing by accident (worst case = prior behaviour, which
    the dedup + editor gate still catch). Re-checked every fire, so a
    promotion / step-down is honoured within one cycle."""
    try:
        from main import is_current_leader
        return bool(is_current_leader())
    except Exception:
        return True


# r-leaderwait (2026-07-31): non-leader recheck cadence. Must beat the ~3min
# deploy cadence on auto-merge evenings — see _wait_for_publish_leadership.
_NONLEADER_RECHECK_SECONDS = 120


def _wait_for_publish_leadership(platform: str) -> None:
    """r-leaderwait (2026-07-31): block until this replica is the publish
    leader, rechecking every _NONLEADER_RECHECK_SECONDS (is_current_leader is
    a main._LEADERSHIP dict read — no DB cost per tick).

    Replaces the old non-leader handling (LinkedIn: sleep(1800) + skip;
    X/Bluesky: `continue`, pushing the recheck a full 6h cadence out). On a
    Railway zero-downtime rollover the NEW worker boots while the OLD one
    still holds the session advisory lock (SIGTERM lands ~60-90s later), so
    the first leadership check ALWAYS loses the race; with deploys landing
    every ~3min on auto-merge evenings, every worker died before its
    30min/6h recheck and publishing froze for a whole evening (5 consecutive
    silent LinkedIn fires, 2026-07-31) — invisibly, because the only line
    was DEBUG. The keepalive wins the freed lock within a tick or two of the
    old worker's death, so a 120s recheck recovers on the first retry.

    Observability: INFO exactly once on entering a non-leader stretch and
    once on acquisition — repeat ticks stay DEBUG, so a parked follower
    replica doesn't spam but a frozen publisher is visible above DEBUG.
    The leadership check is fail-open and the sleep is guarded, so a hiccup
    here can't kill the publisher thread."""
    waited = False
    while not _is_publish_leader():
        if not waited:
            waited = True
            logger.info(
                "%s publisher: not leader — holding publishes, rechecking "
                "every %ss until this replica wins the lock",
                platform, _NONLEADER_RECHECK_SECONDS)
        else:
            logger.debug("%s publisher: still not leader — recheck in %ss",
                         platform, _NONLEADER_RECHECK_SECONDS)
        try:
            time.sleep(_NONLEADER_RECHECK_SECONDS)
        except Exception:
            pass
    if waited:
        logger.info("%s publisher: leadership acquired — resuming publishes",
                    platform)


# =============================================================================
# ITEM deadman (2026-07-02) — 72h platform-silence dead-man switch.
# X was dark for 36 days while every workflow ran green because NOTHING
# consumed the publisher state: _PUBLISHER_STATE above is in-memory only and
# resets on every deploy, so "last success" always looked recent-ish. This
# detector computes last-success DURABLY from the DB (the same tables the
# media sweep counts: social_media_posts for linkedin/twitter/bluesky, plus
# linkedin_quad_posts + linkedin_posts for the LinkedIn quad-daily arm) and
# files a brain finding when an env-configured platform has been silent
# > _DEADMAN_SILENCE_HOURS. UNIQUE(issue,url) in brain_findings dedups
# re-fires (recurrences bump seen_count); clearing/resolving is the
# outcome-verifier's job, not ours.
#
# Scheduling: called from each publisher loop tick (run_publisher_deadman_check
# below is self-throttled to one check per platform per 6h via module-level
# monotonic timestamps, so the three loops calling it are cheap no-ops).
# The DB read runs on every replica (keeps the /publisher-status deadman
# section populated everywhere); the brain-finding WRITE is leader-gated
# like the other per-cycle jobs.
# =============================================================================
_DEADMAN_SILENCE_HOURS = 72.0
_DEADMAN_CHECK_INTERVAL_SECONDS = 6 * 3600
_DEADMAN_PLATFORMS = ('linkedin', 'twitter', 'bluesky')
_DEADMAN_LAST_CHECK_MONO = {}   # platform -> time.monotonic() of last check
_DEADMAN_STATE = {}             # platform -> snapshot dict (see below)

# Per-platform "most recent successful publish" queries. Each runs
# independently (on error we rollback and move on) and the MAX across
# sources wins. SCHEMA TRAP (verified against live Neon 2026-07-02): the
# repo DDL declares social_media_posts.posted_at/published_at as TEXT, but
# LIVE posted_at is `timestamp without time zone` while published_at is
# TEXT — so every cast goes ::text first (works for both types), guarded by
# a leading-ISO-date regex so one junk text row can't error the whole MAX,
# then ::timestamp (naive; the writers store UTC wall time via
# datetime.utcnow().isoformat()+'Z', and _deadman_last_db_success assumes
# UTC for naive values). IMPORTANT: executed with cur.execute(sql) and NO
# params tuple, so psycopg2 does NOT run %-substitution — any LIKE pattern
# here must use single %.
_SMP_TS = ("SELECT MAX(NULLIF({col}::text, '')::timestamp) AS ts "
           "  FROM social_media_posts "
           " WHERE status = 'published' AND publish_platform {plat} "
           "   AND {col}::text ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}'")
_DEADMAN_SUCCESS_SQL = {
    'linkedin': (
        # Legacy auto-publisher + drain-now path
        _SMP_TS.format(col='posted_at',    plat="= 'linkedin'"),
        _SMP_TS.format(col='published_at', plat="= 'linkedin'"),
        # Quad-daily arm records successes ONLY here (r-quad-visibility)
        "SELECT MAX(posted_at) AS ts FROM linkedin_quad_posts "
        " WHERE success = TRUE",
        # linkedin_poster mirror table (post_urn set == real publish)
        "SELECT MAX(COALESCE(posted_at, created_at)) AS ts FROM linkedin_posts "
        " WHERE post_urn IS NOT NULL AND post_urn <> '' "
        "   AND post_urn NOT LIKE 'DRY_RUN%'",
    ),
    'twitter': (
        _SMP_TS.format(col='posted_at',    plat="IN ('twitter', 'x')"),
        _SMP_TS.format(col='published_at', plat="IN ('twitter', 'x')"),
    ),
    'bluesky': (
        _SMP_TS.format(col='posted_at',    plat="= 'bluesky'"),
        _SMP_TS.format(col='published_at', plat="= 'bluesky'"),
    ),
}


def _deadman_env_enabled(platform: str) -> bool:
    """True when the platform has publish credentials configured — mirrors
    the env_gates logic in routes/publisher_status.py. Deliberately does NOT
    look at DCHUB_AUTOPUB_LEGACY / TWITTER_PUBLISHER_ENABLED: a platform with
    creds set but the loop flag off is EXACTLY the silent-dark state this
    switch exists to surface (the flag state goes in the finding detail)."""
    def _set(name):
        try:
            return bool((os.environ.get(name, '') or '').strip())
        except Exception:
            return False
    if platform == 'linkedin':
        return _set('LINKEDIN_ACCESS_TOKEN')
    if platform == 'twitter':
        quad = all(_set(k) for k in ('TWITTER_API_KEY', 'TWITTER_API_SECRET',
                                     'TWITTER_ACCESS_TOKEN', 'TWITTER_ACCESS_SECRET'))
        return _set('TWITTER_BEARER_TOKEN') or quad
    if platform == 'bluesky':
        return _set('BLUESKY_HANDLE') and _set('BLUESKY_APP_PASSWORD')
    return False


def _deadman_last_db_success(cur, platform: str):
    """Most recent successful publish for `platform` across its source
    tables, as a tz-aware datetime, or None if no published row exists
    anywhere (never-configured platform → dead-man stays disarmed).
    Each source query is independent: a missing table / uncastable text
    timestamp rolls back and the other sources still count."""
    best = None
    for sql in _DEADMAN_SUCCESS_SQL.get(platform, ()):
        try:
            cur.execute(sql)
            row = cur.fetchone()
            ts = None
            if row is not None:
                ts = row.get('ts') if hasattr(row, 'get') else (row[0] if row else None)
            if ts is None:
                continue
            from datetime import timezone as _tz
            if getattr(ts, 'tzinfo', None) is None:
                ts = ts.replace(tzinfo=_tz.utc)
            if best is None or ts > best:
                best = ts
        except Exception:
            try:
                cur.connection.rollback()
            except Exception:
                pass
    return best


def run_publisher_deadman_check(force: bool = False) -> None:
    """72h-silence dead-man check across all publisher platforms.

    Self-throttled: at most one DB check per platform per 6h (module-level
    monotonic stamps — a deploy resets the throttle, which is fine because
    the detection itself is DB-durable). Never raises; publisher loops must
    not die on a watchdog hiccup. The brain-finding write is leader-gated;
    the read + _DEADMAN_STATE refresh runs on every replica so the
    /publisher-status deadman section is populated everywhere."""
    try:
        now_mono = time.monotonic()
        due = [p for p in _DEADMAN_PLATFORMS
               if force or (now_mono - _DEADMAN_LAST_CHECK_MONO.get(p, -_DEADMAN_CHECK_INTERVAL_SECONDS - 1)) >= _DEADMAN_CHECK_INTERVAL_SECONDS]
        if not due:
            return
        from datetime import timezone as _tz
        conn = None
        try:
            conn = _get_db()
            cur = conn.cursor()
            is_leader = _is_publish_leader()
            for platform in due:
                _DEADMAN_LAST_CHECK_MONO[platform] = now_mono
                try:
                    enabled = _deadman_env_enabled(platform)
                    last_ts = _deadman_last_db_success(cur, platform)
                    silent_hours = None
                    last_iso = None
                    if last_ts is not None:
                        last_iso = last_ts.astimezone(_tz.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                        silent_hours = round(
                            (datetime.now(_tz.utc) - last_ts).total_seconds() / 3600.0, 1)
                    # Fire ONLY when: creds configured + at least one publish
                    # EVER succeeded (never fire on a never-configured platform)
                    # + silence exceeds the threshold.
                    fired = bool(enabled and silent_hours is not None
                                 and silent_hours > _DEADMAN_SILENCE_HOURS)
                    finding = None
                    if fired:
                        st = _PUBLISHER_STATE.get(platform) or {}
                        detail = (
                            f"last_db_success_at={last_iso}; "
                            f"silent_hours={silent_hours}; "
                            f"threshold_hours={_DEADMAN_SILENCE_HOURS:g}; "
                            f"last_error_class={st.get('last_error_class')}; "
                            f"last_attempt_result={st.get('last_attempt_result')}; "
                            f"boot_started={st.get('boot_started')}; "
                            f"boot_disabled_reason={st.get('boot_disabled_reason')}; "
                            f"autopub_legacy_set={bool((os.environ.get('DCHUB_AUTOPUB_LEGACY', '') or '').strip())}; "
                            f"twitter_publisher_enabled_set={bool((os.environ.get('TWITTER_PUBLISHER_ENABLED', '') or '').strip())}"
                        )
                        if is_leader:
                            try:
                                from routes.brain_findings_writer import upsert_brain_finding
                                finding = upsert_brain_finding(
                                    cur,
                                    issue=f"publisher_silent:{platform}",
                                    url="https://dchub.cloud/api/v1/dchub-media/publisher-status",
                                    detail=detail,
                                    detector="publisher_deadman",
                                )
                                conn.commit()
                            except Exception as e:
                                finding = "error"
                                try:
                                    conn.rollback()
                                except Exception:
                                    pass
                                logger.warning(
                                    "publisher-deadman: finding write failed for %s: %s",
                                    platform, e)
                        else:
                            finding = "not_leader"
                        logger.warning(
                            "publisher-deadman: %s SILENT %.1fh (last DB success %s, "
                            "threshold %.0fh) — brain finding: %s",
                            platform, silent_hours, last_iso,
                            _DEADMAN_SILENCE_HOURS, finding)
                    _DEADMAN_STATE[platform] = {
                        "enabled": enabled,
                        "last_db_success_at": last_iso,
                        "silent_hours": silent_hours,
                        "fired": fired,
                        "finding": finding,
                        "checked_at": _utcnow_iso(),
                    }
                except Exception as e:
                    logger.warning("publisher-deadman: %s check failed: %s", platform, e)
                    try:
                        conn.rollback()
                    except Exception:
                        pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    except Exception as e:
        # Watchdog must never take a publisher loop down with it.
        try:
            logger.warning("publisher-deadman: tick failed: %s", e)
        except Exception:
            pass


def start_auto_publisher():
    global _auto_publisher_running
    if _auto_publisher_running:
        return
    _auto_publisher_running = True
    _record_boot("linkedin", True)

    def _auto_publish_loop():
        # Phase FF+7 (2026-05-18): 2-min initial delay (was 6h) so first
        # post lands soon after a container restart. Subsequent loops still
        # honor the 6h cadence.
        logger.info("LinkedIn auto-publisher started (initial 2min, then every 6h, cap=LINKEDIN_DAILY_CAP default 6/day)")
        _first = True
        while True:
            # r66: leader-only publish — a non-leader replica must not drain
            # the same queue twice (the double-post root cause). r-leaderwait
            # (2026-07-31): the old sleep(1800) skip lost the deploy-rollover
            # race every time — park on a 120s recheck instead.
            _wait_for_publish_leadership("linkedin")
            # Phase FF+7-fix4 (2026-05-19): hard guarantee that every
            # iteration closes its DB connection, even when sub-operations
            # raise. The earlier loop had a raw _get_db() assignment then ~50 lines
            # of work and `conn.close()` only in some branches. When an
            # exception fired mid-way (e.g. RealDictCursor KeyError, network
            # blip), the connection leaked. Across 3 publishers × N
            # iterations, this exhausted Neon's pool — every other endpoint
            # then timed out and Railway marked the container unhealthy.
            # 30-min outage 2026-05-19 was likely this. Wrap in try/finally.
            import traceback as _tb
            conn = None
            try:
                time.sleep(120 if _first else 6 * 3600)
                _first = False
                # ITEM deadman (2026-07-02): 72h platform-silence watchdog.
                # Self-throttled (1 check/platform/6h) + leader-gated write +
                # never raises, so it's safe on every tick even when this
                # replica isn't the publish leader.
                run_publisher_deadman_check()
                # 2026-07-31: DB-first token (env fallback). The drain used to
                # read ONLY the env var and 401'd (EXPIRED_ACCESS_TOKEN) while
                # the refresh cron kept a healthy token in the DB.
                access_token = _li_access_token()
                if not access_token:
                    # Loud-when-queued, quiet-when-empty surface
                    _queued = 0
                    try:
                        with _db_conn() as conn:
                            _qcur = conn.cursor()
                            _qcur.execute("SELECT COUNT(*) AS n FROM social_media_posts WHERE status = 'approved' AND (press_release_id IS NULL OR EXISTS (SELECT 1 FROM press_releases p WHERE p.id = press_release_id AND p.published = TRUE))")
                            _r = _qcur.fetchone() or {}
                            _queued = _r.get('n', 0) if hasattr(_r, 'get') else (_r[0] if _r else 0)
                    except Exception: pass
                    if _queued:
                        logger.warning("Auto-publisher: %s approved post(s) queued but no LinkedIn token (DB empty and LINKEDIN_ACCESS_TOKEN not set) — LinkedIn distribution is DARK", _queued)
                    else:
                        logger.debug("Auto-publisher: no LinkedIn token (DB or env), skipping")
                    continue
                with _db_conn() as conn:
                    cur = conn.cursor()
                    today, _next_day = _utc_day_bounds()
                    cur.execute("""SELECT COUNT(*) AS n FROM social_media_posts
                                    WHERE status = 'published'
                                      AND publish_platform = 'linkedin'
                                      AND published_at >= %s
                                      AND published_at < %s""", (today, _next_day))
                    _row = cur.fetchone() or {}
                    published_today = _row.get('n', 0) if hasattr(_row, 'get') else (_row[0] if _row else 0)
                    # r88 (2026-05-31): raise the daily cap MODESTLY + make it
                    # env-overridable to clear the ~189-approved backlog without
                    # spamming LinkedIn. Default 6/day drains ~189 over ~5 weeks.
                    # DO NOT uncap — dumping the whole backlog in a day reads as
                    # a spam bot and risks LinkedIn throttling/ban. Set
                    # LINKEDIN_DAILY_CAP=N in Railway to tune (e.g. 4 to slow,
                    # 8 to clear faster); keep it well under ~10/day.
                    DAILY_CAP = int(os.environ.get('LINKEDIN_DAILY_CAP', '6'))
                    if published_today >= DAILY_CAP:
                        logger.info(f"Auto-publisher: Already published {published_today} today (cap {DAILY_CAP}), skipping")
                        _record_attempt("linkedin", "skipped_cap")
                        continue  # finally will close conn
                    cur.execute("SELECT COUNT(*) AS n FROM social_media_posts WHERE status = 'approved' AND (press_release_id IS NULL OR EXISTS (SELECT 1 FROM press_releases p WHERE p.id = press_release_id AND p.published = TRUE))")
                    _row = cur.fetchone() or {}
                    _queued = _row.get('n', 0) if hasattr(_row, 'get') else (_row[0] if _row else 0)
                    # r42v (2026-05-26): cap drain at 1 per loop iteration to avoid
                    # three back-to-back AVOID posts (Chantilly/Edison/Buffalo) at
                    # 3m/4m timestamps that looked like a spam bot.
                    # r88 (2026-05-31): with a ~189-post backlog, 1/fire * 4 fires
                    # never reaches the raised cap. When the queue is large, fill
                    # the remaining daily budget THIS fire (each post still spaced
                    # ~8s apart, content-class deduped) so DAILY_CAP actually
                    # governs the rate; otherwise stay at 1/fire for clean cadence.
                    # Same backlog-drain shape the Bluesky loop already uses.
                    _remaining_today = max(DAILY_CAP - published_today, 0)
                    # r91 (2026-06-06): small-queue drain 1->2/fire so a healthy
                    # queue actually reaches DAILY_CAP (1/fire * 4 fires = 4 < cap 6
                    # left good content stuck). Still bounded by DAILY_CAP, 8s
                    # spacing, and per-content-class 1/day dedup — so no spam burst.
                    _drain_budget = _remaining_today if _queued > 10 else min(2, _remaining_today)
                    _attempts = 0
                    # Track which "content_class" patterns we've published TODAY
                    # so we can avoid double-firing the same post type. Pattern
                    # detection is lightweight: look at the first line of the
                    # body. Each pattern can publish at most 1/24h.
                    _seen_classes_today = set()
                    _seen_hooks_run = set()   # r65-qa: in-run opening-hook dedup (same-fire variants)
                    try:
                        # The identical SELECT used to run twice here: the first
                        # copy fed `for ... in cur.fetchall() if False else []`,
                        # which is a no-op loop, so it was a wasted round trip.
                        cur.execute("""SELECT content FROM social_media_posts
                                        WHERE status = 'published'
                                          AND publish_platform = 'linkedin'
                                          AND published_at >= %s
                                          AND published_at < %s""", (today, _next_day))
                        for _row in cur.fetchall() or []:
                            _txt = _row.get('content') if hasattr(_row, 'get') else (_row[0] if _row else '')
                            _seen_classes_today.add(_classify_post_for_dedup(_txt or ''))
                            _seen_hooks_run.add(_opening_hook(_txt or ''))
                    except Exception:
                        pass
                    while _attempts < _drain_budget:
                        # Find next approved post that's not a duplicate class
                        # Item 17 (2026-06-30): priority-first drain — a fresh win /
                        # citation (priority>0) jumps ahead of a stale FIFO backlog;
                        # priority defaults to 0 so same-priority rows keep the
                        # existing oldest-first order (idempotent for legacy rows).
                        # 2026-07-01: candidate window 20 → 60 (still bounded —
                        # never an unbounded scan). With ~74 approved posts the
                        # OLDEST 20 were all being skipped by the class/hook/
                        # judgment filters below, so the drain reported
                        # 'no_queued' while publishable newer-class posts sat
                        # invisible beyond position 20, silently aging toward the
                        # TTL 'expired' sweep (soft starvation). Filters and the
                        # approved/rejected/expired terminal-state contract are
                        # unchanged; this only widens what the filters get to see.
                        cur.execute("SELECT id, content, og_image FROM social_media_posts WHERE status = 'approved' AND platform = 'linkedin' AND (press_release_id IS NULL OR EXISTS (SELECT 1 FROM press_releases p WHERE p.id = press_release_id AND p.published = TRUE)) ORDER BY priority DESC, created_at ASC LIMIT 60")
                        candidates = cur.fetchall() or []
                        if not candidates:
                            cur.execute("SELECT id, content, og_image FROM social_media_posts WHERE status = 'approved' AND (press_release_id IS NULL OR EXISTS (SELECT 1 FROM press_releases p WHERE p.id = press_release_id AND p.published = TRUE)) ORDER BY priority DESC, created_at ASC LIMIT 60")
                            candidates = cur.fetchall() or []

                        row = None
                        _filtered_skips = 0  # 2026-07-01: starvation visibility
                        for _cand in candidates:
                            _ctext = _cand.get('content') if hasattr(_cand, 'get') else (_cand[1] if _cand else '')
                            _cls = _classify_post_for_dedup(_ctext or '')
                            if _cls in _seen_classes_today:
                                _filtered_skips += 1
                                continue
                            # r65-qa: same-fire opening-hook guard — blocks a 2nd
                            # variant sharing the same hook within ONE drain, before
                            # the prior publish commits (the citation-dup case).
                            _chook = _opening_hook(_ctext or '')
                            if _chook and _chook in _seen_hooks_run:
                                logger.warning("Auto-publisher: SKIPPED LinkedIn candidate (same-fire duplicate hook '%s')", _chook[:48])
                                _record_media_block('linkedin', 'same-fire duplicate hook', _ctext)   # r66
                                _filtered_skips += 1
                                continue
                            # r63 (2026-05-29): entity-level + zero-stat judgment.
                            # Catches near-dupes the coarse class tag misses (two
                            # "Montréal BUILD" or "MCP tool-call surge" posts both
                            # land in class "other"), AND blocks "0 tool calls"
                            # zero-stat posts. Skipping a candidate here naturally
                            # rotates to a DIFFERENT topic in the same drain.
                            _skip, _why = _should_skip_publish(cur, _ctext or '', 'linkedin')
                            if _skip:
                                _cand_id = (_cand.get('id') if hasattr(_cand, 'get') else (_cand[0] if _cand else None))
                                logger.warning(
                                    "Auto-publisher: SKIPPED LinkedIn candidate %s for dedup/judgment — %s",
                                    _cand_id if _cand_id is not None else '?', _why)
                                _record_media_block('linkedin', _why, _ctext)   # r66 evolving loop
                                # 2026-07-11: r78 parity with the Twitter/Bluesky
                                # loops — _should_skip_publish reasons are
                                # content-intrinsic (quality/editor/dup-vs-
                                # published) and never pass on retry, so mark the
                                # row TERMINAL 'rejected'. Before this, skipped
                                # candidates stayed 'approved' forever: the same
                                # ~39-row backlog was re-judged (editor-LLM cost
                                # included) every 6h fire while aging toward the
                                # 14d TTL sweep, and the queue looked permanently
                                # sick. The time-scoped class/hook checks ABOVE
                                # keep their non-terminal `continue`.
                                if _cand_id is not None:
                                    try:
                                        cur.execute("UPDATE social_media_posts SET status = 'rejected' WHERE id = %s", (_cand_id,))
                                        conn.commit()
                                    except Exception:
                                        note_swallowed_write("social_media_posts", where="content_publisher._auto_publish_loop.skip_reject")
                                        pass
                                _filtered_skips += 1
                                continue
                            row = _cand
                            _seen_classes_today.add(_cls)
                            if _chook:
                                _seen_hooks_run.add(_chook)
                            break
                        if not row:
                            if _filtered_skips > 3:
                                # 2026-07-01: soft-starvation visibility — a whole
                                # window skipped by filters is NOT an empty queue.
                                # One loud line + a publisher-status field so the
                                # backlog aging toward the TTL sweep is observable.
                                logger.warning(
                                    "Auto-publisher: STARVATION — %d approved LinkedIn candidate(s) in window all skipped by class/hook/judgment filters (queued=%s); none published this fire",
                                    _filtered_skips, _queued)
                                try:
                                    _st = _PUBLISHER_STATE.get('linkedin')
                                    if _st is not None:
                                        _st['last_all_filtered_at'] = _utcnow_iso()
                                        _st['last_all_filtered_count'] = _filtered_skips
                                except Exception:
                                    pass
                            else:
                                logger.debug("Auto-publisher: No approved posts to publish (or all classes/entities already fired)")
                            if _attempts == 0:
                                _record_attempt("linkedin", "no_queued")
                            break
                        post_id = row['id']
                        content_text = row['content']
                        # r62 (2026-05-29): legacy-shape quality gate. If this row
                        # is a pre-r47.38 short DCPI post (📍 X · ISO · DCPI
                        # verdict: VVV / Excess Power: ... / Live page: ...),
                        # rewrite it to the rich narrative shape BEFORE publish.
                        # Without this, queue rows enqueued days ago keep landing
                        # on LinkedIn as "ugly short" posts despite the generator
                        # being fixed. We also persist the rewrite back to the
                        # row so the audit log shows what actually went out.
                        if _is_legacy_short_dcpi_shape(content_text or ''):
                            rewritten = _rewrite_legacy_to_rich(content_text or '')
                            if rewritten and len(rewritten) > len(content_text or ''):
                                try:
                                    cur.execute(
                                        "UPDATE social_media_posts SET content = %s WHERE id = %s",
                                        (rewritten, post_id),
                                    )
                                    conn.commit()
                                except Exception as _e_rw:
                                    logger.warning(
                                        "Legacy-rewrite persist failed for post %s: %s",
                                        post_id, _e_rw,
                                    )
                                content_text = rewritten
                                logger.info(
                                    "Rewrote legacy short-DCPI post %s to rich shape (was %d chars, now %d)",
                                    post_id,
                                    len(row.get('content') or '') if hasattr(row, 'get') else len(row[1] or ''),
                                    len(rewritten),
                                )
                        # Phase FF (#1): promote text-only posts to rich ARTICLE
                        # shares so LinkedIn renders the rotating og:today card (the
                        # "4 designs"). Extract the first URL + a title from the body;
                        # _post_to_linkedin builds an ARTICLE share whose card image
                        # LinkedIn scrapes from that URL's og:image — press-release
                        # pages point og:image at /api/v1/og/today/<slug>.png. This
                        # is the reason posts were weak text-only: line passed no
                        # article_url. FAIL-SAFE: any error / no URL → text-only
                        # (prior behaviour), so it can never make a post worse.
                        _art_url = None
                        _art_title = None
                        try:
                            import re as _re_url
                            _m = _re_url.search(r'https?://[^\s)>\]]+', content_text or '')
                            if _m:
                                _art_url = _m.group(0).rstrip('.,')
                                _first_line = (content_text or '').strip().split('\n', 1)[0].strip()
                                _art_title = _first_line[:180] or None
                        except Exception:
                            _art_url = None
                            _art_title = None
                        # 2026-07-16: attach the row's intended branded card
                        # DIRECTLY (no fragile scrape / ai_hero fallback). NULL ->
                        # prior scrape/fallback behaviour.
                        try:
                            _row_og = (row.get('og_image') if hasattr(row, 'get') else None) or None
                        except Exception:
                            _row_og = None
                        # ★2026-08-22 Claim Loop step 1: PRE-REGISTER the post as
                        # a claim with its expected engagement BEFORE it ships.
                        # The ledger refuses a claim with no expectation; the
                        # outcome is stamped at horizon by the L16 cron, never
                        # here. Fail-soft: a ledger outage cannot hold the
                        # publisher, and the helper uses its OWN connection so
                        # it can never abort this transaction.
                        _claim_id = None
                        try:
                            from routes.claim_ledger import register_linkedin_post_claim as _reg_post_claim
                            _claim_id = _reg_post_claim(post_id, content_text, article_url=_art_url)
                        except Exception as _claim_e:
                            logger.warning("claim-ledger: pre-registration failed for post %s: %s", post_id, _claim_e)
                        success, result = _post_to_linkedin(
                            content_text, access_token,
                            article_url=_art_url, article_title=_art_title,
                            article_thumbnail_url=_row_og)
                        now = datetime.utcnow().isoformat() + 'Z'
                        if success:
                            cur.execute("UPDATE social_media_posts SET status = 'published', posted_at = %s, published_at = %s, publish_platform = 'linkedin' WHERE id = %s", (now, now, post_id))
                            # r72: capture URN → engagement loop can find this post.
                            _persist_linkedin_urn(cur, post_id, result, content_text, article_url=_art_url)
                            conn.commit()
                            # Claim Loop: the share is out — start the horizon clock.
                            if _claim_id is not None:
                                try:
                                    from routes.claim_ledger import stamp_shipped as _stamp_claim_shipped
                                    _stamp_claim_shipped(_claim_id)
                                except Exception as _claim_e2:
                                    logger.warning("claim-ledger: stamp_shipped failed for claim %s: %s", _claim_id, _claim_e2)
                            logger.info(f"Auto-published post {post_id} to LinkedIn (drain {_attempts+1}/{_drain_budget}, queued={_queued}, urn={result})")
                            _record_attempt("linkedin", "ok")
                        else:
                            # 2026-07-11: split editorial-gate refusals from real
                            # API errors. Refusals (quality/policy/duplicate gates
                            # inside _post_to_linkedin) are content-intrinsic and
                            # never pass on retry → terminal 'rejected' (r78
                            # contract), recorded to the media-review loop so the
                            # generator learns. 'failed' is reserved for actual
                            # API/transport errors so failure metrics mean
                            # something again (21/21 "failures" in the 07-11
                            # audit were duplicate-gate refusals of queue-flood
                            # repeats, not publish errors).
                            _refusal = _li_gate_refusal(result)
                            if _refusal:
                                logger.warning(
                                    "Auto-publisher: REJECTED post %s (gate refusal, not an error) — %s",
                                    post_id, _refusal)
                                _record_media_block('linkedin', _refusal, content_text or '')
                                _record_attempt("linkedin", "refused_gate")
                                try:
                                    cur.execute("UPDATE social_media_posts SET status = 'rejected' WHERE id = %s", (post_id,))
                                    conn.commit()
                                except Exception:
                                    note_swallowed_write("social_media_posts", where="content_publisher._auto_publish_loop")
                                    pass
                            else:
                                logger.warning(f"Auto-publish failed for post {post_id}: {result}")
                                _record_attempt("linkedin", "error",
                                                 error_class=_classify_publish_error(result))
                                try:
                                    # growth-fix (2026-07-19): persist WHY. 31 failed
                                    # rows in 14d carried no error — the class lived
                                    # only in memory and vanished on every deploy, so
                                    # the lossy-LinkedIn problem was undiagnosable.
                                    cur.execute(
                                        "UPDATE social_media_posts SET status = 'failed', "
                                        "engagement_data = %s WHERE id = %s",
                                        (json.dumps({
                                            "error_class": _classify_publish_error(result),
                                            "error": str(result)[:400],
                                            "at": datetime.utcnow().isoformat() + "Z",
                                        }), post_id))
                                    conn.commit()
                                except Exception:
                                    note_swallowed_write("social_media_posts", where="content_publisher._auto_publish_loop")
                                    pass
                        _attempts += 1
                        if _attempts < _drain_budget:
                            time.sleep(8)
            except Exception as e:
                # Log FULL traceback so we can diagnose, not just str(e)
                logger.error(f"Auto-publisher error: {type(e).__name__}: {e}")
                logger.error(_tb.format_exc())
            finally:
                # GUARANTEE conn closed every iteration — prevents the pool
                # exhaustion that was likely behind the 2026-05-19 outage.
                if conn is not None:
                    try: conn.close()
                    except Exception: pass

    t = threading.Thread(target=_auto_publish_loop, daemon=True, name="linkedin-auto-publisher")
    t.start()


_twitter_publisher_running = False

def start_twitter_publisher():
    """Phase FF+3 (2026-05-13): parallel auto-publisher for X/Twitter.
    Same shape as the LinkedIn loop. Runs every 6h, max 2/day, gated
    on TWITTER_BEARER_TOKEN OR the OAuth1 quad.

    r41-twitter-disabled (2026-05-25): DISABLED. Three OAuth token
    rotations all failed with 'keys and tokens from a Twitter
    developer App that is attached to a Project' (X API v2 requires
    apps to live inside a Project; the existing app is a legacy
    standalone). Until the dev-portal app is migrated into a Project,
    every cycle just generates 403 log noise. Early-return short-
    circuits the loop entirely. To re-enable: migrate the app, then
    delete this guard.
    """
    global _twitter_publisher_running
    if _twitter_publisher_running:
        return
    if os.environ.get('TWITTER_PUBLISHER_ENABLED', 'false').lower() not in ('1', 'true', 'yes'):
        # Item 17 (2026-06-30): make the DARK-with-backlog case LOUD. The publisher
        # was silently gated off at INFO while approved X rows piled up (the
        # "0 posts/7d, 221 queued" symptom). If there IS a queued backlog, escalate
        # to a WARNING that names the count + the exact env var to flip, and stamp
        # the reason with the depth so the boot record shows it. Fail-soft: any DB
        # hiccup falls back to the original INFO line — never fake a post.
        _queued = None
        try:
            with _db_conn() as _c:
                _qc = _c.cursor()
                _qc.execute("SELECT COUNT(*) AS n FROM social_media_posts WHERE status = 'approved' AND platform = 'twitter' AND (press_release_id IS NULL OR EXISTS (SELECT 1 FROM press_releases p WHERE p.id = press_release_id AND p.published = TRUE))")
                _r = _qc.fetchone() or {}
                _queued = _r.get('n', 0) if hasattr(_r, 'get') else (_r[0] if _r else 0)
        except Exception:
            _queued = None
        if _queued:
            logger.warning(
                "X/Twitter auto-publisher DISABLED but %s approved X post(s) are "
                "QUEUED and going DARK — set TWITTER_PUBLISHER_ENABLED=true (once "
                "the X app is migrated into a Project) to drain them", _queued)
            _reason = f"TWITTER_PUBLISHER_ENABLED missing/false; {_queued} approved X posts queued (DARK)"
        else:
            logger.info("X/Twitter auto-publisher DISABLED — set TWITTER_PUBLISHER_ENABLED=true to re-enable once app is in a Project")
            _reason = "TWITTER_PUBLISHER_ENABLED missing or false"
        _record_boot("twitter", False, reason=_reason)
        return
    _twitter_publisher_running = True
    _record_boot("twitter", True)

    def _twitter_loop():
        # Phase FF+7 (2026-05-18): 2-min first-run delay so posts go out
        # soon after restart instead of 6h dark.
        logger.info("X/Twitter auto-publisher started (initial 2min, then every 6h, max 3/day)")
        _first = True
        while True:
            # Phase FF+7-fix4 (2026-05-19): try/finally guarantees conn.close()
            # to prevent Neon pool exhaustion. Same pattern as LinkedIn loop.
            import traceback as _tb
            conn = None
            try:
                time.sleep(150 if _first else 6 * 3600)
                _first = False
                # ITEM deadman (2026-07-02): 72h platform-silence watchdog
                # (self-throttled + leader-gated write; see LinkedIn loop).
                run_publisher_deadman_check()
                # r78: per-cycle leader re-check (LinkedIn got this in r66;
                # X/Bluesky only checked the import-time IS_LEADER snapshot,
                # so a double-leader window — keepalive fail-open, gunicorn
                # worker recycle re-election — could double-post here).
                # r-leaderwait (2026-07-31): HOLD the due fire until leadership
                # resolves — the old `continue` pushed the recheck a full 6h
                # out, so a deploy-rollover loser stayed dark all evening.
                _wait_for_publish_leadership("twitter")
                bearer = os.environ.get('TWITTER_BEARER_TOKEN', '')
                oauth1 = all([os.environ.get(k, '') for k in
                              ('TWITTER_API_KEY', 'TWITTER_API_SECRET',
                               'TWITTER_ACCESS_TOKEN', 'TWITTER_ACCESS_SECRET')])
                if not (bearer or oauth1):
                    _queued = 0
                    try:
                        with _db_conn() as conn:
                            _qcur = conn.cursor()
                            _qcur.execute("SELECT COUNT(*) AS n FROM social_media_posts WHERE status = 'approved' AND platform = 'twitter' AND (press_release_id IS NULL OR EXISTS (SELECT 1 FROM press_releases p WHERE p.id = press_release_id AND p.published = TRUE))")
                            _r = _qcur.fetchone() or {}
                            _queued = _r.get('n', 0) if hasattr(_r, 'get') else (_r[0] if _r else 0)
                    except Exception: pass
                    if _queued:
                        logger.warning("Twitter auto-publisher: %s approved X post(s) queued but no credentials set — X distribution is DARK", _queued)
                    else:
                        logger.debug("Twitter auto-publisher: no credentials, skipping")
                    continue
                # ITEM 6 (2026-06-14): dead-token circuit breaker. After N
                # consecutive auth/forbidden failures, the X token is expired
                # (the 132-silent-403 pattern). STOP retrying it every cycle —
                # raise the owner re-auth action ONCE and skip the publish until
                # a human re-auths (any success auto-clears the breaker, so the
                # loop self-heals on re-auth with no manual un-pause). This is
                # the engine-side complement to the social_publish_silent_failure
                # autopilot finding: instead of 132 silent 403s, the operator
                # gets one clear "re-auth X" action and the log goes quiet.
                if _auth_breaker_tripped("twitter"):
                    _fails = (_PUBLISHER_STATE.get("twitter") or {}).get(
                        "consecutive_auth_failures", 0)
                    logger.warning(
                        "Twitter auto-publisher: CIRCUIT BREAKER OPEN — %s "
                        "consecutive auth failures. X token is expired/revoked. "
                        "OWNER ACTION: regenerate TWITTER_ACCESS_TOKEN (+secret) "
                        "in Railway env, then POST /api/v1/marketing/publish-now"
                        "?max=20 to drain the backlog. Retries paused until a "
                        "publish succeeds.", _fails)
                    _record_attempt("twitter", "skipped_cap")  # paused, not erroring
                    continue
                with _db_conn() as conn:
                    cur = conn.cursor()
                    today, _next_day = _utc_day_bounds()
                    cur.execute("""SELECT COUNT(*) AS n FROM social_media_posts
                                    WHERE status = 'published'
                                      AND publish_platform = 'twitter'
                                      AND published_at >= %s
                                      AND published_at < %s""", (today, _next_day))
                    _row = cur.fetchone() or {}
                    pub_today = _row.get('n', 0) if hasattr(_row, 'get') else (_row[0] if _row else 0)
                    if pub_today >= 2:
                        logger.info(f"Twitter auto-publisher: already {pub_today} today, skipping")
                        _record_attempt("twitter", "skipped_cap")
                        continue
                    # X-diversity port (2026-07-31): the LinkedIn drain's per-class
                    # 1/day rule, keyed on SOURCE class (_x_source_class) because
                    # with only 2 slots/day the press-release template was taking
                    # both (22 of 27 X posts in the 14d re-measure). Seen-set is
                    # rebuilt from today's published rows each fire, same shape as
                    # _seen_classes_today in the LinkedIn loop. Fail-open: an
                    # unreadable seen-set never dark-holds the publisher.
                    _seen_x_classes = set()
                    try:
                        cur.execute("""SELECT content, press_release_id, lead_kind
                                         FROM social_media_posts
                                        WHERE status = 'published'
                                          AND publish_platform = 'twitter'
                                          AND published_at >= %s
                                          AND published_at < %s""", (today, _next_day))
                        for _r in cur.fetchall() or []:
                            _seen_x_classes.add(_x_source_class(
                                _r.get('press_release_id') if hasattr(_r, 'get') else _r[1],
                                _r.get('lead_kind') if hasattr(_r, 'get') else _r[2],
                                (_r.get('content') if hasattr(_r, 'get') else _r[0]) or ''))
                    except Exception:
                        _seen_x_classes = set()
                    # Item 17 (2026-06-30): priority-first drain (priority defaults
                    # to 0 so legacy rows keep oldest-first order). 2026-07-31:
                    # LIMIT 1 -> bounded candidate window, so a class-conflicted
                    # head row no longer takes the day's slot by default — the
                    # drain scans past it to a row from a class that hasn't fired
                    # today (the LinkedIn drain's rotation shape).
                    cur.execute("SELECT id, content, press_release_id, lead_kind FROM social_media_posts WHERE status = 'approved' AND platform = 'twitter' AND (press_release_id IS NULL OR EXISTS (SELECT 1 FROM press_releases p WHERE p.id = press_release_id AND p.published = TRUE)) ORDER BY priority DESC, created_at ASC LIMIT 40")
                    candidates = cur.fetchall() or []
                    if not candidates:
                        logger.debug("Twitter auto-publisher: no approved Twitter posts")
                        _record_attempt("twitter", "no_queued")
                        continue
                    row = None
                    post_id = None
                    content_text = ''
                    _class_skips = 0
                    for _cand in candidates:
                        _cid = _cand.get('id') if hasattr(_cand, 'get') else _cand[0]
                        _ctext = (_cand.get('content') if hasattr(_cand, 'get') else _cand[1]) or ''
                        _cls = _x_source_class(
                            _cand.get('press_release_id') if hasattr(_cand, 'get') else _cand[2],
                            _cand.get('lead_kind') if hasattr(_cand, 'get') else _cand[3],
                            _ctext)
                        # Time-scoped class conflict — NON-terminal (the LinkedIn
                        # drain's semantics): the row stays approved and becomes
                        # eligible when the day rolls over; content-intrinsic
                        # refusals below stay terminal (r78).
                        if _cls in _seen_x_classes:
                            _class_skips += 1
                            continue
                        # Item 17 (2026-06-30): content_hash dedupe — if this exact
                        # body already published (on ANY platform), don't re-ship
                        # it. Cheap sha256 lookup against the content_hash index;
                        # advances the queue by marking the dup 'rejected' (same
                        # terminal-advance the r78 skip path uses). Fail-soft: any
                        # error falls through to the normal publish path.
                        try:
                            import hashlib as _hl
                            _chash = _hl.sha256(_ctext.strip().encode('utf-8')).hexdigest()
                            cur.execute("UPDATE social_media_posts SET content_hash = %s WHERE id = %s AND content_hash IS DISTINCT FROM %s", (_chash, _cid, _chash))
                            cur.execute("SELECT 1 FROM social_media_posts WHERE content_hash = %s AND status = 'published' LIMIT 1", (_chash,))
                            if cur.fetchone():
                                logger.warning("Twitter auto-publisher: SKIPPED post %s — content_hash already published (dedupe)", _cid)
                                cur.execute("UPDATE social_media_posts SET status = 'rejected' WHERE id = %s", (_cid,))
                                conn.commit()
                                continue
                        except Exception:
                            try: conn.rollback()
                            except Exception: pass
                        # r63 (2026-05-29): same entity-level + zero-stat judgment
                        # as the LinkedIn loop, judged on the wire text.
                        _skip, _why = _should_skip_publish(cur, _ctext, 'twitter')
                        if _skip:
                            logger.warning("Twitter auto-publisher: SKIPPED post %s for dedup/judgment — %s", _cid, _why)
                            # r78: TERMINAL reject — skip reasons here are
                            # content-intrinsic (quality/dedup/editor) and never
                            # pass on retry, so mark the row rejected to advance
                            # the queue, and feed the lesson back to the
                            # generator the way the LinkedIn path already does.
                            cur.execute("UPDATE social_media_posts SET status = 'rejected' WHERE id = %s", (_cid,))
                            conn.commit()
                            _record_media_block('twitter', _why, _ctext)
                            continue
                        row = _cand
                        post_id = _cid
                        content_text = _ctext
                        break
                    if row is None:
                        if _class_skips:
                            logger.warning(
                                "Twitter auto-publisher: %d approved candidate(s) skipped by the per-class 1/day rule (classes already fired today: %s); none published this fire",
                                _class_skips, sorted(_seen_x_classes))
                        _record_attempt("twitter", "no_queued")
                        continue
                    success, result = _post_to_twitter(content_text)
                    now = datetime.utcnow().isoformat() + 'Z'
                    if success:
                        # r-xid (2026-07-18): persist the tweet id. _post_to_twitter
                        # returns it on success but it was discarded — 35 published
                        # rows, 0 twitter_id ever — which made the radar's
                        # twitter_id-keyed x_publisher_dead a permanent false
                        # positive on a HEALTHY publisher (open 397h).
                        cur.execute("UPDATE social_media_posts SET status = 'published', posted_at = %s, published_at = %s, publish_platform = 'twitter', twitter_id = %s WHERE id = %s", (now, now, str(result)[:64], post_id))
                        conn.commit()
                        logger.info(f"Auto-published post {post_id} to X")
                        _record_attempt("twitter", "ok")
                    else:
                        logger.warning(f"Twitter auto-publish failed for {post_id}: {result}")
                        _record_attempt("twitter", "error",
                                         error_class=_classify_publish_error(result))
            except Exception as e:
                logger.error(f"Twitter auto-publisher error: {type(e).__name__}: {e}")
                logger.error(_tb.format_exc())
            finally:
                if conn is not None:
                    try: conn.close()
                    except Exception: pass

    t = threading.Thread(target=_twitter_loop, daemon=True,
                         name="twitter-auto-publisher")
    t.start()


# Phase DDD (2026-05-17) — Bluesky auto-publisher loop.
# Phase PP shipped the standalone _post_to_bluesky function. Phase VV
# wired auto-press to enqueue platform='bluesky' rows. Without this loop,
# those rows pile up in social_media_posts forever. Mirrors the LinkedIn
# + Twitter shape: every 6h, max 2/day, gated on BLUESKY_HANDLE +
# BLUESKY_APP_PASSWORD env vars.
_bluesky_publisher_running = False


def start_bluesky_publisher():
    global _bluesky_publisher_running
    if _bluesky_publisher_running:
        return
    _bluesky_publisher_running = True
    _record_boot("bluesky", True)

    def _bsky_loop():
        # Phase FF+7 (2026-05-18): 2-min first-run delay + 3/day cap.
        logger.info("Bluesky auto-publisher started (initial 2min, then every 6h, max 3/day)")
        _first = True
        while True:
            # Phase FF+7-fix4 (2026-05-19): try/finally guarantees conn.close()
            # to prevent Neon pool exhaustion. Same pattern as LinkedIn loop.
            import traceback as _tb
            conn = None
            try:
                time.sleep(180 if _first else 6 * 3600)
                _first = False
                # ITEM deadman (2026-07-02): 72h platform-silence watchdog
                # (self-throttled + leader-gated write; see LinkedIn loop).
                run_publisher_deadman_check()
                # r78: per-cycle leader re-check — see Twitter loop note.
                # r-leaderwait (2026-07-31): hold the due fire on a 120s
                # recheck instead of skipping into the next 6h sleep.
                _wait_for_publish_leadership("bluesky")
                handle  = os.environ.get('BLUESKY_HANDLE', '').strip()
                app_pwd = os.environ.get('BLUESKY_APP_PASSWORD', '').strip()
                if not handle or not app_pwd:
                    _queued = 0
                    try:
                        with _db_conn() as conn:
                            _qcur = conn.cursor()
                            _qcur.execute(
                                "SELECT COUNT(*) AS n FROM social_media_posts "
                                "WHERE status = 'approved' AND platform = 'bluesky'"
                                " AND (press_release_id IS NULL OR EXISTS (SELECT 1 FROM press_releases p WHERE p.id = press_release_id AND p.published = TRUE))")
                            _r = _qcur.fetchone() or {}
                            _queued = _r.get('n', 0) if hasattr(_r, 'get') else (_r[0] if _r else 0)
                    except Exception: pass
                    if _queued:
                        logger.warning("Bluesky auto-publisher: %s approved post(s) queued "
                                        "but BLUESKY_HANDLE/BLUESKY_APP_PASSWORD not set — "
                                        "Bluesky distribution is DARK", _queued)
                    else:
                        logger.debug("Bluesky auto-publisher: no credentials, skipping")
                    continue

                with _db_conn() as conn:
                    cur = conn.cursor()
                    today, _next_day = _utc_day_bounds()
                    # Phase FF+7-fix3 (2026-05-19): RealDictCursor — pull by name.
                    cur.execute(
                        "SELECT COUNT(*) AS n FROM social_media_posts WHERE status = 'published' "
                        "AND publish_platform = 'bluesky' "
                        "AND published_at >= %s AND published_at < %s",
                        (today, _next_day))
                    _row = cur.fetchone() or {}
                    pub_today = _row.get('n', 0) if hasattr(_row, 'get') else (_row[0] if _row else 0)
                    DAILY_CAP = 3
                    if pub_today >= DAILY_CAP:
                        logger.info(f"Bluesky auto-publisher: already {pub_today} today, skipping")
                        _record_attempt("bluesky", "skipped_cap")
                        continue  # finally will close conn

                    # Phase FF+7 (2026-05-18): Bluesky was filtering for
                    # platform='bluesky' rows ONLY, but auto-press enqueues with
                    # platform='linkedin' by default. Result: Bluesky publisher
                    # found 0 rows every cycle and stayed silent (0 posts in 7d
                    # despite being configured). Match LinkedIn's pattern: try
                    # platform-specific first, fall back to any approved post.
                    # Also backlog-drain like LinkedIn.
                    cur.execute("SELECT COUNT(*) AS n FROM social_media_posts WHERE status = 'approved' AND (press_release_id IS NULL OR EXISTS (SELECT 1 FROM press_releases p WHERE p.id = press_release_id AND p.published = TRUE))")
                    _row = cur.fetchone() or {}
                    _queued = _row.get('n', 0) if hasattr(_row, 'get') else (_row[0] if _row else 0)
                    _drain_budget = (DAILY_CAP - pub_today) if _queued > 10 else 1
                    _attempts = 0
                    while _attempts < _drain_budget:
                        # Item 17 (2026-06-30): priority-first drain (priority
                        # defaults to 0 so legacy rows keep oldest-first order).
                        cur.execute("SELECT id, content FROM social_media_posts "
                                     "WHERE status = 'approved' AND platform = 'bluesky'"
                                     " AND (press_release_id IS NULL OR EXISTS (SELECT 1 FROM press_releases p WHERE p.id = press_release_id AND p.published = TRUE)) "
                                     "ORDER BY priority DESC, created_at ASC LIMIT 1")
                        row = cur.fetchone()
                        if not row:
                            # Fallback: any approved post that hasn't been
                            # published to bluesky yet. Re-using a LinkedIn-targeted
                            # post on Bluesky is fine — different audience, same idea.
                            cur.execute(
                                "SELECT id, content FROM social_media_posts "
                                "WHERE status = 'approved' "
                                "AND (press_release_id IS NULL OR EXISTS ("
                                "SELECT 1 FROM press_releases p WHERE p.id = press_release_id "
                                "AND p.published = TRUE)) "
                                "AND (publish_platform IS NULL OR publish_platform != 'bluesky') "
                                "ORDER BY priority DESC, created_at ASC LIMIT 1")
                            row = cur.fetchone()
                        if not row:
                            logger.debug("Bluesky auto-publisher: no approved posts")
                            if _attempts == 0:
                                _record_attempt("bluesky", "no_queued")
                            break
                        post_id = row['id']
                        content_text = row['content']
                        # r63 (2026-05-29): entity-level + zero-stat judgment.
                        # Bluesky re-selects the same oldest row each drain
                        # iteration (no exclusion), so on a skip we BREAK to end
                        # this cycle's drain rather than spin on the same row.
                        _skip, _why = _should_skip_publish(cur, content_text or '', 'bluesky')
                        if _skip:
                            logger.warning("Bluesky auto-publisher: SKIPPED post %s for dedup/judgment — %s", post_id, _why)
                            # r78: TERMINAL reject instead of break — breaking
                            # on the head row wedged the whole drain on one bad
                            # post (post 751, 5 days). Mark rejected so the
                            # re-SELECT advances; count it against the drain
                            # budget so a junk backlog can't burn unbounded
                            # editor LLM calls in one cycle.
                            cur.execute("UPDATE social_media_posts SET status = 'rejected' WHERE id = %s", (post_id,))
                            conn.commit()
                            _record_media_block('bluesky', _why, content_text or '')
                            _attempts += 1
                            continue
                        ok, result = _post_to_bluesky(content_text)
                        now = datetime.utcnow().isoformat() + 'Z'
                        if ok:
                            cur.execute(
                                "UPDATE social_media_posts SET status = %s, "
                                "       posted_at = %s, published_at = %s, "
                                "       publish_platform = %s WHERE id = %s",
                                ('published', now, now, 'bluesky', post_id))
                            conn.commit()
                            logger.info(f"Auto-published post {post_id} to Bluesky uri={result} (drain {_attempts+1}/{_drain_budget})")
                            _record_attempt("bluesky", "ok")
                        else:
                            logger.warning(f"Bluesky auto-publish failed for post {post_id}: {result}")
                            _record_attempt("bluesky", "error",
                                             error_class=_classify_publish_error(result))
                            try:
                                cur.execute(
                                    "UPDATE social_media_posts SET status = 'failed', "
                                    "engagement_data = %s WHERE id = %s",
                                    (json.dumps({
                                        "error_class": _classify_publish_error(result),
                                        "error": str(result)[:400],
                                        "at": datetime.utcnow().isoformat() + "Z",
                                    }), post_id))
                                conn.commit()
                            except Exception:
                                note_swallowed_write("social_media_posts", where="content_publisher._bsky_loop")
                                pass
                        _attempts += 1
                        if _attempts < _drain_budget:
                            time.sleep(5)
            except Exception as e:
                logger.error(f"Bluesky auto-publisher error: {type(e).__name__}: {e}")
                logger.error(_tb.format_exc())
            finally:
                if conn is not None:
                    try: conn.close()
                    except Exception: pass

    t = threading.Thread(target=_bsky_loop, daemon=True,
                         name="bluesky-auto-publisher")
    t.start()


def register_content_publisher(app):
    init_content_tables()
    app.register_blueprint(content_bp)
    # Phase FF+3 (2026-05-13): start both auto-publishers. Each is
    # idempotent on _running flags, so multi-worker boots are safe.
    # Each gate themselves on the relevant env var so unconfigured
    # channels just log a debug and skip.
    #
    # ★2026-08-22 (step 7c) — r-rolesplit role gate. The auto-publisher THREADS
    # are singleton background machinery and belong to DCHUB_ROLE=worker only.
    # This registrar had NO role gate, so on a DCHUB_ROLE=web replica it started
    # all three loops UNCONDITIONALLY — bypassing main.py's own _ROLE_RUNS_BG-gated
    # start block — and every loop then parked forever in
    # _wait_for_publish_leadership (is_current_leader is always False on web),
    # burning three idle threads per web replica for nothing. Gate the
    # thread-starts here; the BLUEPRINT (admin routes) is still registered on
    # every role above. main.py's gated start block stays idempotent with this
    # one via the _*_running flags, so the worker still starts each loop once.
    _role = (os.environ.get("DCHUB_ROLE", "all").strip().lower() or "all")
    if _role == "web":
        logger.info("Content auto-publishers SKIPPED (DCHUB_ROLE=web — worker owns publishing; routes still registered)")
    else:
        try:
            start_auto_publisher()       # LinkedIn
        except Exception as e:
            logger.warning(f"LinkedIn auto-publisher failed to start: {e}")
        try:
            start_twitter_publisher()    # X/Twitter
        except Exception as e:
            logger.warning(f"Twitter auto-publisher failed to start: {e}")
        try:
            start_bluesky_publisher()    # Bluesky (Phase DDD)
        except Exception as e:
            logger.warning(f"Bluesky auto-publisher failed to start: {e}")
    logger.info("Content Publishing Pipeline registered")
    logger.info("   GET  /api/admin/content/stats")
    logger.info("   GET  /api/admin/content-queue")
    logger.info("   POST /api/admin/content/<id>/approve")
    logger.info("   POST /api/admin/content/<id>/reject")
    logger.info("   POST /api/admin/content/<id>/edit")
    logger.info("   POST /api/admin/publish/linkedin")
    logger.info("   POST /api/admin/publish/bluesky")
    logger.info("   Auto-publishers: LinkedIn + X/Twitter + Bluesky (every 6h, max 2/day)")
