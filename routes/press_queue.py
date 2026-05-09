"""Phase 125C — auto-draft press releases on big DCPI moves."""
import os, json, datetime
from flask import Blueprint, jsonify, request, Response
import psycopg2, psycopg2.extras

press_queue_bp = Blueprint("press_queue", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    return psycopg2.connect(db, sslmode="require")


def _ensure():
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS press_releases_queue (
                id SERIAL PRIMARY KEY,
                slug TEXT UNIQUE NOT NULL,
                title TEXT, subheadline TEXT, body TEXT,
                category TEXT DEFAULT 'DCPI',
                trigger_type TEXT,           -- big_move | new_build | new_avoid | weekly
                trigger_data JSONB,
                status TEXT DEFAULT 'draft', -- draft | reviewed | published | rejected
                created_at TIMESTAMPTZ DEFAULT NOW(),
                published_at TIMESTAMPTZ
            )""")
        c.commit()


@press_queue_bp.route("/api/v1/press/queue", methods=["GET"])
def list_queue():
    _ensure()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT id, slug, title, category, trigger_type, status, created_at, published_at
                       FROM press_releases_queue ORDER BY created_at DESC LIMIT 100""")
        rows = cur.fetchall()
    for r in rows:
        for k in ("created_at","published_at"):
            if r.get(k): r[k] = r[k].isoformat()
    return jsonify(queue=rows, count=len(rows)), 200


@press_queue_bp.route("/api/v1/press/scan", methods=["POST", "GET"])
def scan_for_drafts():
    """Phase 129: tiered. >=15 pts auto-publishes, 10-14 stays draft."""
    _ensure()
    AUTO_PUBLISH_THRESHOLD = 15.0  # auto-publish without human review
    DRAFT_THRESHOLD = 10.0         # draft for review
    drafts = []; auto_published = []
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                WITH latest AS (
                  SELECT DISTINCT ON (market_slug) market_slug, market_name, state, iso,
                    excess_power_score AS now_e, constraint_score AS now_c
                  FROM market_power_scores ORDER BY market_slug, computed_at DESC
                ),
                week_ago AS (
                  SELECT DISTINCT ON (market_slug) market_slug,
                    excess_power_score AS prev_e, constraint_score AS prev_c
                  FROM market_power_scores
                  WHERE computed_at < NOW() - INTERVAL '7 days'
                  ORDER BY market_slug, computed_at DESC
                )
                SELECT l.market_slug, l.market_name, l.state, l.iso,
                       l.now_e, w.prev_e, COALESCE(l.now_e - w.prev_e, 0) AS delta_e,
                       l.now_c, w.prev_c, COALESCE(l.now_c - w.prev_c, 0) AS delta_c
                FROM latest l LEFT JOIN week_ago w ON l.market_slug=w.market_slug
                WHERE w.prev_e IS NOT NULL
                  AND (ABS(COALESCE(l.now_e - w.prev_e, 0)) >= %s
                    OR ABS(COALESCE(l.now_c - w.prev_c, 0)) >= %s)
            """, (DRAFT_THRESHOLD, DRAFT_THRESHOLD))
            big_moves = cur.fetchall()
            for m in big_moves:
                slug = f"dcpi-{m['market_slug']}-{datetime.date.today().isoformat()}"
                cur.execute("SELECT 1 FROM press_releases_queue WHERE slug=%s", (slug,))
                if cur.fetchone(): continue

                de, dc = m["delta_e"], m["delta_c"]
                magnitude = max(abs(de), abs(dc))
                will_auto_publish = magnitude >= AUTO_PUBLISH_THRESHOLD

                direction = "rises" if de > 0 else "falls" if de < 0 else "shifts"
                if abs(dc) > abs(de):
                    direction = "tightens" if dc > 0 else "loosens"

                title = f"DCPI {direction.title()}: {m['market_name']} Posts {magnitude:.1f}-Point Weekly Move"
                sub = f"{m['market_name']} ({m['state']}, {m['iso']}) - Excess Power {m['now_e']:.1f} ({de:+.1f} 7d), Constraint {m['now_c']:.1f} ({dc:+.1f} 7d)"
                body = f"""The DC Hub Power Index recorded a notable shift in {m['market_name']} this week.

The Excess Power Score moved from {m['prev_e']:.1f} to {m['now_e']:.1f} - a {de:+.1f}-point change. The Constraint Score moved from {m['prev_c']:.1f} to {m['now_c']:.1f} ({dc:+.1f} 7d).

The DCPI is updated daily from ISO interconnection-queue data, generation pipeline, renewable curtailment, and behind-the-meter capacity signals. Read the full methodology at https://dchub.cloud/dcpi.

