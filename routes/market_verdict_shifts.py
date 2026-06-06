"""
market_verdict_shifts.py — DCPI Verdict-Shift Auto-Press (2026-06-06).

When a market's DCPI verdict SHIFTS (e.g. Phoenix CAUTION → AVOID), we
have a viral mechanic: post the dramatic shift to LinkedIn with a link
to the canonical Market Brief — every shift = inbound traffic to the
Market Brief feature.

This module:

  1. Scans `dcpi_daily_snapshots` (the persistent per-day history table
     written by the facility-snapshot-daily.yml cron) to find markets
     whose verdict TODAY differs from the verdict 7 days ago.
  2. Filters to DRAMATIC shifts (BUILD or AVOID involved on at least
     one side — sub-tier CAUTION↔CAUTION are skipped).
  3. For each qualifying shift, asks Claude (Anthropic) to write a
     50-80-word press-release-ready blurb. Uses the same
     `_call_claude(prompt, system) -> (text, error)` plumbing as
     brain_v2_layer4.py so the brain model self-heal chain applies
     and the keyless/no-egress path returns a clean None.
  4. Enqueues an LinkedIn post into `social_media_posts` with
     status='approved' so the existing LinkedIn publisher loop picks
     it up — mirroring the dchub_media_accelerator.py cross-post
     pattern. We do NOT directly call the LinkedIn API.
  5. Records the (market_slug, shift_to, DATE) in
     `market_verdict_post_log` so the same shift can't be posted twice
     in one day even across re-runs / cron + manual fire.

Endpoints
---------
POST /api/v1/admin/verdict-shifts/scan
    Admin-keyed. Default DRY-RUN (returns the shifts that WOULD be
    posted). Pass ?live=1 to actually enqueue.

GET  /api/v1/verdict-shifts/recent?days=14
    Public. Recent verdict shifts (data is already public via
    /api/v1/dcpi). Useful as a diagnostic + an XHR for /press pages.

Cron
----
SCHEDULE slot (16, 16, "verdict_shift_post") in crawler_scheduler.py.
Once daily because verdict shifts are slow (the daily snapshot writer
runs once per day; we only ever have ~1 day of new shifts to consider).

Safety
------
  • DEFAULT DRY-RUN — only the cron runner (via X-Internal-Cron header)
    or an explicit ?live=1 actually enqueues.
  • Cap MAX_POSTS_PER_RUN = 3 so we never flood LinkedIn even if a
    big rescore moves many markets at once.
  • Skip when BOTH market_deep_dives.narrative_md is missing AND the
    LLM call fails — only post when there's a real "why".
  • De-dup via UNIQUE(market_slug, shift_to, DATE(posted_at)) on
    `market_verdict_post_log`. A row written there prevents a second
    enqueue for the same day.
  • Soft cap: skip if we already posted for THIS market in the last
    48h regardless of new shift direction (anti-spam belt-and-suspenders).
"""
from __future__ import annotations

import os
import sys
import datetime
from typing import Any

from flask import Blueprint, jsonify, request


market_verdict_shifts_bp = Blueprint("market_verdict_shifts", __name__)


# ── Tunables ──────────────────────────────────────────────────────────
SHIFT_WINDOW_DAYS    = 7    # compare today's verdict vs 7 days ago
MAX_POSTS_PER_RUN    = 3    # don't flood LinkedIn
SOFT_RECENT_HOURS    = 48   # belt-and-suspenders anti-spam per slug


# Dramatic shifts (one side BUILD or AVOID). CAUTION↔CAUTION is the
# sub-tier shift we explicitly skip per the spec.
QUALIFYING_SHIFTS = {
    ("CAUTION", "BUILD"):   ("upgrade",    "{name} just upgraded to BUILD"),
    ("CAUTION", "AVOID"):   ("warning",    "{name} flagged AVOID"),
    ("BUILD",   "CAUTION"): ("downgrade",  "{name} downgraded BUILD → CAUTION"),
    ("BUILD",   "AVOID"):   ("downgrade",  "{name} downgraded BUILD → AVOID"),
    ("AVOID",   "CAUTION"): ("recovery",   "{name} recovering: AVOID → CAUTION"),
    ("AVOID",   "BUILD"):   ("recovery",   "{name} full recovery: AVOID → BUILD"),
}


