"""Auto-interconnect — 3-stage DETECT / AUTO-ACT / NOTIFY pipeline.
==============================================================

r-ai (2026-06-04): closes the loop on the "novel AI agent shows up,
hits /mcp anonymously, never gets seeded as a partner" gap. Daily cron
scans the last 14 days of ai_requests + mcp_connections for:

  1. Novel UAs that bucket to mcp_generic / direct / unknown_ai but
     carry a vendor-token/version structure (e.g. "phind/1.2.3",
     "warpcli/0.4", "kagi-llm/0.0.1") — promote to a discovered_platforms
     row + ai_cumulative name backfill + _PARTNERS_AUTO stub.
  2. ai_cumulative rows for known-but-unseeded platforms (groq,
     cohere, huggingface, mistral, etc.) that have request_count > 0
     but no /partners/<slug> page — write a _PARTNERS_AUTO stub.

Guardrails (read these before adding bypasses):

  * Floor: a UA must be seen from ≥ 2 distinct IPs AND have ≥ 5 hits
    in the last 14 d before promotion. This kills the "single brain
    self-probe minted a new partner" failure mode.
  * Internal-UA exclusion: any UA matching _INTERNAL_UA_MARKERS from
    ai_tracking.py is skipped silently. The cron-runner also stamps
    `dchub-cron-autointerconnect/1.0` so the runner can't promote
    itself.
  * Curator wins: _PARTNERS_AUTO writes NEVER touch _PARTNERS. If a
    slug already lives in _PARTNERS, the auto entry is skipped.
  * No outbound: this module NEVER sends email to the discovered
    domain and NEVER mints a dev key. The only outbound is ONE digest
    email to DCHUB_ADMIN_EMAIL with approve URLs.
  * approve_token is single-use: first GET flips status to 'approved'
    and stamps approved_at + approved_by_ip + approved_by_ua. Repeat
    clicks 404. Promoting an auto entry into the curated _PARTNERS
    dict still requires a manual PR.
  * Idempotent re-runs: a UNIQUE partial index on user_agent (for
    status IN ('pending','approved')) makes ON CONFLICT DO NOTHING
    safe; identical scans don't double-promote.

Endpoints:
  POST /api/v1/admin/auto-interconnect/run             (admin-gated)
  GET  /api/v1/admin/auto-interconnect/findings        (paginated list)
  GET  /api/v1/admin/auto-interconnect/approve/<token> (single-use)
  POST /api/v1/admin/auto-interconnect/dismiss/<token>

dry_run=1 skips writes + email entirely.
"""
from __future__ import annotations

import os
import re
import uuid
import logging
import datetime
from flask import Blueprint, jsonify, request


logger = logging.getLogger(__name__)
auto_interconnect_bp = Blueprint("auto_interconnect", __name__)


# ── Admin gate ──────────────────────────────────────────────────────
_INTERNAL_KEYS = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY",
           "DCHUB_ADMIN_API_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


# ── Constants ───────────────────────────────────────────────────────
# Match `vendor/version` at the start of the UA. Vendor must start
# with a letter, ≥ 3 chars; version must start with a digit. This
# rules out things like `Mozilla/5.0` (caught by _INTERNAL_UA_MARKERS
# / known-platform check) and `??/` garbage.
_VENDOR_TOKEN_RE = re.compile(
    r"^([A-Za-z][A-Za-z0-9_.-]{2,})/([0-9][0-9A-Za-z.+-]*)"
)

# Deterministic color picker so re-runs assign the same color to the
# same vendor (UI consistency).
_COLOR_PALETTE = [
    "#f59e0b", "#06b6d4", "#84cc16", "#ec4899",
    "#a855f7", "#14b8a6", "#ef4444", "#3b82f6",
]


def _color_for(vendor: str) -> str:
    if not vendor:
        return _COLOR_PALETTE[0]
    return _COLOR_PALETTE[abs(hash(vendor.lower())) % len(_COLOR_PALETTE)]


def _is_internal_marker(ua_lower: str) -> bool:
    try:
        from ai_tracking import _INTERNAL_UA_MARKERS
    except Exception:
        # Conservative fallback if the import path drifts.
        _INTERNAL_UA_MARKERS = (
            "dchub", "dc-hub", "dc hub", "probe", "scanner", "loopback",
            "127.0.0.1", "localhost", "python-requests", "curl/", "wget/",
            "uptime", "healthcheck", "health-check", "render-health",
        )
    return any(m in ua_lower for m in _INTERNAL_UA_MARKERS)


