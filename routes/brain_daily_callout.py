"""
routes/brain_daily_callout.py — the brain's DAILY MORNING CALLOUT email.

WHY (2026-07-18): DC Hub Media's PUBLIC /press page sat frozen at 2026-06-22
for ~26 days while press_releases gained 1-2 rows EVERY day — and no detector
said a word, because every press monitor measured the DB end of the pipe:
brain_consistency_radar.check_dchub_media_press_silent reads
auto_press_releases/press_releases (always fresh), check_press_stale_vs_
citations parses response keys the citations endpoint stopped emitting
(observations/recent vs the live `history`), and press_publisher_restart's
status queried a `status` column press_releases doesn't have (pinned 999h).
The operator found the stall by LOOKING. This module is the standing fix for
the visibility half: every morning, one short, honest email that names what
is stuck and WHICH actuator un-sticks it.

Sections (each line names the actuator):
  1. Silent pipelines — cadence_sentinel LANES probed READ-ONLY (no findings
                        write) + the public-press-surface check (edge page
                        date vs DB date — the exact blindness above)
  2. Chronic top-5    — open episodes by chronic_score (reuses
                        routes/episode_analytics._SQL_CHRONIC_LEADERBOARD)
  3. 24h flow         — findings opened / resolved / open now
  4. Human-gated      — open GitHub items labeled needs-human-merge + the
                        '[white-glove] listing copy drift' rolling issue

Guards (send path, in order): BRAIN_DAILY_CALLOUT_ENABLED != 0 →
BRAIN_DIGEST_EMAIL / ADMIN_ALERT_EMAIL set → provider key present → not
already sent today (brain_daily_callout_log claim row; ?force=1 overrides).
KILL SWITCH: BRAIN_DAILY_CALLOUT_ENABLED=0. Default ON — this ships LIVE;
the innovation email's ship-dark default is exactly how a visibility gap
stays dark for a month.

Endpoints (X-Admin-Key = DCHUB_ADMIN_KEY / ADMIN_KEY, gate CLOSED when
neither env is set):
  GET  /api/v1/admin/brain/daily-callout        JSON (?format=html preview)
  POST /api/v1/admin/brain/daily-callout/send   send (?dry=1 | ?force=1)

Wired: cron_heartbeat _DISPATCH, hours 11-13 UTC (7-9 AM ET) with a wide
minute window (the GH-Actions heartbeat lands ~hourly at random minutes);
the per-day claim row makes re-fires idempotent.
"""
from __future__ import annotations

import datetime
import hmac
import logging
import os
import re

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

brain_daily_callout_bp = Blueprint("brain_daily_callout", __name__)

_DASH = "https://dchub.cloud/admin/cadence-sentinel"


# ── gates ─────────────────────────────────────────────────────────────

def _admin_ok() -> bool:
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("ADMIN_KEY") or "").strip()
    if not expected:
        return False
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return bool(sent) and hmac.compare_digest(sent, expected)


def _enabled() -> bool:
    """Kill switch. DEFAULT ON — set BRAIN_DAILY_CALLOUT_ENABLED=0 to kill."""
    return str(os.environ.get("BRAIN_DAILY_CALLOUT_ENABLED", "1")).lower() \
        not in ("0", "false", "no", "off")


def _recipient() -> str:
    return (os.environ.get("BRAIN_DIGEST_EMAIL")
            or os.environ.get("ADMIN_ALERT_EMAIL") or "").strip()


def _has_provider_key() -> bool:
    return bool((os.environ.get("RESEND_API_KEY") or "").strip()
                or (os.environ.get("DCHUB_RESEND_API_KEY") or "").strip()
                or (os.environ.get("SENDGRID_API_KEY") or "").strip())


# ── public press surface check ────────────────────────────────────────
# The June→July stall: DB fresh daily, dchub.cloud/press frozen at 06-22.
# Nothing measured the EDGE. This does — newest ISO date visible in the
# public HTML vs newest press_releases row. Shared with the radar detector
# (brain_consistency_radar.check_press_public_surface_stale imports it).

PUBLIC_PRESS_PAGES = ("https://dchub.cloud/press",
                      "https://dchub.cloud/dc-hub-media/")
_ISO_DATE_RE = re.compile(r"20\d{2}-[01]\d-[0-3]\d")
SURFACE_LAG_DAYS = float(os.environ.get("BRAIN_CALLOUT_SURFACE_LAG_DAYS", "3"))


