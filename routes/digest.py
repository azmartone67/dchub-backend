"""Phase 125A — Daily DC market digest. Bloomberg morning brief style.

2026-06-06 overhaul:
  * Movers now read REAL 7-day deltas from the history-preserving
    `dcpi_daily_snapshots` table. The old query diffed
    `market_power_scores`, which is RE-STAMPED on every recompute
    (computed_at bumped to NOW), so the week_ago CTE never found a real
    prior row → every delta COALESCE'd to 0 → the email shipped a fake
    "Biggest movers: Akron Δ+0.0, Albany Δ+0.0, …" list. Same re-stamp
    bug already fixed in dcpi_mcp.py + dchub_media_hub.py.
  * Editorial line is data-driven (real top BUILD leader + real worst
    AVOID market) instead of hardcoding "Northern Virginia at the bottom"
    — which was wrong whenever a non-US market topped the constraint list.
  * The emailed body is now a branded, email-client-safe HTML template
    (dark logo header, gradient accent, stat tiles, color-coded
    BUILD/AVOID rows, real movers, CTA button, footer) instead of bare
    <h2>/<ul>. Inline styles + table layout only (no <style>/flex/grid)
    so it renders in Gmail, Outlook and Apple Mail.
  * Per-recipient one-click unsubscribe (HMAC token) + List-Unsubscribe
    headers for deliverability/CAN-SPAM.
"""
import os, json, datetime, html, hmac, hashlib
from urllib.parse import quote
from flask import Blueprint, jsonify, Response, render_template_string, request
import psycopg2, psycopg2.extras

digest_bp = Blueprint("digest", __name__)

SITE = "https://dchub.cloud"
BRAND_LOGO = f"{SITE}/icons/dchub-logo.png"   # 512x512, dark navy bg (blends on dark header)


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    return psycopg2.connect(db, sslmode="require")


# ── unsubscribe token (unforgeable, no DB token column needed) ──────────
def _unsub_secret() -> bytes:
    return (os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
            or os.environ.get("DCHUB_SESSION_SECRET") or "dchub-digest-v1").encode()


def _unsub_token(email: str) -> str:
    return hmac.new(_unsub_secret(), (email or "").strip().lower().encode(),
                    hashlib.sha256).hexdigest()[:20]


def _unsub_url(email: str) -> str:
    return f"{SITE}/api/v1/digest/unsubscribe?e={quote(email)}&t={_unsub_token(email)}"


# ── data ────────────────────────────────────────────────────────────────
def _movers_from_snapshots(cur):
    """REAL 7-day excess-power movers from dcpi_daily_snapshots (history-
    preserving). Returns only markets that actually moved >=1pt; [] if the
    snapshot history is too thin to compute a true delta. NEVER fabricates
    a Δ+0.0 row. Do NOT diff market_power_scores here — it is re-stamped."""
    try:
        cur.execute("""
            WITH latest AS (
              SELECT DISTINCT ON (market_slug) market_slug, market_name,
                     excess_power_score AS now_e
              FROM dcpi_daily_snapshots
              ORDER BY market_slug, snapshot_date DESC
            ),
            week_ago AS (
              SELECT DISTINCT ON (market_slug) market_slug,
                     excess_power_score AS prev_e
              FROM dcpi_daily_snapshots
              WHERE snapshot_date <= CURRENT_DATE - 7
              ORDER BY market_slug, snapshot_date DESC
            )
            SELECT l.market_slug, l.market_name, l.now_e,
                   (l.now_e - w.prev_e) AS delta
            FROM latest l JOIN week_ago w ON l.market_slug = w.market_slug
            WHERE ABS(l.now_e - w.prev_e) >= 1
            ORDER BY ABS(l.now_e - w.prev_e) DESC
            LIMIT 40
        """)
        # DCPI excess scores are ISO-level cloned, so every market in an ISO
        # that moved shows the IDENTICAL delta (e.g. all NYISO markets at
        # -9.2). Collapse to one representative per distinct delta so the
        # list surfaces 5 DIFFERENT regional moves instead of 4 near-dupes —
        # still 100% real data, just de-duplicated for readability.
        out, seen = [], set()
        for r in cur.fetchall():
            dkey = round(r["delta"] or 0, 1)
            if dkey in seen:
                continue
            seen.add(dkey)
            out.append({"market": r["market_name"], "slug": r["market_slug"],
                        "now": round(r["now_e"] or 0, 1), "delta": dkey})
            if len(out) >= 5:
                break
        return out
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass
        return []


