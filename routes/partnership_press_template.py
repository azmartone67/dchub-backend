"""
partnership_press_template.py — weekly partnership-themed press release.

Phase ZZZZZ-round47.15 (2026-05-25). LinkedIn quad runs daily about
DCPI/hyperscaler/AI capex/industry pulse. LinkedIn partnership rotation
runs Wednesdays about /partners anchors. This adds a parallel press
release that fires Tuesdays so the cadence is:

  Daily LinkedIn        — 4× /day at 08/12/16/20 UTC (the quad)
  Tuesday press release — partnership-themed, one anchor / week
  Wednesday LinkedIn    — partnership-themed, same anchor (amplifies the press)
  Press → LinkedIn → email outreach (next module) flow into one another

The press release is inserted into the press_releases table so it
shows up on /press-release/<slug>, /news, /changelog, RSS feeds, etc.
Brain v2 then picks it up and threads it through the daily LinkedIn
quad's "shipped this week" topic next time that slot fires.

Routes:
  POST /api/v1/partnerships/press/run         create this week's press
  GET  /api/v1/partnerships/press/preview     preview the markup
  GET  /api/v1/partnerships/press/status      recent + next track
"""
import os
import datetime
from contextlib import contextmanager
from flask import Blueprint, jsonify, request

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

# Reuse the track definitions from the LinkedIn module so press +
# LinkedIn post about the SAME anchor in the same week.
from routes.linkedin_partnership_weekly import _TRACKS as _LINKEDIN_TRACKS, _current_iso_week, _pick_track

partnership_press_bp = Blueprint("partnership_press", __name__,
                                  url_prefix="/api/v1/partnerships/press")


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try: yield c
    finally: c.close()


def _slugify(s, max_len=70):
    import re
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:max_len] or "partnership-update"


def _build_release(track):
    """Build title + subheadline + body markup for the press release.
    Mirrors the LinkedIn body but uses press-release voice."""
    today = datetime.date.today().strftime("%B %d, %Y")
    iso_year, iso_week = _current_iso_week()

    # Pull a couple of live stats so the press release has fresh numbers
    facilities = 21405; markets = 286; deals = 1972
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM discovered_facilities")
            r = cur.fetchone()
            facilities = r[0] if r else facilities
            cur.execute("SELECT COUNT(*) FROM market_power_scores")
            r = cur.fetchone()
            markets = r[0] if r else markets
            cur.execute("SELECT COUNT(*) FROM deals")
            r = cur.fetchone()
            deals = r[0] if r else deals
    except Exception:
        pass

    slug_root = track["slug"]
    # r47.22 (2026-05-26): Switzerland-model framing. Each press release
    # describes a PUBLISHED OPEN INVITATION — never an executed deal.
    # Wording must make clear no agreement exists yet.
    partner_name_map = {
        "dchawk":  "DCHawk",
        "dcbyte":  "DCByte",
        "dcd":     "Data Center Dynamics",
        "dcf":     "Data Center Frontier",
        "cbre":    "CBRE",
        "jll":     "JLL",
    }
    if slug_root == "partners":
        title = (f"DC Hub Publishes Open Partnership Invitations Under "
                  f"the 'Switzerland' Model — CC-BY-4.0, No Channel Conflict")
        subheadline = (f"{facilities:,}-facility intelligence platform extends public "
                        f"invitations to brokers, publications, facility databases, and "
                        f"analyst firms. No deals announced; specific opening asks listed "
                        f"openly at dchub.cloud/partners.")
    else:
        partner_label = partner_name_map.get(slug_root, slug_root.upper())
        title = (f"DC Hub Publishes Open Partnership Invitation to "
                  f"{partner_label} — Switzerland Model, No Channel Conflict")
        subheadline = (f"Week of {today}: DC Hub publishes a specific opening offer for "
                        f"{partner_label} at dchub.cloud/partners{track['anchor'] or ''}. "
                        f"No partnership currently exists; this is a public invitation, "
                        f"not an announcement of any executed agreement.")

    body = (
        f"DC Hub today published an open partnership invitation as part of its "
        f"'Switzerland model' program. **No partnership has been signed**; the body of this "
        f"release is the specific opening offer DC Hub is publicly extending. The full set "
        f"of open invitations lives at https://dchub.cloud/partners.\n\n"
        f"---\n\n"
        f"{track['body']}\n\n"
        f"---\n\n"
        f"Today's snapshot from DC Hub's live data layer: {facilities:,} tracked facilities "
        f"across 170+ countries, {markets} markets scored daily by the DC Hub Power Index "
        f"(DCPI), {deals:,} historical M&A deals, and integrations with 96+ AI platforms via "
        f"the streamable-http MCP server at dchub.cloud/mcp.\n\n"
        f"### About the Switzerland model\n\n"
        f"DC Hub does not compete with brokers, publications, facility databases, or "
        f"analyst firms. We're the neutral live data layer beneath them. We publish six "
        f"open invitations at https://dchub.cloud/partners — each starts with a single, "
        f"specific opening ask, no decks, no NDAs, no exclusivity. Standard $9/mo "
        f"developer tier, $199/mo PRO, custom enterprise. CC-BY-4.0 by default.\n\n"
        f"### Try the data without signing up\n\n"
        f"- {track['url']} — this invitation's full text\n"
        f"- https://dchub.cloud/reports/monthly — live monthly report\n"
        f"- https://dchub.cloud/reports/quarterly-deep — quarterly equivalent\n"
        f"- https://dchub.cloud/dcpi — 286 markets with daily verdicts\n"
        f"- https://dchub.cloud/.well-known/mcp.json — full MCP manifest\n\n"
        f"### Contact\n\n"
        f"For partnership inquiries: partnerships@dchub.cloud. For editorial use of "
        f"the live data: editorial@dchub.cloud. Press: press@dchub.cloud. "
        f"All endpoints CC-BY-4.0 by default. **No partnership announcement has been "
        f"made by either party in this release; this is an open invitation only.**"
    )

    summary = (track["body"].split("\n\n")[0])[:280]

    return {
        "title":       title,
        "subheadline": subheadline,
        "summary":     summary,
        "body":        body,
        "slug":        f"partnership-{slug_root}-{iso_year}-w{iso_week:02d}",
        "category":    "partnership",
        "source":      "DC Hub Media",
        "source_url":  track["url"],
        "iso_year":    iso_year,
        "iso_week":    iso_week,
        "track":       track["slug"],
    }