def newest_date_in_html(html: str) -> datetime.date | None:
    """Max plausible ISO date in the page. Bogus/far-future strings are
    skipped — one junk match must not mark a frozen page fresh."""
    newest = None
    ceiling = datetime.date.today() + datetime.timedelta(days=2)
    for m in _ISO_DATE_RE.findall(html or ""):
        try:
            d = datetime.date.fromisoformat(m)
        except ValueError:
            continue
        if d.year < 2020 or d > ceiling:
            continue
        if newest is None or d > newest:
            newest = d
    return newest


def _fetch_page(url: str) -> str | None:
    try:
        import requests
        r = requests.get(url, timeout=10, allow_redirects=True,
                         headers={"User-Agent": "dchub-brain-callout/1.0"})
        return r.text if r.status_code == 200 else None
    except Exception:
        return None


def press_surface_report(fetch=_fetch_page) -> dict:
    """{db_newest, pages: [{url, newest_visible, lag_days, stale}], error}.
    lag_days = how far the public page trails the DB. fetch is injectable
    for tests. Fail-soft: an unreachable page reports error, never raises."""
    out = {"db_newest": None, "pages": [], "error": None}
    try:
        from routes.episode_analytics import _read_db, _rows
        with _read_db() as conn:
            rows = _rows(conn, "SELECT MAX(created_at)::date FROM press_releases")
        if rows and rows[0] and rows[0][0]:
            out["db_newest"] = rows[0][0]
    except Exception as e:
        out["error"] = f"db: {str(e)[:120]}"
    for url in PUBLIC_PRESS_PAGES:
        html = fetch(url)
        if html is None:
            out["pages"].append({"url": url, "newest_visible": None,
                                 "lag_days": None, "stale": False,
                                 "error": "unreachable"})
            continue
        visible = newest_date_in_html(html)
        lag = None
        if visible is not None and out["db_newest"] is not None:
            lag = (out["db_newest"] - visible).days
        out["pages"].append({
            "url": url,
            "newest_visible": visible.isoformat() if visible else None,
            "lag_days": lag,
            # A page with NO parseable date while the DB has releases is
            # stale-by-definition, not unknown.
            "stale": bool((lag is not None and lag > SURFACE_LAG_DAYS)
                          or (visible is None and out["db_newest"] is not None)),
            "error": None,
        })
    return out


# ── silent pipelines (cadence lanes, probed read-only) ────────────────

# lane key → the actuator the operator (or autopilot) reaches for.
LANE_ACTUATORS = {
    "press_generation":  "POST /api/v1/press-publisher/run?force=1",
    "twitter_publish":   "X publisher: TWITTER_PUBLISHER_ENABLED + "
                         "social_media_posts approved queue",
    "linkedin_publish":  "POST /api/v1/linkedin-quad/run",
    "bluesky_publish":   "bluesky publisher cron (social_media_posts "
                         "approved queue)",
    "weekly_digest_send": "POST /api/v1/admin/brain/strategic-digest/send",
}


def silent_pipelines() -> dict:
    """Probe every cadence_sentinel lane READ-ONLY (no findings write) and
    return only the ones that need a human sentence: stalled or unknown."""
    out = {"stalled": [], "unknown": [], "lanes_checked": 0, "error": None}
    c = None
    try:
        from routes import cadence_sentinel as cs
        now = datetime.datetime.now(datetime.timezone.utc)
        c = cs._conn()
        for spec in cs.LANES:
            probes = cs._probe_lane(c, spec, now)
            verdict = cs.evaluate_lane(spec, **probes)
            out["lanes_checked"] += 1
            entry = {
                "key": spec["key"],
                "label": spec.get("label") or spec["key"],
                "age_hours": verdict.get("age_hours"),
                "reasons": verdict.get("reasons") or [],
                "actuator": LANE_ACTUATORS.get(
                    spec["key"], f"{_DASH}#{spec['key']}"),
            }
            if verdict.get("stalled"):
                out["stalled"].append(entry)
            elif verdict.get("unknown"):
                out["unknown"].append(entry)
    except Exception as e:
        out["error"] = str(e)[:140]
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
    return out


# ── chronic episodes + 24h flow ───────────────────────────────────────