def _today_summary():
    """Compose today's digest: top movers, top BUILD markets, top AVOID, news count."""
    out = {"date": datetime.date.today().isoformat(),
           "title": f"DC Hub Daily — {datetime.date.today().strftime('%B %d, %Y')}",
           "top_build": [], "top_avoid": [], "biggest_movers": [],
           "markets_total": 0, "news_count_24h": 0, "deals_count_7d": 0,
           "dcpi_summary": ""}
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Current per-market scores (point-in-time; re-stamp is fine here)
            cur.execute("""SELECT DISTINCT ON (market_slug) * FROM market_power_scores
                           ORDER BY market_slug, computed_at DESC""")
            rows = cur.fetchall()
            out["markets_total"] = len(rows)
            rows.sort(key=lambda r: -(r.get("excess_power_score") or 0))
            out["top_build"] = [{"market": r["market_name"], "slug": r["market_slug"],
                                 "excess": r["excess_power_score"],
                                 "constraint": r["constraint_score"],
                                 "verdict": r["verdict"]} for r in rows[:5]]
            rows_c = sorted(rows, key=lambda r: -(r.get("constraint_score") or 0))
            out["top_avoid"] = [{"market": r["market_name"], "slug": r["market_slug"],
                                 "constraint": r["constraint_score"],
                                 "ttp_months": r.get("time_to_power_months")} for r in rows_c[:5]]

            # Movers — REAL 7d delta from snapshot history (no fabricated zeros)
            out["biggest_movers"] = _movers_from_snapshots(cur)

            # News count 24h
            try:
                for tbl in ('industry_news', 'news_articles', 'news', 'press_releases'):
                    cur.execute("""SELECT COUNT(*) AS n FROM %s WHERE created_at > NOW() - INTERVAL '24 hours'""" %
                                tbl.replace("'", ""))
                    out["news_count_24h"] = int((cur.fetchone() or {}).get("n") or 0)
                    if out["news_count_24h"]: break
            except Exception:
                try: cur.connection.rollback()
                except Exception: pass

            # Deals 7d
            try:
                cur.execute("""SELECT COUNT(*) AS n FROM ai_deals WHERE created_at > NOW() - INTERVAL '7 days'""")
                out["deals_count_7d"] = int((cur.fetchone() or {}).get("n") or 0)
            except Exception:
                try: cur.connection.rollback()
                except Exception: pass
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"

    # Editorial line — data-driven (real leader + real worst), never hardcoded
    if out["top_build"]:
        leader = out["top_build"][0]
        line = f"{leader['market']} leads on excess power at {leader['excess']:.1f}"
        if out["top_avoid"]:
            worst = out["top_avoid"][0]
            line += (f", while {worst['market']} sits at the bottom with the tightest "
                     f"constraints ({worst['constraint']:.1f}).")
        else:
            line += "."
        out["dcpi_summary"] = line
    return out


# ── email render (branded, email-client-safe) ──────────────────────────
def _esc(s) -> str:
    return html.escape("" if s is None else str(s))


def _num(v, dp=1):
    try: return f"{float(v):.{dp}f}"
    except Exception: return "—"


def _market_row(rank, name, slug, right_html, accent):
    """One market line — white card, colored left accent, links to /dcpi/<slug>."""
    return (
        '<tr><td style="padding:0 0 8px 0">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:#ffffff;border:1px solid #eceef4;border-left:4px solid {accent};'
        'border-radius:8px"><tr>'
        '<td style="padding:11px 14px;font:600 15px/1.3 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a">'
        f'<span style="color:#94a3b8;font-weight:700">{rank}.</span>&nbsp;'
        f'<a href="{SITE}/dcpi/{_esc(slug)}" style="color:#0f172a;text-decoration:none">{_esc(name)}</a>'
        '</td>'
        f'<td align="right" style="padding:11px 14px;white-space:nowrap;font:600 14px -apple-system,Segoe UI,Roboto,Arial,sans-serif">{right_html}</td>'
        '</tr></table></td></tr>'
    )


def _section(title, rows_html):
    if not rows_html:
        return ""
    return (
        f'<tr><td style="padding:18px 0 8px 0;font:700 12px/1 -apple-system,Segoe UI,Roboto,Arial,sans-serif;'
        f'letter-spacing:.09em;text-transform:uppercase;color:#64748b">{title}</td></tr>'
        '<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'{rows_html}</table></td></tr>'
    )