# ── Plumbing ──────────────────────────────────────────────────────────
def _db_conn():
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        return psycopg2.connect(url, connect_timeout=5) if url else None
    except Exception:
        return None


def _admin_or_cron_authorized() -> bool:
    """Same gate as dchub_media_accelerator — admin key OR cron secret."""
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key")
                or request.args.get("key") or "")
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    if expected and provided == expected:
        return True
    cron_hdr = request.headers.get("X-Internal-Cron", "")
    cron_env = os.environ.get("DCHUB_CRON_SECRET", "")
    return bool(cron_env) and cron_hdr == cron_env


def _is_cron_call() -> bool:
    """True iff the SCHEDULE harness fired this (X-Internal-Cron). Used
    to decide LIVE-vs-DRY-RUN when the caller didn't explicitly pass
    ?live=1. Cron callers always run LIVE."""
    cron_hdr = request.headers.get("X-Internal-Cron", "")
    cron_env = os.environ.get("DCHUB_CRON_SECRET", "")
    return bool(cron_env) and cron_hdr == cron_env


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"[verdict-shifts] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


_TABLES_READY = False


def _ensure_log_table(cur) -> None:
    """Create market_verdict_post_log if missing. Idempotent.

    UNIQUE(market_slug, shift_to, DATE(posted_at)) is enforced via a
    functional unique index because Postgres doesn't allow expressions
    in inline UNIQUE constraints — we materialize posted_date for the
    index AND keep posted_at for sorting/diagnostics."""
    global _TABLES_READY
    if _TABLES_READY:
        return
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_verdict_post_log (
                id            SERIAL PRIMARY KEY,
                market_slug   TEXT NOT NULL,
                shift_from    TEXT,
                shift_to      TEXT NOT NULL,
                composite     REAL,
                excess_score  REAL,
                constraint_sc REAL,
                blurb         TEXT,
                smp_id        INT,
                posted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_mvpl_slug_to_day
                ON market_verdict_post_log
                   (market_slug, shift_to, (posted_at::date))
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS ix_mvpl_posted_at "
                    "ON market_verdict_post_log(posted_at DESC)")
        _TABLES_READY = True
    except Exception as e:
        _log(f"ensure_log_table_failed: {e}")
        # leave _TABLES_READY=False so we retry on the next call


# ── Detector ──────────────────────────────────────────────────────────
def _detect_shifts(cur) -> list[dict]:
    """Find markets whose verdict TODAY differs from the verdict 7 days
    ago.

    Source: dcpi_daily_snapshots (clean per-day history,
    UNIQUE(snapshot_date, market_slug)).

    The "CURRENT verdict" is the latest row in market_power_scores
    (canonical, what users see); the "OLDER verdict" is the snapshot
    from 7 days ago. Skip markets where the older snapshot row does
    not exist — that means we don't have enough history to compare."""
    out: list[dict] = []
    try:
        cur.execute("""
            WITH current_v AS (
                SELECT DISTINCT ON (market_slug)
                       market_slug, market_name, iso,
                       constraint_score, excess_power_score, verdict,
                       computed_at
                  FROM market_power_scores
                 ORDER BY market_slug, computed_at DESC
            ),
            older_v AS (
                SELECT DISTINCT ON (market_slug)
                       market_slug, verdict AS prior_verdict,
                       snapshot_date
                  FROM dcpi_daily_snapshots
                 WHERE snapshot_date <= (CURRENT_DATE - %s)
                 ORDER BY market_slug, snapshot_date DESC
            )
            SELECT c.market_slug, c.market_name, c.iso,
                   o.prior_verdict, c.verdict,
                   c.constraint_score, c.excess_power_score,
                   c.computed_at, o.snapshot_date
              FROM current_v c
              JOIN older_v   o USING (market_slug)
             WHERE o.prior_verdict IS NOT NULL
               AND c.verdict       IS NOT NULL
               AND o.prior_verdict <> c.verdict
             ORDER BY c.computed_at DESC
        """, (SHIFT_WINDOW_DAYS,))
        for r in cur.fetchall() or []:
            slug, name, iso, prior, now, con, ex, comp_at, snap_date = r
            out.append({
                "market_slug":   slug,
                "market_name":   name,
                "iso":           iso,
                "shift_from":    (prior or "").upper(),
                "shift_to":      (now or "").upper(),
                "constraint_score":   float(con) if con is not None else None,
                "excess_power_score": float(ex)  if ex  is not None else None,
                "computed_at":   comp_at.isoformat() if comp_at else None,
                "prior_snapshot_date": snap_date.isoformat() if snap_date else None,
            })
    except Exception as e:
        _log(f"detect_shifts_failed: {e}")
    return out


