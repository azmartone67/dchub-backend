"""
dchub_media.py — Autonomous Media Brain for DC Hub
===================================================
Single source of truth for: daily LinkedIn post, press releases,
announcements aggregation, testimonials curation.
"""

from __future__ import annotations
import os
import json
import logging
import datetime
import io


# Phase 232: surface per-source errors so /api/v1/media/diagnose can show them
_agg_errors = {}

def get_aggregator_errors():
    """Return the last-run errors per source category. Read-only."""
    return dict(_agg_errors)


log = logging.getLogger('dchub_media')
DCHUB_API_BASE = os.environ.get("DCHUB_API_BASE", "https://dchub.cloud")


class Aggregator:
    """Pulls data from all source systems."""
    def __init__(self, api_base: str = DCHUB_API_BASE):
        self.api_base = api_base.rstrip("/")

    def _get(self, path: str):
        import urllib.request
        try:
            req = urllib.request.Request(f"{self.api_base}{path}",
                                          headers={"User-Agent": "dchub-media/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except Exception as e:
            log.warning(f"GET {path} failed: {e}")
            return None

    def today_payload(self) -> dict:
        markets = self._get("/api/v1/markets/list")
        news    = self._get("/api/v1/news?limit=10")
        return {
            "date": datetime.date.today().isoformat(),
            "markets": (markets or {}).get("data") or markets or [],
            "news": (news or {}).get("data") or news or [],
        }


class Curator:
    def top_pipeline_growth(self, markets, n=3):
        valid = [m for m in markets if m.get("pipeline_mw_total")]
        return sorted(valid, key=lambda m: -m.get("pipeline_mw_total", 0))[:n]

    def cheapest_energy(self, markets, n=1):
        valid = [m for m in markets if m.get("avg_kwh_price_usd")]
        return sorted(valid, key=lambda m: m.get("avg_kwh_price_usd", 999))[:n]

    def most_expensive_energy(self, markets, n=1):
        valid = [m for m in markets if m.get("avg_kwh_price_usd")]
        return sorted(valid, key=lambda m: -m.get("avg_kwh_price_usd", 0))[:n]

    def us_avg_kwh(self, markets):
        prices = [m.get("avg_kwh_price_usd") for m in markets if m.get("avg_kwh_price_usd")]
        return round(sum(prices) / len(prices), 4) if prices else None


class Generator:
    def compose_linkedin_text(self, payload):
        c = Curator()
        date = datetime.date.today().strftime("%B %d, %Y")
        markets = payload.get("markets", [])
        top_pipe = c.top_pipeline_growth(markets, 3)
        cheapest = c.cheapest_energy(markets, 1)
        priciest = c.most_expensive_energy(markets, 1)
        avg = c.us_avg_kwh(markets)

        lines = [f"📊 DC Hub Daily · {date}", ""]
        lines.append(f"Top moves across {len(markets)} US + international data center markets:")
        lines.append("")
        if top_pipe:
            lines.append("🏗️ PIPELINE GROWTH")
            for m in top_pipe:
                lines.append(f"  • {m.get('name','?')}: {m.get('pipeline_mw_total',0):,.0f} MW")
            lines.append("")
        if avg is not None:
            lines.append("⚡ ENERGY PRICING")
            if cheapest:
                lines.append(f"  • Cheapest: {cheapest[0].get('name','?')} ${cheapest[0].get('avg_kwh_price_usd',0):.3f}/kWh")
            if priciest:
                lines.append(f"  • Most expensive: {priciest[0].get('name','?')} ${priciest[0].get('avg_kwh_price_usd',0):.3f}/kWh")
            lines.append(f"  • US average: ${avg:.3f}/kWh")
            lines.append("")
        lines.append("Track every market live → dchub.cloud/dcpi")
        lines.append("")
        lines.append("#DataCenters #DCPI #PowerInfrastructure #AI")
        return "\n".join(lines)

    def generate_chart_png(self, payload):
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            log.warning("matplotlib not available")
            return None
        markets = payload.get("markets", [])
        valid = [m for m in markets if m.get("pipeline_mw_total")]
        top = sorted(valid, key=lambda m: -m.get("pipeline_mw_total", 0))[:10]
        if not top: return None
        names = [m["name"] for m in top]
        mws = [m["pipeline_mw_total"] for m in top]
        kwhs = [m.get("avg_kwh_price_usd") or 0 for m in top]
        fig, ax = plt.subplots(figsize=(10, 6), facecolor="#0a0e1a")
        ax.set_facecolor("#0a0e1a")
        colors = ["#10b981" if k < 0.12 else "#fbbf24" if k < 0.20 else "#ef4444" for k in kwhs]
        bars = ax.barh(names, mws, color=colors)
        ax.set_xlabel("Pipeline MW", color="#e8eef8")
        ax.set_title(f"DC Hub · Top 10 Markets by Pipeline MW · {datetime.date.today():%Y-%m-%d}",
                     color="#e8eef8", fontsize=13, fontweight="bold")
        for spine in ax.spines.values(): spine.set_color("#374151")
        ax.tick_params(colors="#e8eef8")
        ax.invert_yaxis()
        for bar, mw, kwh in zip(bars, mws, kwhs):
            label = f"{mw:,.0f} MW · ${kwh:.3f}/kWh" if kwh else f"{mw:,.0f} MW"
            ax.text(bar.get_width() + 20, bar.get_y() + bar.get_height()/2,
                    label, va="center", color="#e8eef8", fontsize=9)
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format="png", facecolor="#0a0e1a", dpi=120)
        plt.close()
        return buf.getvalue()


class Publisher:
    def publish_linkedin(self, text: str, image_png: bytes | None = None):
        try:
            from linkedin_poster import post_to_linkedin
        except ImportError:
            return {"ok": False, "error": "linkedin_poster import failed"}
        try:
            if image_png is not None:
                return post_to_linkedin(text=text, image_bytes=image_png)
            return post_to_linkedin(text=text)
        except TypeError:
            # Old signature without image_bytes — fall back to text-only
            try:
                return post_to_linkedin(text=text)
            except Exception as e:
                return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def run_daily(api_base: str = DCHUB_API_BASE) -> dict:
    aggregator = Aggregator(api_base)
    generator = Generator()
    publisher = Publisher()
    payload = aggregator.today_payload()
    text = generator.compose_linkedin_text(payload)
    image = generator.generate_chart_png(payload)
    log.info(f"dchub_media daily — {len(text)} chars text, {'chart attached' if image else 'no chart'}")
    li_result = publisher.publish_linkedin(text, image)
    _brief_result = publish_daily_brief(payload, text)
    _press_results = maybe_publish_press_release(payload)
    return {
        "date": payload["date"],
        "announcement_publish": _brief_result,
        "press_releases": _press_results,
        "text_chars": len(text),
        "image_bytes": len(image) if image else 0,
        "linkedin": li_result,
        "preview": text[:400],
    }


def aggregate_announcements(limit_per_source=20):
    """Phase 234: queries use REAL column names verified via Chrome audit."""
    global _agg_errors
    _agg_errors = {}
    import os, psycopg2
    from datetime import datetime
    DATABASE_URL = os.environ.get("DATABASE_URL")
    items = []
    if not DATABASE_URL:
        return items
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=8)
    except Exception:
        return items

    queries = [
        ("news",
         """SELECT title,
                   COALESCE(source_url, '') AS url,
                   COALESCE(description, body, '') AS summary,
                   COALESCE(source, '') AS source,
                   COALESCE(published_date, created_at, NOW()) AS ts
            FROM news
            WHERE published_date > NOW() - INTERVAL '14 days'
            ORDER BY published_date DESC LIMIT %s""",
         (limit_per_source,)),
        ("press_release",
         """SELECT title,
                   COALESCE(source_url, '/news/' || slug || '/', '') AS url,
                   COALESCE(summary, subheadline, '') AS summary,
                   COALESCE(source, 'DC Hub') AS source,
                   COALESCE(published_date, date, created_at, NOW()) AS ts
            FROM press_releases
            ORDER BY COALESCE(published_date, date, created_at) DESC NULLS LAST
            LIMIT %s""",
         (limit_per_source,)),
        ("press",
         """SELECT title,
                   COALESCE(url, '') AS url,
                   COALESCE(body, '') AS summary,
                   COALESCE(source, '') AS source,
                   COALESCE(published_at, NOW()) AS ts
            FROM announcements_feed
            WHERE category IN ('press','press_release','daily_brief')
            ORDER BY published_at DESC LIMIT %s""",
         (limit_per_source,)),
        ("testimonial",
         """SELECT
               COALESCE(NULLIF(agent_name,''), 'AI Testimonial') AS title,
               COALESCE(url, '') AS url,
               quote AS summary,
               COALESCE(NULLIF(source,''), platform, agent_name, 'AI Industry') AS source,
               COALESCE(approved_at, created_at, NOW()) AS ts
            FROM ai_testimonials
            WHERE COALESCE(approved, true) = true
            ORDER BY COALESCE(approved_at, created_at) DESC NULLS LAST
            LIMIT %s""",
         (limit_per_source,)),
        ("alert",
         """SELECT
               (market_name || ' DCPI ' || verdict) AS title,
               '/dcpi#' || market_slug AS url,
               ('Constraint ' || COALESCE(constraint_score,0)::text ||
                ' Excess ' || COALESCE(excess_power_score,0)::text) AS summary,
               'DCPI Engine' AS source,
               computed_at AS ts
            FROM market_power_scores
            WHERE published = true
              AND computed_at > NOW() - INTERVAL '7 days'
              AND verdict IN ('BUILD','AVOID')
            ORDER BY computed_at DESC LIMIT %s""",
         (limit_per_source,)),
    ]

    for category, sql, params in queries:
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                for row in cur.fetchall():
                    items.append({
                        "category": category,
                        "type": category,
                        "title": row[0] or "(untitled)",
                        "url": row[1] or "",
                        "summary": (row[2] or "")[:500],
                        "source": row[3] or "",
                        "published_at": row[4].isoformat() if hasattr(row[4], "isoformat") else str(row[4]),
                        "ts": row[4].isoformat() if hasattr(row[4], "isoformat") else str(row[4]),
                    })
        except Exception as e:
            conn.rollback()
            try:
                _agg_errors[category] = str(e)[:300]
                import logging
                logging.warning("aggregate %s failed: %s", category, str(e)[:200])
            except Exception:
                pass
            continue

    conn.close()
    items.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return items



