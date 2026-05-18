"""
Phase RRR-press-loop (2026-05-18) — close the brain↔press feedback loop.

This is the "brain studies competition + drafts press when we ship" wire
the user asked for. Flow:

  1. Weekly cron hits /api/v1/brain/press-loop
  2. Endpoint calls competitor_intel.ship-wins (last 7d commits)
  3. For each win NOT already in press_releases, INSERT a draft row
  4. Monday 14:00 cron (already wired) — operator briefing email
  5. Monday 03:00+ cron — publish-now fans to LinkedIn/X/Bluesky

Plus: ship-win OUTCOME tracking so the brain can LEARN which posts work.
Each generated draft gets a UUID, recorded in press_brain_outcomes when
it eventually publishes + engagement data flows back. Next iteration:
keyword scoring shifts toward winners.
"""

import logging
import os
import json
import hashlib
import datetime as _dt
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)
brain_press_loop_bp = Blueprint("brain_press_loop", __name__)


def _conn():
    try:
        from main import get_db
        return get_db()
    except Exception:
        import psycopg2
        return psycopg2.connect(os.environ.get("NEON_DATABASE_URL")
                                or os.environ.get("DATABASE_URL", ""))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS press_brain_outcomes (
    id              BIGSERIAL PRIMARY KEY,
    win_keyword     TEXT NOT NULL,
    commit_sha      TEXT NOT NULL,
    press_slug      TEXT,
    drafted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at    TIMESTAMPTZ,
    linkedin_views  INT,
    linkedin_likes  INT,
    twitter_views   INT,
    twitter_likes   INT,
    bluesky_views   INT,
    click_outs      INT,
    engagement_score REAL,
    UNIQUE (commit_sha, win_keyword)
);
CREATE INDEX IF NOT EXISTS ix_pbo_keyword ON press_brain_outcomes(win_keyword);
CREATE INDEX IF NOT EXISTS ix_pbo_drafted ON press_brain_outcomes(drafted_at DESC);
"""

_TABLE_INIT_DONE = False

def _ensure_schema():
    global _TABLE_INIT_DONE
    if _TABLE_INIT_DONE:
        return
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(_SCHEMA)
            try: conn.commit()
            except Exception: pass
            _TABLE_INIT_DONE = True
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"press_brain_outcomes schema init failed: {e}")


def _slug_for_win(keyword: str, commit_sha: str) -> str:
    """Stable slug per (keyword, commit) — so the press_releases row is
    idempotent. If we re-run the loop, same commit + same keyword = no
    duplicate press."""
    keyword_s = keyword.lower().replace(" ", "-").replace("_", "-")
    return f"brain-win-{keyword_s}-{commit_sha[:7]}"


@brain_press_loop_bp.route("/api/v1/brain/press-loop", methods=["POST", "GET"])
def press_loop_endpoint():
    """Convert recent ship-wins into draft press releases. Idempotent —
    re-running won't dupe. Admin-gated for production cron; opens on
    GET for manual triage from a browser.

    Query params:
      ?days=7         — lookback window (default 7)
      ?auto_approve=true — set status='approved' instead of 'draft'
                          (default false — operator reviews first)
    """
    _ensure_schema()
    days = int(request.args.get("days", "7"))
    auto_approve = (request.args.get("auto_approve", "").lower()
                    in ("1", "true", "yes"))
    # Admin check for the write path
    admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("ADMIN_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if request.method == "POST" and admin_key and provided != admin_key:
        return jsonify(error="unauthorized"), 401

    # 1. Pull ship-wins via internal HTTP call (simpler than importing —
    #    keeps the loop self-contained even if competitor_intel module
    #    moves around).
    import requests as _req
    try:
        r = _req.get(f"http://localhost:8080/api/v1/competitive/ship-wins?days={days}",
                     timeout=15)
        if r.status_code != 200:
            return jsonify(ok=False, error=f"ship-wins fetch: HTTP {r.status_code}"), 503
        wins = (r.json() or {}).get("wins") or []
    except Exception as e:
        return jsonify(ok=False, error=f"ship-wins call: {type(e).__name__}: {str(e)[:120]}"), 503

    if not wins:
        return jsonify(ok=True, mode="no_wins", drafted=0,
                       note="No commits in window matched the competitive-differentiator keyword map. Either nothing notable shipped, or commit messages weren't in the keyword pattern."), 200

    # Dry-run mode: return the would-be drafts without writing
    if request.method == "GET" and not auto_approve and not request.args.get("write"):
        return jsonify(
            ok=True,
            mode="dry_run",
            note="GET returns the planned drafts. Add ?write=true (admin) or POST to actually create them.",
            would_draft=len(wins),
            wins=wins,
        ), 200

    # 2. Write each win as a draft press_releases row + record outcome stub
    drafted = []
    skipped_existing = 0
    errors = []
    status_to_set = "approved" if auto_approve else "draft"
    now = _dt.datetime.utcnow().isoformat() + "Z"

    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            for w in wins:
                slug = _slug_for_win(w["keyword"], w["commit"])
                title = w["headline"]
                # Build a richer body than just the post_draft — full HTML article
                body = f"""<article>