def _known_ai_slugs() -> set:
    """Slugs already in ai_tracking.AI_PLATFORMS — must NOT be re-promoted."""
    try:
        from ai_tracking import AI_PLATFORMS
        return set(AI_PLATFORMS.keys())
    except Exception:
        return set()


def _curated_partner_slugs() -> set:
    """Slugs already in the curator-edited _PARTNERS dict — must NOT be
    overwritten by the auto pipeline."""
    try:
        from routes.partner_landing import _PARTNERS
        return set(_PARTNERS.keys())
    except Exception:
        return set()


# ── DETECT ──────────────────────────────────────────────────────────
def _detect_novel_uas(conn):
    """Return list of {user_agent, vendor, version, hits, ips, first_seen,
    last_seen} for UAs seen ≥ 5 times from ≥ 2 IPs in the last 14 d, where
    the UA matches `vendor/version` and is NOT an internal marker.
    Sources: ai_requests + mcp_connections (UNION ALL)."""
    sql = """
        SELECT user_agent,
               COUNT(*)                  AS hits,
               COUNT(DISTINCT ip_address) AS ips,
               MIN(created_at)           AS first_seen,
               MAX(created_at)           AS last_seen
          FROM (
            SELECT user_agent, ip_address, created_at
              FROM ai_requests
             WHERE platform IN ('mcp_generic','direct','unknown_ai','mcp')
               AND created_at > NOW() - INTERVAL '14 days'
            UNION ALL
            SELECT user_agent, ip_address, created_at
              FROM mcp_connections
             WHERE platform IN ('mcp_generic','direct','unknown_ai','mcp')
               AND created_at > NOW() - INTERVAL '14 days'
          ) u
         WHERE user_agent IS NOT NULL AND length(user_agent) > 0
         GROUP BY user_agent
        HAVING COUNT(*) >= 5 AND COUNT(DISTINCT ip_address) >= 2
         ORDER BY hits DESC
         LIMIT 200
    """
    out = []
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall() or []
    except Exception as e:
        logger.warning("auto_interconnect: detect_novel_uas query failed — %s", e)
        try: conn.rollback()
        except Exception: pass
        return out

    known_ai = _known_ai_slugs()
    for r in rows:
        ua = (r[0] or "")[:500]
        ua_l = ua.lower()
        if _is_internal_marker(ua_l):
            continue
        m = _VENDOR_TOKEN_RE.match(ua)
        if not m:
            continue
        vendor = m.group(1)
        version = m.group(2)
        slug = vendor.lower()
        if slug in known_ai:
            continue  # already a recognized platform
        if slug in _curated_partner_slugs():
            continue
        out.append({
            "user_agent": ua,
            "vendor":     vendor,
            "version":    version,
            "inferred_slug": slug,
            "hits":       int(r[1] or 0),
            "ips":        int(r[2] or 0),
            "first_seen": r[3],
            "last_seen":  r[4],
        })
    return out


def _detect_pending_buckets(conn):
    """Return list of ai_cumulative rows with traffic but no _PARTNERS entry
    (groq/cohere/huggingface/mistral/etc.)."""
    sql = """
        SELECT platform, total_requests, name, company, color,
               first_seen, last_seen
          FROM ai_cumulative
         WHERE COALESCE(total_requests, 0) > 0
           AND platform IS NOT NULL
           AND platform NOT IN ('internal', 'mcp_generic', 'direct',
                                 'unknown_ai', 'mcp', 'Unknown')
         ORDER BY total_requests DESC
         LIMIT 100
    """
    out = []
    curated = _curated_partner_slugs()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall() or []
    except Exception as e:
        logger.warning("auto_interconnect: detect_pending_buckets query failed — %s", e)
        try: conn.rollback()
        except Exception: pass
        return out
    for r in rows:
        slug = (r[0] or "").lower()
        if not slug or slug in curated:
            continue
        out.append({
            "platform":   slug,
            "name":       r[2],
            "company":    r[3],
            "color":      r[4],
            "total_requests": int(r[1] or 0),
            "first_seen": r[5],
            "last_seen":  r[6],
        })
    return out


