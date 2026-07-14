"""
Phase FF+23-daily (2026-05-20) — daily image render fanout.
=============================================================

The Cloudflare Pages worker exposes /api/admin/render-daily which
takes ?theme=X&size=Y and produces one PNG via Browser Rendering →
uploads to R2 at <date>/<theme>_<size>.png.

Each Pages-worker invocation is bounded by CPU time, so we can't ask
it to render all 27 (9 themes × 3 sizes) variants in one call. This
module's job is the fanout: walks the grid, POSTs each combo to the
worker, accumulates results.

Why on Railway instead of inside the worker:
  - Pages Workers don't support cron triggers (need a standalone
    Worker for `[triggers]` in wrangler.toml).
  - Railway already runs a robust scheduler with retries + logging.
  - This module is ~100 lines and reuses the existing pattern.

Endpoint:
  POST /api/jobs/render-daily-fanout    Loop 27 calls (admin gated)

Triggered daily at 06:00 UTC by dchub-scheduler.py.
"""
import os
from internal_auth import accepted_internal_keys
import time
import logging
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
daily_render_fanout_bp = Blueprint("daily_render_fanout", __name__)


# ── Auth ────────────────────────────────────────────────────────────
_INTERNAL_KEYS = accepted_internal_keys()
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "MCP_INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