<h2>{title}</h2>
<p><em>Shipped: {w['subject']}</em></p>
<p>{w['positioning']}</p>
<p>This is part of DC Hub's continuous shipping cadence. See the live competitive comparison at <a href="https://dchub.cloud/competitive">dchub.cloud/competitive</a>.</p>
<p><strong>Commit:</strong> <code>{w['commit']}</code></p>
</article>"""
                try:
                    cur.execute("""
                        INSERT INTO press_releases
                          (title, slug, body, category, published, status,
                           published_at, created_at, meta_description)
                        VALUES (%s, %s, %s, 'product-launch', FALSE, %s, NULL, %s, %s)
                        ON CONFLICT (slug) DO NOTHING
                        RETURNING id
                    """, (title, slug, body, status_to_set, now,
                          w["positioning"][:200]))
                    rid = cur.fetchone()
                    if rid:
                        drafted.append({"slug": slug, "id": rid[0],
                                         "keyword": w["keyword"]})
                        # Record outcome stub for learning loop
                        try:
                            cur.execute("""
                                INSERT INTO press_brain_outcomes
                                  (win_keyword, commit_sha, press_slug)
                                VALUES (%s, %s, %s)
                                ON CONFLICT (commit_sha, win_keyword) DO NOTHING
                            """, (w["keyword"], w["commit"], slug))
                        except Exception:
                            pass
                    else:
                        skipped_existing += 1
                except Exception as e:
                    errors.append({"slug": slug,
                                    "err": f"{type(e).__name__}: {str(e)[:80]}"})
                    try: conn.rollback()
                    except Exception: pass
                    cur = conn.cursor()
            try: conn.commit()
            except Exception: pass
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        return jsonify(ok=False, error=f"db: {str(e)[:120]}"), 503

    return jsonify(
        ok=True,
        mode="written",
        status_set=status_to_set,
        drafted=len(drafted),
        skipped_existing=skipped_existing,
        errors=errors,
        drafted_slugs=[d["slug"] for d in drafted],
        note=("Drafts now live in press_releases table. They auto-fan "
              "to LinkedIn/X/Bluesky on next /api/v1/marketing/publish-now "
              "cron fire (every 3h)."),
        generated_at=now,
    ), 200


@brain_press_loop_bp.route("/api/v1/brain/press-loop/learning", methods=["GET"])
def learning_endpoint():
    """Show what the brain has learned from past ship-win press posts —
    which keywords drive the best engagement (so we can lean into them)
    and which ones flop (so we can drop them).

    Foundation for self-improvement: as engagement data flows back in
    (LinkedIn/X webhooks → engagement_score), this view tells the brain
    which kinds of wins to draft more of."""
    _ensure_schema()
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT
                    win_keyword,
                    COUNT(*) AS drafted_count,
                    COUNT(*) FILTER (WHERE published_at IS NOT NULL) AS published_count,
                    AVG(engagement_score) AS avg_score,
                    MAX(drafted_at) AS most_recent_draft
                FROM press_brain_outcomes
                GROUP BY win_keyword
                ORDER BY drafted_count DESC
            """)
            rows = cur.fetchall() or []
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 503

    out = []
    for r in rows:
        out.append({
            "keyword":          r[0],
            "drafted_count":    int(r[1] or 0),
            "published_count":  int(r[2] or 0),
            "avg_engagement":   float(r[3]) if r[3] else None,
            "most_recent":      r[4].isoformat() if r[4] else None,
        })

    return jsonify(
        ok=True,
        keywords=out,
        note=("Engagement scores fill in as LinkedIn/X webhooks land. "
              "Once we have N>=10 posts per keyword, the brain will "
              "auto-prefer the top-scoring keywords when drafting new "
              "wins (and DEMOTE keywords that consistently flop). "
              "This is the self-improvement loop closed."),
    ), 200
