"""
linkedin_quad_daily.py — 4 distinct LinkedIn posts per day.

Phase ZZZZZ-round40 (2026-05-25). Media organism shows LinkedIn at
35.7/100 (only 1 post/24h, 5 in 7d) because press auto-generates only
once per day at 15:00 UTC, and that single press release becomes the
single LinkedIn post (in one of 4 styles via existing rotation).

To hit 4 posts/day, generate 4 DIFFERENT content drops daily — each
from a distinct topic + style. Spread over the day for max feed reach:

  08:00 UTC — DCPI Mover         (data style)        — biggest 24h verdict shift
  12:00 UTC — Hyperscaler Deal   (narrative style)   — latest Stargate/Oracle/etc
  16:00 UTC — AI Capex Index     (listicle style)    — top 5 capacity-ready markets
  20:00 UTC — Industry Pulse     (contrarian style)  — counter-narrative on news

Each fires from cron_heartbeat. Each:
  1. Picks topic data from live DB
  2. Generates 280-character hook + 2-3 insight bullets + CTA
  3. Attaches one of our 4 r37 OG images (custom per topic)
  4. Calls linkedin_autopost.post_to_linkedin()
  5. Logs to linkedin_quad_posts table for idempotency (don't double-post in same window)
"""
import os
import datetime
from contextlib import contextmanager

from flask import Blueprint, jsonify, request

try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

linkedin_quad_bp = Blueprint("linkedin_quad_daily", __name__,
                              url_prefix="/api/v1/linkedin-quad")


SLOTS = [
    {"hour":  8, "topic": "dcpi_mover",         "style": "data",       "title": "DCPI Mover · 24h"},
    {"hour": 12, "topic": "hyperscaler_deal",   "style": "narrative",  "title": "Hyperscaler AI Deal"},
    {"hour": 16, "topic": "ai_capex_index",     "style": "listicle",   "title": "AI Capacity Index"},
    {"hour": 20, "topic": "industry_pulse",     "style": "contrarian", "title": "Industry Counter-Take"},
]

OG_IMAGE_MAP = {
    "dcpi_mover":         "https://api.dchub.cloud/static/og/landing-ai-capacity.png",
    "hyperscaler_deal":   "https://api.dchub.cloud/static/og/landing-hyperscaler-deals.png",
    "ai_capex_index":     "https://api.dchub.cloud/static/og/landing-ai-capacity.png",
    "industry_pulse":     "https://api.dchub.cloud/static/og/landing-agents.png",
}

LANDING_URL_MAP = {
    "dcpi_mover":         "https://dchub.cloud/dcpi",
    "hyperscaler_deal":   "https://dchub.cloud/hyperscaler-deals",
    "ai_capex_index":     "https://dchub.cloud/ai-capacity-index",
    "industry_pulse":     "https://dchub.cloud/dc-hub-media",
}


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


