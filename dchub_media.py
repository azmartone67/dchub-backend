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


def aggregate_announcements(limit_per_source: int = 20) -> dict:
    """Pulls news + press + testimonials into unified feed.

    Tries multiple table names per category for resilience to schema changes.
    """
    import os
    items = []
    debug = {"queries_tried": [], "queries_succeeded": []}

    try:
        import psycopg2
        url = os.environ.get("DATABASE_URL")
        if not url:
            return {"items": [], "count": 0, "categories": {}, "debug": {"error": "no DATABASE_URL"}}
        conn = psycopg2.connect(url, connect_timeout=8)
        with conn.cursor() as cur:

            # phase 197: also pull from our self-published announcements_feed
            try:
                debug["queries_tried"].append("announcements_feed")
                cur.execute(f"""
                    SELECT id, title, published_at, source, url, excerpt, category
                    FROM announcements_feed
                    WHERE published_at > NOW() - INTERVAL '90 days'
                    ORDER BY published_at DESC
                    LIMIT %s;
                """, (limit_per_source,))
                rows = cur.fetchall()
                if rows:
                    debug["queries_succeeded"].append(f"announcements_feed ({len(rows)} rows)")
                    for r in rows:
                        items.append({
                            "id": f"af-{r[0]}", "category": r[6] or "media",
                            "title": r[1] or "", "date": str(r[2]) if r[2] else "",
                            "source": r[3] or "", "url": r[4] or "",
                            "excerpt": (r[5] or "")[:200],
                        })
            except Exception as e:
                log.debug(f"announcements_feed err: {e}")
                conn.rollback()

            # News — try multiple table/column patterns
            news_candidates = [
                ("news", "SELECT id, title, published_date, source, url, summary FROM news ORDER BY published_date DESC NULLS LAST LIMIT %s"),
                ("news_articles", "SELECT id, title, published_at, source, url, summary FROM news_articles ORDER BY published_at DESC NULLS LAST LIMIT %s"),
                ("articles", "SELECT id, title, published_at, source, url, excerpt FROM articles ORDER BY published_at DESC NULLS LAST LIMIT %s"),
            ]
            for table_name, sql in news_candidates:
                try:
                    debug["queries_tried"].append(f"news:{table_name}")
                    cur.execute(sql, (limit_per_source,))
                    rows = cur.fetchall()
                    if rows:
                        debug["queries_succeeded"].append(f"news:{table_name} ({len(rows)} rows)")
                        for r in rows:
                            items.append({
                                "id": f"news-{r[0]}", "category": "news",
                                "title": r[1] or "", "date": str(r[2]) if r[2] else "",
                                "source": r[3] or "", "url": r[4] or "",
                                "excerpt": (r[5] or "")[:200],
                            })
                        break  # first success wins
                except Exception as e:
                    log.debug(f"news {table_name} err: {e}")
                    conn.rollback()

            # Press releases
            press_candidates = [
                ("press_releases", "SELECT id, title, date, slug, meta_description, category FROM press_releases ORDER BY date DESC LIMIT %s"),
                ("press", "SELECT id, title, published_at, slug, summary, type FROM press ORDER BY published_at DESC NULLS LAST LIMIT %s"),
                ("announcements", "SELECT id, title, created_at, slug, body, category FROM announcements ORDER BY created_at DESC LIMIT %s"),
            ]
            for table_name, sql in press_candidates:
                try:
                    debug["queries_tried"].append(f"press:{table_name}")
                    cur.execute(sql, (limit_per_source,))
                    rows = cur.fetchall()
                    if rows:
                        debug["queries_succeeded"].append(f"press:{table_name} ({len(rows)} rows)")
                        for r in rows:
                            items.append({
                                "id": f"press-{r[0]}", "category": "press",
                                "title": r[1] or "", "date": str(r[2]) if r[2] else "",
                                "source": "DC Hub",
                                "url": f"/news/{r[3]}" if r[3] else "/announcements",
                                "excerpt": (r[4] or "")[:200],
                            })
                        break
                except Exception as e:
                    log.debug(f"press {table_name} err: {e}")
                    conn.rollback()

            # Testimonials (we confirmed ai_testimonials exists with 1198 rows)
            try:
                debug["queries_tried"].append("testimonials:ai_testimonials")
                cur.execute("""
                    SELECT id, quote, source_name, created_at
                    FROM ai_testimonials
                    WHERE created_at > NOW() - INTERVAL '180 days'
                    ORDER BY created_at DESC
                    LIMIT 5;
                """)
                rows = cur.fetchall()
                if rows:
                    debug["queries_succeeded"].append(f"testimonials:ai_testimonials ({len(rows)} rows)")
                    for r in rows:
                        items.append({
                            "id": f"testimonial-{r[0]}", "category": "testimonial",
                            "title": f"AI Testimonial · {r[2] or 'AI agent'}",
                            "date": str(r[3]) if r[3] else "",
                            "source": r[2] or "AI", "url": "/testimonials",
                            "excerpt": (r[1] or "")[:280],
                        })
            except Exception as e:
                log.warning(f"testimonials err: {e}")
                conn.rollback()

        conn.close()
    except Exception as e:
        log.warning(f"aggregator failed: {e}")
        debug["fatal"] = str(e)


            # phase 225: DCPI movers as alerts (biggest score changes)
            try:
                debug["queries_tried"].append("dcpi-movers")
                cur.execute("""
                    SELECT market_slug, market_name, constraint_score, excess_power_score,
                           verdict, computed_at
                    FROM market_power_scores
                    WHERE computed_at > NOW() - INTERVAL '7 days'
                      AND excess_power_score IS NOT NULL
                    ORDER BY ABS(excess_power_score - 50) DESC
                    LIMIT 10;
                """)
                rows = cur.fetchall()
                if rows:
                    debug["queries_succeeded"].append(f"dcpi-movers ({len(rows)} alerts)")
                    for r in rows:
                        slug, name, c_score, e_score, verdict, ts = r
                        title_prefix = "🚨 ALERT" if verdict == "AVOID" else "🚀 BUILD" if verdict == "BUILD" else "👁️ CAUTION"
                        items.append({
                            "id": f"dcpi-{slug}",
                            "category": "alert",
                            "title": f"{title_prefix}: {name} DCPI {verdict.lower()} verdict",
                            "date": str(ts) if ts else "",
                            "source": "DCPI Engine",
                            "url": f"/dcpi/{slug}",
                            "excerpt": f"Constraint: {c_score or 0:.0f} · Excess Power: {e_score or 0:.0f}",
                        })
            except Exception as e:
                log.debug(f"dcpi-movers err: {e}")
                conn.rollback()

    items.sort(key=lambda i: i.get("date", ""), reverse=True)
    counts = {}
    for it in items:
        counts[it["category"]] = counts.get(it["category"], 0) + 1
    return {"items": items, "count": len(items), "categories": counts, "debug": debug}


# ── Auto-Publish to announcements_feed ─────────────────────────────

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
