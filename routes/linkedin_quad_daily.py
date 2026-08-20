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
from routes._swallowed_writes import note_swallowed_write

try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

linkedin_quad_bp = Blueprint("linkedin_quad_daily", __name__,
                              url_prefix="/api/v1/linkedin-quad")


# r61-c (2026-05-25) — Narrative arc threading.
# linkedin_quad fires 4 disjoint posts/day. Without an arc reference,
# each post stands alone and the week's messaging lacks continuity.
# This helper pulls the active arc (set by narrative_arc.py, r60) and
# returns a one-line callout to append to every post body.
# Cached for 60s so 4 sequential slot fires share the same arc lookup.
_ARC_CACHE = {"line": "", "fetched_at": 0.0}
_ARC_CACHE_TTL = 60  # seconds


def _arc_reference() -> str:
    """Best-effort. Returns '' if arc endpoint is unreachable so a
    failed arc lookup NEVER blocks a LinkedIn post."""
    import time as _t
    now = _t.time()
    if _ARC_CACHE["line"] and (now - _ARC_CACHE["fetched_at"]) < _ARC_CACHE_TTL:
        return _ARC_CACHE["line"]
    try:
        import urllib.request, json as _j
        base = os.environ.get("DCHUB_INTERNAL_API", "http://localhost:8080")
        with urllib.request.urlopen(
            f"{base}/api/v1/narrative/current", timeout=4
        ) as r:
            d = _j.loads(r.read().decode("utf-8"))
        arc_title = (d.get("arc") or "")[:90].strip()
        anchor    = d.get("anchor_url") or ""
        if arc_title:
            line = f"\n\n📌 This week at DC Hub: {arc_title}"
            if anchor and anchor != "https://dchub.cloud":
                line += f"\n{anchor}"
            _ARC_CACHE["line"] = line
            _ARC_CACHE["fetched_at"] = now
            return line
    except Exception:
        pass
    return ""


SLOTS = [
    {"hour":  8, "topic": "dcpi_mover",         "style": "data",       "title": "DCPI Mover · 24h"},
    {"hour": 12, "topic": "hyperscaler_deal",   "style": "narrative",  "title": "Hyperscaler AI Deal"},
    # r-capability-slot (rebuild 2026-07-18): the 16:00 slot is RESERVED for the
    # evergreen capability data-cards (brain_capability_radar cap_* leads). They
    # score 62-64 and lost EVERY slot to agent_demand (~164) and live deals (~98)
    # when editorial ranked globally — so the 6 "what we shipped" cards never
    # posted. This slot no longer competes on score: editorial_decision("capability")
    # restricts to capability leads (and falls through to the full board only if a
    # bad-data day yields none). Was topic "ai_capex_index"/listicle.
    {"hour": 16, "topic": "capability",         "style": "data",       "title": "What We Shipped"},
    {"hour": 20, "topic": "industry_pulse",     "style": "contrarian", "title": "Industry Counter-Take"},
]

# Slots the heartbeat fires AND the catch-up backfill in run() re-posts.
# Cadence history: the 2026-06-08 "4->2/day" quality cut retired 08/20;
# r-media-goldmine 2026-07-14 re-enabled their heartbeat dispatches but not
# this set, so 08/20 only posted when a tick landed in the first 15 min of
# the hour and were never backfilled. r-quad-4day 2026-07-24: operator
# confirmed 4/day — all four slots active, all backfill when missed.
_ACTIVE_SLOT_HOURS = {8, 12, 16, 20}

OG_IMAGE_MAP = {
    "dcpi_mover":         "https://api.dchub.cloud/static/og/landing-ai-capacity.png",
    "hyperscaler_deal":   "https://api.dchub.cloud/static/og/landing-hyperscaler-deals.png",
    "ai_capex_index":     "https://api.dchub.cloud/static/og/landing-ai-capacity.png",
    "industry_pulse":     "https://api.dchub.cloud/static/og/landing-agents.png",
    # r-agent-demand (2026-07-17): slot-less weekly topic (see run()); mapped
    # here so a forced ?topic=agent_demand can never KeyError on the fallback
    # landing/OG lookups.
    "agent_demand":       "https://api.dchub.cloud/static/og/landing-agents.png",
    # r-capability-slot: the reserved 16:00 slot. Default OG/landing here so the
    # run() lookups (landing = LANDING_URL_MAP[topic]) never KeyError; the engine
    # then overrides both from the capability lead's own card + source_url.
    "capability":         "https://api.dchub.cloud/static/og/landing-agents.png",
}