def _ensure_announcements_table(conn):
    """Create announcements_feed table if missing — idempotent."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS announcements_feed (
                id SERIAL PRIMARY KEY,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                excerpt TEXT,
                url TEXT,
                source TEXT,
                published_at TIMESTAMPTZ DEFAULT NOW(),
                payload JSONB,
                slug TEXT UNIQUE
            );
            CREATE INDEX IF NOT EXISTS announcements_feed_published_idx
                ON announcements_feed (published_at DESC);
            CREATE INDEX IF NOT EXISTS announcements_feed_category_idx
                ON announcements_feed (category);
        """)
    conn.commit()


def publish_announcement(item: dict) -> dict:
    """Write a single announcement to the announcements_feed table.

    item shape: {"category", "title", "excerpt", "url", "source", "payload": dict}
    """
    import os, json
    try:
        import psycopg2
    except ImportError:
        return {"ok": False, "error": "psycopg2 missing"}
    try:
        conn = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)
        _ensure_announcements_table(conn)
        with conn.cursor() as cur:
            slug = item.get("slug") or f"{item.get('category','misc')}-{datetime.date.today().isoformat()}"
            cur.execute("""
                INSERT INTO announcements_feed (category, title, excerpt, url, source, payload, slug)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE SET
                    title = EXCLUDED.title,
                    excerpt = EXCLUDED.excerpt,
                    url = EXCLUDED.url,
                    payload = EXCLUDED.payload,
                    published_at = NOW()
                RETURNING id;
            """, (
                item.get("category", "media"),
                item.get("title", ""),
                item.get("excerpt", ""),
                item.get("url", ""),
                item.get("source", "DC Hub"),
                json.dumps(item.get("payload", {}), default=str),
                slug,
            ))
            row_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return {"ok": True, "id": row_id, "slug": slug}
    except Exception as e:
        log.warning(f"publish_announcement err: {e}")
        return {"ok": False, "error": str(e)}


