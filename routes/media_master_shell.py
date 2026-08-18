"""
media_master_shell.py — the self-driving MEDIA orchestrator (2026-07-03).

Owns lever #4 of the growth flywheel: keep the analyst voice publishing on
cadence instead of going silent. The media machine already has a strong quality
gate (number-lead required, claim-verified, fact-checked) AND a verified
evergreen publisher (`/api/v1/admin/media/showcase/publish`) — the failure mode
is STARVATION: the editorial desk suppresses whole slots when nothing is
"novel", so the feed deadlocks to 0 posts. This shell measures cadence +
citation velocity and, when the feed is starved, fires ONE number-led evergreen
post — guaranteeing substance over silence.

Mirrors the house master-shell pattern (loopback self-calls + fire-and-forget so
it stays under CF's ~15s proxy limit; admin-gated; killable). Paired with the
2026-07-03 suppression loosening in media_editorial.py (MEDIA_EDITORIAL_REST_DAYS
12->5) so strong stale data points re-run sooner.

Endpoints:
  POST /api/v1/admin/media/master-tick   — measure -> score -> act -> persist
  GET  /api/v1/admin/media/master-state  — latest snapshot + trend
"""
import os
import json
import time
import hmac
import urllib.request
import urllib.error
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write

media_master_shell_bp = Blueprint("media_master_shell", __name__)

# Loopback on Railway (the shell runs INSIDE the backend — see growth_master_shell).
_BACKEND_BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else os.environ.get("DCHUB_BACKEND_BASE", "https://dchub-backend-production.up.railway.app")
)


def _admin_key() -> str | None:
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _admin_ok() -> bool:
    expected = (_admin_key() or "").strip()
    if not expected:
        return False
    got = (request.headers.get("X-Admin-Key") or request.args.get("admin_key") or "").strip()
    return bool(got) and hmac.compare_digest(got, expected)


def _disabled() -> bool:
    return str(os.environ.get("MEDIA_MASTER_DISABLED", "")).lower() in ("1", "true", "yes")


def _act_disabled() -> bool:
    return str(os.environ.get("MEDIA_MASTER_ACT_DISABLED", "")).lower() in ("1", "true", "yes")


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


def _req(path: str, method: str = "GET", timeout: int = 10) -> dict:
    url = (path if path.startswith("http") else _BACKEND_BASE.rstrip("/") + path)
    try:
        req = urllib.request.Request(url, data=(b"" if method == "POST" else None), method=method)
        req.add_header("X-DC-Probe", "media-tick")
        req.add_header("User-Agent", "dchub-media-orchestrator/1.0")
        ak = _admin_key()
        if ak:
            req.add_header("X-Admin-Key", ak)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return {"ok": True, "http": resp.status, "data": json.loads(body)}
            except Exception:
                return {"ok": True, "http": resp.status, "data": {"_raw": body[:200]}}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


def _fire(path: str, timeout: int = 2) -> dict:
    """Trigger an action without blocking on its (possibly LLM-slow) completion."""
    url = (path if path.startswith("http") else _BACKEND_BASE.rstrip("/") + path)
    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        req.add_header("X-DC-Probe", "media-tick")
        req.add_header("User-Agent", "dchub-media-orchestrator/1.0")
        ak = _admin_key()
        if ak:
            req.add_header("X-Admin-Key", ak)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": resp.status < 400, "http": resp.status, "dispatched": True}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "dispatched": True}
    except Exception:
        return {"ok": True, "dispatched": True, "note": "not awaited"}


def _conn():
    try:
        from routes.ai_reach import _conn as _raw
        return _raw()
    except Exception:
        return None


