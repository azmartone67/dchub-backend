"""routes/media_published_review.py — the third media feedback loop.

WHY THIS EXISTS (2026-08-25)
----------------------------
The media desk had three loops and only two of them existed:

    what to talk about            LIVE   eng_rate/eng_weight bandit
    how to write                  LIVE   refusals -> composer prompt (#3167)
    is a PUBLISHED post any good  ABSENT

The gap was not that nothing measured a published post. The claim ledger
DOES pre-register every auto-published LinkedIn post (content_publisher ->
claim_ledger.register_linkedin_post_claim) with an engagement expectation.
Three things were wrong with treating that as the grade, all measured on
2026-08-25 against /api/v1/brain/media/linkedin-engagement-scoreboard
(45 days, 141 posts) and /api/v1/brain/claims:

  1. THE BAR CANNOT FAIL. It is floor(0.5 x 30d avg impressions) ~= 17, and
     the worst-performing kind averages 18.3 impressions. All nine kinds
     clear it on their average.
  2. IT GRADES THE OPPOSITE METRIC TO THE BANDIT. The claim grades
     impressions; the bandit grades eng_rate. Across the nine kinds those are
     anti-correlated (Pearson r = -0.16) — `deal` has the highest impressions
     (46.5) and the lowest eng_rate (2.47%), so a post can CONFIRM its claim
     while being exactly what the bandit is learning to avoid.
  3. THE OUTCOME NEVER REACHED THE COMPOSER. Refuted claims are recalled by
     brain_rag -> brain_lane_driver / brain_strategic_planner. The LinkedIn
     composer reads media_review_log and recent openings, and has zero
     references to claims. The signal was produced and delivered to the wrong
     consumer.

And under all of it: the desk earns 0.5-3.0 interactions per post. The
difference between an excellent post and a mediocre one is under one click.
Engagement has no resolving power here, whatever bar you set.

So this loop grades the TEXT against ANALYST_VOICE — which is what
"analyst-grade" actually means, and which works at 2-3 posts/day.

THE CONTRACT
------------
* ★ SIGNAL UPSTREAM, NOT A FILTER DOWNSTREAM. Verdicts are written to
  media_review_log with decision='published_review' and read by the composer
  BEFORE it writes the next post. Every quality fix in this codebase's
  history has been a filter added downstream; this is not one.
* ★ IT RUNS AFTER PUBLICATION AND CANNOT SUPPRESS A SLOT. There is no gate
  here and no code path that returns a refusal. A gate that silences good
  posts is a worse failure than the flaw it catches.
* ★ FAIL OPEN. Advisory by construction: a model outage, a DB outage or a
  malformed response yields NO lessons, never a block and never an exception
  that reaches a caller.
* ★ THE REVIEWER MAY NOT INVENT A RULE. Every critique must name a dimension
  from media_post_quality.PUBLISHED_REVIEW_DIMENSIONS; anything else is
  dropped by published_critique_block(). The composer obeys this block, so an
  invented rule would quietly become policy.
* decision='published_review' is a DISTINCT value from 'blocked', so these
  rows cannot contaminate the refusals loop (_recent_block_reasons filters on
  decision='blocked').

Surfaces:
  GET  /api/v1/media/published-review       what was reviewed + whether the
                                            critiques ACTUALLY reach the
                                            composer (built by the same
                                            function the composer calls)
  POST /api/v1/media/published-review/run   run a review pass now (admin)
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os

import requests

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
media_published_review_bp = Blueprint("media_published_review", __name__)

REVIEW_DECISION = "published_review"
_REVIEW_MODEL = os.environ.get("DCHUB_MEDIA_REVIEW_MODEL", "claude-fable-5")
_REVIEW_MODEL_FALLBACK = "claude-sonnet-4-5"
_DEFAULT_DAYS = 3
_DEFAULT_LIMIT = 8

try:
    import psycopg2 as _pg
    import psycopg2.extras as _pgx
except Exception:                          # pragma: no cover - import guard
    _pg = None
    _pgx = None


def _dsn():
    return (os.environ.get("DATABASE_URL") or "").strip() or None


def _conn():
    return _pg.connect(_dsn())


def _now_iso() -> str:
    return _dt.datetime.utcnow().isoformat() + "Z"


# ── the spec the reviewer judges against ────────────────────────────────────
def _voice_spec() -> str:
    """ANALYST_VOICE, from the module that owns it. Inline fallback so a
    boot-order hiccup degrades the review, never breaks it — and so the
    reviewer can never silently judge against a DIFFERENT spec than the one
    the composer writes to."""
    try:
        from routes.media_editorial import ANALYST_VOICE
        return ANALYST_VOICE
    except Exception:                      # pragma: no cover - fallback path
        return (
            "You are a senior data-center infrastructure analyst. Lead every "
            "post with a specific NUMBER + the TREND + the SO-WHAT for a "
            "site-selection or capex decision, then a non-obvious implication "
            "— written, never announced, and never under a fixed label such as "
            "\"second-order read\". Dry, specific, no promotion. Never invent a "
            "figure. Attribution is one neutral source line AFTER the insight.")


_REVIEW_SYSTEM = """You are a demanding editor reviewing infrastructure-analyst \
posts that have ALREADY BEEN PUBLISHED. You are not a gate — the post is out. \
Your only job is to tell the writer what to do differently next time.