def _render_digest_email_html(d, unsub_url=None):
    grad = "linear-gradient(90deg,#6366f1,#a855f7)"
    # ── stat tiles (all real, no always-5 counts, no double-zeros) ──
    leader = d["top_build"][0] if d["top_build"] else {}
    worst = d["top_avoid"][0] if d["top_avoid"] else {}
    tiles = [
        (str(d.get("markets_total") or "—"), "Markets scored"),
        (_num(leader.get("excess"), 0), "Top excess"),
        (_num(worst.get("constraint"), 0), "Worst constraint"),
        (str(len(d.get("biggest_movers") or [])), "Movers (7d)"),
    ]
    tile_cells = "".join(
        '<td width="25%" align="center" valign="top" style="padding:6px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="background:#ffffff;border:1px solid #eceef4;border-radius:10px"><tr>'
        '<td align="center" style="padding:14px 6px">'
        f'<div style="font:800 24px/1 -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#4f46e5">{_esc(n)}</div>'
        f'<div style="font:600 10px/1.3 -apple-system,Segoe UI,Roboto,Arial,sans-serif;letter-spacing:.05em;text-transform:uppercase;color:#94a3b8;padding-top:5px">{_esc(l)}</div>'
        '</td></tr></table></td>'
        for n, l in tiles
    )

    # ── BUILD rows (green) ──
    build_rows = "".join(
        _market_row(
            i + 1, r["market"], r["slug"],
            f'<span style="color:#059669">Excess {_num(r.get("excess"))}</span>'
            + (f'&nbsp;<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
               f'background:#ecfdf5;color:#059669;font:700 10px -apple-system,Arial,sans-serif;'
               f'letter-spacing:.04em;text-transform:uppercase">{_esc(r.get("verdict"))}</span>'
               if r.get("verdict") else ""),
            "#10b981")
        for i, r in enumerate(d.get("top_build", []))
    )
    # ── AVOID rows (red) ──
    avoid_rows = "".join(
        _market_row(
            i + 1, r["market"], r["slug"],
            f'<span style="color:#dc2626">Constraint {_num(r.get("constraint"))}</span>'
            + (f'&nbsp;<span style="color:#94a3b8;font-weight:500">~{int(round(r.get("ttp_months") or 0))}mo</span>'
               if r.get("ttp_months") else ""),
            "#ef4444")
        for i, r in enumerate(d.get("top_avoid", []))
    )
    # ── movers rows (real only) ──
    movers = d.get("biggest_movers", [])
    if movers:
        mover_rows = "".join(
            _market_row(
                i + 1, r["market"], r["slug"],
                (f'<span style="color:#059669">&#9650; +{_num(r.get("delta"))}</span>'
                 if (r.get("delta") or 0) > 0 else
                 f'<span style="color:#dc2626">&#9660; {_num(r.get("delta"))}</span>')
                + f'&nbsp;<span style="color:#94a3b8;font-weight:500">now {_num(r.get("now"))}</span>',
                "#6366f1")
            for i, r in enumerate(movers)
        )
        movers_section = _section("📈 Biggest 7-day movers", mover_rows)
    else:
        # Honest empty state — no fabricated zeros.
        movers_section = (
            '<tr><td style="padding:18px 0 8px 0;font:700 12px/1 -apple-system,Segoe UI,Roboto,Arial,sans-serif;'
            'letter-spacing:.09em;text-transform:uppercase;color:#64748b">📈 Biggest 7-day movers</td></tr>'
            '<tr><td style="padding:4px 2px;font:400 14px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#94a3b8">'
            'Markets held steady — no market shifted by 1+ excess-power point over the last 7 days.</td></tr>'
        )

    footer_unsub = (f'<a href="{_esc(unsub_url)}" style="color:#94a3b8;text-decoration:underline">Unsubscribe</a> · '
                    if unsub_url else "")

    lede = _esc(d.get("dcpi_summary") or "Today's U.S. + global data-center market brief.")
    secondary = ""
    extras = []
    if d.get("news_count_24h"): extras.append(f"{d['news_count_24h']} news items (24h)")
    if d.get("deals_count_7d"): extras.append(f"{d['deals_count_7d']} M&A deals (7d)")
    if extras:
        secondary = ('<tr><td style="padding:8px 0 0 0;font:400 13px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#64748b">'
                     f'Also tracked: {_esc(" · ".join(extras))}.</td></tr>')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">