def _ensure_tables() -> bool:
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_master_snapshots (
                    id                   SERIAL PRIMARY KEY,
                    computed_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    media_score          NUMERIC(6,2),
                    posts_24h            INTEGER,
                    attempts_24h         INTEGER,
                    citation_velocity_7d INTEGER,
                    citation_trend_pct   NUMERIC(7,3),
                    starved              BOOLEAN,
                    action_taken         TEXT,
                    detail               JSONB
                )
            """)
        return True
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── TIER 1 — MEASURE cadence + citation velocity ──────────────────────
def tier1_measure() -> dict:
    north = (_req("/api/v1/media/north-star").get("data") or {})
    pub = (_req("/api/v1/dchub-media/publisher-status").get("data") or {})
    loops = pub.get("loops") or {}
    if isinstance(loops, dict):
        posts_24h = sum(_num(v.get("successes_24h")) or 0 for v in loops.values())
        attempts_24h = sum(_num(v.get("attempts_24h")) or 0 for v in loops.values())
    else:
        posts_24h, attempts_24h = 0, 0
    # r-durable-cadence (2026-07-10): the publisher loop counters live in
    # process memory and zero out on every redeploy (the web service redeploys
    # many times a day), so this shell chronically read 0 posts/24h -> STARVED
    # and re-fired the evergreen while LinkedIn was in fact posting on
    # schedule (linkedin_posts rows with real URNs).
    # r-multiplatform-cadence (2026-07-11): the durable fallback only counted
    # linkedin_posts, so X/Bluesky publishes (durable ONLY in
    # social_media_posts) were invisible — the lane read "1 posts/24h" while
    # 7 posts actually shipped across platforms. Count BOTH durable ledgers
    # every tick and take the max with the in-memory counters. LinkedIn rows
    # are excluded from the social_media_posts count because a successful
    # social-queue LinkedIn publish is dual-written into linkedin_posts (see
    # content_publisher._persist_linkedin_urn) and would double-count.
    c = _conn()
    if c is not None:
        durable = 0
        try:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM linkedin_posts "
                    "WHERE status = 'success' "
                    "AND created_at > NOW() - INTERVAL '24 hours'")
                durable += int(cur.fetchone()[0] or 0)
                # published_at is TEXT on social_media_posts (mixed
                # 'YYYY-MM-DD HH:MI+00' / 'YYYY-MM-DDTHH:MIZ' formats, so
                # plain text comparison misorders across variants) — cast
                # to timestamptz; all prod rows are ISO (verified 2026-07-11).
                cur.execute(
                    "SELECT COUNT(*) FROM social_media_posts "
                    "WHERE status = 'published' "
                    "AND COALESCE(publish_platform, platform) <> 'linkedin' "
                    "AND NULLIF(published_at, '') IS NOT NULL "
                    "AND published_at::timestamptz > NOW() - INTERVAL '24 hours'")
                durable += int(cur.fetchone()[0] or 0)
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass
        posts_24h = max(posts_24h, durable)
    # r-cv-none (2026-07-11): the old `a or b or c` chain conflated a REAL 0
    # with "field absent" — north-star ALWAYS returns citation_velocity_7d
    # (int, possibly 0), so a 0 fell through to fields that don't exist and
    # the lane displayed "citations None/7d" forever (masking the north-star
    # dedup freeze fixed in media_north_star.py r-cv-freeze). Take the FIRST
    # PRESENT field instead; None now only means the request itself failed.
    cv7 = None
    for _k in ("citation_velocity_7d", "distinct_agents_citing_7d",
               "citations_7d"):
        _v = _num(north.get(_k))
        if _v is not None:
            cv7 = _v
            break
    return {
        "posts_24h": posts_24h,
        "attempts_24h": attempts_24h,
        "citation_velocity_7d": cv7,
        "citation_prior_7d": _num(north.get("prior_7d")),
        "citation_trend_pct": _num(north.get("trend_pct")),
        "honest_read": (
            f"{posts_24h} posts/24h ({attempts_24h} attempts); "
            f"citation velocity {cv7} agents/7d."
        ),
    }


# ── TIER 2 — SCORE (starved?) ─────────────────────────────────────────
def tier2_score(m: dict) -> dict:
    posts = m.get("posts_24h") or 0
    cv = m.get("citation_velocity_7d") or 0
    # cadence is 70% (2+ posts/24h = full), citation velocity presence is 30%.
    health = round(min(1.0, 0.7 * min(posts, 2) / 2.0 + 0.3 * (1.0 if cv else 0.0)), 3)
    starved = (posts == 0)
    return {"media_score": round(100.0 * health, 2), "health": health, "starved": starved}


# ── TIER 3 — ACT (publish a number-led evergreen when starved) ────────
def tier3_act(m: dict, sc: dict) -> dict:
    if _act_disabled():
        return {"action": "none", "reason": "MEDIA_MASTER_ACT_DISABLED (shadow)"}
    if not sc.get("starved"):
        return {"action": "none", "reason": f"feed not starved ({m.get('posts_24h')} posts/24h)"}
    # Starved -> fire a fact-checked, number-led evergreen (self-gated; the
    # showcase publisher enforces its own _factcheck + number-lead). No force
    # needed: 0 posts/24h means the 12h dedup window is already clear.
    r = _fire("/api/v1/admin/media/showcase/publish")
    return {"action": "showcase_evergreen_publish", "dispatched": r.get("dispatched"),
            "note": "number-led market-pulse; fact-checked + self-gated"}


# ── persist + verify ──────────────────────────────────────────────────
def _persist(m: dict, sc: dict, action: dict) -> bool:
    if not _ensure_tables():
        return False
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO media_master_snapshots
                  (media_score, posts_24h, attempts_24h, citation_velocity_7d,
                   citation_trend_pct, starved, action_taken, detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (
                sc.get("media_score"), m.get("posts_24h"), m.get("attempts_24h"),
                m.get("citation_velocity_7d"), m.get("citation_trend_pct"),
                bool(sc.get("starved")), (action or {}).get("action"),
                json.dumps({"measure": m, "score": sc, "action": action}),
            ))
        return True
    except Exception:
        note_swallowed_write("media_master_snapshots", where="media_master_shell._persist")
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── ORCHESTRATOR ──────────────────────────────────────────────────────
@media_master_shell_bp.route("/api/v1/admin/media/master-tick", methods=["POST", "GET"])
def master_tick():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="MEDIA_MASTER_DISABLED"), 200
    started = time.time()
    measure = tier1_measure()
    score = tier2_score(measure)
    action = tier3_act(measure, score)
    persisted = _persist(measure, score, action)
    headline = (
        f"media {score.get('media_score')}/100 · {measure.get('posts_24h')} posts/24h · "
        f"citations {measure.get('citation_velocity_7d')}/7d · "
        f"{'STARVED -> ' + str(action.get('action')) if score.get('starved') else 'cadence ok'}"
    )
    return jsonify(
        ok=True, ms=int((time.time() - started) * 1000),
        media_score=score.get("media_score"), headline=headline,
        tier1_measure=measure, tier2_score=score, tier3_action=action,
        persisted=persisted, generated_at=datetime.now(timezone.utc).isoformat(),
    ), 200


@media_master_shell_bp.route("/api/v1/admin/media/master-state", methods=["GET"])
def state():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(error="db_unavailable"), 503
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM media_master_snapshots ORDER BY id DESC LIMIT 20")
            rows = cur.fetchall()
        return jsonify(ok=True, latest=(rows[0] if rows else None),
                       trend=[{"at": str(r.get("computed_at")), "score": _num(r.get("media_score")),
                               "posts_24h": r.get("posts_24h"), "starved": r.get("starved"),
                               "acted": r.get("action_taken")} for r in rows]), 200
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}"), 500
    finally:
        try: c.close()
        except Exception: pass
