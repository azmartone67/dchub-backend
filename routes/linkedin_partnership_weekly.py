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
        "headline": "DC Hub: the Switzerland model — open invitations published",
        "url": "https://dchub.cloud/partners",
        "body": """DC Hub is publishing an open invitation today to brokers, publications, facility databases, and analyst firms across the data-center industry. We're not announcing any signed deal — we're publicly extending an offer, under our Switzerland model: neutral, CC-BY-4.0, no channel conflict.

If you're DCHawk, DCByte, CBRE, JLL, Data Center Dynamics, Data Center Frontier, Gartner, IDC, Synergy Research, 451 Research, Omdia — or anyone else working in this space — there's a specific track ready for you at dchub.cloud/partners.

What we bring: live data layer beneath the static reports the rest of the industry publishes quarterly. 21,405 tracked facilities globally, 286 markets scored daily by DCPI, $324B+ in tracked M&A. Already cited by ChatGPT, Claude, Gemini, Perplexity, Cursor, and 90+ other AI platforms.

What we'd love in return: cross-licensing, editorial collaboration, co-branded research drops, or a paid MCP feed your brokers/analysts can query. Each track has one specific opening ask — no decks, no NDAs.

Read the six open invitations and tell us which one resonates: dchub.cloud/partners""",
    },
    {
        "slug": "dchawk",
        "anchor": "#dchawk",
        "headline": "DC Hub publishes open invitation: bidirectional facility exchange with DCHawk",
        "url": "https://dchub.cloud/partners#dchawk",
        "body": """We don't have a partnership with DCHawk. We're publishing an open invitation for one — under the Switzerland model.

DCHawk's depth on North-American sub-markets is sharp. DC Hub's depth on LATAM + APAC + EMEA-non-FLAP, plus M&A pipeline + grid intelligence + AI-citation flow, is sharp the other way. Complementary, not competing.

Our specific opening offer: a 30-day data-exchange pilot. We'd expose 500 of our highest-confidence non-FLAP facilities via DCHawk's API; we'd ask DCHawk to expose 500 of their highest-confidence FLAP records via ours. Both sides report join-rate + value lifted. No money changes hands during the pilot. A written commercial framework only triggers if both sides see >15% incremental coverage.

We have 21,405 tracked facilities globally. 286 markets scored daily by DCPI. 1,972 M&A deals tracked. We're cited by 96+ AI platforms.

Try our data right now — no signup, CC-BY-4.0:
→ /api/v1/facilities?country=GB&limit=10
→ /api/v1/dcpi/scores?limit=25
→ /.well-known/mcp.json

To anyone at DCHawk reading this — partnerships@dchub.cloud. The full open invitation lives at dchub.cloud/partners#dchawk.""",
    },
    {
        "slug": "cbre",
        "anchor": "#cbre",
        "headline": "DC Hub publishes open invitation: live data feed for CBRE Data Center Solutions",
        "url": "https://dchub.cloud/partners#cbre",
        "body": """We don't have a partnership with CBRE. We're publishing an open invitation for one — under our Switzerland model. No channel conflict; we're not in brokerage and never will be.

CBRE's semi-annual Data Center Trends report is the most-cited piece in the industry. We admire it — and we think we can complement it. Our DCPI generates a similar dataset live, daily, CC-BY-4.0, AI-agent native. 286 markets vs the tier-1 focus of broker reports. International coverage (AESO, Hydro-Québec, Nord Pool) that's not in the broker decks.

Our specific opening offer: a 90-day MCP-server pilot for one CBRE Data Center Solutions team. We'd set up Slack or Microsoft Copilot integration in 48 hours so CBRE brokers can query the data inside their existing workflow. Free during the pilot; standard CBRE+ feed pricing after.

For any CBRE broker reading this — try the data right now, no login required:
→ /reports/quarterly-deep · live quarterly equivalent
→ /dcpi · 14 BUILD, 64 AVOID, 141 CAUTION markets today
→ /transactions · 1,972 historical M&A deals, $324B+ tracked
→ /dcpi/intl · AESO + Hydro-Québec + Nord Pool

partnerships@dchub.cloud · the full open invitation lives at dchub.cloud/partners#cbre""",
    },
    {
        "slug": "dcd",
        "anchor": "#dcd",
        "headline": "DC Hub publishes open invitation: live data for Data Center Dynamics editorial",
        "url": "https://dchub.cloud/partners#dcd",
        "body": """We don't have a partnership with DCD. We're publishing an open invitation for one — under our Switzerland model. CC-BY-4.0 from day one, no exclusivity.

DCD has the audience and the editorial muscle. We have the live data behind the stories. Most industry pieces still cite a static report from CBRE, JLL, or Synergy that's already 6 months old by publication. DC Hub generates the same numbers fresh, refreshed every 24 hours.

Three feeds DCD's editorial team can pull right now, byline-cited use, CC-BY-4.0:
→ /reports/monthly · full DCPI rankings + M&A + supply pipeline
→ /changelog · daily press cadence
→ /hyperscaler-deals · $1B+ tracker with headlines + source URLs

Sample byline you could publish tomorrow: "Per DC Hub's live DCPI, Midlothian, TX leads BUILD rankings at composite 48.0, edging Williston, ND (47.6) and Cheyenne, WY (47.0)..."

Our specific opening offer: one co-branded data piece for an upcoming DCD print issue or event. We'd provide a 1-page dataset; DCD would wrap the editorial. Both names byline. Free. If engagement works, optional recurring monthly piece.

For anyone at DCD reading this — editorial@dchub.cloud · the full open invitation lives at dchub.cloud/partners#dcd""",
    },
    {
        "slug": "jll",
        "anchor": "#jll",
        "headline": "DC Hub publishes open invitation: parallel data feed for JLL Data Centers",
        "url": "https://dchub.cloud/partners#jll",
        "body": """We don't have a partnership with JLL. We're publishing an open invitation for one — same Switzerland model offer we're extending to CBRE, in parallel. We don't compete with either; we feed both.

What we'd love to offer JLL: a JLL-branded portal into DC Hub's live intelligence layer. Pipeline + M&A intelligence — we're tracking $324B+ historical deal volume + live pipeline. A JLL-co-branded "Market Velocity" report quarterly powered by our dataset. Lead-share split on pocket-listing inquiries from JLL-actively-brokered metros.

Our specific opening offer: a 90-day MCP pilot for one JLL Data Centers regional team (we'd suggest Americas given our coverage density). Same shape as the CBRE invitation — runs in parallel, isolated data planes, no leak across.

For any JLL broker reading this — try the data right now, no login:
→ /dcpi (286 markets, daily verdicts)
→ /transactions (1,972 deals)
→ /reports/quarterly-deep (live H2-equivalent)

partnerships@dchub.cloud · the full open invitation lives at dchub.cloud/partners#jll""",
    },
    {
        "slug": "dcf",
        "anchor": "#dcf",
        "headline": "DC Hub publishes open invitation: live data widgets for Data Center Frontier",
        "url": "https://dchub.cloud/partners#dcf",
        "body": """We don't have a partnership with Data Center Frontier. We're publishing an open invitation for one — same Switzerland-model offer we're extending to DCD.

DCF's reports lean on industry-source citations — JLL, CBRE, Synergy. We're the upstart citation source growing fastest in AI-platform reach. We'd love to offer something simple: a "DCF Live Data" widget — a paragraph at the bottom of every DCF article auto-populated from our API ("As of today, DC Hub tracks 21,405 operational facilities globally, +XX in the last 30 days...").

Other ideas on the table: weekly market-pulse newsletter co-distribution. Joint annual deep-dive sponsorship — e.g. "Where the next 10 GW of AI capacity is actually breaking ground" with us providing the dataset, DCF the editorial polish.

Our specific opening offer: one sponsored research piece this quarter, our dataset + DCF's editorial. Footer attribution + link-back. Free.

For anyone at DCF reading this — editorial@dchub.cloud · the full open invitation lives at dchub.cloud/partners#dcf""",
    },
    {
        "slug": "dcbyte",
        "anchor": "#dcbyte",
        "headline": "DC Hub publishes open invitation: EMEA capacity exchange with DCByte",
        "url": "https://dchub.cloud/partners#dcbyte",
        "body": """We don't have a partnership with DCByte. We're publishing an open invitation for one — under our Switzerland model, no exclusivity, CC-BY-4.0.

DCByte's EMEA capacity dataset is the regional standard. Our European footprint is thinner (1,400 facilities across 16 countries). Our intelligence layers (grid, M&A pipeline, AI-citation footprint) are deeper than what DCByte exposes today.

Regional cross-licensing could make both sides bigger. We'd offer DCByte access to our intelligence layers + AI citation reach. In return, we'd ask for authoritative EMEA capacity rows. Joint AI distribution — DCByte's brand cited alongside ours in every Claude/ChatGPT/Gemini EMEA answer about data centers.

Our specific opening offer: one Frankfurt or London market as proof-of-concept. DCByte contributes their full capacity dataset for that metro; we power their public-facing market page with our intelligence layers. Co-branded URL: dcbyte.com/market/frankfurt with footer "Intelligence layer by DC Hub." 60-day trial.

For anyone at DCByte reading this — partnerships@dchub.cloud · the full open invitation lives at dchub.cloud/partners#dcbyte""",
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
