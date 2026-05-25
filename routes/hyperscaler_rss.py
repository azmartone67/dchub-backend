"""Phase ZZZZZ-round40 — Hyperscaler $1B+ deal RSS feed (moat extension).

Item #6: every CRE/AI/finance bot crawls RSS. We have the data already
(dc_transactions table); just expose it as RSS so distribution is
zero-effort. Free, ungated, attribution-only output.

Wiring (main.py):
    from routes.hyperscaler_rss import hyperscaler_rss_bp
    app.register_blueprint(hyperscaler_rss_bp)
"""
import os
from datetime import datetime, timezone
from email.utils import format_datetime
from flask import Blueprint, Response
import psycopg

hyperscaler_rss_bp = Blueprint("hyperscaler_rss", __name__)
NEON_URL = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")


def _xml(s):
    return (str(s or "")
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace("\"", "&quot;").replace("\'", "&apos;"))


@hyperscaler_rss_bp.route("/hyperscaler-deals.rss")
@hyperscaler_rss_bp.route("/hyperscaler-deals.xml")
@hyperscaler_rss_bp.route("/rss/hyperscaler-deals")
def feed():
    rows = []
    if NEON_URL:
        try:
            with psycopg.connect(NEON_URL, autocommit=True) as conn, conn.cursor() as cur:
                # Defensive: column names vary by schema version. Use to_jsonb to
                # work regardless. If your schema differs, adjust the SELECT.
                cur.execute("""
                    SELECT buyer, seller, deal_value_usd, deal_type,
                           announcement_date, region,
                           COALESCE(notes,'') AS notes,
                           COALESCE(source_url,'') AS source_url
                    FROM dc_transactions
                    WHERE deal_value_usd >= 1000000000
                      AND announcement_date >= NOW() - INTERVAL '180 days'
                    ORDER BY announcement_date DESC NULLS LAST
                    LIMIT 50
                """)
                rows = cur.fetchall()
        except Exception:
            rows = []  # graceful empty feed beats 500

    items = []
    for r in rows:
        buyer, seller, val, dtype, date, region, notes, url = r
        title = f"{buyer or '?'} → {seller or '?'} · ${(val or 0)/1e9:.1f}B {dtype or 'deal'}"
        pub = format_datetime(date) if hasattr(date, "tzinfo") and date else format_datetime(datetime.now(timezone.utc))
        link = url or "https://dchub.cloud/deals"
        desc = _xml(f"{notes} (region: {region or 'global'})")
        guid = f"dchub-deal-{buyer}-{seller}-{date}"
        items.append(
            f"<item><title>{_xml(title)}</title>"
            f"<link>{_xml(link)}</link>"
            f"<description>{desc}</description>"
            f"<pubDate>{pub}</pubDate>"
            f"<guid isPermaLink=\"false\">{_xml(guid)}</guid></item>"
        )

    now = format_datetime(datetime.now(timezone.utc))
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
        '<title>DC Hub — Hyperscaler $1B+ Deals</title>'
        '<link>https://dchub.cloud/deals</link>'
        '<description>Every data center M&amp;A transaction over $1B in the last 6 months. Updated daily.</description>'
        '<language>en-us</language>'
        f'<lastBuildDate>{now}</lastBuildDate>'
        '<atom:link href="https://dchub.cloud/hyperscaler-deals.rss" rel="self" type="application/rss+xml"/>'
        '<ttl>360</ttl>'
        f"{''.join(items)}"
        '</channel></rss>'
    )
    return Response(body, mimetype="application/rss+xml; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=3600",
                             "X-DC-Phase": "ZZZZZ-round40-rss"})
