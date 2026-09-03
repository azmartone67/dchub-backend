"""Phase ZZZZZ-round40 — Hyperscaler $1B+ deal RSS feed (moat extension).

Item #6: every CRE/AI/finance bot crawls RSS. Expose the deal data as RSS so
distribution is zero-effort. Free, ungated, attribution-only output.

★ 2026-08-01 — THIS FEED HAD NEVER PUBLISHED A SINGLE ITEM.
The original of this docstring said "We have the data already
(dc_transactions table)". There has never been a `dc_transactions` table in
this database. The read raised UndefinedTable, the handler swallowed it into
`rows = []` ("graceful empty feed beats 500"), and the feed went out — HTTP
200, valid XML, correct `lastBuildDate` — carrying zero items under a channel
description that reads "Every data center M&A transaction over $1B in the
last 6 months. Updated daily."

An empty feed is not a neutral placeholder here. It is an affirmative
editorial claim that no $1B+ data-center transaction happened in six months,
published to every CRE/AI/finance crawler that subscribes. Measured live on
2026-08-01, the real answer over `deals` was 106 publishable transactions in
the trailing 180 days.

Two things changed:
  * the read points at `deals`, quarantine-guarded via util.deals.DEALS_OK
    (4,711 raw / 1,843 publishable), dated through util.db_honesty.DEAL_DATE
    because `deals.date` is TEXT and a bare cast throws on one bad row;
  * a read that FAILS no longer renders as an empty feed. It returns 503 with
    no-store, so a broken read cannot be cached and re-served as "no deals".

Wiring (main.py):
    from routes.hyperscaler_rss import hyperscaler_rss_bp
    app.register_blueprint(hyperscaler_rss_bp)
"""
import os
from datetime import datetime, timezone
from email.utils import format_datetime
from flask import Blueprint, Response
import psycopg

from util.db_honesty import DEAL_DATE
from util.deals import DEALS_OK

hyperscaler_rss_bp = Blueprint("hyperscaler_rss", __name__)
NEON_URL = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")

#: `deals.value` is denominated in MILLIONS of USD, not USD. The dead query
#: filtered `deal_value_usd >= 1000000000`; against `value` that threshold is
#: 1000. Getting this backwards is a 1,000,000x error in a published headline
#: — the same unit gate tests/test_route_read_honesty.py fences by banning a
#: `value_usd` key over this column.
_ONE_BILLION_IN_MILLIONS = 1000


def _xml(s):
    return (str(s or "")
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace("\"", "&quot;").replace("\'", "&apos;"))


@hyperscaler_rss_bp.route("/hyperscaler-deals.rss")
@hyperscaler_rss_bp.route("/hyperscaler-deals.xml")
@hyperscaler_rss_bp.route("/rss/hyperscaler-deals")
def feed():
    rows, read_error = [], None
    if not NEON_URL:
        read_error = "no_database_url"
    else:
        conn = None
        try:
            # try/finally, NOT `with <conn>` — see util/db_honesty. This read
            # path must not sit inside an implicit transaction.
            conn = psycopg.connect(NEON_URL, autocommit=True)
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT buyer, seller, value, type,
                           {DEAL_DATE} AS announced_on, region,
                           COALESCE(notes,'') AS notes,
                           COALESCE(source_url,'') AS source_url
                    FROM deals
                    WHERE {DEALS_OK}
                      AND value >= {_ONE_BILLION_IN_MILLIONS}
                      AND {DEAL_DATE} >= CURRENT_DATE - 180
                    ORDER BY {DEAL_DATE} DESC NULLS LAST
                    LIMIT 50
                """)
                rows = cur.fetchall()
        except Exception as e:
            read_error = f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"
            rows = []
        finally:
            if conn is not None:
                try: conn.close()
                except Exception: pass

    # ★ A read that failed must NOT render as an empty feed. "0 items" on this
    # channel means "no $1B+ deal in six months", which is a claim; 503 +
    # no-store means "ask again", which is the truth, and keeps a broken read
    # out of every downstream cache.
    if read_error:
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0"><channel>'
            '<title>DC Hub — Hyperscaler $1B+ Deals</title>'
            '<link>https://dchub.cloud/deals</link>'
            '<description>Temporarily unavailable: the deal feed could not be '
            f'built ({_xml(read_error)}). This is NOT a report of zero deals.'
            '</description>'
            '</channel></rss>'
        )
        return Response(body, status=503,
                        content_type="application/rss+xml; charset=utf-8",
                        headers={"Cache-Control": "no-store",
                                 "X-DC-Feed-Error": read_error[:120]})

    items = []
    for r in rows:
        buyer, seller, val_musd, dtype, date, region, notes, url = r
        # val_musd is MILLIONS -> billions is /1e3, not /1e9.
        title = (f"{buyer or '?'} → {seller or '?'} · "
                 f"${(val_musd or 0)/1e3:.1f}B {dtype or 'deal'}")
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
    return Response(body, content_type="application/rss+xml; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=3600",
                             "X-DC-Phase": "ZZZZZ-round40-rss"})