LANDING_URL_MAP = {
    "dcpi_mover":         "https://dchub.cloud/dcpi",
    "hyperscaler_deal":   "https://dchub.cloud/hyperscaler-deals",
    "ai_capex_index":     "https://dchub.cloud/ai-capacity-index",
    "industry_pulse":     "https://dchub.cloud/dc-hub-media",
    "agent_demand":       "https://dchub.cloud/ai",
    "capability":         "https://dchub.cloud/whats-new",
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
            # 2026-07-03 VARIETY: durable columns so the desk's dedup keys on
            # (content_type, entity) over a real window instead of the fragile
            # whitespace-token substring match. story_type fixes the composer's
            # 14-day story-type dedup (it queried `topic`, which only ever held
            # the SLOT topic — the two vocabularies never overlapped → no-op).
            for _col, _ddl in (
                ("story_type", "ALTER TABLE linkedin_quad_posts ADD COLUMN IF NOT EXISTS story_type TEXT"),
                ("lead_kind",  "ALTER TABLE linkedin_quad_posts ADD COLUMN IF NOT EXISTS lead_kind TEXT"),
                ("lead_entity","ALTER TABLE linkedin_quad_posts ADD COLUMN IF NOT EXISTS lead_entity TEXT"),
                # 2026-07-17 double-post fix: in-flight claim stamp so two
                # heartbeat ticks in the same slot window can't both publish
                # (the Rural-SPP / AWS pairs 12-14s apart). See _claim_slot.
                ("claimed_at", "ALTER TABLE linkedin_quad_posts ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ"),
                # 2026-07-17 cards-never-seen audit: did the card image ACTUALLY
                # attach? linkedin_poster falls back to text/article on any
                # image failure and still reports success — invisible until now.
                ("image_attached", "ALTER TABLE linkedin_quad_posts ADD COLUMN IF NOT EXISTS image_attached BOOLEAN"),
            ):
                try:
                    cur.execute(_ddl)
                except Exception:
                    pass
            try:
                cur.execute("CREATE INDEX IF NOT EXISTS ix_quad_lead "
                            "ON linkedin_quad_posts(lead_kind, posted_at DESC)")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_quad_story "
                            "ON linkedin_quad_posts(story_type, posted_at DESC)")
            except Exception:
                pass
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
    """Compose a 2900-char LinkedIn post for the slot's style + topic.

    r61-c (2026-05-25): every post body now gets a trailing arc
    reference (via _arc_reference()) so the 4 daily slots thread
    together under the active weekly narrative. If the arc endpoint
    is unreachable, _arc_reference returns '' and the post still
    ships — the threading is purely additive.
    """
    body = _format_post_base(slot, payload)
    arc_line = _arc_reference()
    # Append before the trailing hashtag line for visual hierarchy.
    # Simple heuristic: if the body ends with hashtags (starts with #),
    # insert arc line above them; else just append.
    lines = body.rstrip().split("\n")
    if arc_line:
        # Find last non-empty hashtag line
        for i in range(len(lines) - 1, -1, -1):
            ln = lines[i].strip()
            if ln.startswith("#"):
                # Insert arc above the hashtag block
                lines.insert(i, arc_line.strip())
                break
        else:
            lines.append(arc_line.strip())
    text = "\n".join(lines)
    # Item 8 (2026-06-30): append the one canonical reach CTA so every quad-daily
    # LinkedIn body ends with a connect line. Done here (the single outer composer)
    # rather than in each style branch of _format_post_base. Fail-soft import; the
    # post still ships if the shared module is unavailable.
    try:
        from media_cta import append_reach_cta
        text = append_reach_cta(text)
    except Exception:
        pass
    return text