# ── Config ──────────────────────────────────────────────────────────
# Hit the prod Pages worker. (Local dev sets DCHUB_FRONTEND_URL.)
FRONTEND_URL = os.environ.get("DCHUB_FRONTEND_URL", "https://dchub.cloud")
RENDER_PATH = "/api/admin/render-daily"
INTERNAL_KEY = os.environ.get("DCHUB_INTERNAL_KEY", "")
PER_CALL_TIMEOUT = 60          # Browser Rendering screenshots ~5-15s typically
INTER_CALL_DELAY = 0.5         # mild pacing so we don't burst the worker
THEMES = ['d', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l']
SIZES = ['square', 'portrait', 'landscape']


def _trigger_render(theme: str, size: str, date_str: str) -> dict:
    import requests
    url = (f"{FRONTEND_URL}{RENDER_PATH}"
           f"?theme={theme}&size={size}&date={date_str}")
    headers = {"X-Internal-Key": INTERNAL_KEY}
    started = time.time()
    try:
        r = requests.post(url, headers=headers, timeout=PER_CALL_TIMEOUT)
        elapsed = round(time.time() - started, 2)
        body = {}
        try:
            body = r.json()
        except Exception:
            body = {"raw": (r.text or "")[:200]}
        return {
            "theme": theme,
            "size": size,
            "status_code": r.status_code,
            "elapsed_s": elapsed,
            "ok": r.ok and bool(body.get("success")),
            "key": body.get("key"),
            "bytes": body.get("bytes"),
            "error": body.get("error"),
            "detail": body.get("detail"),
        }
    except Exception as e:
        return {
            "theme": theme,
            "size": size,
            "ok": False,
            "elapsed_s": round(time.time() - started, 2),
            "error": "request_failed",
            "detail": str(e)[:200],
        }


def _run_fanout(date_str: str = None) -> dict:
    """Hit /api/admin/render-daily for all 27 theme×size combos."""
    if not date_str:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    started = time.time()
    results = []
    successes = 0
    for theme in THEMES:
        for size in SIZES:
            res = _trigger_render(theme, size, date_str)
            results.append(res)
            if res.get("ok"):
                successes += 1
            time.sleep(INTER_CALL_DELAY)

    return {
        "ok": successes > 0,
        "date": date_str,
        "total": len(results),
        "succeeded": successes,
        "failed": len(results) - successes,
        "duration_seconds": round(time.time() - started, 2),
        "results": results,
        "r2_browse_url": (f"https://pub-18706471a3884f1eae0fc54ed7d41341.r2.dev/"
                          f"{date_str}/"),
    }


@daily_render_fanout_bp.route("/api/jobs/render-daily-fanout", methods=["POST"])
def run_fanout():
    if not _admin_ok():
        return jsonify(error="forbidden", hint="X-Internal-Key required"), 403
    date_str = (request.args.get("date") or "").strip() or None
    out = _run_fanout(date_str=date_str)
    return jsonify(out), (200 if out["ok"] else 500)


@daily_render_fanout_bp.route("/api/jobs/render-daily-fanout/status", methods=["GET"])
def status():
    """Diagnostic: today's R2 bucket inventory (whether the renders
    landed). No auth — read-only."""
    import requests
    date_str = (request.args.get("date") or "").strip() \
        or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base = f"https://pub-18706471a3884f1eae0fc54ed7d41341.r2.dev/{date_str}"
    found = []
    missing = []
    for theme in THEMES:
        for size in SIZES:
            url = f"{base}/{theme}_{size}.png"
            try:
                r = requests.head(url, timeout=5)
                if r.status_code == 200:
                    found.append({"theme": theme, "size": size,
                                  "bytes": int(r.headers.get("content-length", 0))})
                else:
                    missing.append({"theme": theme, "size": size, "status": r.status_code})
            except Exception as e:
                missing.append({"theme": theme, "size": size, "error": str(e)[:80]})
    return jsonify(
        date=date_str,
        total=27,
        found=len(found),
        missing=len(missing),
        found_list=found[:5],
        missing_list=missing[:5],
        r2_browse_url=base + "/",
    )


# ── Public-page canary ──────────────────────────────────────────────
# The R2 inventory /status probe above answers "did the renders land in
# the bucket." It does NOT answer the question a human hits on their phone:
# "does the PUBLIC /daily page show real images, or broken tiles." Those
# diverge during the nightly UTC-rollover gap (the page advances to today's
# date at 00:00 UTC; the render cron writes that folder ~06:00 UTC) and
# whenever the CF edge serves a stale page. This canary fetches the live
# public page, extracts every tile, and — mirroring the browser — treats a
# tile as BROKEN only when BOTH its primary src AND its onerror fallback
# fail. On a genuine break it files one `daily_page_broken_tiles` brain
# finding; when healthy it resolves an open one. Findings-only + idempotent
# (canonical upsert dedupe). Kill: DAILY_PAGE_CANARY_DISABLE=1.
import re as _re
from urllib.parse import urljoin as _urljoin

PUBLIC_DAILY_URL = os.environ.get("DAILY_PUBLIC_URL", "https://dchub.cloud/daily")


def _probe_status(url):
    """HEAD (fall back to a 1-byte ranged GET when HEAD is refused).
    Returns int status, or None on transport error."""
    import requests
    try:
        r = requests.head(url, timeout=10, allow_redirects=True)
        if r.status_code in (403, 405, 501):
            r = requests.get(url, timeout=15, allow_redirects=True,
                             stream=True, headers={"Range": "bytes=0-0"})
            code = r.status_code
            r.close()
            return code
        return r.status_code
    except Exception:
        return None


def _canary_scan():
    """Fetch the public page, HEAD every tile (+ its fallback), summarize.
    A tile is broken only when primary AND fallback both fail — exactly what
    a real browser renders as a broken image."""
    import requests
    out = {"ok": True, "url": PUBLIC_DAILY_URL, "checked": 0,
           "broken": [], "page_status": None}
    try:
        resp = requests.get(PUBLIC_DAILY_URL, timeout=15,
                            headers={"User-Agent": "DCHub-DailyCanary/1"})
        out["page_status"] = resp.status_code
        if resp.status_code != 200:
            out["ok"] = False
            out["error"] = f"page HTTP {resp.status_code}"
            return out
        html = resp.text
    except Exception as e:
        out["ok"] = False
        out["error"] = f"page fetch failed: {str(e)[:120]}"
        return out

    for tag in _re.findall(r"<img[^>]+>", html):
        m_src = _re.search(r'src="([^"]+)"', tag)
        if not m_src:
            continue
        src = m_src.group(1)
        # Only real tiles: R2 PNGs or a /generate render. Skip inline SVG
        # placeholders (data:) and nav/logo imagery.
        is_tile = (("r2.dev" in src and src.endswith(".png"))
                   or "/generate?" in src)
        if not is_tile:
            continue
        out["checked"] += 1
        m_fb = _re.search(r"this\.src='([^']+)'", tag)
        fb = _urljoin(PUBLIC_DAILY_URL, m_fb.group(1)) if m_fb else None
        s_src = _probe_status(_urljoin(PUBLIC_DAILY_URL, src))
        if s_src == 200:
            continue
        s_fb = _probe_status(fb) if fb else None
        if s_fb == 200:
            continue  # browser would silently recover — not user-visible
        out["broken"].append({
            "src": src, "src_status": s_src,
            "fallback": fb, "fallback_status": s_fb,
        })
    out["ok"] = not out["broken"]
    return out


def _file_canary_finding(scan):
    """Upsert one open finding when tiles are broken; resolve it when the
    page recovers. Mirrors cadence_sentinel: transaction-mode conn, savepoint-
    wrapped upsert, resolve only an already-open row so healthy ticks don't
    inflate seen_count toward the runaway-quarantine threshold."""
    issue = "daily_page_broken_tiles"
    result = {"filed": 0, "resolved": 0}
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return result
    conn = None
    try:
        import psycopg2 as _pg
        from routes.brain_findings_writer import upsert_brain_finding
        conn = _pg.connect(db, connect_timeout=8)
        with conn.cursor() as cur:
            if scan["broken"]:
                n = len(scan["broken"])
                sample = "; ".join(
                    f"{b['src'].split('/')[-1]}(src={b['src_status']},"
                    f"fb={b['fallback_status']})"
                    for b in scan["broken"][:6])
                detail = (f"{n}/{scan['checked']} tiles broken on {scan['url']} "
                          f"(primary + fallback both failed). {sample}")
                r = upsert_brain_finding(cur, issue=issue, url=scan["url"],
                                         count=n, detail=detail,
                                         detector="daily_page_canary")
                result["filed"] = 1 if r in ("inserted", "updated") else 0
            else:
                cur.execute(
                    "UPDATE brain_findings SET status='resolved' "
                    "WHERE issue=%s AND COALESCE(status,'open') "
                    "NOT IN ('resolved','wont_fix','dismissed')", (issue,))
                result["resolved"] = cur.rowcount or 0
        conn.commit()
    except Exception as e:
        logger.warning("[daily-canary] finding write failed: %s", e)
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
    return result


@daily_render_fanout_bp.route("/api/jobs/daily-page-canary",
                              methods=["GET", "POST"])
def daily_page_canary():
    """Public read-only canary over the live /daily page. Returns the scan and
    files/resolves the brain finding (heartbeat POSTs every 3h). No auth:
    read-only + findings-only, same posture as the /status inventory probe.
    Always 200 (the finding is the alert channel). Kill: DAILY_PAGE_CANARY_DISABLE=1."""
    if os.environ.get("DAILY_PAGE_CANARY_DISABLE") == "1":
        return jsonify(ok=True, disabled=True), 200
    scan = _canary_scan()
    scan["findings"] = _file_canary_finding(scan)
    return jsonify(**scan), 200


def _smoke():
    logger.info("[daily-render-fanout] ready, target=%s%s", FRONTEND_URL, RENDER_PATH)

_smoke()