def chronic_top5() -> list | None:
    """Top-5 open episodes by chronic_score — the leaderboard the operator
    should work FIRST. Reuses episode_analytics' SQL verbatim (LIMIT 15
    there; sliced to 5 here). None = query broke (render as unavailable)."""
    try:
        from routes.episode_analytics import (_read_db, _rows, _r1,
                                              _SQL_CHRONIC_LEADERBOARD)
        with _read_db() as conn:
            rows = _rows(conn, _SQL_CHRONIC_LEADERBOARD)
        if rows is None:
            return None
        out = []
        for r in rows[:5]:
            if len(r) < 8:
                continue
            out.append({"issue": r[0], "url": r[1], "detector": r[2],
                        "hours_open": _r1(r[3]), "reobs_per_day": _r1(r[6]),
                        "chronic_score": _r1(r[7])})
        return out
    except Exception as e:
        logger.debug("[callout] chronic query failed: %s", e)
        return None


def flow_24h() -> dict | None:
    try:
        from routes.episode_analytics import _read_db, _rows
        with _read_db() as conn:
            rows = _rows(conn, """
                SELECT COUNT(*) FILTER (WHERE created_at  >= NOW() - INTERVAL '24 hours'),
                       COUNT(*) FILTER (WHERE resolved_at >= NOW() - INTERVAL '24 hours'),
                       COUNT(*) FILTER (WHERE status = 'open')
                FROM brain_findings
            """)
        if not rows or not rows[0]:
            return None
        return {"opened_24h": int(rows[0][0] or 0),
                "resolved_24h": int(rows[0][1] or 0),
                "open_now": int(rows[0][2] or 0)}
    except Exception:
        return None


# ── human-gated queue (GitHub) ────────────────────────────────────────

_WG_DRIFT_TITLE = "[white-glove] listing copy drift"