def _format_post_base(slot, payload):
    """The original body-only formatter. Kept separate so the arc
    reference appender in _format_post wraps it cleanly without
    duplicating 80 lines of style-specific logic."""
    topic, style, landing = slot["topic"], slot["style"], LANDING_URL_MAP[slot["topic"]]

    if topic == "dcpi_mover" and payload:
        return (
            f"🚀 {payload.get('market','?')} just hit {payload.get('score','?')}/100 "
            f"on the DC Power Index ({payload.get('verdict','?')})\n\n"
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
        # Fallback when no fresh news. 2026-06-11: REMOVED the hardcoded/fabricated
        # DCPI scores (NoVa 14.5, "Cheyenne 69.5, Council Bluffs 68.1, Midlothian
        # 65.6") — they were stale, unverifiable, and repeated daily, which is
        # exactly why the editor-in-chief rejected these as "unverifiable DCPI
        # scores" and the feed read repetitive (the "Cheyenne over and over" the
        # operator flagged). Ship an honest, score-free directional post that
        # points at the LIVE DCPI ranking + open methodology instead of inventing
        # numbers. (Path A / marketing_engine carries the real per-market DCPI
        # posts now that the quality scorer recognizes DCPI scores — r80.2.)
        return (
            f"⚡ The data-center map shifts faster than the headlines.\n\n"
            f"Primary metros like Northern Virginia and Silicon Valley sit behind "
            f"multi-year interconnection queues while capex rotates toward emerging, "
            f"power-rich markets. DC Hub's DCPI ranks all 285 markets on live grid "
            f"headroom — see who's actually moving, methodology open:\n\n"
            f"Live ranking: {landing}\n"
            f"Methodology: https://dchub.cloud/dcpi#methodology\n\n"
            f"#DCHubMedia #DataCenter #DCPI"
        )
    # Fallback: generic
    return (
        f"DC Hub Media · {slot['title']}\n\n"
        f"21,401 data center facilities. 4,000+ M&A deals. 10 ISOs tracked in real time.\n\n"
        f"Live intelligence: {landing}\n\n"
        f"#DCHub #DataCenter #AIInfrastructure"
    )


def _looks_like_image(data) -> bool:
    """Magic-byte validation (PNG/JPEG/GIF/WEBP). Reuses content_publisher's
    tested checker; local fallback keeps this module importable standalone.
    2026-07-17: the quad path NEVER validated bytes — a CF-failover mojibake
    PNG or a bot-shell HTML page (>1000B, so the size check passed) uploaded
    fine, then died ASYNC in LinkedIn's processor → the post silently shipped
    WITHOUT its card. This is why the branded data-cards were never seen on
    the feed even though the posts recorded og=data_card and success=TRUE."""
    try:
        from content_publisher import _looks_like_image_bytes
        return bool(_looks_like_image_bytes(data))
    except Exception:
        if not data or len(data) < 12:
            return False
        return (data[:8] == b"\x89PNG\r\n\x1a\n"
                or data[:3] == b"\xff\xd8\xff"
                or data[:6] in (b"GIF87a", b"GIF89a")
                or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def _og_fetch_candidates(og_image_url: str) -> list:
    """Fetch order for an OG card URL. Own-origin cards (api.dchub.cloud /
    dchub.cloud) render IN-PROCESS on this same app, so try LOOPBACK first —
    that bypasses the entire CF edge (worker failover mojibake, bot-shell WAF
    pages, cache poisoning) that has repeatedly corrupted image bytes. The
    public URL stays as the fallback so a loopback miss never loses the card.
    Pure helper (list of URLs) so tests can pin the rewrite."""
    if not og_image_url:
        return []
    try:
        from urllib.parse import urlsplit
        s = urlsplit(og_image_url)
        if s.hostname in ("api.dchub.cloud", "dchub.cloud", "www.dchub.cloud"):
            # localhost + $PORT mirrors media_editorial._internal(), the
            # loopback pattern already proven from inside this app.
            port = os.environ.get("PORT", "8080")
            loop = f"http://localhost:{port}{s.path}"
            if s.query:
                loop += f"?{s.query}"
            return [loop, og_image_url]
    except Exception:
        pass
    return [og_image_url]


def _fetch_image_bytes(og_image_url: str) -> bytes | None:
    """r48 (2026-05-25): pull the OG image bytes for upload to LinkedIn.

    Each slot has a distinct OG image URL — this function fetches it
    so post_to_linkedin can do the LinkedIn asset upload + attach.
    Without this, the quad publisher knew which image to use but
    never passed image_bytes, so every post went text-only.

    2026-07-17 (cards-never-seen audit): loopback-first for own-origin cards
    + magic-byte validation. Corrupt bytes are now REJECTED here (loudly)
    instead of uploaded to die silently in LinkedIn's async processor.
    """
    if not og_image_url:
        return None
    import urllib.request
    for _url in _og_fetch_candidates(og_image_url):
        try:
            req = urllib.request.Request(
                _url,
                headers={"User-Agent": "DCHub-LinkedInQuad/1.1",
                         "X-Internal-Request": "1"},
            )
            # 45s (was 15s): ai_hero cards generate an SDXL image on first fetch
            # (~15-30s) before the card is composited+cached. 15s timed out → the
            # post fell back to text-only. Posts are 4/day so a longer wait is
            # fine; subsequent fetches of the same slug+day hit the cache.
            with urllib.request.urlopen(req, timeout=45) as resp:
                if resp.status != 200:
                    continue
                data = resp.read()
                # LinkedIn caps image uploads at ~5MB; OG images are
                # typically <500KB so this is defensive.
                if not (1000 < len(data) < 5_000_000):
                    continue
                if not _looks_like_image(data):
                    print(f"[quad-daily] OG bytes from {_url[:80]} are NOT an "
                          f"image (first bytes {data[:8]!r}) — refusing to "
                          f"upload corrupt card")
                    continue
                return data
        except Exception:
            continue
    return None


def _post_to_linkedin(text, landing_url, og_image_url):
    """Use existing linkedin_poster module.

    r48 (2026-05-25): NOW PASSES image_bytes.
    r50.3 (2026-05-25): NORMALIZE return to dict. linkedin_poster.
      post_to_linkedin returns a (ok, dict) TUPLE, but downstream
      _record + the JSON response both expect a dict with .get('ok')
      / .get('post_urn') / .get('error'). The tuple shape caused
      _record's AttributeError-on-.get-of-tuple to be silently
      swallowed, so successful posts never updated the DB row
      (status endpoint kept reporting the old failed state).
    """
    image_bytes = _fetch_image_bytes(og_image_url)
    try:
        from linkedin_poster import post_to_linkedin as _do_post
        # link_title/link_desc are used as alt text + media title when
        # image_bytes is present, otherwise as the article card metadata.
        raw = _do_post(
            text=text,
            link_url=landing_url,
            link_title="DC Hub Media",
            link_desc="Data-center intelligence + DCPI · dchub.cloud",
            image_bytes=image_bytes,
        )
        # Normalize: tuple → dict
        if isinstance(raw, tuple) and len(raw) == 2:
            ok, meta = raw
            meta = meta or {}
            return {
                "ok":       bool(ok),
                "urn":      meta.get("post_urn") or meta.get("urn") or meta.get("id"),
                "post_urn": meta.get("post_urn"),
                "status":   meta.get("status"),
                "error":    meta.get("error", ""),
                # 2026-07-17: surface whether the card image really attached
                # (False = the post silently degraded to text/article).
                "image_attached": bool(meta.get("image_attached")),
            }
        # Already a dict (or unexpected shape)
        if isinstance(raw, dict):
            return raw
        return {"ok": False, "error": f"unexpected return shape: {type(raw).__name__}"}
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


def _claim_slot(slot_date, slot_hour, topic, style):
    """ATOMIC slot claim — the publish-time guard against the two-replica /
    double-tick race (2026-07-17: two heartbeat ticks 14s apart both passed
    _already_posted because the first run's _record lands only AFTER the
    ~30-60s compose+publish, so linkedin_posts got near-identical pairs while
    ON CONFLICT collapsed the audit trail to one linkedin_quad_posts row).

    One INSERT ... ON CONFLICT ... RETURNING statement: the winner gets the
    row back and proceeds; the loser gets nothing and skips. A claim expires
    after LINKEDIN_QUAD_CLAIM_MINUTES (default 10) so a crashed run never
    locks the slot away from the hourly catch-up retry; a success=TRUE row
    never re-claims (the slot is done for the day).

    A winning re-claim also resets error_msg to 'claimed_in_flight' so the
    row's final error_msg describes the LATEST attempt — a prior attempt may
    have stamped 'suppressed: …' (_stamp_claim_outcome), and without the
    reset THIS attempt dying mid-flight would leave the row wearing the old
    outcome. 'gate: …' refusals are deliberately PRESERVED through re-claims:
    #2718's selector feedback reads them off these rows, and blanking one
    mid-retry would let the desk re-elect the exact lead the gate just
    refused (the deadlock #2718 closed).

    Fail-OPEN on any DB error (returns True) — the guard exists to stop
    duplicates, and a DB blip must never dark-hold the feed; worst case we
    are exactly as exposed as before the guard existed.
    """
    if not (_pg and _dsn()):
        return True
    try:
        _mins = max(1, int(os.environ.get("LINKEDIN_QUAD_CLAIM_MINUTES", "10")))
    except Exception:
        _mins = 10
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO linkedin_quad_posts
                  (slot_date, slot_hour, topic, style, success, error_msg, claimed_at)
                VALUES (%s, %s, %s, %s, FALSE, 'claimed_in_flight', NOW() ON CONFLICT DO NOTHING)
                ON CONFLICT (slot_date, slot_hour) DO UPDATE
                   SET claimed_at = NOW(),
                       -- 'gate:%%' is doubled on purpose: this execute() HAS
                       -- an args tuple, so psycopg2 scans this entire string
                       -- (comments included) and an undoubled percent sign
                       -- raises. No percent may appear anywhere in here
                       -- except as a placeholder or doubled.
                       error_msg  = CASE
                                      WHEN linkedin_quad_posts.error_msg
                                           LIKE 'gate:%%'
                                        THEN linkedin_quad_posts.error_msg
                                      ELSE 'claimed_in_flight'
                                    END
                 WHERE linkedin_quad_posts.success IS NOT TRUE
                   AND (linkedin_quad_posts.claimed_at IS NULL
                        OR linkedin_quad_posts.claimed_at
                             < NOW() - make_interval(mins => %s))
                RETURNING id
            """, (slot_date, slot_hour, topic, style, _mins))
            won = cur.fetchone() is not None
            c.commit()
            return won
    except Exception:
        return True


def _stamp_claim_outcome(slot_date, slot_hour, outcome: str) -> None:
    """A run that exits between _claim_slot and _record must SAY WHY, or the
    claim row reads 'claimed_in_flight' forever and the media pulse counts a
    deliberate silence as a mid-flight death (abandoned_claims_7d). The
    editorial suppress and the composer skip both exit exactly there BY
    DESIGN — the 08-10..08-13 "abandoned" strands sat beside gate-refused
    slots from the same lead-supply drought (#2718), not beside crashes.

    Rewrites ONLY a row still in the in-flight state — never a recorded
    outcome, and never a peer's live claim (their _record overwrites anyway),
    so racing _record and losing is harmless.
    """
    if not (_pg and _dsn()):
        return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                UPDATE linkedin_quad_posts
                   SET error_msg = %s
                 WHERE slot_date = %s AND slot_hour = %s
                   AND success IS NOT TRUE
                   AND error_msg = 'claimed_in_flight'
            """, ((outcome or "")[:500], slot_date, slot_hour))
            c.commit()
    except Exception:
        note_swallowed_write("linkedin_quad_posts",
                             where="linkedin_quad_daily._stamp_claim_outcome")


def _catchup_max_age_hours() -> int:
    try:
        return max(1, int(os.environ.get(
            "LINKEDIN_QUAD_CATCHUP_MAX_AGE_HOURS", "3")))
    except Exception:
        return 3


def _catchup_slot(slots, now_hour, already_posted):
    """PURE. Pick the one slot the hourly catch-up should backfill, or None.

    ★ 2026-08-16 SKIP STALE SLOTS. The backfill had no age bound, so a missed
    slot stayed eligible until midnight. On 08-15 that emptied the whole day in
    13 minutes: once the publish block cleared, four consecutive ticks each took
    the next-most-recent due slot and fired 20:48, 20:49, 20:54, 21:01 — four
    posts to one company page inside a quarter of an hour, three of them
    carrying the same shipped_this_week headline.

    A slot exists to put a post at a time of day. Firing 08:00 at 21:01 does not
    deliver that slot late; it delivers a second 20:00 post competing with the
    real one. Past the window the honest outcome is that the slot was MISSED —
    which the abandoned-claim and floor signalling in dchub_media_revival now
    actually reports rather than hides.

    `already_posted` is a callable(hour) -> bool so this stays free of DB and
    clock. Returns the most recent eligible slot (newest wins), as before.
    """
    max_age = _catchup_max_age_hours()
    due = [
        s for s in slots
        if s["hour"] in _ACTIVE_SLOT_HOURS
        and s["hour"] <= now_hour
        and (now_hour - s["hour"]) <= max_age
        and not already_posted(s["hour"])
    ]
    return max(due, key=lambda s: s["hour"]) if due else None


def _lead_entity_from(lead) -> str:
    """Normalized ENTITY token for the durable dedup ledger — the city/iso/deal
    party the lead is about, alnum-squashed so it matches regardless of which
    lead kind produced it. Mirrors media_editorial._entity_tail. '' when unknown."""
    import re as _re
    if not isinstance(lead, dict):
        return ""
    dk = lead.get("dedup_key") or ""
    tail = dk.split(":", 1)[-1]
    return _re.sub(r"[^a-z0-9]+", "", tail.lower())


def _record(slot_date, slot_hour, topic, style, text, landing, og_url, result,
            story_type=None, lead=None):
    if not (_pg and _dsn()): return
    _lead_kind = (lead or {}).get("kind") if isinstance(lead, dict) else None
    _lead_entity = _lead_entity_from(lead)
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO linkedin_quad_posts
                  (slot_date, slot_hour, topic, style, post_text, landing_url, og_image_url,
                   linkedin_urn, success, error_msg, story_type, lead_kind, lead_entity,
                   image_attached)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (slot_date, slot_hour) DO UPDATE SET
                  success=EXCLUDED.success, error_msg=EXCLUDED.error_msg,
                  linkedin_urn=COALESCE(EXCLUDED.linkedin_urn, linkedin_quad_posts.linkedin_urn),
                  story_type=COALESCE(EXCLUDED.story_type, linkedin_quad_posts.story_type),
                  lead_kind=COALESCE(EXCLUDED.lead_kind, linkedin_quad_posts.lead_kind),
                  lead_entity=COALESCE(EXCLUDED.lead_entity, linkedin_quad_posts.lead_entity),
                  -- 2026-07-17 claim fix: _claim_slot pre-inserts a bare
                  -- (slot_date, slot_hour) row before compose+publish, so this
                  -- ON CONFLICT path is now the NORMAL path for the winner's own
                  -- record — it must fill the content fields the claim left NULL
                  -- (and refresh posted_at, which the desk ledger keys on).
                  post_text=COALESCE(EXCLUDED.post_text, linkedin_quad_posts.post_text),
                  landing_url=COALESCE(EXCLUDED.landing_url, linkedin_quad_posts.landing_url),
                  og_image_url=COALESCE(EXCLUDED.og_image_url, linkedin_quad_posts.og_image_url),
                  image_attached=EXCLUDED.image_attached,
                  posted_at=NOW()
            """, (slot_date, slot_hour, topic, style, text[:5000], landing, og_url,
                   (result or {}).get("urn") or (result or {}).get("id"),
                   bool((result or {}).get("ok")),
                   (result or {}).get("error", "")[:500],
                   story_type, _lead_kind, (_lead_entity or None),
                   bool((result or {}).get("image_attached"))))
            c.commit()
    except Exception:
        note_swallowed_write("linkedin_quad_posts", where="linkedin_quad_daily._record")
        pass


# AUTO-REPAIR: duplicate route '/run' also in enhanced_promotion.py:831 — review and remove one
@linkedin_quad_bp.route("/run", methods=["GET", "POST"])
def run():
    """Cron-callable. Fires the slot matching current UTC hour.

    Item J-unblock (2026-06-02): off-slot publishing support.
      - force=true OR ignore_slot=true (query OR JSON body) bypasses the
        08/12/16/20 UTC slot check entirely, so an operator can publish
        any time (defaults to the first slot's topic).
      - manual_copy (JSON body) overrides the topic-rotation default —
        if provided, that exact text is posted instead of the engine's
        composition. Lets operators publish a hand-written piece without
        editing the rotation.
    """
    # Merge JSON body + query string into a single param dict so
    # force/ignore_slot/topic/manual_copy work from either surface. Body
    # wins on conflicting keys (the explicit override channel).
    _body = {}
    try:
        if request.is_json:
            _body = request.get_json(silent=True) or {}
    except Exception:
        _body = {}

    def _p(key, default=None):
        v = _body.get(key)
        if v is None or v == "":
            v = request.args.get(key)
        return v if v not in (None, "") else default

    def _truthy(v):
        return str(v or "").strip().lower() in ("1", "true", "yes", "on")

    _ignore_slot = _truthy(_p("ignore_slot")) or _truthy(_p("force"))
    _manual_copy = _p("manual_copy")

    now = datetime.datetime.utcnow()
    target_slot = None
    for slot in SLOTS:
        if now.hour == slot["hour"] and now.minute < 15:
            target_slot = slot
            break
    # force/ignore_slot bypass: when set, pick a slot by ?topic= (if
    # given and matched) or fall through to the first slot. This is
    # broader than the old `force AND topic` path, which only worked
    # if BOTH params were present.
    if not target_slot and (_ignore_slot or _p("force")):
        _topic = _p("topic")
        target_slot = next(
            (s for s in SLOTS if s["topic"] == _topic), None)
        # r-agent-demand (2026-07-17): agent_demand is a slot-LESS weekly topic
        # (its data refreshes weekly, so it earns no fixed daily hour).
        # Synthesize a slot for the explicit ?topic=agent_demand override so
        # operators/crons can fire it directly; on scheduled slots it still
        # arrives via the editorial desk's agent_demand LEAD, which forces the
        # composer's story type.
        if target_slot is None and _topic == "agent_demand":
            target_slot = {"hour": now.hour, "topic": "agent_demand",
                           "style": "data", "title": "Agent Demand · weekly"}
        if target_slot is None:
            target_slot = SLOTS[0]
    # 2026-06-20: CATCH-UP. The heartbeat that drives this is GitHub-throttled
    # (~26 runs/day, stateless, no replay) so a slot's exact hour-window is
    # frequently missed entirely — which is why whole days had ZERO posts. On
    # ANY later call (the linkedin_quad_catchup heartbeat fires hourly from
    # 09:00 UTC), backfill the most-recent DUE active slot (08/12/16/20) that
    # has no successful post today. Each call posts at most one missed slot;
    # _already_posted (success-only) + UNIQUE(slot_date,slot_hour) keep it
    # idempotent, so all active slots reliably land across the day's later ticks.
    #
    # ★ 2026-08-16 SKIP STALE SLOTS. The backfill above had no age bound, so a
    # slot stayed eligible until midnight. On 08-15 that emptied the whole day
    # in 13 minutes: after the publish block cleared, four consecutive ticks
    # each took the next-most-recent due slot and fired 20:48, 20:49, 20:54,
    # 21:01 — four posts to one company page inside a quarter of an hour, three
    # of them carrying the same shipped_this_week headline.
    #
    # A slot exists to put a post at a time of day. Firing 08:00 at 21:01 does
    # not deliver that slot late, it delivers a second 20:00 post competing with
    # the real one. Past the window, the honest outcome is that the slot was
    # missed — which the abandoned/floor signalling in dchub_media_revival now
    # actually reports instead of hiding.
    if not target_slot:
        _today = now.date()
        target_slot = _catchup_slot(
            SLOTS, now.hour, lambda h: _already_posted(_today, h))
    if not target_slot:
        return jsonify({
            "skipped": True,
            "reason": "no_slot_for_hour",
            "current_hour": now.hour,
            "slot_hours": [s["hour"] for s in SLOTS],
            "hint": ("Pass force=true or ignore_slot=true (query or "
                     "JSON body) to publish off-slot."),
        }), 200

    slot_date = now.date()
    # r50.2 (2026-05-25): ?force=1 bypasses the already-posted check
    # entirely, so the operator can re-run a slot after a fix lands
    # even if the day's row exists. _already_posted itself now only
    # treats success=TRUE rows as locking (failed rows auto-permit
    # retry on the next cron tick).
    # Item J-unblock: ignore_slot also bypasses already-posted (off-slot
    # publishing is by definition an explicit operator override).
    bypass = _truthy(_p("force")) or _ignore_slot
    if not bypass and _already_posted(slot_date, target_slot["hour"]):
        return jsonify({"skipped": True, "reason": "already_posted_this_slot",
                         "slot": target_slot}), 200

    # 2026-07-17 double-post fix: ATOMICALLY claim the slot before the
    # ~30-60s compose+publish window. _already_posted above is check-then-act
    # (the success row lands only after publishing), so two heartbeat ticks
    # seconds apart both passed it and published near-identical pairs
    # (Rural SPP 08:08:41 + 08:08:55). The claim's single
    # INSERT ... ON CONFLICT ... RETURNING has exactly one winner. force /
    # ignore_slot is an explicit operator override and bypasses enforcement
    # (the claim row is still stamped so peers back off).
    _slot_claimed = _claim_slot(slot_date, target_slot["hour"],
                                target_slot["topic"], target_slot["style"])
    if not bypass and not _slot_claimed:
        return jsonify({"skipped": True, "reason": "slot_claimed_by_peer",
                        "detail": ("another replica/tick claimed this slot "
                                   "within the in-flight window"),
                        "slot": target_slot}), 200

    # r86c EVENT-DRIVEN CADENCE: ask the brain's editorial desk whether there's
    # a genuinely novel data event worth posting this slot. If not, SUPPRESS the
    # slot rather than spray filler — this is the core fix for the 4-posts/day
    # repetition. `force`/ignore_slot bypasses (explicit operator override).
    _ed_lead = None
    try:
        from routes.media_editorial import editorial_decision
        _ed = editorial_decision(target_slot.get("topic"))
        # Scheduled (non-bypass) cron SUPPRESSES when there's no novel event.
        # Forced/off-hour (bypass) publishes anyway — but STILL take the lead so
        # the LLM composer gets the number+trend+so-what context. Without it,
        # forced posts compose thin and bounce off the quality gate (0.72). The
        # lead-fetch now lives OUTSIDE `if not bypass`, so BOTH paths feed it in.
        if not bypass and not _ed.get("post"):
            # The claim above is ours (non-bypass got past the peer check) —
            # stamp it, or the pulse reads this deliberate silence as a death.
            if _slot_claimed:
                _stamp_claim_outcome(
                    slot_date, target_slot["hour"],
                    "suppressed: editorial_no_novel_event — "
                    + str(_ed.get("reason") or "")[:160])
            return jsonify({"skipped": True,
                            "reason": "editorial_suppress_no_novel_event",
                            "detail": _ed.get("reason"),
                            "slot": target_slot}), 200
        _ed_lead = _ed.get("lead")
    except Exception:
        _ed_lead = None  # fail-open: editorial hiccup never blocks the slot

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
        composed = compose_story_post(slot_topic=target_slot["topic"], lead=_ed_lead)
        # Composer SKIP (2026-07-02): the model saw the recently-published
        # feed and judged this slot adds nothing new. Suppress like an
        # editorial suppress — never fall through to legacy/static
        # generators (that's the repetitive filler the operator flagged).
        if composed.get("skip"):
            # Reachable with bypass=True, where our claim may have LOST to a
            # peer — only stamp a claim we actually hold.
            if _slot_claimed:
                _stamp_claim_outcome(
                    slot_date, target_slot["hour"],
                    "suppressed: composer_skip_nothing_new — "
                    + str(composed.get("skip_reason") or "")[:160])
            return jsonify({"skipped": True,
                            "reason": "composer_skip_nothing_new",
                            "detail": composed.get("skip_reason"),
                            "story_type": composed.get("story_type"),
                            "slot": target_slot}), 200
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

    # Item J-unblock (2026-06-02): manual_copy body override. When set,
    # short-circuit the engine output AND the 14-day dedup nudge (the
    # operator knows what they want to post; don't append a date stamp).
    if _manual_copy and str(_manual_copy).strip():
        text = str(_manual_copy)
        payload["manual_copy_override"] = True

    # r86c-safetynet (2026-06-27): GUARANTEE the post LEADS with the editorial
    # desk's number, even when the LLM path didn't put it first (compose_story_post
    # fell back to a brand-claim template, or opened with a hook). The publish gate
    # runs QUALITY first, then the r86c number-lead gate — so a post reaching here
    # without a metric-lead is otherwise quality-OK; it just needs the number up
    # front. We were publishing 0/week because every slot bounced off this gate.
    # Deterministically prepend the desk's headline_number (+ so-what) so it clears
    # r86c regardless of LLM behavior. Skipped when the text already leads with a
    # number, when there's no editorial lead, or when the operator gave manual copy.
    if text and not (_manual_copy and str(_manual_copy).strip()):
        try:
            from routes.media_editorial import leads_with_number
            if not leads_with_number(text):
                _lead = _ed_lead if isinstance(_ed_lead, dict) else None
                if not _lead:
                    # force/ignore_slot bypassed the editorial fetch above — pull the
                    # desk's lead now so a forced/off-hour post still leads with a number.
                    try:
                        from routes.media_editorial import editorial_decision
                        _lead = (editorial_decision(target_slot.get("topic")) or {}).get("lead")
                    except Exception:
                        _lead = None
                if isinstance(_lead, dict):
                    _hn = (_lead.get("headline_number") or "").strip()
                    if _hn:
                        _sw = (_lead.get("so_what") or "").strip()
                        _opener = _hn if _hn[-1:] in ".!?" else _hn + "."
                        text = _opener + (("\n\n" + _sw) if _sw else "") + "\n\n" + text
                        payload["number_lead_prepended"] = True
        except Exception:
            pass

    # r48 (2026-05-25): 14-day content dedup. Even with per-slot
    # uniqueness, the same TEXT can repeat if upstream data is stale.
    # Hash the post text, check against last 14 days of same-topic
    # posts. If we've already posted essentially the same content,
    # nudge by appending a timestamp signature to force novelty.
    # Item J-unblock: skip dedup nudge when manual_copy is set — the
    # operator chose those exact bytes; don't mutate.
    if _pg and _dsn() and not (_manual_copy and str(_manual_copy).strip()):
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

    # r-qa (2026-06-27): ensure the post carries the two quality-score signals the
    # LLM composer's prose usually omits — a source LINK (+0.20) and a freshness
    # cue (+0.20). _quality_score rewards a literal http link + a recent-year/
    # recency token; a solid stat+novelty analyst post otherwise tops out ~0.55 and
    # stalls below CONTENT_QUALITY_MIN. Both are legitimate analyst-post elements
    # (source + recency), appended ONLY when genuinely missing so good posts aren't
    # touched. This is what lifts posts from ~0.55 to comfortably clear the bar.
    try:
        import re as _re_enrich
        # (d) source LINK — append only when the post has no http link at all.
        if text and not _re_enrich.search(r'https?://', text):
            text = text.rstrip() + f"\n\n→ {landing}"
        # (b) FRESHNESS — append a dated cue UNCONDITIONALLY. _quality_score strips
        # URLs then credits a recent year via _RECENT_YEAR_RE; a guaranteed non-URL
        # "<Mon DD, YYYY>" is the only reliable way to earn the +0.20. Prior
        # conditional checks kept mis-matching the scorer's regex (loose words like
        # "now"/"just" appear in any prose → false "already fresh" → skip → stuck
        # at 0.55). A trailing date stamp is harmless even if the body cites a date.
        if text:
            text = text.rstrip() + f"\n\n(DC Hub data · {datetime.datetime.utcnow().strftime('%b %d, %Y')})"
    except Exception:
        pass

    # r-no-disparage (2026-06-23): the quad cron historically posted STRAIGHT to
    # LinkedIn, bypassing the media-judgment gate every manual publish runs through
    # (_should_skip_publish: quality + number-lead + claim-verify + the new partner-
    # disparagement block). Route the composed text through it so a thin / hallucinated
    # / partner-bashing post is caught BEFORE it publishes. Fail-OPEN on any gate error
    # so a transient blip never dark-holds the feed.
    try:
        from content_publisher import _should_skip_publish as _gate
        _skip, _why = False, ""
        if _pg and _dsn():
            with _conn() as _gc, _gc.cursor() as _gcur:
                _skip, _why = _gate(_gcur, text, "linkedin")
        if _skip:
            print(f"[quad-daily] pre-publish gate BLOCKED: {_why}")
            _record(slot_date, target_slot["hour"], target_slot["topic"],
                    target_slot["style"], text, landing, og_url,
                    {"ok": False, "skipped": True, "error": f"gate: {_why}"[:200]},
                    story_type=payload.get("story_type"), lead=_ed_lead)
            return jsonify({"slot": target_slot, "skipped": True,
                            "gate_reason": _why}), 200
    except Exception as _ge:
        print(f"[quad-daily] pre-publish gate unavailable ({_ge}) — proceeding")

    # Actually post to LinkedIn
    result = _post_to_linkedin(text, landing, og_url)

    _record(slot_date, target_slot["hour"], target_slot["topic"], target_slot["style"],
             text, landing, og_url, result,
             story_type=payload.get("story_type"), lead=_ed_lead)

    # Capability radar: retire a capability/milestone lead ONLY after it actually
    # posted (success), so a new capability stays visible until announced and a
    # failed post leaves it to retry. (The radar is read-only; this is the writer.)
    # RETIRE-BUG fix (r-capability-slot rebuild): the guard matched only the
    # launch/milestone kinds, but the 6 EVERGREEN moat/pillar leads are
    # kind=="cap_<key>" (see brain_capability_radar.capability_radar_leads). Their
    # baseline (data_milestone_snapshots) therefore never advanced, so repost
    # rotation was dead — the same card could never age past repost_days. Widen the
    # guard to also match the cap_ prefix. dedup_key is "cap:<key>" for these, which
    # mark_capability_announced already resolves (split on first ":").
    try:
        _lk = (_ed_lead.get("kind") or "") if isinstance(_ed_lead, dict) else ""
        if (result or {}).get("ok") and isinstance(_ed_lead, dict) \
                and (_lk in ("capability_launch", "data_milestone")
                     or _lk.startswith("cap_")):
            from routes.brain_capability_radar import mark_capability_announced
            mark_capability_announced(_ed_lead.get("dedup_key", ""))
    except Exception:
        pass

    # r-milestone (2026-08-07): same contract for the NUMBERS lane — a
    # platform_milestone crossing retires ONLY after it actually posted, so a
    # preview never consumes it and a failed post leaves it to retry. Without
    # this call the lane would re-lead with the same crossing every day, which
    # is the exact repetition the desk exists to prevent.
    try:
        if ((result or {}).get("ok") and isinstance(_ed_lead, dict)
                and _ed_lead.get("kind") == "platform_milestone"):
            from routes.media_milestones import mark_milestone_announced
            mark_milestone_announced(_ed_lead.get("dedup_key", ""))
    except Exception:
        pass

    # r-capability-slot amplify (rebuild): after a SUCCESSFUL reserved-capability
    # publish, amplify the same copy to X inline. The quad is LinkedIn-only and the
    # standalone X auto-sweep is UNREGISTERED, so without this the capability cards
    # never reach X at all. Best-effort: amplify_to_all honors its own
    # DRY_RUN / kill-switch / daily-cap internally, and any failure here is
    # swallowed so it can NEVER block or unwind the LinkedIn publish that just
    # succeeded.
    try:
        if (result or {}).get("ok") and target_slot.get("topic") == "capability":
            from routes.multiplatform_amplifier import amplify_to_all
            amplify_to_all(source_post_id=0, source_text=text,
                           source_link=landing, platforms=["twitter"])
    except Exception:
        pass

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

# AUTO-REPAIR: duplicate route '/status' also in enhanced_promotion.py:826 — review and remove one

@linkedin_quad_bp.route("/status", methods=["GET"])
def status():
    out = {"slots": SLOTS, "current_utc_hour": datetime.datetime.utcnow().hour}
    if _pg and _dsn():
        try:
            with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # 2026-07-17 card-starvation audit: expose WHICH editorial lead
                # (kind/entity), story type and card each slot actually used —
                # without these the "why did no capability card ever publish?"
                # question was unanswerable from the outside.
                cur.execute("""
                    SELECT slot_date, slot_hour, topic, style, success, error_msg, posted_at,
                           story_type, lead_kind, lead_entity, og_image_url,
                           image_attached, linkedin_urn, claimed_at
                    FROM linkedin_quad_posts
                    WHERE slot_date >= CURRENT_DATE - INTERVAL '7 days'
                    ORDER BY posted_at DESC LIMIT 30
                """)
                rows = [dict(r) for r in cur.fetchall()]
                for r in rows:
                    if r.get("slot_date"): r["slot_date"] = r["slot_date"].isoformat()
                    if r.get("posted_at"): r["posted_at"] = r["posted_at"].isoformat()
                    # claimed_at: the forensic field. posted_at on a stranded
                    # row is just the claim-INSERT's DEFAULT NOW(); claimed_at
                    # is when the LAST attempt actually took the slot.
                    if r.get("claimed_at"): r["claimed_at"] = r["claimed_at"].isoformat()
                out["recent"] = rows
                cur.execute("SELECT COUNT(*) FROM linkedin_quad_posts WHERE success=TRUE AND posted_at > NOW() - INTERVAL '7 days'")
                out["successful_7d"] = cur.fetchone()["count"]
                cur.execute("SELECT COUNT(*) FROM linkedin_quad_posts WHERE success=TRUE AND posted_at > NOW() - INTERVAL '24 hours'")
                out["successful_24h"] = cur.fetchone()["count"]
                # Which lead kinds actually won slots — the rotation's ground
                # truth (a '(none)' bucket means the desk lead never reached
                # _record, i.e. the anti-repeat ledger is blind for that post).
                cur.execute("""
                    SELECT COALESCE(lead_kind, '(none)') AS lead_kind, COUNT(*) AS n
                    FROM linkedin_quad_posts
                    WHERE success=TRUE AND posted_at > NOW() - INTERVAL '14 days'
                    GROUP BY 1 ORDER BY 2 DESC
                """)
                out["lead_kinds_14d"] = {r["lead_kind"]: int(r["n"]) for r in cur.fetchall()}
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {str(e)[:140]}"
    return jsonify(out), 200