Cite as: DC Hub Power Index, {m['market_name']}, {datetime.date.today().isoformat()}, https://dchub.cloud/dcpi/{m['market_slug']}.
"""

                status = 'published' if will_auto_publish else 'draft'
                published_at = "NOW()" if will_auto_publish else "NULL"

                cur.execute(f"""INSERT INTO press_releases_queue
                    (slug, title, subheadline, body, trigger_type, trigger_data, status, published_at)
                    VALUES (%s, %s, %s, %s, 'big_move', %s, %s, {published_at})
                    RETURNING id""",
                    (slug, title, sub, body,
                     json.dumps({"market": m["market_slug"], "delta_e": de, "delta_c": dc, "magnitude": magnitude}),
                     status))
                rid = cur.fetchone()[0]
                rec = {"id": rid, "slug": slug, "title": title, "magnitude": magnitude}
                if will_auto_publish:
                    auto_published.append(rec)
                else:
                    drafts.append(rec)
            c.commit()

        # If anything auto-published, distribute via email to subscribers
        if auto_published:
            try:
                _distribute_published_releases(auto_published)
            except Exception as e:
                print(f"[distribute] error: {e}")
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {str(e)[:200]}",
                       drafts=drafts, auto_published=auto_published), 500
    return jsonify(drafts=drafts, drafts_count=len(drafts),
                   auto_published=auto_published, auto_published_count=len(auto_published),
                   auto_threshold=AUTO_PUBLISH_THRESHOLD,
                   draft_threshold=DRAFT_THRESHOLD), 200


def _distribute_published_releases(releases):
    """Email each just-published release to subscribers + dev-key holders."""
    import os, requests as _rq
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    if not api_key: return
    from_email = os.environ.get("DCHUB_FROM_EMAIL", "DC Hub <jonathan@dchub.cloud>")

    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT DISTINCT email FROM mcp_dev_keys WHERE email IS NOT NULL AND email != ''""")
        keys_emails = [r[0] for r in cur.fetchall()]
        try:
            cur.execute("""SELECT DISTINCT email FROM digest_subscribers WHERE unsubscribed_at IS NULL""")
            subs_emails = [r[0] for r in cur.fetchall()]
        except Exception:
            subs_emails = []
        emails = sorted(set(keys_emails + subs_emails))

    for rec in releases:
        slug = rec["slug"]
        title = rec["title"]
        url = f"https://dchub.cloud/press/{slug}"
        html = f"""<div style="font-family:-apple-system,system-ui;max-width:580px;margin:0 auto;color:#222;line-height:1.6;">
<div style="font-family:'JetBrains Mono',monospace;font-size:0.7rem;color:#6366f1;text-transform:uppercase;letter-spacing:0.12em;margin-bottom:0.4rem;">DC HUB · DCPI ALERT</div>
<h2 style="margin:0 0 0.5rem;">{title}</h2>
<p style="color:#555;">A market in our Data Center Power Index moved enough this week to warrant a release. Magnitude: {rec.get('magnitude',0):.1f} points (auto-published threshold: 15.0).</p>
<p style="margin:1.5rem 0;"><a href="{url}" style="background:linear-gradient(135deg,#6366f1,#a855f7);color:white;padding:0.7rem 1.4rem;border-radius:6px;text-decoration:none;font-weight:700;">Read the release →</a></p>
<p style="color:#888;font-size:0.85rem;margin-top:2rem;">Sent because you have a DC Hub dev key. <a href="https://dchub.cloud/dcpi">Open DCPI dashboard</a></p>
</div>"""
        for em in emails:
            try:
                _rq.post("https://api.resend.com/emails",
                    json={"from": from_email, "to": [em], "subject": title, "html": html},
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json", "Accept": "application/json",
                             "User-Agent": "Mozilla/5.0 (compatible; DCHub/1.0)"},
                    timeout=15)
            except Exception:
                pass



@press_queue_bp.route("/api/v1/press/<slug>", methods=["GET"])
def get_draft(slug):
    _ensure()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM press_releases_queue WHERE slug=%s", (slug,))
        r = cur.fetchone()
    if not r: return jsonify(error="not found"), 404
    for k in ("created_at","published_at"):
        if r.get(k): r[k] = r[k].isoformat()
    return jsonify(r), 200


@press_queue_bp.route("/api/v1/press/<slug>/publish", methods=["POST"])
def publish(slug):
    expected = os.environ.get("DCHUB_ADMIN_KEY")
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    if expected and provided != expected:
        return jsonify(error="unauthorized"), 401
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE press_releases_queue SET status='published', published_at=NOW()
                       WHERE slug=%s RETURNING id""", (slug,))
        r = cur.fetchone(); c.commit()
    if not r: return jsonify(error="not found"), 404
    return jsonify(published=slug, id=r[0]), 200


@press_queue_bp.route("/api/v1/press/feed.json", methods=["GET"])
def press_feed_json():
    _ensure()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT id, slug, title, subheadline, category, status, published_at
                       FROM press_releases_queue WHERE status = 'published'
                       ORDER BY published_at DESC LIMIT 50""")
        rows = cur.fetchall()
    for r in rows:
        if r.get("published_at"): r["published_at"] = r["published_at"].isoformat()
    return jsonify(releases=rows, count=len(rows)), 200