Judge each post against this voice spec:

{spec}

For each post return a verdict on these dimensions and NOTHING ELSE:
  number_lead  — opens with a specific metric + trend, not a brand line
  implication  — the so-what is WRITTEN into the prose, never announced under a
                 fixed label ("second-order read:", "the takeaway:")
  specificity  — concrete figures and named entities, not category talk
  attribution  — at most one neutral source line, AFTER the insight
  promotion    — no brand-pillar speech, no "we are the authority"
  hook         — the strongest concrete number is inside the first ~12 words
  ending       — the post finishes its last sentence

OUTPUT CONTRACT: a JSON array, one object per post, and nothing else — no \
prose, no markdown fence. Each object:
  {{"post_id": <int>, "misses": [{{"dimension": "<one of the seven above>", \
"critique": "<one concrete sentence naming what THIS post did>"}}]}}

A post that meets the spec has "misses": []. Do not invent a dimension that is \
not in the list of seven. Do not praise. Be concrete: "opened with 'DC Hub now \
tracks' instead of a number" teaches; "weak opening" does not."""


# ── reading what shipped ────────────────────────────────────────────────────
# ★★★ social_media_posts.posted_at / published_at are TEXT, not timestamps.
# The first version of this query wrote `s.published_at > NOW() - interval` and
# Postgres answered `operator does not exist: text > timestamp with time zone`.
# The fail-soft except swallowed it, `recent_published_posts` returned [], and
# the whole review loop reported ok:true / candidates:0 / "nothing to grade"
# while grading NOTHING — measured in production 2026-08-25T21:55:57Z.
#
# content_publisher._SMP_TS already knew: it casts `{col}::text` then
# `::timestamp` and regex-guards the format, because these columns are strings
# that can hold junk. This mirrors that shape exactly.
#
# ★ COALESCE(published_at, posted_at): the auto-publisher sets both, but the
#   admin publish paths set them independently. Taking whichever exists means a
#   post is reviewable however it shipped.
# ★ Compare against a NAIVE timestamp — `::timestamp` yields naive, and
#   `naive > timestamptz` is the same error class this comment exists about.
# ★★★ AND THE TWO COLUMNS DO NOT SHARE A TYPE. published_at is TEXT;
# posted_at is `timestamp without time zone`. `COALESCE(published_at,
# posted_at)` therefore raises
#     DatatypeMismatch: COALESCE types text and timestamp without time zone
#     cannot be matched
# — a SECOND, different error from the first (`text > timestamp with time
# zone`), surfaced in production 2026-08-25T22:31Z only because the read error
# is now reported instead of swallowed.
#
# This is why content_publisher._SMP_TS casts `{col}::text` on EVERY column
# before doing anything else: ::text first normalises the pair, THEN they can
# be coalesced, THEN cast to timestamp. Cast each side, never the result.
_TS = "NULLIF(COALESCE(s.published_at::text, s.posted_at::text), '')"
_TS_OK = ("COALESCE(s.published_at::text, s.posted_at::text) "
          "~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'")

_RECENT_SQL = f"""
    SELECT s.id, s.content
      FROM social_media_posts s
     WHERE s.status = 'published'
       AND s.publish_platform = 'linkedin'
       AND s.content IS NOT NULL AND s.content <> ''
       AND {_TS_OK}
       AND {_TS}::timestamp
             > (NOW() AT TIME ZONE 'UTC') - make_interval(days => %s)
       AND NOT EXISTS (
             SELECT 1 FROM media_review_log m
              WHERE m.decision = %s
                AND m.content_excerpt = 'post:' || s.id::text)
     ORDER BY {_TS}::timestamp DESC
     LIMIT %s
