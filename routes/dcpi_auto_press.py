"""
dcpi_auto_press.py — Phase r64 (2026-05-25).

Every time a DCPI market moves ≥15 points week-over-week OR flips
between BUILD/AVOID/CAUTION, auto-drafts + INSERTs a press release
into the press_releases table.

Once landed, the existing press-scan-daily cron picks it up and
ships it to LinkedIn / X / Bluesky / press feed at the 13:00 UTC
tick — no human intervention.

Constant content drumbeat: 285+ US markets + 14 international, each
recomputed every 4 hours, means ~5-10 ≥15pt moves per week. Each
gives us a free press release with real news in it.

Endpoints:
  POST /api/v1/dcpi/auto-press/scan
       Admin-keyed or X-Internal-Cron. Scans for ≥15pt WoW moves
       OR verdict flips. For each, drafts + inserts a release.
       Idempotent — won't double-insert the same (market,date)
       pair within 7 days.

  GET  /api/v1/dcpi/auto-press/recent
       Public — last 20 auto-generated releases (slug, title, date).

Pairs with .github/workflows/dcpi-auto-press.yml which fires every
6h, off-cycle from press-scan-daily.
"""
from __future__ import annotations

import datetime
import json
import os

from flask import Blueprint, jsonify, request


dcpi_auto_press_bp = Blueprint("dcpi_auto_press", __name__)


_MIN_POINT_MOVE = 15
_LOOKBACK_DAYS  = 7
_DEDUP_DAYS     = 7


def _db_conn():
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        return psycopg2.connect(url, connect_timeout=5) if url else None
    except Exception:
        return None


def _admin_or_cron_authorized() -> bool:
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    if expected and provided == expected:
        return True
    cron_hdr = request.headers.get("X-Internal-Cron", "")
    cron_env = os.environ.get("DCHUB_CRON_SECRET", "")
    return bool(cron_env) and cron_hdr == cron_env


def _find_significant_shifts() -> list[dict]:
    """Find markets with ≥15pt excess_power_score WoW move OR a
    verdict flip in the last 7 days. Returns list of shift dicts."""
    out = []
    c = _db_conn()
    if not c: return out
    try:
        with c.cursor() as cur:
            # Self-join: latest score per market vs the score from ~7d ago
            cur.execute("""
                WITH latest AS (
                    SELECT DISTINCT ON (market_slug)
                           market_slug, market_name, iso, verdict,
                           excess_power_score AS ex, constraint_score AS cs,
                           computed_at
                      FROM market_power_scores
                     WHERE published = TRUE
                     ORDER BY market_slug, computed_at DESC
                ),
                prior AS (
                    SELECT DISTINCT ON (market_slug)
                           market_slug,
                           verdict AS prior_verdict,
                           excess_power_score AS prior_ex,
                           constraint_score AS prior_cs,
                           computed_at AS prior_at
                      FROM market_power_scores
                     WHERE published = TRUE
                       AND computed_at < NOW() - (%s || ' days')::interval
                     ORDER BY market_slug, computed_at DESC
                )
                SELECT l.market_slug, l.market_name, l.iso,
                       p.prior_verdict, l.verdict,
                       p.prior_ex, l.ex,
                       p.prior_cs, l.cs,
                       (l.ex - p.prior_ex) AS ex_delta,
                       l.computed_at
                  FROM latest l
                  JOIN prior  p USING (market_slug)
                 WHERE ABS(l.ex - p.prior_ex) >= %s
                    OR (p.prior_verdict IS DISTINCT FROM l.verdict
                        AND p.prior_verdict IN ('BUILD','AVOID','CAUTION'))
                 ORDER BY ABS(l.ex - p.prior_ex) DESC
                 LIMIT 10
            """, (str(_LOOKBACK_DAYS), _MIN_POINT_MOVE))
            for r in cur.fetchall() or []:
                slug, name, iso, was, now_v, ex_was, ex_now, cs_was, cs_now, delta, ts = r
                out.append({
                    "slug":         slug,
                    "name":         name,
                    "iso":          iso,
                    "prior_verdict": was,
                    "new_verdict":  now_v,
                    "prior_excess": float(ex_was or 0),
                    "new_excess":   float(ex_now or 0),
                    "delta":        float(delta or 0),
                    "computed_at":  ts.isoformat() if ts else None,
                    "is_verdict_flip": was != now_v,
                })
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass
    return out