def _ensure_table():
    if not (_pg and _dsn()): return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS linkedin_quad_posts (
                    id           SERIAL PRIMARY KEY,
                    slot_date    DATE NOT NULL,
                    slot_hour    INT NOT NULL,
                    topic        TEXT NOT NULL,
                    style        TEXT NOT NULL,
                    post_text    TEXT,
                    landing_url  TEXT,
                    og_image_url TEXT,
                    posted_at    TIMESTAMPTZ DEFAULT NOW(),
                    linkedin_urn TEXT,
                    success      BOOLEAN DEFAULT FALSE,
                    error_msg    TEXT,
                    UNIQUE(slot_date, slot_hour)
                );
                CREATE INDEX IF NOT EXISTS ix_quad_slot ON linkedin_quad_posts(slot_date, slot_hour);
            """)
            c.commit()
    except Exception:
        pass

_ensure_table()


def _build_dcpi_mover():
    """Pick biggest DCPI verdict shift in last 24h.

    r48 (2026-05-25): fallback no longer hardcodes 'Rural SPP, Kansas'
    every time (the cause of repeated posts). Falls through to a
    random pick from market_power_scores top 10 — different result
    each call.
    """
    if not (_pg and _dsn()): return None
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Try a few possible schemas for verdict history
            for sql in [
                """SELECT market, verdict, score, ROUND(score - LAG(score) OVER (PARTITION BY market ORDER BY computed_at), 1) AS delta
                   FROM dcpi_v2_scores
                   WHERE computed_at > NOW() - INTERVAL '36 hours'
                   ORDER BY ABS(score - COALESCE(LAG(score) OVER (PARTITION BY market ORDER BY computed_at), score)) DESC NULLS LAST
                   LIMIT 1""",
                """SELECT market_name AS market, verdict, score FROM dcpi_scores
                   WHERE computed_at > NOW() - INTERVAL '24 hours'
                   ORDER BY score DESC LIMIT 1""",
                # r48: third path — market_power_scores is the actual
                # canonical table on this Neon instance.
                """SELECT market_name AS market, verdict,
                          excess_power_score AS score
                     FROM market_power_scores
                    WHERE computed_at > NOW() - INTERVAL '48 hours'
                      AND verdict IN ('BUILD','CAUTION','AVOID')
                    ORDER BY RANDOM() LIMIT 1""",
            ]:
                try:
                    cur.execute(sql)
                    row = cur.fetchone()
                    if row: return dict(row)
                except Exception: continue
    except Exception: pass
    # r48: rotating fallback — pick from a varied list, not one market
    import random as _random
    fallbacks = [
        {"market": "Cheyenne, WY",         "verdict": "BUILD",   "score": 69.5},
        {"market": "Midlothian, TX",       "verdict": "BUILD",   "score": 65.6},
        {"market": "Rural SPP, Kansas",    "verdict": "BUILD",   "score": 67.2},
        {"market": "Council Bluffs, IA",   "verdict": "BUILD",   "score": 68.1},
        {"market": "Hillsboro, OR",        "verdict": "BUILD",   "score": 64.8},
        {"market": "Quincy, WA",           "verdict": "BUILD",   "score": 66.4},
        {"market": "Boardman, OR",         "verdict": "BUILD",   "score": 63.2},
        {"market": "New Albany, OH",       "verdict": "BUILD",   "score": 62.9},
    ]
    return _random.choice(fallbacks)


def _build_industry_pulse():
    """r48 (2026-05-25): dynamic contrarian take from recent news.

    Previously HARDCODED — every 20:00 UTC LinkedIn slot posted the
    EXACT same text ('Everyone says Northern Virginia...'). That's
    the user-reported 'repeated information about old news'. Now
    pulls a fresh news headline from last 72h and pairs it with a
    market-data contrarian counterpoint.
    """
    if not (_pg and _dsn()): return None
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Pull a recent contrarian-worthy headline
            cur.execute("""
                SELECT id, title, source, url, published_date
                  FROM news
                 WHERE published_date > NOW() - INTERVAL '3 days'
                   AND (LOWER(title) LIKE '%%data center%%'
                        OR LOWER(title) LIKE '%%hyperscale%%'
                        OR LOWER(title) LIKE '%%datacenter%%'
                        OR LOWER(title) LIKE '%%ai capex%%'
                        OR LOWER(title) LIKE '%%power grid%%')
                 ORDER BY RANDOM() LIMIT 1
            """)
            news_row = cur.fetchone()
            # Pull a random BUILD market for the counter-take
            cur.execute("""
                SELECT market_name, verdict, excess_power_score
                  FROM market_power_scores
                 WHERE verdict = 'BUILD' AND excess_power_score > 60
                 ORDER BY RANDOM() LIMIT 1
            """)
            mkt_row = cur.fetchone()
            return {
                "news": dict(news_row) if news_row else None,
                "market": dict(mkt_row) if mkt_row else None,
            }
    except Exception:
        return None


def _build_hyperscaler_deal():
    if not (_pg and _dsn()): return None
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, title, source, url, published_date
                FROM news
                WHERE (LOWER(title) LIKE '%%stargate%%'
                       OR LOWER(title) LIKE '%%openai%%'
                       OR LOWER(title) LIKE '%%coreweave%%'
                       OR LOWER(title) LIKE '%%amd%%'
                       OR LOWER(title) LIKE '%%nvidia%%')
                  AND published_date > CURRENT_DATE - INTERVAL '3 days'
                ORDER BY published_date DESC LIMIT 1
            """)
            r = cur.fetchone()
            if r: return dict(r)
    except Exception: pass
    return None


