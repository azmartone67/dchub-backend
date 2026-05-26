"""
linkedin_partnership_weekly.py — weekly LinkedIn post spotlighting one
partnership track + the /partners URL anchors.

Phase ZZZZZ-round47.14 (2026-05-25). Quad rotation runs 4 posts/day
about DCPI/hyperscaler/AI capex/industry pulse — none about /partners.
This module adds a separate weekly post that cycles through the 6
partnership tracks (DCHawk, DCByte, DCD, DCF, CBRE, JLL) so each
anchor on /partners gets one dedicated LinkedIn post every 6 weeks,
with a 7th catch-all post for /partners overall.

Cron-wired in cron_heartbeat.py to fire Wednesdays at 14:00-14:09 UTC
(10 AM ET — peak LinkedIn engagement). Idempotency via DB row:
won't double-post in the same ISO week.

  POST /api/v1/linkedin-partnership/run        fire the current week
  GET  /api/v1/linkedin-partnership/status     recent posts + next cycle

Anchors cycle in priority order; one rotates out per week.
"""
import datetime
import os
import sys
from contextlib import contextmanager
from flask import Blueprint, request, jsonify

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

linkedin_partnership_bp = Blueprint("linkedin_partnership_weekly", __name__,
                                     url_prefix="/api/v1/linkedin-partnership")


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try: yield c
    finally: c.close()


# ── Content ──────────────────────────────────────────────────────────
# Each entry: anchor slug, headline, body, landing URL.
# Body is ~900-1200 chars to fit LinkedIn's organic-favored sweet spot.