def _already_drafted_recently(slug: str) -> bool:
    """Has an auto-press release for this market shipped in last 7d?"""
    c = _db_conn()
    if not c: return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM press_releases
                 WHERE slug LIKE %s
                   AND date > NOW() - (%s || ' days')::interval
            """, (f"dcpi-shift-{slug}-%", str(_DEDUP_DAYS)))
            n = (cur.fetchone() or [0])[0]
            return int(n or 0) > 0
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


def _draft_press_release(shift: dict) -> dict:
    """Build a press_releases row from one shift dict."""
    name = shift["name"]
    iso  = shift["iso"]
    delta = shift["delta"]
    was  = shift["prior_verdict"]
    now  = shift["new_verdict"]
    ex_was = shift["prior_excess"]
    ex_now = shift["new_excess"]

    today = datetime.date.today().isoformat()
    slug = f"dcpi-shift-{shift['slug']}-{today}"

    if shift["is_verdict_flip"]:
        if was == "AVOID" and now == "BUILD":
            arc = "AVOID → BUILD reversal"
            headline = f"DCPI flips {name} from AVOID to BUILD — what changed"
            angle = ("a meaningful structural improvement in power "
                       "availability — either a queue clearing, a major "
                       "transmission upgrade, or a curtailment-recovery event")
        elif was == "BUILD" and now == "AVOID":
            arc = "BUILD → AVOID reversal"
            headline = f"DCPI downgrades {name} from BUILD to AVOID — fresh constraint signal"
            angle = ("a deterioration in market fundamentals — sudden "
                       "queue saturation, reserve-margin compression, or "
                       "an emergency event that closed the buildable window")
        else:
            arc = f"{was} → {now} verdict shift"
            headline = f"DCPI shifts {name} verdict from {was} to {now}"
            angle = "a notable change in the market's risk/opportunity profile"
    else:
        direction = "up" if delta > 0 else "down"
        arc = f"{abs(delta):.1f}-point ExcessPower move ({direction})"
        headline = f"{name} DCPI ExcessPower moves {direction} {abs(delta):.1f} points"
        if delta > 0:
            angle = ("a power-headroom expansion the legacy data center "
                       "indices haven't caught yet — typical causes: "
                       "interconnection queue progress, new transmission "
                       "approvals, or curtailment patterns shifting")
        else:
            angle = ("a tightening market signal — possible drivers: "
                       "approved new loads consuming reserve margin, new "
                       "queue applications, or grid-emergency events")

    title = headline
    subhead = (f"Week-over-week DCPI score change of {abs(delta):.1f} "
                 f"points (ExcessPower {ex_was:.1f} → {ex_now:.1f}) puts "
                 f"{name} on the watchlist. {arc}.")

    body = f"""<p><strong>NEW YORK — {today}</strong> — DC Hub's Data Center Power Index (DCPI) recorded a significant shift in <strong>{name}</strong> ({iso}) this week. The market's ExcessPower score moved from <strong>{ex_was:.1f}</strong> to <strong>{ex_now:.1f}</strong> (a {abs(delta):.1f}-point swing), with verdict moving from <strong>{was}</strong> to <strong>{now}</strong>.</p>

<p>The shift reflects {angle}.</p>

<p>DCPI scores 300+ data center markets on two axes — ExcessPower (higher = more buildable headroom) and Constraint (higher = more friction to new builds). Markets are recomputed every 4 hours from interconnection-queue, capacity-pipeline, and grid-emergency data across 10 U.S. ISOs plus 13 international grid operators (NGESO, EirGrid, ENTSO-E, AEMO, Nord Pool, TEPCO, KEPCO, EMA, IESO, HQ, BCH).</p>

<p><strong>Live page for this market:</strong> <a href="https://dchub.cloud/dcpi/{shift['slug']}">dchub.cloud/dcpi/{shift['slug']}</a></p>