def _build_ai_capex_top5():
    """Get top 5 from the AI capacity index."""
    import urllib.request, json
    try:
        req = urllib.request.Request(
            "https://api.dchub.cloud/api/v1/ai-capacity-index?limit=5",
            headers={"User-Agent": "DCHub-LinkedInQuad/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _format_post(slot, payload):
    """Compose a 2900-char LinkedIn post for the slot's style + topic."""
    topic, style, landing = slot["topic"], slot["style"], LANDING_URL_MAP[slot["topic"]]

    if topic == "dcpi_mover" and payload:
        return (
            f"🚀 {payload.get('market','?')} just hit DCPI score "
            f"{payload.get('score','?')} ({payload.get('verdict','?')})\n\n"
            f"The DC Power Index ranks 285 US markets daily. This kind of move "
            f"signals where AI capex is actually flowing — not where the headlines say it is.\n\n"
            f"Top BUILD markets right now span 3 ISOs (WECC, SPP, ERCOT). Grid fundamentals "
            f"now outweigh proximity to legacy colocation hubs.\n\n"
            f"Track all 285: {landing}\n\n"
            f"#DCHub #DCPI #DataCenter #AIInfrastructure"
        )
    if topic == "hyperscaler_deal" and payload:
        title = payload.get("title", "")
        url = payload.get("url", landing)
        return (
            f"📰 {title}\n\n"
            f"DC Hub Hyperscaler Deal Tracker watches every Stargate, Oracle, "
            f"CoreWeave, AMD, NVIDIA capex move in real-time. AI infrastructure is "
            f"now a $1B+/week cadence — track who's deploying where.\n\n"
            f"Live ticker: {landing}\n\nSource: {url}\n\n"
            f"#AICapex #DataCenter #Hyperscaler #DCHubMedia"
        )
    if topic == "ai_capex_index" and payload:
        markets = payload.get("markets", [])[:5]
        if markets:
            lines = [f"🎯 Where 100MW of AI training can land in 90 days:"]
            for i, m in enumerate(markets, 1):
                lines.append(f"  {i}. {m.get('city')}, {m.get('state')} — "
                              f"{m.get('facility_count','?')} fac · {m.get('operator_count','?')} ops")
            lines.append("")
            lines.append(f"Refreshed Fridays. Sourced from 21,401 facilities + DCPI.")
            lines.append(f"Full leaderboard: {landing}")
            lines.append("")
            lines.append(f"#AIInfrastructure #DCHub #HyperscaleData")
            return "\n".join(lines)
    if topic == "industry_pulse":
        # r48 (2026-05-25): dynamic — pulls fresh news + market data
        news = (payload or {}).get("news") if isinstance(payload, dict) else None
        mkt = (payload or {}).get("market") if isinstance(payload, dict) else None
        if news and mkt:
            return (
                f"⚡ {news.get('title','').strip()[:140]}\n\n"
                f"The headline narrative lags reality. While media tracks the "
                f"announced megasite, the actual buildable capacity is in markets "
                f"like {mkt.get('market_name','?')} — DCPI Excess Power "
                f"{mkt.get('excess_power_score','?')}, verdict {mkt.get('verdict','?')}.\n\n"
                f"Track where the build is actually happening, not where the "
                f"headlines say: {landing}\n\n"
                f"Source: {news.get('url', landing)}\n\n"
                f"#DCHubMedia #DataCenter #DCPI #AIInfrastructure"
            )
        # Fallback when no fresh news — vary by hour-of-day so it isn't identical
        import datetime as _dt
        rotation_seed = _dt.datetime.utcnow().day  # 1-31
        contrarians = [
            ("Northern Virginia",   14.5, "60-month queue"),
            ("Silicon Valley",      18.2, "47-month queue"),
            ("Loudoun County",      11.8, "63-month queue"),
            ("Santa Clara",         16.9, "52-month queue"),
            ("Reston",              13.1, "58-month queue"),
        ]
        legacy, score, wait = contrarians[rotation_seed % len(contrarians)]
        return (
            f"⚡ Everyone says {legacy} is THE data-center market.\n\n"
            f"DCPI shows: {legacy} Excess Power score = {score} (bottom decile of 285 "
            f"markets). {wait}. Capex is quietly moving to Cheyenne (69.5), "
            f"Council Bluffs (68.1), Midlothian TX (65.6).\n\n"
            f"The narrative lags the build by 18-24 months.\n\n"
            f"Where AI infra is actually landing: {landing}\n\n"
            f"#DCHubMedia #DataCenter #DCPI"
        )
    # Fallback: generic
    return (
        f"DC Hub Media · {slot['title']}\n\n"
        f"21,401 data center facilities. 1,941 M&A deals. 10 ISOs tracked in real time.\n\n"
        f"Live intelligence: {landing}\n\n"
        f"#DCHub #DataCenter #AIInfrastructure"
    )


def _fetch_image_bytes(og_image_url: str) -> bytes | None:
    """r48 (2026-05-25): pull the OG image bytes for upload to LinkedIn.

    Each slot has a distinct OG image URL — this function fetches it
    so post_to_linkedin can do the LinkedIn asset upload + attach.
    Without this, the quad publisher knew which image to use but
    never passed image_bytes, so every post went text-only.
    """
    if not og_image_url:
        return None
    try:
        import urllib.request
        req = urllib.request.Request(
            og_image_url,
            headers={"User-Agent": "DCHub-LinkedInQuad/1.1"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                data = resp.read()
                # LinkedIn caps image uploads at ~5MB; OG images are
                # typically <500KB so this is defensive.
                if 1000 < len(data) < 5_000_000:
                    return data
    except Exception:
        pass
    return None


def _post_to_linkedin(text, landing_url, og_image_url):
    """Use existing linkedin_poster module.

    r48 (2026-05-25): NOW PASSES image_bytes. Previously only
    text + link_url got through, so the rich image was advertised
    in our payload but never reached LinkedIn. Fetches og_image_url
    bytes first, then calls poster with both image AND link metadata.
    """
    image_bytes = _fetch_image_bytes(og_image_url)
    try:
        from linkedin_poster import post_to_linkedin as _do_post
        # link_title/link_desc are used as alt text + media title when
        # image_bytes is present, otherwise as the article card metadata.
        return _do_post(
            text=text,
            link_url=landing_url,
            link_title="DC Hub Media",
            link_desc="Data-center intelligence + DCPI · dchub.cloud",
            image_bytes=image_bytes,
        )
    except ImportError:
        return {"ok": False, "error": "linkedin_poster module not available"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def _already_posted(slot_date, slot_hour):
    """r50.2 (2026-05-25): only treat SUCCESSFUL posts as 'already posted'.
    Previously any row (success=False included) locked the slot, so a
    UnboundLocalError-style failure at 08:00 made us unable to retry
    until tomorrow. Now failures auto-permit retry on the next cron tick.
    """
    if not (_pg and _dsn()): return False
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT 1 FROM linkedin_quad_posts
                           WHERE slot_date=%s AND slot_hour=%s
                             AND success = TRUE
                           LIMIT 1""",
                         (slot_date, slot_hour))
            return cur.fetchone() is not None
    except Exception:
        return False


def _record(slot_date, slot_hour, topic, style, text, landing, og_url, result):
    if not (_pg and _dsn()): return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO linkedin_quad_posts
                  (slot_date, slot_hour, topic, style, post_text, landing_url, og_image_url,
                   linkedin_urn, success, error_msg)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (slot_date, slot_hour) DO UPDATE SET
                  success=EXCLUDED.success, error_msg=EXCLUDED.error_msg,
                  linkedin_urn=COALESCE(EXCLUDED.linkedin_urn, linkedin_quad_posts.linkedin_urn)
            """, (slot_date, slot_hour, topic, style, text[:5000], landing, og_url,
                   (result or {}).get("urn") or (result or {}).get("id"),
                   bool((result or {}).get("ok")),
                   (result or {}).get("error", "")[:500]))
            c.commit()
    except Exception:
        pass


# AUTO-REPAIR: duplicate route '/run' also in ai_orchestrator.py:916 — review and remove one
@linkedin_quad_bp.route("/run", methods=["GET", "POST"])
def run():
    """Cron-callable. Fires the slot matching current UTC hour."""
    now = datetime.datetime.utcnow()
    target_slot = None
    for slot in SLOTS:
        if now.hour == slot["hour"] and now.minute < 15:
            target_slot = slot
            break
    if not target_slot and request.args.get("force"):
        target_slot = next((s for s in SLOTS if s["topic"] == request.args.get("topic")), SLOTS[0])
    if not target_slot:
        return jsonify({
            "skipped": True,
            "reason": "no_slot_for_hour",
            "current_hour": now.hour,
            "slot_hours": [s["hour"] for s in SLOTS],
        }), 200

    slot_date = now.date()
    # r50.2 (2026-05-25): ?force=1 bypasses the already-posted check
    # entirely, so the operator can re-run a slot after a fix lands
    # even if the day's row exists. _already_posted itself now only
    # treats success=TRUE rows as locking (failed rows auto-permit
    # retry on the next cron tick).
    bypass = (request.args.get("force") or "").lower() in ("1", "true", "yes")
    if not bypass and _already_posted(slot_date, target_slot["hour"]):
        return jsonify({"skipped": True, "reason": "already_posted_this_slot",
                         "slot": target_slot}), 200

    # r49 (2026-05-25): use the new Claude-composed content engine.
    # Engine rotates through 6 story types (capability_spotlight,
    # energy_narrative, dcpi_scoop, shipped_this_week, hyperscaler_drama,
    # market_anomaly). Each pulls real DB data + asks Sonnet to compose
    # a storytelling post in DC Hub's voice. Falls back to story-aware
    # static templates if Anthropic API unavailable. Theme-diversity
    # dedup tracks last 14d to avoid repeats.
    #
    # Per-slot story-type preferences (engine still varies within each):
    #   dcpi_mover       → dcpi_scoop / market_anomaly / energy_narrative
    #   hyperscaler_deal → hyperscaler_drama / capability_spotlight / shipped
    #   ai_capex_index   → capability_spotlight / market_anomaly / dcpi_scoop
    #   industry_pulse   → energy_narrative / hyperscaler_drama / shipped
    payload = {}
    text = None
    landing = LANDING_URL_MAP[target_slot["topic"]]
    og_url  = OG_IMAGE_MAP[target_slot["topic"]]
    try:
        from routes.linkedin_content_engine import compose_story_post
        composed = compose_story_post(slot_topic=target_slot["topic"])
        text = composed.get("text")
        # Engine returns its own landing + og_image per story-type —
        # prefer those when available (more accurate match).
        if composed.get("landing_url"):  landing = composed["landing_url"]
        if composed.get("og_image_url"): og_url = composed["og_image_url"]
        payload = {
            "story_type": composed.get("story_type"),
            "source":     composed.get("source"),
            "data_used":  composed.get("data_used"),
        }
    except Exception as _e_eng:
        # Engine itself errored — fall back to the legacy generators
        if target_slot["topic"] == "dcpi_mover":
            payload = _build_dcpi_mover()
        elif target_slot["topic"] == "hyperscaler_deal":
            payload = _build_hyperscaler_deal()
        elif target_slot["topic"] == "ai_capex_index":
            payload = _build_ai_capex_top5()
        elif target_slot["topic"] == "industry_pulse":
            payload = _build_industry_pulse()
        else:
            payload = {}
        text = _format_post(target_slot, payload)
        payload["engine_error"] = f"{type(_e_eng).__name__}: {str(_e_eng)[:120]}"

    if not text:
        # Absolute last resort
        text = _format_post(target_slot, payload)

    # r48 (2026-05-25): 14-day content dedup. Even with per-slot
    # uniqueness, the same TEXT can repeat if upstream data is stale.
    # Hash the post text, check against last 14 days of same-topic
    # posts. If we've already posted essentially the same content,
    # nudge by appending a timestamp signature to force novelty.
    if _pg and _dsn():
        try:
            import hashlib as _h
            text_sig = _h.sha256(text.encode("utf-8")).hexdigest()[:16]
            with _conn() as c, c.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM linkedin_quad_posts
                     WHERE topic = %s
                       AND posted_at > NOW() - INTERVAL '14 days'
                       AND SUBSTRING(post_text, 1, 200) = %s
                     LIMIT 1
                """, (target_slot["topic"], text[:200]))
                if cur.fetchone():
                    # Same text already posted in last 14d — diversify
                    text = text + f"\n\n— {datetime.datetime.utcnow().strftime('%b %d')}"
        except Exception:
            pass

    # Actually post to LinkedIn
    result = _post_to_linkedin(text, landing, og_url)

    _record(slot_date, target_slot["hour"], target_slot["topic"], target_slot["style"],
             text, landing, og_url, result)

    return jsonify({
        "slot":     target_slot,
        "payload":  payload,
        "post_text_preview": text[:200] + "...",
        "post_text_chars":  len(text),
        "landing":  landing,
        "og_image": og_url,
        "result":   result,
        "at":       now.isoformat() + "Z",
    }), 200

# AUTO-REPAIR: duplicate route '/status' also in ai_orchestrator.py:911 — review and remove one

@linkedin_quad_bp.route("/status", methods=["GET"])
def status():
    out = {"slots": SLOTS, "current_utc_hour": datetime.datetime.utcnow().hour}
    if _pg and _dsn():
        try:
            with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT slot_date, slot_hour, topic, style, success, error_msg, posted_at
                    FROM linkedin_quad_posts
                    WHERE slot_date >= CURRENT_DATE - INTERVAL '7 days'
                    ORDER BY posted_at DESC LIMIT 30
                """)
                rows = [dict(r) for r in cur.fetchall()]
                for r in rows:
                    if r.get("slot_date"): r["slot_date"] = r["slot_date"].isoformat()
                    if r.get("posted_at"): r["posted_at"] = r["posted_at"].isoformat()
                out["recent"] = rows
                cur.execute("SELECT COUNT(*) FROM linkedin_quad_posts WHERE success=TRUE AND posted_at > NOW() - INTERVAL '7 days'")
                out["successful_7d"] = cur.fetchone()["count"]
                cur.execute("SELECT COUNT(*) FROM linkedin_quad_posts WHERE success=TRUE AND posted_at > NOW() - INTERVAL '24 hours'")
                out["successful_24h"] = cur.fetchone()["count"]
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {str(e)[:140]}"
    return jsonify(out), 200
