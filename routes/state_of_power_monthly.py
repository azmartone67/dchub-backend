"""state_of_power_monthly.py — immutable, citable monthly State of Power snapshots.

/state-of-power is live: energy_report recomputes it about hourly, so a press
citation of it names a page whose numbers have moved by the time a reader
clicks. Press asked for a URL that says one month and keeps saying it. On
2026-09-26 /state-of-power/2026-09 and /dcpi/monthly both answered 404.

    GET  /state-of-power/<YYYY-MM>                    HTML, the stored snapshot
    GET  /api/v1/reports/state-of-power/<YYYY-MM>     JSON, the stored snapshot
    GET  /api/v1/reports/state-of-power/months        index of captured months
    POST /api/v1/reports/state-of-power/snapshot      admin: capture a month

HOW A MONTH IS FROZEN. The capture takes state_of_power._gather(), the same
payload the live page serves, and stores it once in state_of_power_snapshots
with INSERT ... ON CONFLICT DO NOTHING. Nothing updates or deletes a row, and a
GET never computes or writes. A second capture of the same month is refused
(409) and the stored row is returned unchanged.

★ "AS OF" IS THE CAPTURE TIME, NOT MONTH-END. The live data has no
point-in-time history to replay, so a month holds whatever the live report said
at the moment it was captured. Every snapshot says so in `snapshot.as_of_note`
and carries `captured_at` and `capture_basis`:
  · month_close — captured on or after the 1st of the next month, which is
    what .github/workflows/state-of-power-monthly.yml does at 00:10 UTC on the
    1st, so the values are as of the first minutes after the month closed;
  · mid_month   — captured while the month was still open, which is how
    September 2026 is frozen: it is the report as of that day, not month-end.

The dated path is routed to this backend by dchub-frontend's existing
`/state*` _routes.json include and its `/state-of-power/` worker prefix, so no
frontend change is needed. Werkzeug ranks the typed
<int:year>-<int:month> rule above quarterly_report's `/state-of-power/<quarter>`
string rule, so q3-2026 keeps working and 2026-09 lands here.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import json
import logging
import os
import re

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)
state_of_power_monthly_bp = Blueprint("state_of_power_monthly", __name__)

_BASE = "https://dchub.cloud"
_TABLE = "state_of_power_snapshots"
_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
    "  month         TEXT PRIMARY KEY CHECK (month ~ '^[0-9]{4}-[0-9]{2}$'),"
    "  captured_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
    "  capture_basis TEXT NOT NULL,"
    "  payload       JSONB NOT NULL"
    ")",
)
_AS_OF_NOTE = (
    "Values are as of captured_at, not the last day of the month: this is the "
    "live State of Power report exactly as DC Hub served it at the moment the "
    "month was frozen. It is stored once and never recomputed.")
_MONTH_RE = re.compile(r"^(20\d\d)-(0[1-9]|1[0-2])$")
_IMMUTABLE_CACHE = "public, max-age=3600, s-maxage=86400"


def _conn():
    """A direct autocommit psycopg2 connection, or None. Never the wrapped pool
    cursor (db_utils drops DDL there; see scripts/check_ddl_through_pool.py)."""
    url = (os.environ.get("DATABASE_URL")
           or os.environ.get("NEON_DATABASE_URL") or "").strip()
    if not url:
        return None
    try:
        import psycopg2
        c = psycopg2.connect(url, sslmode="require", connect_timeout=6)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning(f"state_of_power_monthly: no db ({e})")
        return None


def _ensure_table(c) -> None:
    from util.ddl_once import ensure_once
    ensure_once(f"{_TABLE}:v1", c, _DDL)


def _month_label(month: str) -> str:
    y, m = month.split("-")
    return _dt.date(int(y), int(m), 1).strftime("%B %Y")


def _urls(month: str) -> dict:
    return {"html": f"{_BASE}/state-of-power/{month}",
            "json": f"{_BASE}/api/v1/reports/state-of-power/{month}"}


def _this_month(today: _dt.date | None = None) -> str:
    t = today or _dt.datetime.now(_dt.timezone.utc).date()
    return f"{t.year:04d}-{t.month:02d}"


def _previous_month(today: _dt.date | None = None) -> str:
    t = today or _dt.datetime.now(_dt.timezone.utc).date()
    first = t.replace(day=1) - _dt.timedelta(days=1)
    return f"{first.year:04d}-{first.month:02d}"


def _dated(payload: dict, month: str, captured_at: str, basis: str) -> dict:
    """The stored payload, re-addressed to its dated URL. Pure."""
    d = dict(payload or {})
    urls = _urls(month)
    label = _month_label(month)
    d["snapshot"] = {
        "month": month,
        "month_label": label,
        "captured_at": captured_at,
        "capture_basis": basis,
        "as_of_note": _AS_OF_NOTE,
        "immutable": True,
        "url": urls["html"],
        "json_url": urls["json"],
        "live_url": f"{_BASE}/state-of-power",
        "index_url": f"{_BASE}/api/v1/reports/state-of-power/months",
    }
    d["stable_url"] = urls["html"]
    d["refresh"] = ("Frozen monthly snapshot: stored once, never recomputed. "
                    "The live report is at " + f"{_BASE}/state-of-power.")
    year = month[:4]
    cap_day = (captured_at or "")[:10]
    d["citation"] = {
        "stable_url": urls["html"],
        "methodology_url": (payload.get("citation") or {}).get("methodology_url")
        or payload.get("methodology_url"),
        "apa": (f"DC Hub. ({year}). The State of Data Center Power — {label} "
                f"(snapshot captured {cap_day}). {urls['html']}. Licensed CC-BY-4.0."),
        "license": "CC-BY-4.0",
    }
    return d


def _read(month: str):
    """(captured_at_iso, basis, payload) for a stored month, or None. Raises
    only when the database is unreachable, so a 503 is never shown as a 404."""
    c = _conn()
    if c is None:
        raise RuntimeError("no_database")
    try:
        _ensure_table(c)
        with c.cursor() as cur:
            cur.execute(f"SELECT captured_at, capture_basis, payload FROM {_TABLE} "
                        "WHERE month = %s", (month,))
            r = cur.fetchone()
    finally:
        try:
            c.close()
        except Exception:
            pass
    if not r:
        return None
    cap, basis, payload = r
    if isinstance(payload, str):
        payload = json.loads(payload)
    cap_s = cap.isoformat() if hasattr(cap, "isoformat") else str(cap)
    return cap_s, basis, payload


def _not_captured(month: str):
    closed = month < _this_month()
    return jsonify({
        "ok": False,
        "error": "month_not_captured",
        "month": month,
        "detail": ("No snapshot was stored for this month." if closed else
                   "This month has not closed yet; its snapshot is captured on the "
                   "1st of next month. The live report is at /state-of-power."),
        "live_url": f"{_BASE}/state-of-power",
        "index_url": f"{_BASE}/api/v1/reports/state-of-power/months",
    }), 404, {"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=300"}


def _unavailable():
    return jsonify({"ok": False, "error": "snapshot_store_unavailable"}), 503, \
        {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}


def _month_or_none(year: int, month: int):
    s = f"{year:04d}-{month:02d}"
    return s if _MONTH_RE.match(s) else None


# ── JSON ─────────────────────────────────────────────────────────────────
@state_of_power_monthly_bp.route(
    "/api/v1/reports/state-of-power/<int(fixed_digits=4):year>-<int(fixed_digits=2):month>",
    methods=["GET"], strict_slashes=False)
def snapshot_json(year, month):
    m = _month_or_none(year, month)
    if not m:
        return _not_captured(f"{year:04d}-{month:02d}")
    try:
        row = _read(m)
    except Exception:
        return _unavailable()
    if not row:
        return _not_captured(m)
    cap, basis, payload = row
    from routes.state_of_power import _CC_LINK_HEADER
    return jsonify(_dated(payload, m, cap, basis)), 200, {
        "Cache-Control": _IMMUTABLE_CACHE, "Link": _CC_LINK_HEADER,
        "X-License": "CC-BY-4.0", "Access-Control-Allow-Origin": "*"}


@state_of_power_monthly_bp.route("/api/v1/reports/state-of-power/months",
                                 methods=["GET"], strict_slashes=False)
def snapshot_index():
    c = _conn()
    if c is None:
        return _unavailable()
    try:
        _ensure_table(c)
        with c.cursor() as cur:
            cur.execute(f"SELECT month, captured_at, capture_basis FROM {_TABLE} "
                        "ORDER BY month DESC")
            rows = cur.fetchall() or []
    except Exception:
        return _unavailable()
    finally:
        try:
            c.close()
        except Exception:
            pass
    months = []
    for month, cap, basis in rows:
        months.append({
            "month": month,
            "month_label": _month_label(month),
            "captured_at": cap.isoformat() if hasattr(cap, "isoformat") else str(cap),
            "capture_basis": basis,
            **_urls(month),
        })
    return jsonify({
        "report": "The State of Data Center Power — monthly snapshots",
        "as_of_note": _AS_OF_NOTE,
        "live_url": f"{_BASE}/state-of-power",
        "count": len(months),
        "months": months,
    }), 200, {"Cache-Control": "public, max-age=300",
              "Access-Control-Allow-Origin": "*"}


# ── HTML ─────────────────────────────────────────────────────────────────
def _render_snapshot_html(d: dict) -> str:
    """The live report's renderer over the stored payload, re-pointed at the
    dated URL and headed by the as-of banner. Pure."""
    from routes.state_of_power import _render_html, STABLE_URL
    snap = d["snapshot"]
    page = _render_html(d)
    url = _html.escape(snap["url"])
    page = page.replace(f'<link rel="canonical" href="{STABLE_URL}">',
                        f'<link rel="canonical" href="{url}">', 1)
    page = page.replace(f'<meta property="og:url" content="{STABLE_URL}">',
                        f'<meta property="og:url" content="{url}">', 1)
    page = page.replace("<title>The State of Data Center Power",
                        f"<title>The State of Data Center Power — "
                        f"{_html.escape(snap['month_label'])}", 1)
    banner = (
        '<div role="note" style="max-width:1100px;margin:16px auto;padding:12px 16px;'
        'border:1px solid #f59e0b;border-radius:8px;background:rgba(245,158,11,.08);'
        'font-size:14px;line-height:1.5">'
        f"<strong>Monthly snapshot — {_html.escape(snap['month_label'])}.</strong> "
        f"Captured {_html.escape(snap['captured_at'][:16].replace('T', ' '))} UTC "
        f"({_html.escape(snap['capture_basis'])}). {_html.escape(_AS_OF_NOTE)} "
        f'Permanent URL: <a href="{url}">{url}</a> · '
        f'<a href="{_html.escape(snap["json_url"])}">JSON</a> · '
        f'<a href="{_html.escape(snap["live_url"])}">live report</a> · '
        f'<a href="{_html.escape(snap["index_url"])}">all months</a></div>')
    return page.replace("<body>", "<body>" + banner, 1)


@state_of_power_monthly_bp.route(
    "/state-of-power/<int(fixed_digits=4):year>-<int(fixed_digits=2):month>",
    methods=["GET"], strict_slashes=False)
def snapshot_html(year, month):
    m = _month_or_none(year, month)
    if not m:
        return _not_captured(f"{year:04d}-{month:02d}")
    try:
        row = _read(m)
    except Exception:
        return _unavailable()
    if not row:
        return _not_captured(m)
    cap, basis, payload = row
    return Response(_render_snapshot_html(_dated(payload, m, cap, basis)),
                    mimetype="text/html",
                    headers={"Cache-Control": _IMMUTABLE_CACHE, "X-License": "CC-BY-4.0"})


# ── Capture (admin) ──────────────────────────────────────────────────────
def capture(month: str, *, today: _dt.date | None = None):
    """Store `month` from the live report if it is not stored yet.
    Returns (status, body). Never overwrites a stored month."""
    if not _MONTH_RE.match(month or ""):
        return 400, {"ok": False, "error": "bad_month", "expected": "YYYY-MM"}
    now_month = _this_month(today)
    if month > now_month:
        return 400, {"ok": False, "error": "future_month", "month": month}
    basis = "mid_month" if month == now_month else "month_close"

    from routes.state_of_power import _gather
    payload = _gather()
    c = _conn()
    if c is None:
        return 503, {"ok": False, "error": "no_database"}
    try:
        _ensure_table(c)
        with c.cursor() as cur:
            cur.execute(
                f"INSERT INTO {_TABLE} (month, capture_basis, payload) "
                "VALUES (%s, %s, %s::jsonb) ON CONFLICT (month) DO NOTHING "
                "RETURNING captured_at",
                (month, basis, json.dumps(payload, default=str)))
            r = cur.fetchone()
    finally:
        try:
            c.close()
        except Exception:
            pass
    urls = _urls(month)
    if not r:
        return 409, {"ok": False, "error": "already_captured", "month": month,
                     "detail": "A stored month is never overwritten.", **urls}
    cap = r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0])
    return 201, {"ok": True, "month": month, "captured_at": cap,
                 "capture_basis": basis, **urls}


@state_of_power_monthly_bp.route("/api/v1/reports/state-of-power/snapshot",
                                 methods=["POST"])
def snapshot_capture():
    from internal_auth import require_internal_or_admin
    if not require_internal_or_admin(request):
        return jsonify(ok=False, error="forbidden"), 403
    month = (request.args.get("month") or "").strip() or _previous_month()
    status, body = capture(month)
    return jsonify(body), status, {"Cache-Control": "no-store"}
