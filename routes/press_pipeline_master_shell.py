"""press_pipeline_master_shell.py — PRESS PIPELINE TRUTH (2026-08-10).

WHY THIS SHELL EXISTS
=====================
On 2026-08-09 the press desk had been silent for FIVE DAYS (last published
release 2026-08-04 17:24 UTC) and every dashboard read healthy. Three
independent failures stacked, and not one of them raised anything:

  1. THE WRITE PATH WAS ROLLING BACK. `media_editorial_reviews` is written on
     its OWN connection, so a review row survives; `press_releases` +
     `auto_press_releases` + `press_integrity_reviews` are one transaction in
     `marketing_engine._write_release` ending at `c.commit()`. On 08-09 the
     gate logged **20 reviews and exactly 1 row landed in each of those three
     tables** — 19 composer runs died between the gate and the commit and the
     whole transaction was discarded. One of the nineteen was the only story
     the desk APPROVED that day (score 8, three new national grids).

  2. THE COMPOSER HAS AMNESIA ABOUT ITS OWN REJECTED OUTPUT. The
     DO-NOT-REPEAT prompt block and the near-duplicate guard both read
     `auto_press_releases`, which only gains a row when the write COMPLETES.
     So every rolled-back attempt left no trace, the do-not-repeat list stayed
     near-empty, and the generator proposed the SAME Midland-Odessa story
     16 times in two hours — each time believing it was novel, each time
     paying for an editorial LLM review to be told "static index snapshot, no
     concrete change". The worse the write path got, the harder it repeated.

  3. HELD DRAFTS WENT PUBLIC ANYWAY. `_write_release` queues LinkedIn /
     Twitter / Bluesky rows at `status='approved'` regardless of the
     `published` value it just computed, and all three drains select on
     `status='approved'` alone — none joins `press_releases.published`.
     Every press-linked social post between 08-04 and 08-09 pointed at a
     DRAFT (100202 Meta, 100200 MISO, 100186 Tulsa); only 100196 CoreWeave
     was actually published. Whether a held draft reached the public was
     decided by `_should_skip_publish`'s unrelated content judgement.

★ The through-line: every one of those is a SILENT stage-to-stage LOSS. The
  press pipeline has five stages and nothing was comparing adjacent ones.
  Counting output at any single stage looks fine — the gate was "working",
  the composer was "running", social was "posting". The defect only appears
  as a RATIO between neighbours. That is what this shell measures.

WHAT IT DOES — five read-only lanes, no actuators. This shell NEVER composes,
publishes, sends, or mutates a release; worst case it records a RED snapshot
that the pending-drafts digest and a human can act on.

  A  publish_silence     — days since the last PUBLISHED release. The 5-day
                           outage would have gone RED on day 3.
  B  compose_to_write    — editorial reviews vs press rows in the same 24h.
                           19-of-20 lost = the rollback, visible as a ratio.
  C  approved_unwritten  — verdict='publish' reviews with NO press_releases
                           row. Any non-zero is a lost approved story.
  D  repetition          — distinct story stems / total attempts over 7d.
                           16 Midland-Odessa variants = 0.19 distinct.
  E  draft_social_leak   — social posts actually POSTED whose release is
                           published=FALSE. The gate bypass.

Endpoints (admin-keyed):
  POST /api/v1/admin/press-pipeline/master-tick   measure -> score -> persist
  GET  /api/v1/admin/press-pipeline/master-state  last snapshot, no recompute

Kill switch: PRESS_PIPELINE_SHELL_DISABLED=1.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from routes._swallowed_writes import note_swallowed_write

logger = logging.getLogger("press_pipeline_master_shell")
press_pipeline_master_shell_bp = Blueprint("press_pipeline_master_shell", __name__)

SHELL_NAME = "press-pipeline"

# ── thresholds, all derived from the 08-09 measurement ────────────────
# Cadence: the data lanes compose daily, so two consecutive silent days is
# already abnormal and three is the outage. 08-04 -> 08-09 was five.
SILENCE_WARN_DAYS = 2
SILENCE_RED_DAYS = 3
# Stage loss: a healthy day writes a row for most reviews. 08-09 was 0.05.
WRITE_RATIO_WARN = 0.60
WRITE_RATIO_RED = 0.30
# Repetition, two ways. The distinct-stem RATIO is the blunt read; the
# CONCENTRATION of the single most-repeated stem is the sharp one, and it is
# the one that catches this failure earliest. Measured over the 7d window
# ending 2026-08-10: 13 stems / 33 attempts = 0.39 ratio (only amber), but
# 'midland-odessa' alone accounted for 19 of the 33 = 0.58 concentration.
# A composer stuck on one story shows up in the second number first.
DISTINCT_WARN = 0.60
DISTINCT_RED = 0.35
TOP_SHARE_WARN = 0.25
TOP_SHARE_RED = 0.40
# Below this many attempts the ratios are noise, not signal.
MIN_ATTEMPTS_FOR_RATIO = 4


def _admin_ok() -> bool:
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key")
                or request.headers.get("Authorization", "")
                .replace("Bearer ", "").strip())
    return bool(expected) and provided == expected


def _disabled() -> bool:
    return (os.environ.get("PRESS_PIPELINE_SHELL_DISABLED", "").strip().lower()
            in ("1", "true", "yes", "on"))


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        c.autocommit = True
        return c
    except Exception:
        return None


# ── story-stem normalisation (lane D) ─────────────────────────────────
# The 16 repeats differed only in slug wording, so anything keyed on the raw
# slug counts them as 16 distinct stories. Strip the date stamp, the
# composer's 'auto-' prefix, and the words every DC Hub headline carries, then
# keep the first few surviving tokens as the story's identity.
_DATE_RE = re.compile(r"\b20\d{2}[-_]?\d{2}[-_]?\d{2}\b")
_STEM_STOPWORDS = frozenset("""
auto the a an and or of in on at for to with as by from is are new now
dc hub data center centers centre centres dcpi index score scores market
markets power excess constraint mw gw report daily live top tops
""".split())
# TWO tokens, not three. Measured on the live 7d window: at three tokens the
# 08-09 storm split across 'midland-odessa' (7), 'midland-odessa-ercot' (4),
# 'midland-odessa-leader' (2) and more — 23 stems / 33 attempts, which reads
# GREEN. The extra descriptive token is exactly the thing the composer varies
# between re-proposals, so including it hands the metric back to the noise it
# is supposed to see through. At two tokens the same window collapses to
# 'midland-odessa' x19 of 33.
_STEM_TOKENS = 2


def story_stem(slug_or_title: str) -> str:
    """Collapse a slug/headline to the story it is ABOUT.

    '2026-08-09-midland-odessa-86-excess-power-dcpi',
    'auto-2026-08-09-midland-odessa-86-excess-power' and
    'midland-odessa-86-excess-power-2026-08-09' must all yield the same stem —
    otherwise lane D counts the 08-09 repeat storm as 16 distinct stories,
    which is exactly how it stayed invisible.
    """
    s = _DATE_RE.sub(" ", (slug_or_title or "").lower())
    toks = [t for t in re.split(r"[^a-z0-9]+", s) if t]
    keep = [t for t in toks if t not in _STEM_STOPWORDS and not t.isdigit()]
    return "-".join(keep[:_STEM_TOKENS]) if keep else ""


# ── TIER 1 — MEASURE ──────────────────────────────────────────────────
def tier1_measure() -> dict:
    """Five lanes. Every lane fails to None (UNMEASURED), never to a zero —
    a zero here reads as 'nothing is wrong', which is the exact lie this
    shell exists to stop telling."""
    out: dict = {
        "publish_silence_days": None,
        "last_published_at": None,
        "reviews_24h": None,
        "press_rows_24h": None,
        "write_ratio_24h": None,
        "approved_unwritten_7d": None,
        "approved_unwritten_slugs": [],
        "attempts_7d": None,
        "distinct_stems_7d": None,
        "distinct_ratio_7d": None,
        "top_repeated_stem": None,
        "top_repeated_count": None,
        "top_stem_share_7d": None,
        "draft_social_leak_7d": None,
        "draft_social_leak_slugs": [],
        "unmeasured": [],
    }
    c = _conn()
    if c is None:
        out["unmeasured"] = ["no_database"]
        return out
    try:
        with c.cursor() as cur:
            # A — publish silence
            try:
                cur.execute("SELECT MAX(published_at) FROM press_releases "
                            "WHERE published = TRUE")
                last = (cur.fetchone() or [None])[0]
                if last is not None:
                    out["last_published_at"] = last.isoformat()
                    delta = datetime.now(timezone.utc) - last
                    out["publish_silence_days"] = round(
                        delta.total_seconds() / 86400.0, 2)
            except Exception:
                out["unmeasured"].append("publish_silence")

            # B — compose vs write in the same 24h. press_releases.created_at
            # is timestamp WITHOUT tz while the review table is WITH tz, so
            # each side is anchored explicitly rather than left to session TZ.
            try:
                cur.execute("SELECT count(*) FROM media_editorial_reviews "
                            "WHERE created_at > NOW() - INTERVAL '24 hours'")
                reviews = (cur.fetchone() or [0])[0] or 0
                cur.execute("SELECT count(*) FROM press_releases WHERE "
                            "(created_at AT TIME ZONE 'UTC') > "
                            "NOW() - INTERVAL '24 hours'")
                rows = (cur.fetchone() or [0])[0] or 0
                out["reviews_24h"] = reviews
                out["press_rows_24h"] = rows
                if reviews:
                    out["write_ratio_24h"] = round(min(rows, reviews) / reviews, 3)
            except Exception:
                out["unmeasured"].append("compose_to_write")

            # C — the desk said publish and no row exists
            try:
                cur.execute("""
                    SELECT r.press_slug
                      FROM media_editorial_reviews r
                      LEFT JOIN press_releases p ON p.slug = r.press_slug
                     WHERE r.verdict = 'publish'
                       AND r.created_at > NOW() - INTERVAL '7 days'
                       AND p.id IS NULL
                     ORDER BY r.created_at DESC
                     LIMIT 25
                """)
                lost = [r[0] for r in cur.fetchall() if r and r[0]]
                out["approved_unwritten_7d"] = len(lost)
                out["approved_unwritten_slugs"] = lost[:10]
            except Exception:
                out["unmeasured"].append("approved_unwritten")

            # D — repetition over every ATTEMPT (the review table is the only
            # record of an attempt that survives a rolled-back write)
            try:
                cur.execute("SELECT press_slug FROM media_editorial_reviews "
                            "WHERE created_at > NOW() - INTERVAL '7 days'")
                slugs = [r[0] for r in cur.fetchall() if r and r[0]]
                out["attempts_7d"] = len(slugs)
                if slugs:
                    stems: dict[str, int] = {}
                    for s in slugs:
                        st = story_stem(s)
                        if st:
                            stems[st] = stems.get(st, 0) + 1
                    out["distinct_stems_7d"] = len(stems)
                    if stems:
                        out["distinct_ratio_7d"] = round(
                            len(stems) / len(slugs), 3)
                        top = max(stems.items(), key=lambda kv: kv[1])
                        out["top_repeated_stem"] = top[0]
                        out["top_repeated_count"] = top[1]
                        out["top_stem_share_7d"] = round(top[1] / len(slugs), 3)
            except Exception:
                out["unmeasured"].append("repetition")

            # E — a held draft that reached the public anyway
            try:
                cur.execute("""
                    SELECT p.slug, s.platform
                      FROM social_media_posts s
                      JOIN press_releases p ON p.id = s.press_release_id
                     WHERE s.posted_at IS NOT NULL
                       AND (s.posted_at AT TIME ZONE 'UTC')
                             > NOW() - INTERVAL '7 days'
                       AND COALESCE(p.published, FALSE) = FALSE
                     ORDER BY s.posted_at DESC
                     LIMIT 40
                """)
                leaks = [f"{r[0]} -> {r[1]}" for r in cur.fetchall() if r]
                out["draft_social_leak_7d"] = len(leaks)
                out["draft_social_leak_slugs"] = leaks[:10]
            except Exception:
                out["unmeasured"].append("draft_social_leak")
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


# ── TIER 2 — SCORE ────────────────────────────────────────────────────
def _verdict(m: dict) -> tuple[str, list[str]]:
    """green / amber / red plus the reasons. UNMEASURED is never green —
    a lane that could not run must not read as a lane that passed."""
    reasons: list[str] = []
    red = amber = False

    if m.get("unmeasured"):
        amber = True
        reasons.append("unmeasured lanes: " + ", ".join(m["unmeasured"]))

    d = m.get("publish_silence_days")
    if d is None:
        amber = True
        reasons.append("no published release on record")
    elif d >= SILENCE_RED_DAYS:
        red = True
        reasons.append(f"{d:.1f}d since last publish (red at {SILENCE_RED_DAYS})")
    elif d >= SILENCE_WARN_DAYS:
        amber = True
        reasons.append(f"{d:.1f}d since last publish")

    wr = m.get("write_ratio_24h")
    if wr is not None and (m.get("reviews_24h") or 0) >= MIN_ATTEMPTS_FOR_RATIO:
        if wr <= WRITE_RATIO_RED:
            red = True
            reasons.append(
                f"write ratio {wr} — {m.get('reviews_24h')} reviews produced "
                f"{m.get('press_rows_24h')} rows (transaction loss)")
        elif wr <= WRITE_RATIO_WARN:
            amber = True
            reasons.append(f"write ratio {wr}")

    au = m.get("approved_unwritten_7d")
    if au:
        red = True
        reasons.append(f"{au} approved release(s) never written")

    enough = (m.get("attempts_7d") or 0) >= MIN_ATTEMPTS_FOR_RATIO
    dr = m.get("distinct_ratio_7d")
    ts = m.get("top_stem_share_7d")
    if enough and ts is not None and ts >= TOP_SHARE_RED:
        red = True
        reasons.append(
            f"composer stuck: '{m.get('top_repeated_stem')}' is "
            f"{m.get('top_repeated_count')} of {m.get('attempts_7d')} attempts "
            f"({ts:.0%})")
    elif enough and ts is not None and ts >= TOP_SHARE_WARN:
        amber = True
        reasons.append(
            f"'{m.get('top_repeated_stem')}' is {ts:.0%} of attempts")
    if enough and dr is not None:
        if dr <= DISTINCT_RED:
            red = True
            reasons.append(
                f"repetition: {m.get('distinct_stems_7d')} stems / "
                f"{m.get('attempts_7d')} attempts")
        elif dr <= DISTINCT_WARN:
            amber = True
            reasons.append(f"repetition ratio {dr}")

    leak = m.get("draft_social_leak_7d")
    if leak:
        red = True
        reasons.append(f"{leak} held draft(s) posted to social anyway")

    return ("red" if red else "amber" if amber else "green"), reasons


def tier2_score(m: dict) -> dict:
    verdict, reasons = _verdict(m)
    return {"verdict": verdict, "reasons": reasons,
            "healthy": verdict == "green"}


# ── persist ───────────────────────────────────────────────────────────
def _ensure_tables(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS press_pipeline_snapshots (
            id                    SERIAL PRIMARY KEY,
            computed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            verdict               TEXT,
            publish_silence_days  NUMERIC(8,2),
            reviews_24h           INTEGER,
            press_rows_24h        INTEGER,
            write_ratio_24h       NUMERIC(6,3),
            top_stem_share_7d     NUMERIC(6,3),
            approved_unwritten_7d INTEGER,
            attempts_7d           INTEGER,
            distinct_ratio_7d     NUMERIC(6,3),
            draft_social_leak_7d  INTEGER,
            detail                JSONB
        )
    """)
    # ★ ONE row per UTC hour, and the tick UPSERTS into it.
    # cron_heartbeat's dispatch window is `now.hour == 19 and now.minute < 55`,
    # which fires roughly ELEVEN times inside that hour by design — the wide
    # window exists because narrow ones were unreliable under GitHub-cron
    # latency, and idempotency is explicitly the endpoint's job. A plain
    # append would therefore write ~11 identical snapshots a day and make
    # "how many red days this week" a count of heartbeats rather than of
    # days. The unique key is what makes the UPSERT below legal, so this
    # index and that ON CONFLICT are one mechanism, not two.
    cur.execute("""
        ALTER TABLE press_pipeline_snapshots
        ADD COLUMN IF NOT EXISTS snapshot_hour TIMESTAMPTZ
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ix_pps_hour
        ON press_pipeline_snapshots(snapshot_hour)
    """)