def _insert_press_release(release):
    """Insert into press_releases table (idempotent by slug)."""
    if not (_pg and _dsn()):
        return {"ok": False, "error": "no_db"}
    try:
        with _conn() as c, c.cursor() as cur:
            # Check for existing slug
            cur.execute("SELECT id FROM press_releases WHERE slug = %s", (release["slug"],))
            existing = cur.fetchone()
            if existing:
                return {"ok": True, "id": existing[0], "slug": release["slug"], "existed": True}

            cur.execute("""
                INSERT INTO press_releases
                  (title, subheadline, summary, body, slug, source, source_url,
                   category, date, published_date, published, created_at, published_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_DATE, CURRENT_DATE, TRUE, NOW(), NOW())
                RETURNING id
            """, (release["title"], release["subheadline"], release["summary"],
                  release["body"], release["slug"], release["source"],
                  release["source_url"], release["category"]))
            new_id = cur.fetchone()[0]
            return {"ok": True, "id": int(new_id), "slug": release["slug"], "existed": False}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


@partnership_press_bp.route("/preview", methods=["GET"])
def preview():
    slug = (request.args.get("slug") or "").strip().lower()
    if slug:
        track = next((t for t in _LINKEDIN_TRACKS if t["slug"] == slug), None)
        if not track:
            return jsonify({"error": "unknown_slug",
                            "available": [t["slug"] for t in _LINKEDIN_TRACKS]}), 400
    else:
        track = _pick_track()
    release = _build_release(track)
    return jsonify(release), 200


@partnership_press_bp.route("/run", methods=["GET", "POST"])
def run():
    """Cron-callable. Creates this ISO-week's partnership press release."""
    slug = (request.args.get("slug") or "").strip().lower()
    if slug:
        track = next((t for t in _LINKEDIN_TRACKS if t["slug"] == slug), None)
        if not track:
            return jsonify({"error": "unknown_slug"}), 400
    else:
        track = _pick_track()

    release = _build_release(track)
    result = _insert_press_release(release)
    return jsonify({
        "ok":        result.get("ok"),
        "track":     track["slug"],
        "slug":      release["slug"],
        "title":     release["title"],
        "id":        result.get("id"),
        "existed":   result.get("existed", False),
        "error":     result.get("error"),
        "press_url": f"https://dchub.cloud/press-release/{release['slug']}",
        "at":        datetime.datetime.utcnow().isoformat() + "Z",
    }), 200 if result.get("ok") else 500


@partnership_press_bp.route("/status", methods=["GET"])
def status():
    if not (_pg and _dsn()):
        return jsonify({"recent": []}), 200
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT id, slug, title, created_at
                  FROM press_releases
                 WHERE category = 'partnership'
                 ORDER BY created_at DESC LIMIT 12
            """)
            recent = [{
                "id": r[0], "slug": r[1], "title": r[2],
                "created_at": r[3].isoformat() if r[3] else None,
                "url": f"https://dchub.cloud/press-release/{r[1]}",
            } for r in cur.fetchall()]
        return jsonify({
            "recent": recent,
            "next_track": _pick_track()["slug"],
            "current_iso_week": "{}-W{:02d}".format(*_current_iso_week()),
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)[:140]}), 500