def publish_daily_brief(payload: dict, text: str) -> dict:
    """Convenience: persist today's DChub Daily brief as an announcement."""
    date = payload.get("date") or str(datetime.date.today())
    return publish_announcement({
        "category": "daily-brief",
        "title": f"DC Hub Daily Brief · {date}",
        "excerpt": text[:280],
        "url": f"/api/v1/social/posts/{date}",
        "source": "DC Hub Media",
        "slug": f"daily-{date}",
        "payload": {"text": text, "markets_count": len(payload.get("markets") or [])},
    })


def maybe_publish_press_release(payload: dict, threshold: float = 15.0) -> list[dict]:
    """If any DCPI score moved >threshold WoW, auto-publish a press release.

    Reads the dcpi movers data and emits one press release per significant mover.
    """
    import os
    results = []
    try:
        import urllib.request, json as _json
        with urllib.request.urlopen(
            f"{DCHUB_API_BASE}/api/v1/dcpi/movers", timeout=10
        ) as r:
            movers_data = _json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        log.warning(f"dcpi/movers fetch err: {e}")
        return results

    movers = movers_data.get("movers") or movers_data.get("data") or movers_data if isinstance(movers_data, list) else []
    for m in movers:
        delta = abs(m.get("delta") or 0)
        if delta < threshold:
            continue
        market = m.get("market") or m.get("name") or ""
        slug = m.get("slug") or market.lower().replace(" ", "-")
        direction = "rises" if (m.get("delta") or 0) > 0 else "falls"
        result = publish_announcement({
            "category": "press",
            "title": f"DCPI score for {market} {direction} {delta:.1f} points week-over-week",
            "excerpt": f"The Data Center Power Index for {market} moved by {m.get('delta'):+.1f} points week-over-week, driven by changes in pipeline announcements, grid headroom, and energy pricing in the market.",
            "url": f"/news/dcpi-alert-{slug}",
            "source": "DCPI Alert",
            "slug": f"dcpi-alert-{slug}-{datetime.date.today().isoformat()}",
            "payload": {"market": market, "delta": m.get("delta"), "auto_generated": True},
        })
        results.append(result)
    return results