"""

# ★ Set by recent_published_posts when the read FAILS, so a zero can be told
# apart from an empty desk. A silent [] is what let the broken query above look
# like a quiet week.
_last_read_error = {"value": None}


def recent_published_posts(days: int = _DEFAULT_DAYS, limit: int = _DEFAULT_LIMIT):
    """Published LinkedIn posts not yet reviewed, newest first.

    ★ SCOPE, stated rather than implied: this reads social_media_posts only.
      Measured 2026-08-25 over the last 7 days — social_media_posts carried 11
      published LinkedIn rows and linkedin_quad_posts carried 16 successes. The
      quad-daily arm records ONLY in its own table (r-quad-visibility), so this
      reviewer currently grades roughly 40% of what the desk publishes. Widening
      it is a follow-up, not a silent assumption.

    Best-effort: [] on any error — but the error is RECORDED, never just
    logged, so `candidates: 0` is distinguishable from a failed read.
    The NOT EXISTS excludes posts already carrying a published_review row so a
    re-run does not re-review (and does not re-weight the same miss)."""
    _last_read_error["value"] = None
    if not (_pg and _dsn()):
        _last_read_error["value"] = "no database configured"
        return []
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(_RECENT_SQL, (int(days), REVIEW_DECISION, int(limit)))
            return [{"id": r[0], "content": r[1]} for r in (cur.fetchall() or [])]
    except Exception as e:                 # noqa: BLE001
        _last_read_error["value"] = f"{type(e).__name__}: {str(e)[:160]}"
        logger.warning("[pubreview] candidate read FAILED: %s", str(e)[:200])
        return []


def recent_published_critiques(days: int = 7, n: int = 200) -> list:
    """(dimension, critique) pairs the reviewer recorded — the composer's read.

    Mirrors linkedin_content_engine._recent_block_reasons: same table, same
    best-effort contract, DIFFERENT decision value so the two loops cannot
    contaminate each other."""
    if not (_pg and _dsn()):
        return []
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT reason FROM media_review_log
                 WHERE decision = %s
                   AND created_at > NOW() - make_interval(days => %s)
                 ORDER BY created_at DESC LIMIT %s
            """, (REVIEW_DECISION, int(days), int(n)))
            out = []
            for r in (cur.fetchall() or []):
                raw = (r[0] if not hasattr(r, "get") else r.get("reason")) or ""
                dim, _, text = str(raw).partition(":")
                if text.strip():
                    out.append((dim.strip(), text.strip()))
            return out
    except Exception as e:                 # noqa: BLE001
        logger.info("[pubreview] critique read skipped: %s", str(e)[:160])
        return []


# ★ The idempotency index. One critique per (post, critique-text) — a re-run of
# a pass must not double-weight the same miss in the composer's prompt, and
# lessons_prompt_block RANKS BY COUNT, so a duplicate row is not harmless: it
# promotes that miss above real ones.
#
# ★★ PARTIAL, and deliberately so. media_review_log is owned by
# content_publisher and already holds thousands of decision='blocked' rows with
# no uniqueness among them; a full unique index would fail to build against
# them. `WHERE decision = 'published_review'` scopes it to rows only this module
# writes — a value that did not exist before this commit, so nothing can violate
# it at build time.
#
# ★★★ AND THE CONFLICT CLAUSE REPEATS THE PREDICATE. `ON CONFLICT (a, b) DO
# NOTHING` does NOT match a partial unique index — Postgres raises "no unique or
# exclusion constraint matching the ON CONFLICT specification". The WHERE must
# appear in BOTH places.
_UNIQ_DDL = ("CREATE UNIQUE INDEX IF NOT EXISTS media_review_log_pubreview_uniq "
             "ON media_review_log (content_excerpt, reason) "
             "WHERE decision = 'published_review'")
_uniq_ready = False


def _ensure_uniq_index() -> bool:
    """Build the partial unique index once per process, through the ONE cursor
    that actually runs DDL.

    ★ db_utils.ddl_cursor() exists because the POOLED cursor silently returns
      early for CREATE INDEX when SKIP_DDL is set — which it is by default. A
      pooled cursor here would log nothing, raise nothing, and build no index,
      and the ON CONFLICT below would then fail on every insert.
    """
    global _uniq_ready
    if _uniq_ready:
        return True
    try:
        from db_utils import ddl_cursor
        with ddl_cursor() as cur:
            cur.execute(_UNIQ_DDL)
        _uniq_ready = True
        return True
    except Exception as e:                 # noqa: BLE001
        logger.warning("[pubreview] uniq index not ready: %s", str(e)[:160])
        return False