def _classify_shift(shift_from: str, shift_to: str) -> dict | None:
    """Return {kind, headline_template} if the shift qualifies, else None."""
    key = (shift_from, shift_to)
    if key not in QUALIFYING_SHIFTS:
        return None
    kind, tpl = QUALIFYING_SHIFTS[key]
    return {"kind": kind, "headline_template": tpl}


def _composite_score(con: float | None, ex: float | None) -> float | None:
    """DCPI composite score = average of constraint + excess sub-scores.
    Matches the legacy derive_composite_score behaviour used on the
    /dcpi page when no live composite is present. None-safe."""
    if con is None and ex is None:
        return None
    parts = [v for v in (con, ex) if v is not None]
    if not parts:
        return None
    return round(sum(parts) / len(parts), 1)


# ── Narrative & blurb ─────────────────────────────────────────────────
def _fetch_deep_dive_narrative(cur, slug: str) -> str | None:
    """Pull the persisted narrative_md for this market (per-market summary
    written by routes/market_deep_dive.py). Return first ~600 chars so
    the LLM has signal without over-prompting."""
    try:
        cur.execute("""
            SELECT narrative_md FROM market_deep_dives
             WHERE LOWER(market_slug) = LOWER(%s)
             LIMIT 1
        """, (slug,))
        r = cur.fetchone()
        if not r or not r[0]:
            return None
        text = str(r[0]).strip()
        return text[:600] if text else None
    except Exception:
        return None


_BLURB_SYSTEM = (
    "You are DC Hub's autonomous market-intelligence press writer. "
    "When the DCPI (Data Center Power Index) verdict for a market "
    "SHIFTS — e.g. CAUTION → AVOID or AVOID → BUILD — you draft a "
    "single short LinkedIn blurb (50-80 words) that announces the "
    "shift in a way that's useful to data-center executives. "
    "Lead with the market name + the direction. Cite the live "
    "DCPI numbers (composite, excess-power, constraint). Add ONE "
    "concrete sentence on the 'why' grounded in the provided "
    "deep-dive narrative. End with the canonical Market Brief URL. "
    "No hashtags except #DCPI #DataCenters. No emoji except a "
    "single leading siren/alert glyph if the shift is dramatic. "
    "Plain text only — no markdown. Do NOT make up numbers. If a "
    "number isn't provided, omit it rather than guess."
)


def _build_blurb_prompt(shift: dict, narrative: str | None) -> str:
    """Compact prompt — the brain budget is per-call, so we stay tight."""
    composite = _composite_score(
        shift.get("constraint_score"),
        shift.get("excess_power_score"),
    )
    bits = [
        f"Market: {shift['market_name']} (slug={shift['market_slug']}, "
        f"ISO={shift.get('iso') or 'unknown'})",
        f"Verdict shift: {shift['shift_from']} -> {shift['shift_to']}",
        f"Current composite_score: {composite}" if composite is not None
            else "Current composite_score: not available",
        f"Current excess_power_score: {shift.get('excess_power_score')}",
        f"Current constraint_score:   {shift.get('constraint_score')}",
        f"Market Brief URL: https://dchub.cloud/markets/"
            f"{shift['market_slug']}/brief",
    ]
    if narrative:
        bits.append("Deep-dive narrative (use ONE sentence as the 'why'):")
        bits.append(narrative)
    bits.append(
        "\nWrite the blurb now. 50-80 words. End with the Market Brief "
        "URL on its own line."
    )
    return "\n".join(bits)


def _ask_claude_for_blurb(shift: dict, narrative: str | None) -> tuple[str | None, str | None]:
    """Single Claude call via the canonical layer-4 client. Returns
    (blurb, error). On any failure, returns (None, error_code)."""
    try:
        from routes.brain_v2_layer4 import _call_claude
    except Exception as e:
        return None, f"layer4_import_fail: {e}"
    try:
        text, err = _call_claude(
            _build_blurb_prompt(shift, narrative),
            _BLURB_SYSTEM,
        )
    except Exception as e:
        return None, f"call_claude_raised: {e}"
    if not text:
        return None, err or "empty_blurb"
    text = text.strip()
    if len(text) < 40:
        return None, "blurb_too_short"
    return text, None