def _persist(m: dict, sc: dict) -> bool:
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            _ensure_tables(cur)
            cur.execute("""
                INSERT INTO press_pipeline_snapshots
                  (snapshot_hour, verdict, publish_silence_days, reviews_24h,
                   press_rows_24h, write_ratio_24h, top_stem_share_7d,
                   approved_unwritten_7d, attempts_7d, distinct_ratio_7d,
                   draft_social_leak_7d, detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (snapshot_hour) DO UPDATE SET
                    computed_at           = NOW(),
                    verdict               = EXCLUDED.verdict,
                    publish_silence_days  = EXCLUDED.publish_silence_days,
                    reviews_24h           = EXCLUDED.reviews_24h,
                    press_rows_24h        = EXCLUDED.press_rows_24h,
                    write_ratio_24h       = EXCLUDED.write_ratio_24h,
                    top_stem_share_7d     = EXCLUDED.top_stem_share_7d,
                    approved_unwritten_7d = EXCLUDED.approved_unwritten_7d,
                    attempts_7d           = EXCLUDED.attempts_7d,
                    distinct_ratio_7d     = EXCLUDED.distinct_ratio_7d,
                    draft_social_leak_7d  = EXCLUDED.draft_social_leak_7d,
                    detail                = EXCLUDED.detail
            """, (
                # ★ The hour key is BOUND here rather than computed inline
                # with a date-truncation call. regression-lint's
                # insert-no-on-conflict rule scans the statement with a
                # character class that stops at a single quote, so ANY quoted
                # SQL literal inside the statement ends the match before it
                # reaches the ON CONFLICT clause — and the rule then fires on
                # an upsert that plainly has one. marketing_engine's
                # _write_release carries the same note for the same reason.
                datetime.now(timezone.utc).replace(
                    minute=0, second=0, microsecond=0),
                sc.get("verdict"), m.get("publish_silence_days"),
                m.get("reviews_24h"), m.get("press_rows_24h"),
                m.get("write_ratio_24h"), m.get("top_stem_share_7d"),
                m.get("approved_unwritten_7d"),
                m.get("attempts_7d"), m.get("distinct_ratio_7d"),
                m.get("draft_social_leak_7d"),
                json.dumps({"measure": m, "score": sc}),
            ))
        return True
    except Exception:
        note_swallowed_write("press_pipeline_snapshots",
                             where="press_pipeline_master_shell._persist")
        return False
    finally:
        try:
            c.close()
        except Exception:
            pass