def _record_review(post_id, dimension: str, critique: str) -> bool:
    """One miss -> one media_review_log row. Returns False on any failure;
    a write that does not land must never abort a review pass."""
    if not (_pg and _dsn()):
        return False
    if not _ensure_uniq_index():
        # ★ Without the arbiter index the ON CONFLICT below RAISES. Skipping the
        # write costs one lesson; attempting it would raise once per miss and
        # fill the log with the same error. Advisory data, fail-open.
        return False
    try:
        conn = _conn()
        try:
            with conn.cursor() as cur:
                # One string literal on purpose: scripts/regression_lint.py
                # scans forward from "INSERT INTO <tbl>" and stops at the first
                # quote, so a concatenated form hides its own ON CONFLICT from
                # the very check that exists to require one.
                cur.execute(
                    """INSERT INTO media_review_log
                           (platform, decision, reason, content_excerpt)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (content_excerpt, reason)
                           WHERE decision = 'published_review' DO NOTHING""",
                    ("linkedin", REVIEW_DECISION,
                     f"{dimension}: {critique}"[:300],
                     f"post:{post_id}"[:280]))
            conn.commit()
            return True
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:                 # noqa: BLE001
        logger.warning("[pubreview] write failed for post %s: %s", post_id, str(e)[:160])
        return False


# ── the review call ─────────────────────────────────────────────────────────
def _call_model(posts: list, model: str) -> list:
    """Raw model call. Returns the parsed JSON array, or [] on anything odd.

    ★ Reads stop_reason (the #3166 lesson): a review CUT OFF at the ceiling
      returns partial JSON, and a partial review must not be recorded as if it
      were a complete one."""
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        return []
    from utils.anthropic_helper import anthropic_messages_url, cached_system
    numbered = "\n\n".join(
        f"--- post_id {p['id']} ---\n{(p.get('content') or '')[:3000]}"
        for p in posts)
    body = {
        "model": model,
        "max_tokens": 2000,
        "system": cached_system(_REVIEW_SYSTEM.format(spec=_voice_spec())),
        "messages": [{"role": "user", "content":
                      f"Review these {len(posts)} published posts.\n\n{numbered}"}],
    }
    r = requests.post(
        anthropic_messages_url(), json=body, timeout=60,
        headers={"Content-Type": "application/json",
                 "X-API-Key": key,
                 "User-Agent": "dchub-brain/1.0",
                 "Anthropic-Version": "2023-06-01"})
    payload = r.json()
    stop = payload.get("stop_reason")
    if stop == "max_tokens":
        logger.warning("[pubreview] review TRUNCATED (stop_reason=max_tokens) "
                       "— discarding partial verdicts for %d post(s)", len(posts))
        return []
    text = "".join(p.get("text", "") for p in (payload.get("content") or [])
                   if isinstance(p, dict)).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(text)
    except Exception:                      # noqa: BLE001
        logger.info("[pubreview] non-JSON review discarded: %r", text[:160])
        return []
    return parsed if isinstance(parsed, list) else []


def review_published_posts(days: int = _DEFAULT_DAYS,
                           limit: int = _DEFAULT_LIMIT) -> dict:
    """One review pass. Never raises — the caller is a cron tick."""
    from routes.media_post_quality import PUBLISHED_REVIEW_DIMENSIONS
    out = {"ok": True, "as_of": _now_iso(), "reviewed": 0,
           "misses_recorded": 0, "dropped_unknown_dimension": 0, "posts": []}
    try:
        posts = recent_published_posts(days=days, limit=limit)
        out["candidates"] = len(posts)
        if not posts:
            # ★ A zero is only evidence if the query could have returned
            # non-zero. Say which zero this is.
            err = _last_read_error["value"]
            out["read_error"] = err
            out["note"] = (
                f"candidate read FAILED ({err}) — this is NOT an empty desk"
                if err else
                f"no unreviewed published LinkedIn posts in the last {days}d "
                "— nothing to grade")
            return out
        verdicts = []
        try:
            verdicts = _call_model(posts, _REVIEW_MODEL)
            if not verdicts:
                verdicts = _call_model(posts, _REVIEW_MODEL_FALLBACK)
        except Exception as e:             # noqa: BLE001
            out["model_error"] = f"{type(e).__name__}: {str(e)[:160]}"
        _ids = {int(p["id"]) for p in posts}
        for v in verdicts:
            if not isinstance(v, dict):
                continue
            try:
                pid = int(v.get("post_id"))
            except Exception:              # noqa: BLE001
                continue
            if pid not in _ids:
                continue                   # a verdict for a post we did not send
            out["reviewed"] += 1
            misses = v.get("misses") or []
            recorded = []
            for m in (misses if isinstance(misses, list) else []):
                if not isinstance(m, dict):
                    continue
                dim = str(m.get("dimension") or "").strip().lower()
                crit = str(m.get("critique") or "").strip()
                if dim not in PUBLISHED_REVIEW_DIMENSIONS:
                    out["dropped_unknown_dimension"] += 1
                    continue
                if not crit:
                    continue
                if _record_review(pid, dim, crit):
                    out["misses_recorded"] += 1
                    recorded.append({"dimension": dim, "critique": crit})
            out["posts"].append({"post_id": pid, "misses": recorded})
        # A post reviewed clean still needs a row, or recent_published_posts
        # re-offers it forever and every pass re-reviews the same clean posts.
        for p in posts:
            if not any(x["post_id"] == int(p["id"]) for x in out["posts"]):
                continue
            if any(x["post_id"] == int(p["id"]) and x["misses"] for x in out["posts"]):
                continue
            _record_review(p["id"], "clean", "met the voice spec")
    except Exception as e:                 # noqa: BLE001
        out["ok"] = False
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return out