def _fallback_blurb(shift: dict, narrative: str | None) -> str | None:
    """When the LLM call fails AND a narrative exists, we can ship a
    deterministic blurb so the viral mechanic still fires. Returns None
    when even the narrative is missing — caller will skip the post.

    Per spec: 'Skip markets where market_deep_dives.narrative is
    missing AND the LLM call fails — only post when we have a real why.'
    """
    if not narrative:
        return None
    composite = _composite_score(
        shift.get("constraint_score"),
        shift.get("excess_power_score"),
    )
    kind_info = _classify_shift(shift["shift_from"], shift["shift_to"])
    if not kind_info:
        return None
    headline = kind_info["headline_template"].format(name=shift["market_name"])
    glyph = ""
    if shift["shift_to"] == "AVOID":
        glyph = "[ALERT] "
    elif shift["shift_to"] == "BUILD":
        glyph = "[UPGRADE] "

    # Trim the narrative to one sentence-ish (first 180 chars at the
    # nearest period). Falls back to first 180 chars if no period.
    snippet = narrative.strip().replace("\n", " ")
    if "." in snippet:
        snippet = snippet.split(".", 1)[0].strip() + "."
    snippet = snippet[:200]

    parts = [
        f"{glyph}{headline}.",
    ]
    if composite is not None:
        parts.append(
            f"DCPI composite is {composite} "
            f"(excess-power {shift.get('excess_power_score')}, "
            f"constraint {shift.get('constraint_score')}). "
        )
    parts.append(snippet)
    parts.append("")
    parts.append(
        f"Full Market Brief: "
        f"https://dchub.cloud/markets/{shift['market_slug']}/brief"
    )
    parts.append("#DCPI #DataCenters")
    return " ".join(parts).strip()


# ── Enqueue ───────────────────────────────────────────────────────────
def _already_posted_today(cur, slug: str, shift_to: str) -> bool:
    """De-dup check against market_verdict_post_log."""
    try:
        cur.execute("""
            SELECT 1 FROM market_verdict_post_log
             WHERE market_slug = %s
               AND shift_to    = %s
               AND posted_at::date = CURRENT_DATE
             LIMIT 1
        """, (slug, shift_to))
        return cur.fetchone() is not None
    except Exception:
        return False


def _recently_posted_for_slug(cur, slug: str) -> bool:
    """Soft 48h cap per slug — even if a NEW shift fires for this market
    we don't want to flood LinkedIn within 2 days."""
    try:
        cur.execute("""
            SELECT 1 FROM market_verdict_post_log
             WHERE market_slug = %s
               AND posted_at > NOW() - INTERVAL '%s hours'
             LIMIT 1
        """, (slug, SOFT_RECENT_HOURS))
        return cur.fetchone() is not None
    except Exception:
        return False


def _enqueue_linkedin_post(cur, blurb: str) -> int | None:
    """INSERT an approved LinkedIn row. Returns the new id, or None on
    failure. Mirrors dchub_media_accelerator._enqueue_cross_post."""
    try:
        cur.execute("""
            INSERT INTO social_media_posts
                   (content, platform, status, created_at, approved_at)
            VALUES (%s, 'linkedin', 'approved', NOW(), NOW()::text)
            RETURNING id
        """, (blurb,))
        row = cur.fetchone()
        return int(row[0]) if row else None
    except Exception as e:
        _log(f"enqueue_linkedin_failed: {e}")
        return None