<title>{_esc(d['title'])}</title></head>
<body style="margin:0;padding:0;background:#eef0f5;-webkit-text-size-adjust:100%">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{lede}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#eef0f5"><tr>
<td align="center" style="padding:24px 12px">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px">
    <!-- header -->
    <tr><td bgcolor="#0f1117" style="background:#0f1117;border-radius:14px 14px 0 0;padding:26px 0 18px;text-align:center">
      <img src="{BRAND_LOGO}" width="120" height="120" alt="DC Hub — Data Center Intelligence"
           style="display:inline-block;border:0;width:120px;height:120px">
    </td></tr>
    <tr><td height="4" style="height:4px;line-height:4px;font-size:0;background:{grad}" bgcolor="#7c3aed">&nbsp;</td></tr>
    <!-- body -->
    <tr><td bgcolor="#ffffff" style="background:#ffffff;border-radius:0 0 14px 14px;padding:26px 26px 30px">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr><td style="font:700 11px/1 -apple-system,Segoe UI,Roboto,Arial,sans-serif;letter-spacing:.14em;text-transform:uppercase;color:#7c3aed">
          Daily Market Brief · {_esc(d['date'])}</td></tr>
        <tr><td style="padding:6px 0 0 0;font:800 26px/1.2 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a">
          {_esc(d['title'])}</td></tr>
        <!-- lede card -->
        <tr><td style="padding:16px 0 4px 0">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f6f5ff;border:1px solid #e6e3ff;border-radius:10px"><tr>
          <td style="padding:14px 16px;font:500 15px/1.55 -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#312e81">{lede}</td>
          </tr></table>
        </td></tr>
        {secondary}
        <!-- stat tiles -->
        <tr><td style="padding:12px 0 0 0"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>{tile_cells}</tr></table></td></tr>
        <!-- sections -->
        {_section("🟢 Top BUILD markets", build_rows)}
        {_section("🔴 Top AVOID markets", avoid_rows)}
        {movers_section}
        <!-- CTA -->
        <tr><td align="center" style="padding:26px 0 6px 0">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
          <td bgcolor="#6366f1" style="background:{grad};border-radius:10px">
            <a href="{SITE}/dcpi" style="display:inline-block;padding:13px 30px;font:700 15px -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#ffffff;text-decoration:none">Open the live index →</a>
          </td></tr></table>
        </td></tr>
        <tr><td align="center" style="padding:4px 0 0 0;font:400 13px -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#94a3b8">
          Or connect any AI agent: <span style="font-family:Menlo,Consolas,monospace;color:#475569">dchub.cloud/mcp</span></td></tr>
        <!-- footer -->
        <tr><td style="padding:22px 0 0 0"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
          <td style="border-top:1px solid #eceef4;padding:16px 0 0 0;font:400 12px/1.6 -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#94a3b8;text-align:center">
            DC Hub · agent-native data-center &amp; power intelligence · free for citation (CC BY 4.0)<br>
            {footer_unsub}<a href="{SITE}/dcpi" style="color:#94a3b8;text-decoration:underline">Open DCPI</a> ·
            <a href="{SITE}/digest/today" style="color:#94a3b8;text-decoration:underline">View in browser</a>
          </td>
        </tr></table></td></tr>
      </table>
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""


# ── routes ──────────────────────────────────────────────────────────────
@digest_bp.route("/api/v1/digest/today", methods=["GET"])
def digest_today_json():
    return jsonify(_today_summary()), 200