# ── surfaces ────────────────────────────────────────────────────────────────
def _is_admin(req) -> bool:
    try:
        from internal_auth import accepted_internal_keys
        keys = set(accepted_internal_keys() or ())
    except Exception:                      # noqa: BLE001
        keys = set()
    k = (req.headers.get("X-Admin-Key") or req.headers.get("X-Internal-Key") or "").strip()
    env = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    if env:
        keys.add(env)
    return bool(k and k in keys)


@media_published_review_bp.route("/api/v1/media/published-review", methods=["GET"])
def published_review_status():
    """What the reviewer found, and whether it ACTUALLY reaches the composer.

    ★ `critiques_actually_injected` is built by calling the SAME function the
      composer calls, on the SAME rows — the #3167 contract. This endpoint and
      the prompt cannot drift apart into a field that reports a loop nothing
      reads. `false` with a non-empty `critiques_fed_to_generator` means the
      block was built and rejected every critique (all dimensions unknown) —
      which is a wiring bug, not an empty desk."""
    days = 7
    try:
        days = max(1, min(90, int(request.args.get("days", 7))))
    except Exception:                      # noqa: BLE001
        pass
    out = {"ok": True, "as_of": _now_iso(), "window_days": days,
           "decision_value": REVIEW_DECISION}
    pairs = recent_published_critiques(days=days)
    out["critiques_recorded"] = len(pairs)
    # ★ Whether the CANDIDATE side can even see posts. `critiques_recorded: 0`
    # with a healthy candidate read is an empty desk; with a failed one it is a
    # broken loop, and those must not look the same from outside.
    _probe = recent_published_posts(days=days, limit=1)
    out["candidate_probe"] = {"visible": len(_probe),
                              "read_error": _last_read_error["value"]}
    try:
        from routes.media_post_quality import published_critique_block
        block = published_critique_block(pairs)
        out["critiques_actually_injected"] = bool(block)
        out["critiques_fed_to_generator"] = [
            line.strip("  - ") for line in block.splitlines()
            if line.startswith("  - ")]
        by_dim: dict = {}
        for dim, _crit in pairs:
            by_dim[dim] = by_dim.get(dim, 0) + 1
        out["misses_by_dimension"] = dict(
            sorted(by_dim.items(), key=lambda kv: (-kv[1], kv[0])))
    except Exception as e:                 # noqa: BLE001
        out["critiques_actually_injected"] = False
        out["critiques_fed_to_generator"] = []
        out["error"] = str(e)[:160]
    return jsonify(out), 200


@media_published_review_bp.route("/api/v1/media/published-review/run", methods=["POST"])
def published_review_run():
    if not _is_admin(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    days = _DEFAULT_DAYS
    limit = _DEFAULT_LIMIT
    try:
        body = request.get_json(silent=True) or {}
        days = max(1, min(30, int(body.get("days", _DEFAULT_DAYS))))
        limit = max(1, min(25, int(body.get("limit", _DEFAULT_LIMIT))))
    except Exception:                      # noqa: BLE001
        pass
    return jsonify(review_published_posts(days=days, limit=limit)), 200


def register_media_published_review(app):
    app.register_blueprint(media_published_review_bp)