def _record_post(cur, shift: dict, blurb: str, smp_id: int | None) -> bool:
    """Write to market_verdict_post_log so future runs skip this shift."""
    try:
        cur.execute("""
            INSERT INTO market_verdict_post_log
                   (market_slug, shift_from, shift_to,
                    composite, excess_score, constraint_sc,
                    blurb, smp_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT ON CONSTRAINT uq_mvpl_slug_to_day DO NOTHING
        """, (
            shift["market_slug"], shift["shift_from"], shift["shift_to"],
            _composite_score(shift.get("constraint_score"),
                             shift.get("excess_power_score")),
            shift.get("excess_power_score"),
            shift.get("constraint_score"),
            blurb, smp_id,
        ))
        return True
    except Exception as e:
        # functional unique index name may differ; fall back to plain insert
        _log(f"record_post_constrained_failed: {e}")
        try:
            cur.execute("""
                INSERT INTO market_verdict_post_log
                       (market_slug, shift_from, shift_to,
                        composite, excess_score, constraint_sc,
                        blurb, smp_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                shift["market_slug"], shift["shift_from"], shift["shift_to"],
                _composite_score(shift.get("constraint_score"),
                                 shift.get("excess_power_score")),
                shift.get("excess_power_score"),
                shift.get("constraint_score"),
                blurb, smp_id,
            ))
            return True
        except Exception as e2:
            _log(f"record_post_plain_failed: {e2}")
            return False


# ── Core scan ─────────────────────────────────────────────────────────
def scan_verdict_shifts(live: bool = False) -> dict[str, Any]:
    """Main detector. Returns a result dict. When live=False, computes
    everything (including the blurb) but does NOT enqueue / log."""
    result: dict[str, Any] = {
        "ran_at":        datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "live":          bool(live),
        "window_days":   SHIFT_WINDOW_DAYS,
        "cap_per_run":   MAX_POSTS_PER_RUN,
        "scanned":       0,
        "qualifying":    0,
        "would_post":    0,
        "posted":        0,
        "skipped":       [],
        "posts":         [],
        "errors":        [],
    }
    conn = _db_conn()
    if conn is None:
        result["errors"].append("no_db_connection")
        return result
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            _ensure_log_table(cur)
            try: conn.commit()
            except Exception: pass

            shifts = _detect_shifts(cur)
            result["scanned"] = len(shifts)

            for s in shifts:
                cls = _classify_shift(s["shift_from"], s["shift_to"])
                if not cls:
                    # sub-tier (e.g. CAUTION->CAUTION) — skip silently
                    continue
                result["qualifying"] += 1

                if result["would_post"] >= MAX_POSTS_PER_RUN:
                    result["skipped"].append({
                        "market_slug": s["market_slug"],
                        "reason":      "cap_reached",
                    })
                    continue

                # De-dup gates — apply BEFORE we burn an LLM call.
                if _already_posted_today(cur, s["market_slug"], s["shift_to"]):
                    result["skipped"].append({
                        "market_slug": s["market_slug"],
                        "reason":      "already_posted_today",
                    })
                    continue
                if _recently_posted_for_slug(cur, s["market_slug"]):
                    result["skipped"].append({
                        "market_slug": s["market_slug"],
                        "reason":      "soft_recent_48h",
                    })
                    continue

                # Build the blurb (LLM → fallback → skip)
                narrative = _fetch_deep_dive_narrative(cur, s["market_slug"])
                blurb, llm_err = _ask_claude_for_blurb(s, narrative)
                if not blurb:
                    blurb = _fallback_blurb(s, narrative)
                    if not blurb:
                        result["skipped"].append({
                            "market_slug": s["market_slug"],
                            "reason":      "no_narrative_and_llm_fail",
                            "llm_error":   llm_err,
                        })
                        continue

                # At this point the shift WOULD be posted.
                result["would_post"] += 1
                post_record = {
                    "market_slug":  s["market_slug"],
                    "market_name":  s["market_name"],
                    "shift_from":   s["shift_from"],
                    "shift_to":     s["shift_to"],
                    "kind":         cls["kind"],
                    "composite":    _composite_score(
                                        s.get("constraint_score"),
                                        s.get("excess_power_score")),
                    "excess_power_score":  s.get("excess_power_score"),
                    "constraint_score":    s.get("constraint_score"),
                    "iso":          s.get("iso"),
                    "blurb":        blurb,
                    "brief_url":    f"https://dchub.cloud/markets/"
                                    f"{s['market_slug']}/brief",
                    "smp_id":       None,
                    "llm_used":     bool(llm_err is None),
                    "llm_error":    llm_err,
                }

                if not live:
                    result["posts"].append(post_record)
                    continue

                # LIVE — enqueue + record.
                smp_id = _enqueue_linkedin_post(cur, blurb)
                if smp_id is None:
                    result["errors"].append(
                        f"enqueue_failed slug={s['market_slug']}")
                    continue
                ok = _record_post(cur, s, blurb, smp_id)
                if not ok:
                    # rollback the enqueue so we don't post a row that
                    # isn't tracked in the log — bad for de-dup.
                    try:
                        cur.execute(
                            "DELETE FROM social_media_posts WHERE id = %s",
                            (smp_id,))
                    except Exception:
                        pass
                    result["errors"].append(
                        f"log_failed_rollback slug={s['market_slug']}")
                    continue
                post_record["smp_id"] = smp_id
                result["posted"]      += 1
                result["posts"].append(post_record)
                _log(
                    f"POSTED slug={s['market_slug']} "
                    f"shift={s['shift_from']}->{s['shift_to']} "
                    f"smp_id={smp_id} llm={post_record['llm_used']}"
                )

            try:
                conn.commit()
            except Exception as e:
                result["errors"].append(f"commit_failed: {e}")
                try: conn.rollback()
                except Exception: pass
    except Exception as e:
        result["errors"].append(f"scan_failed: {e}")
        try: conn.rollback()
        except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass
    return result


# ── HTTP surface ──────────────────────────────────────────────────────
@market_verdict_shifts_bp.route(
    "/api/v1/admin/verdict-shifts/scan", methods=["POST"])
def http_admin_scan():
    """Admin-keyed scan. DEFAULT DRY-RUN. Pass ?live=1 to actually
    enqueue (or call with X-Internal-Cron set — cron always runs live)."""
    if not _admin_or_cron_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    live_param = (request.args.get("live") or "").lower() in ("1", "true", "yes")
    live = live_param or _is_cron_call()
    out = scan_verdict_shifts(live=live)
    return jsonify(out), 200


@market_verdict_shifts_bp.route(
    "/api/v1/verdict-shifts/recent", methods=["GET"])
def http_public_recent():
    """Public diagnostic — recent verdict shifts as raw shift dicts.
    Data is already public via /api/v1/dcpi so no gating. Useful for
    /press pages, JS widgets, and external agents."""
    try:
        days = int(request.args.get("days", "14"))
    except Exception:
        days = 14
    days = max(1, min(days, 60))

    out: dict[str, Any] = {
        "days":     days,
        "count":    0,
        "shifts":   [],
        "posted":   [],
    }
    conn = _db_conn()
    if conn is None:
        out["error"] = "no_db"
        return jsonify(out), 200
    try:
        with conn.cursor() as cur:
            # Live qualifying shifts (current verdict vs window-back snapshot).
            shifts = _detect_shifts(cur)
            for s in shifts:
                cls = _classify_shift(s["shift_from"], s["shift_to"])
                if not cls:
                    continue
                out["shifts"].append({
                    "market_slug":  s["market_slug"],
                    "market_name":  s["market_name"],
                    "iso":          s.get("iso"),
                    "shift_from":   s["shift_from"],
                    "shift_to":     s["shift_to"],
                    "kind":         cls["kind"],
                    "composite":    _composite_score(
                                        s.get("constraint_score"),
                                        s.get("excess_power_score")),
                    "excess_power_score": s.get("excess_power_score"),
                    "constraint_score":   s.get("constraint_score"),
                    "computed_at":  s.get("computed_at"),
                    "brief_url":    f"https://dchub.cloud/markets/"
                                    f"{s['market_slug']}/brief",
                })
            # Recent POSTED shifts (last `days` days).
            try:
                cur.execute("""
                    SELECT market_slug, shift_from, shift_to,
                           composite, excess_score, constraint_sc,
                           smp_id, posted_at
                      FROM market_verdict_post_log
                     WHERE posted_at > NOW() - (%s || ' days')::interval
                     ORDER BY posted_at DESC
                     LIMIT 100
                """, (str(days),))
                for r in cur.fetchall() or []:
                    out["posted"].append({
                        "market_slug": r[0],
                        "shift_from":  r[1],
                        "shift_to":    r[2],
                        "composite":   float(r[3]) if r[3] is not None else None,
                        "excess_power_score": float(r[4]) if r[4] is not None else None,
                        "constraint_score":   float(r[5]) if r[5] is not None else None,
                        "smp_id":      r[6],
                        "posted_at":   r[7].isoformat() if r[7] else None,
                        "brief_url":   f"https://dchub.cloud/markets/{r[0]}/brief",
                    })
            except Exception as e:
                # log table may not exist yet on this deploy
                out["posted_error"] = f"log_table_missing_or_error: {e}"
            out["count"] = len(out["shifts"])
    finally:
        try: conn.close()
        except Exception: pass
    return jsonify(out), 200
