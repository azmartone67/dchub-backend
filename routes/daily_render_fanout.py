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
import time
import logging
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
daily_render_fanout_bp = Blueprint("daily_render_fanout", __name__)


# ── Auth ────────────────────────────────────────────────────────────
_INTERNAL_KEYS = {"dchub-internal-sync-2026"}
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
INTERNAL_KEY = "dchub-internal-sync-2026"
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


def _smoke():
    logger.info("[daily-render-fanout] ready, target=%s%s", FRONTEND_URL, RENDER_PATH)

_smoke()