_TRACKS = [
    {
        "slug": "partners",
        "anchor": None,
        "headline": "The Switzerland of data center intelligence",
        "url": "https://dchub.cloud/partners",
        "body": """We don't compete with brokers, publications, or facility databases. We feed them.

DC Hub is the neutral, live data layer beneath the data-center research industry. The same backend ChatGPT, Claude, Gemini, and Perplexity cite when their users ask about data centers — and the same one we'll quietly power inside your stack, your newsletter, your dashboard, or your next research drop.

Six partnership tracks open right now:
→ DCHawk · bidirectional facility-data exchange
→ DCByte · EMEA cross-licensing
→ Data Center Dynamics · live data for editorial
→ Data Center Frontier · sponsored research
→ CBRE · MCP license for the broker team
→ JLL · parallel pilot, no channel conflict

No NDAs. CC-BY-4.0. Standard $9/mo dev tier, $199/mo PRO, custom enterprise. One conversation, six tracks: dchub.cloud/partners""",
    },
    {
        "slug": "dchawk",
        "anchor": "#dchawk",
        "headline": "DCHawk + DC Hub — bidirectional facility coverage",
        "url": "https://dchub.cloud/partners#dchawk",
        "body": """DCHawk's depth on North-American sub-markets is sharp. DC Hub's depth on LATAM + APAC + EMEA-non-FLAP, plus M&A pipeline + grid intelligence + AI-citation flow, is sharp the other way.

So here's the open ask: 30-day data-exchange pilot. We expose 500 of our highest-confidence non-FLAP facilities via your API. You expose 500 of your highest-confidence FLAP records via ours. Both sides report join-rate + value lifted. No money changes hands. Written framework only triggers if both sides see >15% incremental coverage.

We have 21,405 tracked facilities globally. 286 markets scored daily by DCPI. 1,972 M&A deals tracked. We're cited by 96+ AI platforms (ChatGPT, Claude, Gemini, Cursor, Cline, more).

Try the data right now — no signup:
→ /api/v1/facilities?country=GB&limit=10
→ /api/v1/dcpi/scores?limit=25
→ /.well-known/mcp.json

DCHawk team — partnerships@dchub.cloud. Full pitch at dchub.cloud/partners#dchawk""",
    },
    {
        "slug": "cbre",
        "anchor": "#cbre",
        "headline": "CBRE H2 2025 → DC Hub live equivalent (same data, no 6-month lag)",
        "url": "https://dchub.cloud/partners#cbre",
        "body": """CBRE's H2 2025 Data Center Trends report is the most-cited piece in the industry. It's also six months stale by the time it drops in November — and locked behind a CBRE-© license.

DC Hub publishes the same dataset live. Daily refresh. CC-BY-4.0. AI-agent native (29 MCP tools). 286 markets scored vs CBRE's tier-1-only set. International coverage on AESO + Hydro-Québec + Nord Pool that's not in any of the broker decks.

For CBRE brokers, the data is ready to query right now:
→ /reports/quarterly-deep · live quarterly equivalent
→ /dcpi · 14 BUILD, 64 AVOID, 141 CAUTION markets today
→ /transactions · 1,972 historical M&A deals, $324B+ tracked

The open ask: 90-day MCP-server pilot for one CBRE Data Center Solutions team. We set up Slack or Microsoft Copilot integration in 48 hours. Free during pilot; standard CBRE+ feed pricing after.

partnerships@dchub.cloud · full pitch at dchub.cloud/partners#cbre""",
    },
    {
        "slug": "dcd",
        "anchor": "#dcd",
        "headline": "Data Center Dynamics editorial — live numbers for every story",
        "url": "https://dchub.cloud/partners#dcd",
        "body": """DCD has the audience. We have the data. Most DCD pieces cite a static report (CBRE, JLL, Synergy) that's already 6 months old by publication. DC Hub generates the same numbers fresh, refreshed every 24 hours.

Three feeds your editorial team can pull right now, byline-cited use, CC-BY-4.0:
→ /reports/monthly · full DCPI rankings + M&A + supply pipeline
→ /changelog · daily press cadence (17 in last 30 days)
→ /hyperscaler-deals · $1B+ tracker with headlines + source URLs

Sample byline: "Per DC Hub's live DCPI, Midlothian, TX leads BUILD rankings at composite 48.0, edging Williston, ND (47.6) and Cheyenne, WY (47.0)..."

The open ask: one co-branded data piece for an upcoming DCD print issue or event. We provide a 1-page dataset, DCD wraps the editorial. Both names byline. Free. If engagement works, recurring monthly piece.

editorial@dchub.cloud · full pitch at dchub.cloud/partners#dcd""",
    },
    {
        "slug": "jll",
        "anchor": "#jll",
        "headline": "JLL Data Centers — parallel pilot, no channel conflict with CBRE",
        "url": "https://dchub.cloud/partners#jll",
        "body": """JLL competes head-on with CBRE in data center brokerage. We're Switzerland — both can have their own pipe to DC Hub's live intelligence layer, no shared data, no leaks across.

What JLL gets: dual-broker dashboard with JLL-branded portal, pipeline + M&A intelligence (we're tracking $324B+ historical deal volume + live pipeline), JLL-co-branded "Market Velocity" report quarterly powered by our dataset, lead-share split on pocket-listing inquiries from JLL-actively-brokered metros.

The open ask: 90-day MCP pilot for one JLL Data Centers regional team (we'd suggest Americas given our coverage density). Same shape as the CBRE pilot — runs in parallel, isolated data planes.

Try the data right now, no login:
→ /dcpi (286 markets, daily verdicts)
→ /transactions (1,972 deals)
→ /reports/quarterly-deep (live H2-equivalent)

partnerships@dchub.cloud · full pitch at dchub.cloud/partners#jll""",
    },
    {
        "slug": "dcf",
        "anchor": "#dcf",
        "headline": "Data Center Frontier — live data widgets in every article",
        "url": "https://dchub.cloud/partners#dcf",
        "body": """Same pattern as DCD with a North America focus. DCF reports lean heavily on industry-source citations — JLL, CBRE, Synergy. We're the upstart citation source growing fastest in AI-platform reach.

What DCF gets: "DCF Live Data" widget — a paragraph at the bottom of every DCF article auto-populated from our API ("As of today, DC Hub tracks 21,405 operational facilities globally, +XX in the last 30 days..."). Weekly market-pulse newsletter co-distribution. Joint annual deep-dive sponsorship — e.g. "AI Build-Out Pipeline Q3 2026" with us providing the dataset, DCF the editorial polish.

The open ask: one sponsored research piece this quarter. We'd suggest "Where the next 10 GW of AI capacity is actually breaking ground." We provide the dataset, DCF writes editorial. Footer attribution + link-back. Free.

editorial@dchub.cloud · full pitch at dchub.cloud/partners#dcf""",
    },
    {
        "slug": "dcbyte",
        "anchor": "#dcbyte",
        "headline": "DCByte — EMEA capacity exchange, joint AI citation",
        "url": "https://dchub.cloud/partners#dcbyte",
        "body": """DCByte's EMEA capacity dataset is the regional standard. Our European footprint is thinner (1,400 facilities across 16 countries). Our intelligence layers (grid, M&A pipeline, AI-citation footprint) are deeper than what DCByte exposes via their UI.

Regional cross-licensing makes both sides bigger: DCByte gets our intelligence layers + AI citation reach. We get authoritative EMEA capacity rows. Joint AI distribution — DCByte's brand cited alongside ours in every Claude/ChatGPT/Gemini EMEA answer.

The open ask: one Frankfurt or London market as proof-of-concept. They contribute their full capacity dataset for that metro; we power their public-facing market page with our intelligence layers. Co-branded URL: dcbyte.com/market/frankfurt with footer "Intelligence layer by DC Hub." 60-day trial.

partnerships@dchub.cloud · full pitch at dchub.cloud/partners#dcbyte""",
    },
]