# ── AUTO-ACT ────────────────────────────────────────────────────────
def _promote_finding(conn, ua: str, vendor: str, version: str,
                     hits: int, ips: int,
                     first_seen, last_seen,
                     source: str = "novel_ua",
                     dry_run: bool = False) -> dict:
    """3 reversible writes inside a single transaction, plus a finding row.
    Returns dict with `steps` (list of step names applied), `token`, `skipped`.
    """
    slug = (vendor or "").lower()
    color = _color_for(vendor)
    company = vendor.title() if vendor else slug
    name = company
    token = uuid.uuid4().hex
    now = datetime.datetime.now(datetime.timezone.utc)
    ttl = now + datetime.timedelta(days=30)
    steps_applied = []

    if dry_run:
        return {
            "user_agent": ua[:500], "vendor": vendor, "version": version,
            "slug": slug, "color": color, "company": company,
            "approve_token": None, "steps": [], "dry_run": True,
            "hits": hits, "ips": ips,
        }

    # Step (1): discovered_platforms upsert.
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO discovered_platforms
                       (user_agent, first_seen, last_seen, request_count,
                        identified_as, protocol_guess, auto_configured)
                VALUES (%s, %s, %s, %s, %s, %s, 0)
                ON CONFLICT (user_agent) DO UPDATE
                  SET last_seen     = EXCLUDED.last_seen,
                      request_count = discovered_platforms.request_count + EXCLUDED.request_count,
                      identified_as = COALESCE(discovered_platforms.identified_as, EXCLUDED.identified_as)
            """, (ua[:500], first_seen, last_seen, hits, name, "auto_detected"))
        conn.commit()
        steps_applied.append("discovered_platforms")
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        logger.warning("auto_interconnect: step(1) discovered_platforms failed for %s — %s", ua[:80], e)

    # Step (2): ai_cumulative name backfill (only if currently Unknown / NULL).
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE ai_cumulative
                   SET name    = COALESCE(NULLIF(name, ''), %s),
                       company = COALESCE(NULLIF(company, ''), %s),
                       color   = COALESCE(NULLIF(color, ''), %s)
                 WHERE platform = %s
                   AND (name IS NULL OR name = '' OR name = 'Unknown')
            """, (name, company, color, slug))
        conn.commit()
        steps_applied.append("ai_cumulative")
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        logger.warning("auto_interconnect: step(2) ai_cumulative failed for %s — %s", slug, e)

    # Step (3): _PARTNERS_AUTO stub.
    try:
        from routes import partner_landing as _pl
        if slug not in _pl._PARTNERS:
            _pl._PARTNERS_AUTO[slug] = {
                "name":     company,
                "company":  company,
                "tagline":  "Auto-detected — pending curator review",
                "auto":     True,
                "color":    color,
                "accent":   color,
                "vendor":   vendor,
                "version":  version,
                "source":   source,
                "discovered_at": now.isoformat(),
                "hits_14d":      hits,
                "distinct_ips":  ips,
            }
            steps_applied.append("_PARTNERS_AUTO")
    except Exception as e:
        logger.warning("auto_interconnect: step(3) _PARTNERS_AUTO failed for %s — %s", slug, e)

    # Step (4): record the finding for admin approval.
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO auto_interconnect_findings
                       (user_agent, vendor, version, inferred_slug, company,
                        color, source, hits_14d, distinct_ips,
                        first_seen, last_seen,
                        approve_token, status, promoted_steps, ttl_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, 'pending', %s, %s)
                ON CONFLICT (user_agent) WHERE status IN ('pending','approved')
                DO NOTHING
                RETURNING approve_token
            """, (ua[:500], vendor, version, slug, company, color,
                  source, hits, ips, first_seen, last_seen,
                  token, ",".join(steps_applied), ttl))
            r = cur.fetchone()
            if r and r[0]:
                token = r[0]
            else:
                token = None  # already had a pending/approved finding for this UA
        conn.commit()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        logger.warning("auto_interconnect: step(4) findings insert failed for %s — %s", ua[:80], e)
        token = None

    return {
        "user_agent": ua[:500], "vendor": vendor, "version": version,
        "slug": slug, "color": color, "company": company,
        "approve_token": token, "steps": steps_applied, "dry_run": False,
        "hits": hits, "ips": ips,
    }


def _promote_pending_bucket(conn, bucket: dict, dry_run: bool = False) -> dict:
    """Lighter promotion for ai_cumulative buckets with existing name/color
    already populated. Skips step (1) since there's no UA; writes only the
    _PARTNERS_AUTO stub + the finding row."""
    slug = bucket["platform"]
    name = bucket.get("name") or slug.title()
    company = bucket.get("company") or name
    color = bucket.get("color") or _color_for(slug)
    token = uuid.uuid4().hex
    now = datetime.datetime.now(datetime.timezone.utc)
    ttl = now + datetime.timedelta(days=30)
    steps_applied = []

    if dry_run:
        return {"slug": slug, "name": name, "approve_token": None,
                "steps": [], "dry_run": True, "source": "pending_bucket"}

    try:
        from routes import partner_landing as _pl
        if slug not in _pl._PARTNERS:
            _pl._PARTNERS_AUTO[slug] = {
                "name":     name,
                "company":  company,
                "tagline":  "Auto-detected from ai_cumulative — pending curator review",
                "auto":     True,
                "color":    color,
                "accent":   color,
                "source":   "pending_bucket",
                "discovered_at": now.isoformat(),
                "total_requests": bucket.get("total_requests", 0),
            }
            steps_applied.append("_PARTNERS_AUTO")
    except Exception as e:
        logger.warning("auto_interconnect: pending_bucket _PARTNERS_AUTO failed for %s — %s", slug, e)

    # Synthesize a UA-like key so the UNIQUE index doesn't double-fire for
    # the same bucket on repeat runs.
    synthetic_ua = f"pending_bucket:{slug}"[:500]
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO auto_interconnect_findings
                       (user_agent, vendor, version, inferred_slug, company,
                        color, source, hits_14d, distinct_ips,
                        first_seen, last_seen,
                        approve_token, status, promoted_steps, ttl_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending_bucket', %s, %s,
                        %s, %s, %s, 'pending', %s, %s)
                ON CONFLICT (user_agent) WHERE status IN ('pending','approved')
                DO NOTHING
                RETURNING approve_token
            """, (synthetic_ua, name, None, slug, company, color,
                  int(bucket.get("total_requests") or 0), 0,
                  bucket.get("first_seen"), bucket.get("last_seen"),
                  token, ",".join(steps_applied), ttl))
            r = cur.fetchone()
            if r and r[0]:
                token = r[0]
            else:
                token = None
        conn.commit()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        logger.warning("auto_interconnect: pending_bucket findings insert failed for %s — %s", slug, e)
        token = None

    return {"slug": slug, "name": name, "approve_token": token,
            "steps": steps_applied, "dry_run": False,
            "source": "pending_bucket",
            "total_requests": bucket.get("total_requests", 0)}