def human_gated() -> dict:
    """Open needs-human-merge items (issues AND draft PRs — the /issues
    API returns both; both wait on the same human) + the rolling
    white-glove copy-drift issue. Fail-soft: no token → says so."""
    out = {"needs_human_merge": [], "drift_issue": None, "error": None}
    tok = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if not tok:
        out["error"] = "no_github_token"
        return out
    repo = (os.environ.get("GITHUB_REPO") or "azmartone67/dchub-backend").strip()
    headers = {"Authorization": f"Bearer {tok}",
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    try:
        import requests
        r = requests.get(f"https://api.github.com/repos/{repo}/issues",
                         headers=headers,
                         params={"state": "open", "labels": "needs-human-merge",
                                 "per_page": 10, "sort": "created",
                                 "direction": "asc"},
                         timeout=12)
        if r.status_code == 200:
            for it in (r.json() or []):
                age_d = None
                try:
                    created = datetime.datetime.fromisoformat(
                        str(it.get("created_at", "")).replace("Z", "+00:00"))
                    age_d = round((datetime.datetime.now(datetime.timezone.utc)
                                   - created).total_seconds() / 86400.0, 1)
                except Exception:
                    pass
                out["needs_human_merge"].append({
                    "number": it.get("number"),
                    "title": (it.get("title") or "")[:110],
                    "kind": "pr" if "pull_request" in it else "issue",
                    "age_days": age_d,
                    "url": it.get("html_url"),
                })
        else:
            out["error"] = f"github_{r.status_code}"
        r2 = requests.get("https://api.github.com/search/issues",
                          headers=headers,
                          params={"q": f'repo:{repo} is:issue is:open '
                                       f'in:title "{_WG_DRIFT_TITLE}"'},
                          timeout=12)
        if r2.status_code == 200:
            for it in (r2.json() or {}).get("items", []):
                if (it.get("title") or "").strip() == _WG_DRIFT_TITLE:
                    out["drift_issue"] = {"number": it.get("number"),
                                          "title": _WG_DRIFT_TITLE,
                                          "url": it.get("html_url")}
                    break
    except Exception as e:
        out["error"] = str(e)[:140]
    return out


# ── compose + render ──────────────────────────────────────────────────

def compose_daily_callout() -> dict:
    """Every section fail-soft: a broken source degrades its section to
    None/error, never 500s the digest (the whole point is that it SENDS)."""
    surface = press_surface_report()
    lanes = silent_pipelines()
    stale_pages = [p for p in surface.get("pages", []) if p.get("stale")]
    digest = {
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
        "surface": surface,
        "stale_pages": stale_pages,
        "lanes": lanes,
        "chronic": chronic_top5(),
        "flow": flow_24h(),
        "human": human_gated(),
    }
    n_silent = len(stale_pages) + len(lanes.get("stalled") or [])
    flow = digest["flow"] or {}
    digest["subject"] = (
        f"[DC Hub brain] daily callout — "
        f"{n_silent} silent pipeline{'s' if n_silent != 1 else ''}, "
        f"{flow.get('open_now', '?')} open findings")
    return digest


def _fmt_age(h) -> str:
    if h is None:
        return "?"
    try:
        h = float(h)
    except Exception:
        return "?"
    return f"{h / 24.0:.1f}d" if h >= 48 else f"{h:.0f}h"


def render_text(d: dict) -> str:
    L = [f"DC Hub brain — daily callout ({d['generated_at'][:10]})", ""]

    stale = d.get("stale_pages") or []
    lanes = d.get("lanes") or {}
    stalled = lanes.get("stalled") or []
    L.append(f"SILENT PIPELINES ({len(stale) + len(stalled)})")
    if not stale and not stalled:
        L.append(f"• none — {lanes.get('lanes_checked', 0)} lanes + "
                 f"{len((d.get('surface') or {}).get('pages') or [])} public "
                 f"pages checked")
    surf = d.get("surface") or {}
    for p in stale:
        L.append(f"• public page {p['url']}: newest visible "
                 f"{p.get('newest_visible') or 'NO DATE'}, DB newest "
                 f"{surf.get('db_newest')} "
                 f"({p.get('lag_days', '?')}d behind) → static press bake: "
                 f"the generator is fine — in dchub-frontend run "
                 f"`gh workflow run press-rss.yml` (bakes releases into the "
                 f"page HTML + pushes → Pages deploy purges the URL); still "
                 f"stale after a green run → `gh workflow run cf-purge.yml "
                 f"-f urls={p['url']}`, then re-check {p['url']}")
    for e in stalled:
        why = "; ".join(e.get("reasons") or []) or "stalled"
        L.append(f"• {e['label']}: {why} → {e['actuator']}")
    for e in (lanes.get("unknown") or []):
        L.append(f"• {e['label']}: probe UNKNOWN (broken monitor is a bug "
                 f"too) → {e['actuator']}")
    if lanes.get("error"):
        L.append(f"• lane probe error: {lanes['error']}")
    L.append("")

    L.append("CHRONIC OPEN EPISODES (top 5 by hours-open × reobs/day)")
    chronic = d.get("chronic")
    if chronic is None:
        L.append("• unavailable (query failed)")
    elif not chronic:
        L.append("• none open")
    else:
        for c in chronic:
            L.append(f"• {c['issue'][:90]} — open {_fmt_age(c['hours_open'])}, "
                     f"{c.get('reobs_per_day') or 0}/day re-obs "
                     f"({c.get('detector') or '?'}) → {c.get('url') or _DASH}")
    L.append("")

    flow = d.get("flow")
    if flow:
        L.append(f"LAST 24H — opened {flow['opened_24h']} · resolved "
                 f"{flow['resolved_24h']} · open now {flow['open_now']}")
    else:
        L.append("LAST 24H — unavailable")
    L.append("")

    h = d.get("human") or {}
    items = h.get("needs_human_merge") or []
    drift = h.get("drift_issue")
    L.append(f"WAITING ON A HUMAN ({len(items) + (1 if drift else 0)})")
    if not items and not drift:
        L.append("• queue empty" + (f" ({h['error']})" if h.get("error") else ""))
    for it in items:
        L.append(f"• {it['kind']} #{it['number']} ({it.get('age_days', '?')}d "
                 f"old): {it['title']} → {it['url']}")
    if drift:
        L.append(f"• issue #{drift['number']}: {drift['title']} → "
                 f"{drift['url']}")
    L.append("")
    L.append(f"— lanes: {_DASH} · kill switch: BRAIN_DAILY_CALLOUT_ENABLED=0")
    return "\n".join(L)


def render_html(d: dict) -> str:
    """Minimal HTML: the text rendering in a <pre> plus a heading. This
    email's value is honesty and scannability, not design."""
    from html import escape
    return (
        '<div style="font-family:ui-monospace,Menlo,monospace;font-size:13px;'
        'color:#111;max-width:720px">'
        f"<pre style='white-space:pre-wrap'>{escape(render_text(d))}</pre>"
        "</div>")


# ── per-day claim (idempotent under heartbeat re-fires) ──────────────
# DIRECT psycopg2 to the primary, NOT db_utils: live-verified 2026-07-19
# that db_utils' PGCursorWrapper (a) is not a context manager and
# (b) SILENTLY DROPS DDL (`if _is_ddl(sql): return self`), so the
# CREATE TABLE never ran and the dedupe fail-opened. Mirrors
# cadence_sentinel's separate-write-connection doctrine.

def _write_conn():
    import psycopg2
    url = (os.environ.get("DATABASE_URL")
           or os.environ.get("NEON_DATABASE_URL") or "")
    if not url:
        return None
    c = psycopg2.connect(url, connect_timeout=8)
    c.autocommit = True
    return c


def _claim_today(force: bool = False):
    """INSERT today's claim row; False = already sent today. Returns
    (claimed, err). force skips the dedupe but still records the send."""
    try:
        conn = _write_conn()
        if conn is None:
            return True, "no_database_url"
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_daily_callout_log (
                    day      date PRIMARY KEY,
                    sent_at  timestamptz NOT NULL DEFAULT now(),
                    to_email text,
                    subject  text)
            """)
            cur.execute("""
                INSERT INTO brain_daily_callout_log (day)
                VALUES (CURRENT_DATE)
                ON CONFLICT (day) DO NOTHING
            """)
            claimed = cur.rowcount > 0
            return (True if force else claimed), None
        finally:
            conn.close()
    except Exception as e:
        # No DB → send anyway (a lost dedupe beats a lost callout).
        return True, str(e)[:120]


def _stamp_sent(to_email: str, subject: str) -> None:
    try:
        conn = _write_conn()
        if conn is None:
            return
        try:
            conn.cursor().execute("""
                UPDATE brain_daily_callout_log
                   SET sent_at = now(), to_email = %s, subject = %s
                 WHERE day = CURRENT_DATE
            """, (to_email, subject))
        finally:
            conn.close()
    except Exception:
        pass


def _release_claim() -> None:
    """Send failed → drop today's claim so a later heartbeat retries."""
    try:
        conn = _write_conn()
        if conn is None:
            return
        try:
            conn.cursor().execute(
                "DELETE FROM brain_daily_callout_log "
                "WHERE day = CURRENT_DATE AND to_email IS NULL")
        finally:
            conn.close()
    except Exception:
        pass


# ── send ──────────────────────────────────────────────────────────────

def send_daily_callout(force: bool = False) -> dict:
    if not _enabled():
        return {"sent": False, "skipped": "disabled"}
    to = _recipient()
    if not to:
        return {"sent": False, "skipped": "no_recipient"}
    if not _has_provider_key():
        return {"sent": False, "skipped": "no_provider", "to": to}
    claimed, claim_err = _claim_today(force=force)
    if not claimed:
        return {"sent": False, "skipped": "already_sent_today"}

    d = compose_daily_callout()
    try:
        from email_fallback import send_email_resilient
        ok = send_email_resilient(
            to, d["subject"],
            html_content=render_html(d),
            text_content=render_text(d),
            from_email=os.environ.get("SENDGRID_FROM_EMAIL", "alerts@dchub.cloud"),
            from_name="DC Hub Brain",
        )
    except Exception as e:
        logger.warning("send_daily_callout: sender raised: %s", e)
        _release_claim()
        return {"sent": False, "error": str(e)[:160], "to": to}
    if ok:
        _stamp_sent(to, d["subject"])
        return {"sent": True, "to": to, "subject": d["subject"],
                "claim_note": claim_err}
    _release_claim()
    return {"sent": False, "error": "send_failed", "to": to}


# ── endpoints ─────────────────────────────────────────────────────────

@brain_daily_callout_bp.route("/api/v1/admin/brain/daily-callout",
                              methods=["GET"])
def daily_callout_view():
    if not _admin_ok():
        return jsonify(ok=False, error="admin only",
                       hint="X-Admin-Key or ?admin_key="), 403
    d = compose_daily_callout()
    if (request.args.get("format") or "").lower() == "html":
        return Response(render_html(d), mimetype="text/html")
    d["text"] = render_text(d)
    return jsonify(ok=True, **d), 200


@brain_daily_callout_bp.route("/api/v1/admin/brain/daily-callout/send",
                              methods=["POST"])
def daily_callout_send():
    """The cron target. ?dry=1 composes + returns text without sending;
    ?force=1 bypasses the per-day dedupe."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only",
                       hint="X-Admin-Key or ?admin_key="), 403
    if str(request.args.get("dry", "")).lower() in ("1", "true", "yes"):
        d = compose_daily_callout()
        return jsonify(ok=True, dry=True, sent=False, subject=d["subject"],
                       text=render_text(d)), 200
    force = str(request.args.get("force", "")).lower() in ("1", "true", "yes")
    return jsonify(ok=True, **send_daily_callout(force=force)), 200


def register_brain_daily_callout(app) -> None:
    try:
        if "brain_daily_callout" not in app.blueprints:
            app.register_blueprint(brain_daily_callout_bp)
    except Exception as e:
        logger.warning("brain_daily_callout register skipped: %s", e)