<p><strong>Full methodology:</strong> <a href="https://dchub.cloud/dcpi/methodology">dchub.cloud/dcpi/methodology</a> — open weights, open data sources, daily-refreshed.</p>

<p><em>This release was auto-drafted by DC Hub's brain when the {abs(delta):.1f}-point shift exceeded the {_MIN_POINT_MOVE}-point threshold. DCPI press releases are free for citation; cite as: <em>DC Hub Data Center Power Index, dchub.cloud/dcpi, accessed {today}.</em></em></p>
"""

    return {
        "slug":        slug,
        "title":       title,
        "subheadline": subhead,
        "body":        body,
        "category":    "DCPI Shift",
        "date":        today,
        "meta_description": subhead[:200],
        "published":   True,
    }


def _insert_release(release: dict) -> int | None:
    """INSERT into press_releases. Returns id or None on failure."""
    c = _db_conn()
    if not c: return None
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO press_releases
                    (title, slug, category, date, subheadline, body,
                     meta_description, published)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE SET
                    title = EXCLUDED.title,
                    body  = EXCLUDED.body,
                    published = EXCLUDED.published
                RETURNING id
            """, (
                release["title"], release["slug"], release["category"],
                release["date"], release["subheadline"], release["body"],
                release["meta_description"], release["published"],
            ))
            new_id = (cur.fetchone() or [None])[0]
            c.commit()
            return new_id
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


# ── Endpoints ───────────────────────────────────────────────────────

@dcpi_auto_press_bp.route(
    "/api/v1/dcpi/auto-press/scan", methods=["POST"]
)
def scan_and_draft():
    """Scan DCPI for significant shifts + auto-draft press releases."""
    if not _admin_or_cron_authorized():
        return jsonify({"ok": False, "error": "auth_required"}), 401

    shifts = _find_significant_shifts()
    drafted = []
    skipped = []
    for shift in shifts:
        if _already_drafted_recently(shift["slug"]):
            skipped.append({"slug": shift["slug"], "reason": "dedup_7d"})
            continue
        release = _draft_press_release(shift)
        new_id = _insert_release(release)
        if new_id:
            drafted.append({
                "release_id":  new_id,
                "slug":        release["slug"],
                "title":       release["title"][:100],
                "market":      shift["name"],
                "delta":       shift["delta"],
                "verdict_flip": shift["is_verdict_flip"],
                "url":         f"https://dchub.cloud/news/{release['slug']}",
            })
        else:
            skipped.append({"slug": shift["slug"], "reason": "insert_failed"})

    return jsonify({
        "ok":              True,
        "ran_at":          datetime.datetime.utcnow().isoformat() + "Z",
        "shifts_detected": len(shifts),
        "drafted_count":   len(drafted),
        "skipped_count":   len(skipped),
        "drafted":         drafted,
        "skipped":         skipped,
        "min_point_move":  _MIN_POINT_MOVE,
        "lookback_days":   _LOOKBACK_DAYS,
        "next_step":       ("press-scan-daily (13:00 UTC) will pick up these "
                              "releases + push to LinkedIn / press feed / RSS."),
    }), 200


@dcpi_auto_press_bp.route(
    "/api/v1/dcpi/auto-press/recent", methods=["GET"]
)
def recent_auto_press():
    """Last 20 auto-generated DCPI shift releases (public)."""
    c = _db_conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, slug, title, subheadline, date
                  FROM press_releases
                 WHERE slug LIKE 'dcpi-shift-%%'
                   AND published = TRUE
                 ORDER BY date DESC, id DESC
                 LIMIT 20
            """)
            rows = cur.fetchall() or []
    except Exception as e:
        try: c.close()
        except Exception: pass
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
    finally:
        try: c.close()
        except Exception: pass

    return jsonify({
        "ok":    True,
        "count": len(rows),
        "items": [{
            "id":           r[0],
            "slug":         r[1],
            "title":        r[2],
            "subheadline":  r[3],
            "date":         r[4].isoformat() if r[4] else None,
            "url":          f"https://dchub.cloud/news/{r[1]}",
        } for r in rows],
    }), 200