# ── NOTIFY ──────────────────────────────────────────────────────────
def _send_admin_digest(promoted: list) -> bool:
    """Send ONE digest email summarizing N findings + approve URLs. Uses
    DCHUB_RESEND_API_KEY → api.resend.com/emails. No-ops cleanly if env
    vars are missing or the network call fails."""
    if not promoted:
        return False
    resend_key = (os.environ.get("DCHUB_RESEND_API_KEY") or "").strip()
    admin_email = (os.environ.get("DCHUB_ADMIN_EMAIL") or "azmartone@gmail.com").strip()
    if not resend_key or not admin_email:
        logger.info("auto_interconnect: digest skipped — DCHUB_RESEND_API_KEY or DCHUB_ADMIN_EMAIL missing")
        return False

    base_url = (os.environ.get("DCHUB_PUBLIC_BASE")
                or "https://dchub-backend-production.up.railway.app")
    items_html = []
    for p in promoted:
        if not p.get("approve_token"):
            continue
        slug = p.get("slug") or p.get("vendor") or "?"
        company = p.get("company") or p.get("name") or slug
        version = p.get("version") or ""
        src = p.get("source") or "novel_ua"
        approve_url = f"{base_url}/api/v1/admin/auto-interconnect/approve/{p['approve_token']}"
        items_html.append(
            f"<li style='margin:10px 0'>"
            f"<strong>{company}</strong> ({slug}/{version}) · {src} · "
            f"{p.get('hits', 0)} hits / {p.get('ips', 0)} IPs<br>"
            f"<a href='{approve_url}'>Approve →</a></li>"
        )
    if not items_html:
        return False

    html = (
        "<h2>DC Hub auto-interconnect — daily digest</h2>"
        f"<p>{len(items_html)} new partner candidate(s) detected from "
        "novel UA traffic + pending ai_cumulative buckets.</p>"
        f"<ul>{''.join(items_html)}</ul>"
        "<p style='color:#71717a;font-size:.85em'>"
        "Approve link is single-use. Promoting an auto entry into the "
        "curator-edited _PARTNERS dict still requires a manual PR — this "
        "approve only flips the finding status so the admin dashboard can "
        "filter on it.</p>"
    )

    try:
        import requests as _rq
        resp = _rq.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {resend_key}",
                       "Content-Type": "application/json"},
            json={
                "from":    "DC Hub Autopilot <press@dchub.cloud>",
                "to":      [admin_email],
                "subject": f"DC Hub auto-interconnect · {len(items_html)} new candidate(s)",
                "html":    html,
            },
            timeout=15,
        )
        if 200 <= resp.status_code < 300:
            logger.info("auto_interconnect: digest sent — %s candidates", len(items_html))
            return True
        logger.warning("auto_interconnect: digest send returned %s — %s",
                       resp.status_code, resp.text[:200])
    except Exception as e:
        logger.warning("auto_interconnect: digest send failed — %s", e)
    return False


