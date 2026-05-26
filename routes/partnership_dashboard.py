"""
partnership_dashboard.py — unified campaign view per track.

Phase ZZZZZ-round47.21 (2026-05-26). After landing 4 partnership
channels (press / LinkedIn / email / clicks), the operator needs ONE
view that ties them together: "for the cbre track, did press land,
did LinkedIn post, did we email anyone, how many clicks?".

Endpoints:
  GET /api/v1/partnerships/dashboard.json     all tracks, all channels
  GET /partnerships/dashboard                 HTML view
"""
import os
import datetime
from contextlib import contextmanager
from flask import Blueprint, jsonify, Response

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

from routes.linkedin_partnership_weekly import _TRACKS as _LINKEDIN_TRACKS

partnership_dashboard_bp = Blueprint("partnership_dashboard", __name__)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try: yield c
    finally: c.close()


def _gather():
    """Pull per-track status across 4 tables: linkedin_partnership_posts,
    press_releases (category=partnership), partnership_emails_sent,
    partnership_clicks."""
    by_track = {t["slug"]: {
        "slug":           t["slug"],
        "headline":       t["headline"],
        "url":            t["url"],
        "linkedin":       {"posted_at": None, "linkedin_urn": None, "success": None, "iso_week": None},
        "press":          {"slug": None, "title": None, "created_at": None},
        "emails_sent":    0,
        "emails_recent":  [],
        "clicks_7d":      0,
        "clicks_30d":     0,
        "clicks_total":   0,
    } for t in _LINKEDIN_TRACKS}

    if not (_pg and _dsn()):
        return list(by_track.values())

    try:
        with _conn() as c:
            # LinkedIn per track — most recent
            with c.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT ON (track_slug) track_slug, posted_at,
                           linkedin_urn, success, iso_year, iso_week
                      FROM linkedin_partnership_posts
                     ORDER BY track_slug, posted_at DESC
                """)
                for r in cur.fetchall():
                    if r[0] in by_track:
                        by_track[r[0]]["linkedin"] = {
                            "posted_at": r[1].isoformat() if r[1] else None,
                            "linkedin_urn": r[2],
                            "success": r[3],
                            "iso_week": f"{r[4]}-W{r[5]:02d}" if r[4] and r[5] else None,
                        }

            # Press releases by category=partnership, parse slug for track
            with c.cursor() as cur:
                cur.execute("""
                    SELECT slug, title, created_at
                      FROM press_releases
                     WHERE category = 'partnership'
                     ORDER BY created_at DESC
                """)
                # slug format: partnership-<track>-YYYY-wNN — keep latest per track
                for r in cur.fetchall():
                    parts = (r[0] or "").split("-")
                    if len(parts) >= 2 and parts[0] == "partnership":
                        track_slug = parts[1]
                        if track_slug in by_track and not by_track[track_slug]["press"]["slug"]:
                            by_track[track_slug]["press"] = {
                                "slug":       r[0],
                                "title":      r[1],
                                "created_at": r[2].isoformat() if r[2] else None,
                                "url":        f"https://dchub.cloud/press-release/{r[0]}",
                            }

            # Emails sent per track
            with c.cursor() as cur:
                cur.execute("""
                    SELECT track_slug, COUNT(*),
                           ARRAY_AGG(to_email ORDER BY sent_at DESC) FILTER
                              (WHERE sent_at > NOW() - INTERVAL '30 days')
                      FROM partnership_emails_sent
                     GROUP BY track_slug
                """)
                for r in cur.fetchall():
                    if r[0] in by_track:
                        by_track[r[0]]["emails_sent"] = int(r[1] or 0)
                        by_track[r[0]]["emails_recent"] = (r[2] or [])[:5]

            # Clicks per track
            with c.cursor() as cur:
                cur.execute("""
                    SELECT track_slug,
                           COUNT(*) FILTER (WHERE clicked_at > NOW() - INTERVAL '7 days'),
                           COUNT(*) FILTER (WHERE clicked_at > NOW() - INTERVAL '30 days'),
                           COUNT(*)
                      FROM partnership_clicks
                     GROUP BY track_slug
                """)
                for r in cur.fetchall():
                    if r[0] in by_track:
                        by_track[r[0]]["clicks_7d"] = int(r[1] or 0)
                        by_track[r[0]]["clicks_30d"] = int(r[2] or 0)
                        by_track[r[0]]["clicks_total"] = int(r[3] or 0)
    except Exception:
        pass

    return list(by_track.values())


@partnership_dashboard_bp.route("/api/v1/partnerships/dashboard.json",
                                 methods=["GET"], strict_slashes=False)
@partnership_dashboard_bp.route("/api/v1/partnerships/dashboard",
                                 methods=["GET"], strict_slashes=False)
def dashboard_json():
    data = _gather()
    return jsonify({
        "computed_at": datetime.datetime.utcnow().isoformat() + "Z",
        "tracks":      data,
        "totals": {
            "press_landed":   sum(1 for t in data if t["press"]["slug"]),
            "linkedin_posted": sum(1 for t in data if t["linkedin"]["posted_at"]),
            "emails_sent":    sum(t["emails_sent"] for t in data),
            "clicks_total":   sum(t["clicks_total"] for t in data),
            "clicks_30d":     sum(t["clicks_30d"] for t in data),
        },
    }), 200, {"Cache-Control": "public, max-age=120"}


def _render_row(t):
    li_ok = "✅" if t["linkedin"]["success"] else ("❌" if t["linkedin"]["success"] is False else "—")
    li_when = (t["linkedin"]["posted_at"] or "")[:16].replace("T", " ") or "—"
    press_link = f'<a href="{t["press"].get("url","#")}">{(t["press"]["title"] or "")[:60]}</a>' if t["press"]["slug"] else '<span style="color:#94a3b8">—</span>'
    return f"""
    <tr>
      <td><b>{t['slug']}</b><br><span style="color:#64748b;font-size:.78rem">{t['url']}</span></td>
      <td>{li_ok} {li_when}<br><span style="color:#64748b;font-size:.78rem">{t['linkedin'].get('iso_week') or '—'}</span></td>
      <td>{press_link}<br><span style="color:#64748b;font-size:.78rem">{(t['press']['created_at'] or '')[:10]}</span></td>
      <td>{t['emails_sent']} sent<br><span style="color:#64748b;font-size:.78rem">{', '.join((t['emails_recent'] or [])[:2])[:60]}</span></td>
      <td><b>{t['clicks_30d']}</b> / {t['clicks_total']}<br><span style="color:#64748b;font-size:.78rem">30d / all-time</span></td>
    </tr>"""


@partnership_dashboard_bp.route("/partnerships/dashboard",
                                 methods=["GET"], strict_slashes=False)
def dashboard_html():
    data = _gather()
    rows = "".join(_render_row(t) for t in data)
    totals = {
        "press_landed":    sum(1 for t in data if t["press"]["slug"]),
        "linkedin_posted": sum(1 for t in data if t["linkedin"]["posted_at"]),
        "emails_sent":     sum(t["emails_sent"] for t in data),
        "clicks_30d":      sum(t["clicks_30d"] for t in data),
    }
    today = datetime.datetime.utcnow().strftime("%B %d, %Y · %H:%M UTC")
    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Partnership Campaign Dashboard — DC Hub</title>
<meta name="robots" content="noindex">
<style>
 body{{max-width:1200px;margin:0 auto;padding:32px 24px;font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.55;color:#0f172a;background:#f8fafc}}
 h1{{font-size:2rem;margin:.3em 0;letter-spacing:-.025em}}
 .eyebrow{{color:#6366f1;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;font-weight:600}}
 .lead{{color:#475569;font-size:1rem;max-width:780px}}
 .stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin:24px 0}}
 .stat{{background:#fff;border:1px solid #e2e8f0;padding:18px;border-radius:10px}}
 .stat-num{{font-size:1.9rem;font-weight:700;color:#6366f1;letter-spacing:-.02em;line-height:1}}
 .stat-label{{color:#64748b;font-size:.78rem;text-transform:uppercase;letter-spacing:.05em;margin-top:6px}}
 table{{width:100%;border-collapse:collapse;margin:18px 0;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.05);font-size:.92rem}}
 th{{background:#0f172a;color:#fff;text-align:left;padding:10px 14px;font-size:.78rem;text-transform:uppercase;letter-spacing:.05em}}
 td{{padding:12px 14px;border-top:1px solid #e2e8f0;vertical-align:top}}
 td a{{color:#6366f1;text-decoration:none}}
 td a:hover{{text-decoration:underline}}
 .footer{{color:#64748b;font-size:.85rem;margin-top:24px;padding-top:14px;border-top:1px solid #e2e8f0}}
</style></head><body>
<div class="eyebrow">DC Hub · Partnership Campaign Dashboard</div>
<h1>Per-track status across all 4 channels</h1>
<p class="lead">Press releases · LinkedIn posts · email outreach · /go/ click attribution.
Refreshed each load. Computed {today}.</p>

<div class="stat-grid">
  <div class="stat"><div class="stat-num">{totals['press_landed']}/7</div><div class="stat-label">Press releases live</div></div>
  <div class="stat"><div class="stat-num">{totals['linkedin_posted']}/7</div><div class="stat-label">LinkedIn posted</div></div>
  <div class="stat"><div class="stat-num">{totals['emails_sent']}</div><div class="stat-label">Emails sent</div></div>
  <div class="stat"><div class="stat-num">{totals['clicks_30d']}</div><div class="stat-label">Clicks (30d)</div></div>
</div>

<table>
 <thead><tr><th>Track</th><th>LinkedIn</th><th>Press release</th><th>Email outreach</th><th>Clicks (30d / all)</th></tr></thead>
 <tbody>{rows}</tbody>
</table>

<p class="footer">
<a href="/api/v1/partnerships/dashboard.json">JSON version</a> ·
<a href="/api/v1/partnerships/clicks/stats">Click stats</a> ·
<a href="/api/v1/cron/last-fired">Cron health</a> ·
<a href="/api/v1/admin/drift-check">Worker drift</a>
</p>
</body></html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "private, max-age=60"})