@press_queue_bp.route("/api/v1/press/feed.html", methods=["GET"])
def press_feed_html():
    _ensure()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT slug, title, subheadline, published_at, trigger_type
                       FROM press_releases_queue WHERE status = 'published'
                       ORDER BY published_at DESC LIMIT 30""")
        rows = cur.fetchall()
    cards = ""
    for r in rows:
        date = r["published_at"].strftime("%B %d, %Y") if r.get("published_at") else ""
        cards += f"""<a href="/press/{r['slug']}" style="display:block;background:#11121a;border:1px solid #1f2030;border-radius:10px;padding:1.25rem 1.5rem;margin-bottom:1rem;text-decoration:none;color:white;">
<div style="font-family:'JetBrains Mono',monospace;font-size:0.7rem;color:#6366f1;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:0.4rem;">DCPI {r.get('trigger_type','').replace('_',' ').upper()} · {date}</div>
<h3 style="margin:0 0 0.4rem;font-size:1.05rem;font-weight:700;">{r['title']}</h3>
<p style="margin:0;color:#9ca3af;font-size:0.92rem;">{(r.get('subheadline') or '')[:160]}</p>
</a>"""
    if not rows:
        cards = '<p style="color:#9ca3af;text-align:center;padding:3rem 0;">No press releases yet. Check back when DCPI moves &gt;15 points week-over-week.</p>'
    return Response(f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>DC Hub · Press</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&family=JetBrains+Mono&display=swap" rel="stylesheet">
<style>body{{font-family:Inter,system-ui;background:#0a0a12;color:#fff;margin:0;line-height:1.6}}
.wrap{{max-width:780px;margin:0 auto;padding:3rem 1.5rem}}
h1{{font-size:2.4rem;margin:0 0 0.5rem;font-weight:800;letter-spacing:-0.02em}}
.sub{{color:#9ca3af;margin:0 0 2.5rem}}
</style></head><body><div class="wrap">
<h1>DC Hub Press</h1>
<p class="sub">Auto-published when the DCPI moves more than 15 points week-over-week. Free for citation.</p>
{cards}
</div></body></html>""", mimetype="text/html")


@press_queue_bp.route("/press/<slug>", methods=["GET"])
@press_queue_bp.route("/api/v1/press/<slug>/page", methods=["GET"])
def press_release_page(slug):
    _ensure()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT * FROM press_releases_queue WHERE slug=%s
                       AND status = 'published'""", (slug,))
        r = cur.fetchone()
    if not r:
        return Response("<h1>Release not found or not yet published</h1>", status=404, mimetype="text/html")
    body_p = (r.get("body") or "").replace("\n\n", "</p><p>").replace("\n", "<br>")
    date = r["published_at"].strftime("%B %d, %Y") if r.get("published_at") else ""
    return Response(f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>{r['title']} · DC Hub Press</title>
<meta property="og:title" content="{r['title']}">
<meta property="og:description" content="{(r.get('subheadline') or '')[:200]}">
<meta property="og:type" content="article">
<meta property="og:url" content="https://dchub.cloud/press/{slug}">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&family=Georgia&display=swap" rel="stylesheet">
<style>body{{font-family:Georgia,serif;max-width:720px;margin:0 auto;padding:3rem 1.5rem;line-height:1.7;color:#222;background:white}}
.kicker{{font-family:Inter,system-ui;font-size:0.72rem;color:#6366f1;text-transform:uppercase;letter-spacing:0.12em;margin-bottom:0.4rem;font-weight:700}}
h1{{font-family:Inter,system-ui;font-size:2.2rem;margin:0 0 0.6rem;line-height:1.15;font-weight:800}}
.sub{{color:#555;font-size:1.1rem;margin:0 0 2rem}}
.meta{{font-family:Inter,system-ui;color:#999;font-size:0.85rem;margin-bottom:2.5rem;padding-bottom:1.5rem;border-bottom:1px solid #ddd}}
p{{font-size:1.05rem;margin:0 0 1.2rem}}
.cite{{font-family:Inter,system-ui;color:#666;font-size:0.85rem;margin-top:3rem;border-top:1px solid #ddd;padding-top:1.5rem}}
</style></head><body>
<div class="kicker">DC Hub Press · DCPI Alert</div>
<h1>{r['title']}</h1>
<p class="sub">{r.get('subheadline','') or ''}</p>
<p class="meta">Published {date} · <a href="/press" style="color:#6366f1;">All releases</a> · <a href="/dcpi" style="color:#6366f1;">DCPI dashboard</a></p>
<p>{body_p}</p>
<div class="cite"><strong>Cite as:</strong> DC Hub Press, "{r['title']}", https://dchub.cloud/press/{slug}, accessed {datetime.date.today().isoformat()}.</div>
</body></html>""", mimetype="text/html")