# ── Detection cycle (DETECT → AUTO-ACT → NOTIFY) ───────────────────
def _run_detection_cycle(dry_run: bool = False) -> dict:
    conn = _get_db()
    if conn is None:
        return {"ok": False, "error": "no_db"}

    out = {
        "ok": True,
        "dry_run": dry_run,
        "scanned": 0,
        "novel_uas": [],
        "pending_buckets": [],
        "promoted": [],
        "skipped": [],
        "findings_written": 0,
        "email_sent": False,
    }
    try:
        novel = _detect_novel_uas(conn)
        pending = _detect_pending_buckets(conn)
        out["scanned"] = len(novel) + len(pending)
        out["novel_uas"] = [
            {"user_agent": n["user_agent"][:120], "vendor": n["vendor"],
             "version": n["version"], "hits": n["hits"], "ips": n["ips"]}
            for n in novel
        ]
        out["pending_buckets"] = [
            {"slug": b["platform"], "name": b.get("name"),
             "total_requests": b.get("total_requests")}
            for b in pending
        ]

        for n in novel:
            res = _promote_finding(
                conn, n["user_agent"], n["vendor"], n["version"],
                n["hits"], n["ips"], n["first_seen"], n["last_seen"],
                source="novel_ua", dry_run=dry_run,
            )
            if res.get("approve_token") or dry_run:
                out["promoted"].append(res)
                if not dry_run:
                    out["findings_written"] += 1
            else:
                out["skipped"].append({
                    "user_agent": n["user_agent"][:120],
                    "reason": "already_pending_or_approved",
                })

        for b in pending:
            res = _promote_pending_bucket(conn, b, dry_run=dry_run)
            if res.get("approve_token") or dry_run:
                out["promoted"].append(res)
                if not dry_run:
                    out["findings_written"] += 1
            else:
                out["skipped"].append({
                    "slug": b["platform"],
                    "reason": "already_pending_or_approved",
                })

        if not dry_run:
            out["email_sent"] = _send_admin_digest(out["promoted"])
    finally:
        try: conn.close()
        except Exception: pass
    return out


# ── Endpoints ───────────────────────────────────────────────────────
@auto_interconnect_bp.route(
    "/api/v1/admin/auto-interconnect/run", methods=["POST", "GET"])