def _current_iso_week():
    """ISO year-week tuple like (2026, 22) for idempotency keying."""
    today = datetime.date.today()
    iso = today.isocalendar()
    return iso.year, iso.week


def _pick_track():
    """Rotate through tracks by ISO week so each anchor fires once per
    7-week cycle. Index = isoweek % len(_TRACKS)."""
    _, week = _current_iso_week()
    return _TRACKS[week % len(_TRACKS)]


def _ensure_table():
    if not (_pg and _dsn()):
        return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS linkedin_partnership_posts (
                    id            SERIAL PRIMARY KEY,
                    iso_year      INT NOT NULL,
                    iso_week      INT NOT NULL,
                    track_slug    TEXT NOT NULL,
                    track_headline TEXT,
                    track_url     TEXT,
                    posted_at     TIMESTAMPTZ DEFAULT NOW(),
                    linkedin_urn  TEXT,
                    success       BOOLEAN,
                    error_msg     TEXT,
                    UNIQUE(iso_year, iso_week)
                )
            """)
    except Exception:
        pass


_ensure_table()


def _already_posted(iso_year, iso_week):
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT success FROM linkedin_partnership_posts
                 WHERE iso_year=%s AND iso_week=%s
            """, (iso_year, iso_week))
            r = cur.fetchone()
            return bool(r and r[0])
    except Exception:
        return False