def _headline(m: dict, sc: dict) -> str:
    d = m.get("publish_silence_days")
    return (
        f"press {sc.get('verdict', '?').upper()} · "
        f"{'no publish on record' if d is None else f'{d:.1f}d since publish'} · "
        f"{m.get('press_rows_24h')}/{m.get('reviews_24h')} written 24h · "
        f"{m.get('distinct_stems_7d')} stems/{m.get('attempts_7d')} attempts 7d · "
        f"{m.get('approved_unwritten_7d')} approved-lost · "
        f"{m.get('draft_social_leak_7d')} draft-leaks"
    )


# ── ORCHESTRATOR ──────────────────────────────────────────────────────
@press_pipeline_master_shell_bp.route(
    "/api/v1/admin/press-pipeline/master-tick", methods=["POST", "GET"])
def master_tick():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="PRESS_PIPELINE_SHELL_DISABLED"), 200
    started = time.time()
    measure = tier1_measure()
    score = tier2_score(measure)
    persisted = _persist(measure, score)
    head = _headline(measure, score)
    if score.get("verdict") == "red":
        logger.warning("[press-pipeline] RED — %s | %s",
                       head, "; ".join(score.get("reasons") or []))
    return jsonify(
        ok=True, ms=int((time.time() - started) * 1000),
        verdict=score.get("verdict"), headline=head,
        tier1_measure=measure, tier2_score=score,
        persisted=persisted,
        generated_at=datetime.now(timezone.utc).isoformat(),
    ), 200


@press_pipeline_master_shell_bp.route(
    "/api/v1/admin/press-pipeline/master-state", methods=["GET"])
def master_state():
    """Last stored snapshot — no recompute, so a dashboard read is cheap."""
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 200
    try:
        with c.cursor() as cur:
            _ensure_tables(cur)
            cur.execute("""
                SELECT computed_at, verdict, detail
                  FROM press_pipeline_snapshots
                 ORDER BY computed_at DESC LIMIT 1
            """)
            row = cur.fetchone()
        if not row:
            return jsonify(ok=True, snapshot=None,
                           note="no tick has run yet"), 200
        return jsonify(ok=True, computed_at=row[0].isoformat(),
                       verdict=row[1], snapshot=row[2]), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200
    finally:
        try:
            c.close()
        except Exception:
            pass