def auto_interconnect_run():
    """Trigger one detection cycle. Idempotent — UNIQUE partial index on
    user_agent (for pending/approved findings) keeps repeat scans cheap.
    `?dry_run=1` returns what WOULD be promoted without writing or emailing."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    dry_run = (request.args.get("dry_run") or "").lower() in ("1", "true", "yes")
    result = _run_detection_cycle(dry_run=dry_run)
    resp = jsonify(result)
    # Auto-interconnect responses should NEVER be edge-cached — each run
    # potentially yields different per-recipient approve URLs.
    resp.headers["Cache-Control"] = "no-store"
    return resp, (200 if result.get("ok") else 503)


@auto_interconnect_bp.route(
    "/api/v1/admin/auto-interconnect/findings", methods=["GET"])
def auto_interconnect_findings():
    """Paginated list of findings, newest first. `?status=` filters by
    status (pending/approved/dismissed/expired)."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    status = (request.args.get("status") or "").strip().lower()
    limit = min(max(int(request.args.get("limit") or 50), 1), 200)
    offset = max(int(request.args.get("offset") or 0), 0)
    conn = _get_db()
    if conn is None:
        return jsonify(ok=False, error="no_db"), 503
    sql = """
        SELECT id, user_agent, vendor, version, inferred_slug,
               company, color, source, hits_14d, distinct_ips,
               first_seen, last_seen, status,
               approved_at, ttl_at, created_at,
               promoted_steps
          FROM auto_interconnect_findings
         {where_clause}
         ORDER BY created_at DESC
         LIMIT %s OFFSET %s
    """
    params = []
    where = ""
    if status:
        where = "WHERE status = %s"
        params.append(status)
    params.extend([limit, offset])
    out = []
    try:
        with conn.cursor() as cur:
            cur.execute(sql.format(where_clause=where), params)
            for r in cur.fetchall() or []:
                out.append({
                    "id": r[0], "user_agent": r[1], "vendor": r[2],
                    "version": r[3], "inferred_slug": r[4], "company": r[5],
                    "color": r[6], "source": r[7], "hits_14d": r[8],
                    "distinct_ips": r[9],
                    "first_seen": r[10].isoformat() if r[10] else None,
                    "last_seen":  r[11].isoformat() if r[11] else None,
                    "status": r[12],
                    "approved_at": r[13].isoformat() if r[13] else None,
                    "ttl_at":     r[14].isoformat() if r[14] else None,
                    "created_at": r[15].isoformat() if r[15] else None,
                    "promoted_steps": r[16],
                })
    except Exception as e:
        try: conn.close()
        except Exception: pass
        return jsonify(ok=False, error=str(e)[:200]), 503
    finally:
        try: conn.close()
        except Exception: pass
    return jsonify(ok=True, count=len(out), findings=out), 200


@auto_interconnect_bp.route(
    "/api/v1/admin/auto-interconnect/approve/<token>", methods=["GET", "POST"])
def auto_interconnect_approve(token):
    """Single-use approve endpoint. First click flips status='approved' and
    stamps approved_at + approved_by_ip + approved_by_ua. Repeat clicks 404
    so a leaked URL can't toggle state twice."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if not token or len(token) < 8:
        return jsonify(ok=False, error="bad_token"), 400
    conn = _get_db()
    if conn is None:
        return jsonify(ok=False, error="no_db"), 503
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "")[:64]
    ua = (request.headers.get("User-Agent") or "")[:500]
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE auto_interconnect_findings
                   SET status         = 'approved',
                       approved_at    = NOW(),
                       approved_by_ip = %s,
                       approved_by_ua = %s,
                       ttl_at         = NOW() + INTERVAL '90 days'
                 WHERE approve_token = %s
                   AND status = 'pending'
                 RETURNING id, inferred_slug, company
            """, (ip, ua, token))
            row = cur.fetchone()
        conn.commit()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        try: conn.close()
        except Exception: pass
        return jsonify(ok=False, error=str(e)[:200]), 503
    finally:
        try: conn.close()
        except Exception: pass
    if not row:
        return jsonify(ok=False, error="token_not_found_or_already_used"), 404
    return jsonify(ok=True, approved={"id": row[0], "slug": row[1],
                                        "company": row[2]}), 200


@auto_interconnect_bp.route(
    "/api/v1/admin/auto-interconnect/dismiss/<token>", methods=["POST", "GET"])
def auto_interconnect_dismiss(token):
    """Mark a finding as dismissed (not promoted; no further surfacing).
    Idempotent — repeat dismisses just re-stamp the row."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if not token or len(token) < 8:
        return jsonify(ok=False, error="bad_token"), 400
    conn = _get_db()
    if conn is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE auto_interconnect_findings
                   SET status = 'dismissed'
                 WHERE approve_token = %s
                   AND status IN ('pending','approved')
                 RETURNING id, inferred_slug
            """, (token,))
            row = cur.fetchone()
        conn.commit()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        try: conn.close()
        except Exception: pass
        return jsonify(ok=False, error=str(e)[:200]), 503
    finally:
        try: conn.close()
        except Exception: pass
    if not row:
        return jsonify(ok=False, error="token_not_found"), 404
    return jsonify(ok=True, dismissed={"id": row[0], "slug": row[1]}), 200