def _record(iso_year, iso_week, track, result):
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO linkedin_partnership_posts
                  (iso_year, iso_week, track_slug, track_headline, track_url,
                   linkedin_urn, success, error_msg)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (iso_year, iso_week) DO UPDATE SET
                  track_slug=EXCLUDED.track_slug,
                  track_headline=EXCLUDED.track_headline,
                  track_url=EXCLUDED.track_url,
                  linkedin_urn=EXCLUDED.linkedin_urn,
                  success=EXCLUDED.success,
                  error_msg=EXCLUDED.error_msg,
                  posted_at=NOW()
            """, (iso_year, iso_week, track["slug"], track["headline"], track["url"],
                  (result or {}).get("urn"),
                  bool((result or {}).get("ok")),
                  (result or {}).get("error", "")[:500]))
    except Exception:
        pass


def _post_to_linkedin(text, landing_url):
    """Reuse linkedin_poster with link preview."""
    try:
        sys.path.insert(0, "/app")
        from linkedin_poster import post_to_linkedin as _do
        ok, result = _do(
            text=text,
            link_url=landing_url,
            link_title="DC Hub Partnerships",
            link_desc="The neutral live data layer · dchub.cloud/partners",
        )
        if isinstance(result, dict):
            return {"ok": bool(ok), "urn": result.get("urn"),
                    "error": result.get("error", "") if not ok else ""}
        return {"ok": bool(ok), "urn": str(result) if ok else None,
                "error": "" if ok else str(result)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


@linkedin_partnership_bp.route("/run", methods=["GET", "POST"])
def run():
    """Cron-callable. Fires the current ISO-week's partnership track."""
    iso_year, iso_week = _current_iso_week()
    bypass = (request.args.get("force") or "").lower() in ("1", "true", "yes")
    if not bypass and _already_posted(iso_year, iso_week):
        return jsonify({"skipped": True, "reason": "already_posted_this_week",
                         "iso_year": iso_year, "iso_week": iso_week}), 200

    track = _pick_track()
    if (request.args.get("slug") or "").strip():
        # Manual override: ?slug=cbre
        slug = request.args.get("slug").strip().lower()
        match = next((t for t in _TRACKS if t["slug"] == slug), None)
        if match: track = match

    text = f"{track['body']}\n\n{track['url']}"
    result = _post_to_linkedin(text, track["url"])
    _record(iso_year, iso_week, track, result)

    return jsonify({
        "ok":          result.get("ok"),
        "iso_year":    iso_year,
        "iso_week":    iso_week,
        "track":       track["slug"],
        "anchor":      track["anchor"],
        "url":         track["url"],
        "linkedin":    result,
        "at":          datetime.datetime.utcnow().isoformat() + "Z",
    }), 200 if result.get("ok") else 502


@linkedin_partnership_bp.route("/status", methods=["GET"])
def status():
    iso_year, iso_week = _current_iso_week()
    next_track = _pick_track()
    out = {
        "current_iso_week": f"{iso_year}-W{iso_week:02d}",
        "next_track":       next_track["slug"],
        "next_url":         next_track["url"],
        "total_tracks":     len(_TRACKS),
        "rotation_weeks":   len(_TRACKS),
        "tracks":           [{"slug": t["slug"], "url": t["url"],
                              "headline": t["headline"]} for t in _TRACKS],
    }
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT iso_year, iso_week, track_slug, success, posted_at, linkedin_urn
                  FROM linkedin_partnership_posts
                 ORDER BY iso_year DESC, iso_week DESC LIMIT 10
            """)
            out["recent"] = [{
                "iso_year": r[0], "iso_week": r[1], "track": r[2],
                "success": r[3],
                "posted_at": r[4].isoformat() if r[4] else None,
                "linkedin_urn": r[5],
            } for r in cur.fetchall()]
    except Exception:
        out["recent"] = []
    return jsonify(out), 200


@linkedin_partnership_bp.route("/preview", methods=["GET"])
def preview():
    """Return the post-text for inspection without posting."""
    slug = (request.args.get("slug") or "").strip().lower()
    if slug:
        track = next((t for t in _TRACKS if t["slug"] == slug), None)
        if not track:
            return jsonify({"error": "unknown_slug",
                            "available": [t["slug"] for t in _TRACKS]}), 400
    else:
        track = _pick_track()
    return jsonify({
        "slug":     track["slug"],
        "anchor":   track["anchor"],
        "url":      track["url"],
        "headline": track["headline"],
        "body":     track["body"],
        "char_count": len(track["body"]),
    }), 200