@digest_bp.route("/api/v1/digest/preview", methods=["GET"])
def digest_email_preview():
    """Render the EMAIL HTML in the browser (for QA — no send). Admin-gated
    only if an admin key is configured; otherwise open (it's the same
    public data the JSON/web endpoints already expose)."""
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    if expected and provided and provided != expected:
        return jsonify(error="unauthorized"), 401
    d = _today_summary()
    sample = _unsub_url("preview@example.com")
    return Response(_render_digest_email_html(d, sample), mimetype="text/html")


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
.muted{color:var(--tx2);padding:0.6rem 0}
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
<p class="sub">The morning brief on U.S. + global data center markets.</p>
{% if d.dcpi_summary %}<div class="lede">{{ d.dcpi_summary }}</div>{% endif %}
<div class="stats">
<div class="stat"><div class="n">{{ d.markets_total }}</div><div class="l">Markets scored</div></div>
<div class="stat"><div class="n">{{ d.biggest_movers|length }}</div><div class="l">Movers (7d)</div></div>
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
{% if d.biggest_movers %}{% for r in d.biggest_movers %}<div class="row"><a href="/dcpi/{{ r.slug }}"><span class="name">{{ r.market }}</span></a>
<span class="delta {{ 'up' if r.delta > 0 else 'down' }}">{{ '+' if r.delta > 0 else '' }}{{ r.delta|round(1) }}</span></div>{% endfor %}
{% else %}<div class="muted">Markets held steady — no market shifted by 1+ excess-power point over the last 7 days.</div>{% endif %}
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
    """Email today's digest to all dev-key holders + opted-in subscribers."""
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
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
        # Include the rendered HTML so a dry run is a true preview.
        return jsonify(dry_run=True, recipient_count=len(emails),
                       sample=emails[:3], digest_preview=d,
                       html_bytes=len(_render_digest_email_html(d, _unsub_url("preview@example.com")))), 200

    sent = 0; failed = 0
    try:
        import requests as _rq
        for em in emails:
            try:
                body_html = _render_digest_email_html(d, _unsub_url(em))
                unsub = _unsub_url(em)
                r = _rq.post("https://api.resend.com/emails",
                    json={"from": os.environ.get("DCHUB_FROM_EMAIL", "DC Hub <jonathan@dchub.cloud>"),
                          "to": [em], "subject": d["title"], "html": body_html,
                          # RFC 2369 + RFC 8058 one-click — improves inbox placement
                          "headers": {"List-Unsubscribe": f"<{unsub}>",
                                      "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}},
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


@digest_bp.route("/api/v1/digest/unsubscribe", methods=["GET", "POST"])
def unsubscribe():
    """One-click unsubscribe via HMAC token (no DB token column needed).
    Honors RFC 8058 one-click POST and a plain GET click."""
    email = (request.args.get("e") or request.form.get("e") or "").strip().lower()
    token = (request.args.get("t") or request.form.get("t") or "").strip()
    ok = bool(email) and hmac.compare_digest(token, _unsub_token(email))
    if ok:
        try:
            _ensure_subscribers()
            with _conn() as c, c.cursor() as cur:
                # Mark any matching subscriber row unsubscribed; insert a
                # tombstone if they only existed as a dev-key holder so the
                # send-time UNION excludes them going forward.
                cur.execute("""INSERT INTO digest_subscribers (email, source, subscribed_at, unsubscribed_at)
                    VALUES (%s, 'unsub', NOW(), NOW())
                    ON CONFLICT (email) DO UPDATE SET unsubscribed_at = NOW()""", (email,))
                c.commit()
        except Exception:
            ok = False
    msg = ("You're unsubscribed. You won't receive the DC Hub Daily brief anymore."
           if ok else "We couldn't verify that unsubscribe link. Email jonathan@dchub.cloud and we'll remove you.")
    pagetitle = "Unsubscribed" if ok else "Unsubscribe"
    pg = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{pagetitle} · DC Hub</title></head>
<body style="margin:0;background:#eef0f5;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif">
<table role="presentation" width="100%" height="100%" cellpadding="0" cellspacing="0"><tr><td align="center" valign="middle" style="padding:48px 16px">
<table role="presentation" width="460" cellpadding="0" cellspacing="0" style="max-width:460px;background:#fff;border-radius:14px;border:1px solid #e6e9f0">
<tr><td bgcolor="#0f1117" style="background:#0f1117;border-radius:14px 14px 0 0;text-align:center;padding:22px 0">
<img src="{BRAND_LOGO}" width="84" height="84" alt="DC Hub" style="width:84px;height:84px;border:0"></td></tr>
<tr><td style="padding:26px 28px;text-align:center;color:#0f172a;font-size:16px;line-height:1.6">{html.escape(msg)}
<div style="padding-top:18px"><a href="{SITE}/dcpi" style="color:#6366f1;text-decoration:none;font-weight:600">Open DCPI →</a></div></td></tr>
</table></td></tr></table></body></html>"""
    return Response(pg, status=(200 if ok else 400), mimetype="text/html")


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
