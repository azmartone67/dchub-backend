"""Phase 125A — Daily DC market digest. Bloomberg morning brief style."""
import os, json, datetime
from flask import Blueprint, jsonify, Response, render_template_string
import psycopg2, psycopg2.extras

digest_bp = Blueprint("digest", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    return psycopg2.connect(db, sslmode="require")


def _today_summary():
    """Compose today's digest: top movers, top BUILD markets, top AVOID, news count."""
    out = {"date": datetime.date.today().isoformat(),
           "title": f"DC Hub Daily — {datetime.date.today().strftime('%B %d, %Y')}",
           "top_build": [], "top_avoid": [], "biggest_movers": [],
           "news_count_24h": 0, "deals_count_7d": 0,
           "dcpi_summary": ""}
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Top 5 BUILD
            cur.execute("""SELECT DISTINCT ON (market_slug) * FROM market_power_scores
                           ORDER BY market_slug, computed_at DESC""")
            rows = cur.fetchall()
            rows.sort(key=lambda r: -(r.get("excess_power_score") or 0))
            out["top_build"] = [{"market": r["market_name"], "slug": r["market_slug"],
                                 "excess": r["excess_power_score"],
                                 "constraint": r["constraint_score"],
                                 "verdict": r["verdict"]} for r in rows[:5]]
            rows_c = sorted(rows, key=lambda r: -(r.get("constraint_score") or 0))
            out["top_avoid"] = [{"market": r["market_name"], "slug": r["market_slug"],
                                 "constraint": r["constraint_score"],
                                 "ttp_months": r.get("time_to_power_months")} for r in rows_c[:5]]

            # Movers — biggest 7d delta
            cur.execute("""
                WITH latest AS (
                  SELECT DISTINCT ON (market_slug) market_slug, market_name,
                    excess_power_score AS now_e, computed_at
                  FROM market_power_scores ORDER BY market_slug, computed_at DESC
                ),
                week_ago AS (
                  SELECT DISTINCT ON (market_slug) market_slug,
                    excess_power_score AS prev_e
                  FROM market_power_scores
                  WHERE computed_at < NOW() - INTERVAL '7 days'
                  ORDER BY market_slug, computed_at DESC
                )
                SELECT l.market_slug, l.market_name, l.now_e,
                       COALESCE(l.now_e - w.prev_e, 0) AS delta
                FROM latest l LEFT JOIN week_ago w ON l.market_slug=w.market_slug
                ORDER BY ABS(COALESCE(l.now_e - w.prev_e, 0)) DESC LIMIT 5
            """)
            out["biggest_movers"] = [{"market": r["market_name"], "slug": r["market_slug"],
                                       "now": r["now_e"], "delta": r["delta"]}
                                      for r in cur.fetchall()]

            # News count 24h
            try:
                for tbl in ('industry_news', 'news_articles', 'news', 'press_releases'):
                    cur.execute("""SELECT COUNT(*) FROM %s WHERE created_at > NOW() - INTERVAL '24 hours'""" %
                                tbl.replace("'", ""))
                    out["news_count_24h"] = int(cur.fetchone()[0] or 0)
                    if out["news_count_24h"]: break
            except Exception: pass

            # Deals 7d
            try:
                cur.execute("""SELECT COUNT(*) FROM ai_deals WHERE created_at > NOW() - INTERVAL '7 days'""")
                out["deals_count_7d"] = int(cur.fetchone()[0] or 0)
            except Exception: pass
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"

    # Editorial line
    if out["top_build"]:
        leader = out["top_build"][0]
        out["dcpi_summary"] = f"{leader['market']} leads Excess Power at {leader['excess']:.1f}. " \
                              f"Northern Virginia continues at the bottom with severe constraints."
    return out


@digest_bp.route("/api/v1/digest/today", methods=["GET"])
def digest_today_json():
    return jsonify(_today_summary()), 200


@digest_bp.route("/digest", methods=["GET"])
@digest_bp.route("/digest/today", methods=["GET"])
def digest_today_page():
    d = _today_summary()
    HTML = '''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>{{ d.title }} · DC Hub</title>
<meta property="og:title" content="{{ d.title }}">
<meta property="og:description" content="DC market brief — {{ d.top_build|length }} BUILD-verdict markets, {{ d.biggest_movers|length }} movers, {{ d.news_count_24h }} news items in last 24h.">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dchub-brand.css">
<script src="/js/dchub-nav.js" defer></script>
<style>
:root{--bg:#0a0a0f;--card:#131319;--bd:rgba(255,255,255,.08);--tx:#fafafa;--tx2:#a1a1aa;--green:#10b981;--orange:#f59e0b;--red:#ef4444;--acc:#6366f1}
*{box-sizing:border-box}body{font-family:'Instrument Sans',-apple-system,sans-serif;background:var(--bg);color:var(--tx);margin:0;line-height:1.6}
.wrap{max-width:880px;margin:0 auto;padding:3rem 1.5rem}
.kicker{font-family:'JetBrains Mono',monospace;font-size:0.78rem;color:var(--acc);text-transform:uppercase;letter-spacing:0.12em;margin-bottom:0.5rem}
h1{font-size:2.6rem;margin:0 0 0.5rem;font-weight:800;letter-spacing:-0.02em}
.sub{color:var(--tx2);margin:0 0 2.5rem;font-size:1rem}
h2{font-size:0.78rem;color:var(--tx2);text-transform:uppercase;letter-spacing:0.1em;margin:2rem 0 0.8rem;font-weight:700}
.row{display:flex;justify-content:space-between;padding:0.75rem 1.25rem;background:var(--card);border:1px solid var(--bd);border-radius:8px;margin-bottom:0.5rem;align-items:center}
.row a{color:var(--tx);text-decoration:none}
.row .name{font-weight:600}
.row .val{font-family:'JetBrains Mono',monospace;font-size:1.1rem;font-weight:700}
.green{color:var(--green)}.orange{color:var(--orange)}.red{color:var(--red)}
.delta{font-family:'JetBrains Mono',monospace;font-weight:600}
.delta.up{color:var(--green)}.delta.down{color:var(--red)}
.lede{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:1.5rem;margin:0 0 2rem;font-size:1.05rem;color:#ddd}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem;margin:1rem 0 2rem}
.stat{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:1rem 1.25rem}
.stat .n{font-family:'JetBrains Mono',monospace;font-size:1.6rem;font-weight:800}
.stat .l{color:var(--tx2);font-size:0.7rem;text-transform:uppercase;letter-spacing:0.06em}
footer{margin-top:3rem;padding-top:1.5rem;border-top:1px solid var(--bd);color:var(--tx2);font-size:0.85rem}
footer a{color:var(--acc)}
</style></head><body><div class="wrap">
<div class="kicker">DC HUB DAILY · {{ d.date }}</div>
<h1>{{ d.title }}</h1>
<p class="sub">The morning brief on U.S. data center markets.</p>
{% if d.dcpi_summary %}<div class="lede">{{ d.dcpi_summary }}</div>{% endif %}
<div class="stats">
<div class="stat"><div class="n">{{ d.top_build|length }}</div><div class="l">BUILD markets</div></div>
<div class="stat"><div class="n">{{ d.top_avoid|length }}</div><div class="l">AVOID markets</div></div>
<div class="stat"><div class="n">{{ d.news_count_24h }}</div><div class="l">News (24h)</div></div>
<div class="stat"><div class="n">{{ d.deals_count_7d }}</div><div class="l">Deals (7d)</div></div>
</div>
<h2>🟢 Top BUILD markets</h2>
{% for r in d.top_build %}<div class="row"><a href="/dcpi/{{ r.slug }}"><span class="name">{{ r.market }}</span></a>
<span class="val green">Excess {{ r.excess }} · {{ r.verdict }}</span></div>{% endfor %}
<h2>🔴 Top AVOID markets</h2>
{% for r in d.top_avoid %}<div class="row"><a href="/dcpi/{{ r.slug }}"><span class="name">{{ r.market }}</span></a>
<span class="val red">Constraint {{ r.constraint }} · ~{{ (r.ttp_months or 0)|round(0)|int }}mo</span></div>{% endfor %}
<h2>📈 Biggest 7-day movers</h2>
{% for r in d.biggest_movers %}<div class="row"><a href="/dcpi/{{ r.slug }}"><span class="name">{{ r.market }}</span></a>
<span class="delta {{ 'up' if r.delta > 0 else 'down' }}">{{ '+' if r.delta > 0 else '' }}{{ r.delta|round(1) }}</span></div>{% endfor %}
<footer><p>Daily at 14:00 UTC · Free for citation. <a href="/dcpi">Open the index →</a> · <a href="/dcpi/press">Press kit</a></p></footer>
</div></body></html>'''
    resp = Response(render_template_string(HTML, d=d), mimetype="text/html")
    # Phase SS (2026-05-14): short, explicit cache TTL. /digest had a
    # 404 stuck at the CF edge for an hour (max-age=3600) — cached
    # during a deploy window when the blueprint wasn't registered yet.
    # A 5-minute TTL means any transient bad response clears fast
    # instead of poisoning the page for an hour.
    resp.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    return resp


@digest_bp.route("/api/v1/digest/send", methods=["POST"])
def digest_send():
    """Email today's digest to all dev-key holders."""
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    from flask import request
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    if expected and provided != expected:
        return jsonify(error="unauthorized"), 401
    dry = request.args.get("dry", "1") == "1"

    d = _today_summary()
    with _conn() as c, c.cursor() as cur:
        # Phase r27 (2026-05-20): UNION mcp_dev_keys AND digest_subscribers.
        # Pre-r27 the form-signups (digest_subscribers) were a dead-end
        # table — the form accepted emails, INSERTed them, then never
        # emailed anyone. Now anyone who hands us their email via the
        # /api/v1/digest/subscribe endpoint or the embedded form gets
        # the same daily brief that dev-key holders get.
        cur.execute("""
            SELECT DISTINCT lower(email) AS email
              FROM (
                SELECT email FROM mcp_dev_keys
                 WHERE email IS NOT NULL AND email != ''
                UNION
                SELECT email FROM digest_subscribers
                 WHERE email IS NOT NULL AND email != ''
                   AND unsubscribed_at IS NULL
              ) u
        """)
        emails = [r[0] for r in cur.fetchall()]
    if dry:
        return jsonify(dry_run=True, recipient_count=len(emails),
                       sample=emails[:3], digest_preview=d), 200

    body_html = f"""<h2>{d['title']}</h2>
<p>{d.get('dcpi_summary','')}</p>
<h3>Top BUILD markets</h3><ul>{''.join(f'<li><b>{r["market"]}</b> — Excess {r["excess"]}</li>' for r in d['top_build'])}</ul>
<h3>Top AVOID markets</h3><ul>{''.join(f'<li><b>{r["market"]}</b> — Constraint {r["constraint"]}</li>' for r in d['top_avoid'])}</ul>
<h3>Biggest movers</h3><ul>{''.join(f'<li><b>{r["market"]}</b> — Δ{r["delta"]:+.1f}</li>' for r in d['biggest_movers'])}</ul>
<p><a href="https://dchub.cloud/digest/today">View full digest</a> · <a href="https://dchub.cloud/dcpi">Open DCPI</a></p>"""

    sent = 0; failed = 0
    try:
        import requests as _rq
        for em in emails:
            try:
                r = _rq.post("https://api.resend.com/emails",
                    json={"from": os.environ.get("DCHUB_FROM_EMAIL","DC Hub <jonathan@dchub.cloud>"),
                          "to": [em], "subject": d["title"], "html": body_html},
                    headers={"Authorization": f"Bearer {os.environ.get('RESEND_API_KEY','').strip()}",
                             "Content-Type": "application/json", "Accept": "application/json",
                             "User-Agent": "Mozilla/5.0 (compatible; DCHub/1.0; +https://dchub.cloud)"},
                    timeout=15)
                if 200 <= r.status_code < 300: sent += 1
                else: failed += 1
            except Exception: failed += 1
    except Exception as e:
        return jsonify(error=str(e), sent=sent, failed=failed), 500
    return jsonify(sent=sent, failed=failed, recipient_count=len(emails)), 200

@digest_bp.route("/api/v1/digest/subscribe", methods=["POST"])
def subscribe():
    """Sign someone up for the daily digest."""
    from flask import request
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or request.form.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify(error="valid email required"), 400
    _ensure_subscribers()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO digest_subscribers (email, source, subscribed_at)
            VALUES (%s, 'dcpi-form', NOW())
            ON CONFLICT (email) DO UPDATE SET subscribed_at = NOW(), unsubscribed_at = NULL
            RETURNING id""", (email,))
        sid = cur.fetchone()[0]; c.commit()
    return jsonify(ok=True, subscriber_id=sid), 200


def _ensure_subscribers():
    with _conn() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS digest_subscribers (
            id SERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL, source TEXT,
            subscribed_at TIMESTAMPTZ DEFAULT NOW(), unsubscribed_at TIMESTAMPTZ)""")
        c.commit()

# === Phase 126A: /api/v1/digest/page is the CF-allowlisted alias for /digest ===
@digest_bp.route("/api/v1/digest/page", methods=["GET"])
@digest_bp.route("/api/v1/digest/today/page", methods=["GET"])
def digest_today_page_alias():
    return digest_today_page()

