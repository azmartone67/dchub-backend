"""
changelog_landing.py — public /changelog page auto-built from press releases.

Phase ZZZZZ-round47.3 (2026-05-25). Pages worker advertised /changelog
in nav + OG metadata but no backend route existed → 404. Rather than
maintain a manual changelog, this page pulls the daily press_releases
table (7 releases / 7 days cadence — see /press-release/*) and renders
them as a single-page timeline with anchors.

Bonus: gives reviewers/buyers a tight one-glance signal of platform
velocity ("they ship something newsworthy every day").
"""
import datetime
import os
from contextlib import contextmanager
from flask import Blueprint

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

changelog_bp = Blueprint("changelog_landing", __name__)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


def _fetch_releases(limit=60):
    if not (_pg and _dsn()):
        return []
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT slug, title, created_at, summary
                FROM press_releases
                ORDER BY created_at DESC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()
    except Exception:
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute("""
                    SELECT slug, title, created_at, NULL
                    FROM press_releases
                    ORDER BY created_at DESC
                    LIMIT %s
                """, (limit,))
                return cur.fetchall()
        except Exception:
            return []


def _render(releases):
    by_month = {}
    for slug, title, created_at, summary in releases:
        month_key = created_at.strftime("%Y-%m")
        by_month.setdefault(month_key, []).append((slug, title, created_at, summary))

    sections = []
    for month_key in sorted(by_month.keys(), reverse=True):
        month_label = datetime.datetime.strptime(month_key, "%Y-%m").strftime("%B %Y")
        rows = []
        for slug, title, ts, summary in by_month[month_key]:
            date_label = ts.strftime("%b %d")
            summary_html = ""
            if summary:
                summary_html = f'<p style="margin:.3em 0 .6em 0;color:#475569;font-size:.92rem">{summary[:280]}</p>'
            rows.append(f"""
        <li>
          <div class="date">{date_label}</div>
          <div class="card">
            <a href="/press-release/{slug}"><b>{title}</b></a>
            {summary_html}
          </div>
        </li>""")
        sections.append(f"""
    <h2 id="{month_key}">{month_label}</h2>
    <ul class="timeline">{"".join(rows)}
    </ul>""")

    return "".join(sections) or "<p>No releases yet.</p>"


@changelog_bp.route("/changelog", methods=["GET"], strict_slashes=False)
def changelog():
    releases = _fetch_releases(60)
    sections_html = _render(releases)
    today = datetime.datetime.utcnow().strftime("%B %d, %Y")
    count = len(releases)

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Changelog — DC Hub</title>
<meta name="description" content="DC Hub platform changelog — daily press-release-driven timeline of platform updates, DCPI movers, $1B+ hyperscaler deals, and intelligence drops.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="https://dchub.cloud/changelog">
<meta property="og:title" content="DC Hub Changelog — daily intelligence drops">
<meta property="og:description" content="Every newsworthy data-center event we surface, indexed by month. Daily cadence.">
<style>
 body{{max-width:880px;margin:0 auto;padding:32px 24px;font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.55;color:#0f172a}}
 h1{{font-size:2.1rem;margin:.3em 0;letter-spacing:-.02em}}
 h2{{font-size:1.15rem;margin:1.8em 0 .6em;color:#1e293b;border-bottom:1px solid #e2e8f0;padding-bottom:6px}}
 .eyebrow{{color:#6366f1;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;font-weight:600}}
 .lead{{color:#475569;font-size:1.05rem;max-width:780px}}
 .timeline{{list-style:none;padding:0;margin:0}}
 .timeline li{{display:flex;gap:18px;margin-bottom:14px;align-items:flex-start}}
 .timeline .date{{flex:0 0 70px;color:#64748b;font-size:.85rem;font-weight:600;font-family:ui-monospace,monospace;padding-top:14px}}
 .timeline .card{{flex:1;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:14px 18px;font-size:.96rem}}
 .timeline .card a{{color:#0f172a;text-decoration:none}}
 .timeline .card a:hover{{color:#6366f1}}
 .footer{{color:#64748b;font-size:.85rem;margin-top:30px;padding-top:18px;border-top:1px solid #e2e8f0}}
 .footer a{{color:#6366f1;text-decoration:none}}
</style></head><body>
<div class="eyebrow">DC Hub · Changelog</div>
<h1>Platform changelog</h1>
<p class="lead">Auto-built from the daily press cadence. {count} releases tracked. Each entry links
to the full release page with the underlying data citation.</p>

{sections_html}

<p class="footer">
<a href="/">Home</a> · <a href="/dcpi">DCPI</a> · <a href="/architecture">Architecture</a>
· <a href="/transparency">Live ops</a> · <a href="/news">News</a> · Updated {today}
</p>
</body></html>"""
    return html, 200, {
        "Content-Type":  "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=900, s-maxage=3600",
        "X-DC-Phase":    "ZZZZZ-round47.3-changelog",
    }
