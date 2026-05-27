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
        "headline": "DC Hub is open to partnerships — Switzerland model, all comers welcome",
        "url": "https://dchub.cloud/partners",
        "body": """DC Hub is open to collaboration with every facility database, broker, publication, and analyst firm in the data-center industry. We're not announcing any signed deal — we're publicly extending an open invitation to anyone whose work intersects with ours.

Our model is simple: Switzerland — neutral, CC-BY-4.0, no channel conflict. We don't broker; we don't replace anyone's offering. We're the live data layer underneath whatever you're already building.

What we bring: 21,405 tracked facilities globally, 286 markets scored daily by DCPI, $324B+ in tracked M&A, real-time grid intelligence across 10 ISOs. Already cited by ChatGPT, Claude, Gemini, Perplexity, Cursor, and 90+ other AI platforms.

What we'd love in return: cross-licensing, editorial collaboration, co-branded research drops, or a paid MCP feed your team can query. The specific shape is up to you.

If you're at DCHawk, DCByte, DCD, DCF, CBRE, JLL, 451 Research, Synergy, Omdia, Gartner, IDC, or anywhere else in this space — let's talk: partnerships@dchub.cloud · dchub.cloud/partners""",
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
    """r42v (2026-05-26): operator feedback was that the partner-specific
    tracks (dchawk/cbre/dcd/jll/dcf/dcbyte) came across as confrontational
    when auto-posted weekly. The generic 'partners' track conveys the
    same Switzerland-model invitation without singling anyone out. Lock
    the rotation to the generic track only. The partner-specific tracks
    remain in _TRACKS for reference + direct link use (partnership_press
    _template.py uses them by slug) but are excluded from auto-rotation."""
    for t in _TRACKS:
        if t.get("slug") == "partners":
            return t
    # Fallback to old behavior if generic track is somehow missing
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


def _is_admin(req):
    expected = os.environ.get("DCHUB_ADMIN_KEY", "").strip()
    if not expected:
        return False
    got = req.headers.get("X-Admin-Key", "").strip()
    return bool(got and got == expected)


def _ensure_drafts_table():
    if not (_pg and _dsn()):
        return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS linkedin_partnership_drafts (
                    id           SERIAL PRIMARY KEY,
                    iso_year     INT NOT NULL,
                    iso_week     INT NOT NULL,
                    track_slug   TEXT NOT NULL,
                    headline     TEXT,
                    body         TEXT,
                    url          TEXT,
                    created_at   TIMESTAMPTZ DEFAULT NOW(),
                    approved_at  TIMESTAMPTZ,
                    posted_at    TIMESTAMPTZ,
                    linkedin_urn TEXT,
                    status       TEXT DEFAULT 'pending',
                    UNIQUE(iso_year, iso_week)
                )
            """)
    except Exception:
        pass


_ensure_drafts_table()


@linkedin_partnership_bp.route("/run", methods=["GET", "POST"])
def run():
    """Cron-callable. Generates a DRAFT for the current ISO-week's partnership
    track. r47.23: does NOT auto-post to LinkedIn anymore — operator must
    POST /approve to fire to LinkedIn. Pass auto_publish=1 + admin key for
    emergency override.
    """
    iso_year, iso_week = _current_iso_week()
    bypass = (request.args.get("force") or "").lower() in ("1", "true", "yes")
    auto_publish = (request.args.get("auto_publish") or "").lower() in ("1", "true", "yes")

    if auto_publish and not _is_admin(request):
        return jsonify({"error": "unauthorized",
                        "hint": "auto_publish=1 requires X-Admin-Key. Without admin, only drafts are created."}), 401

    track = _pick_track()
    if (request.args.get("slug") or "").strip():
        slug_q = request.args.get("slug").strip().lower()
        match = next((t for t in _TRACKS if t["slug"] == slug_q), None)
        if match: track = match

    # Check / create draft (idempotent on iso_year+iso_week)
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503

    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT id, status, posted_at, linkedin_urn
                  FROM linkedin_partnership_drafts
                 WHERE iso_year=%s AND iso_week=%s
            """, (iso_year, iso_week))
            existing = cur.fetchone()
            if existing and not bypass:
                draft_id, status, posted_at, urn = existing
                return jsonify({
                    "skipped": True,
                    "reason":  "already_has_draft_this_week",
                    "draft_id": draft_id,
                    "status":   status,
                    "posted_at": posted_at.isoformat() if posted_at else None,
                    "linkedin_urn": urn,
                    "preview_url":  f"https://api.dchub.cloud/api/v1/linkedin-partnership/drafts/{draft_id}",
                    "approve_url":  f"https://api.dchub.cloud/api/v1/linkedin-partnership/approve/{draft_id}",
                }), 200

            # If forcing OR auto-publishing, may need to clean up existing
            if existing and (bypass or auto_publish):
                cur.execute("DELETE FROM linkedin_partnership_drafts WHERE iso_year=%s AND iso_week=%s",
                            (iso_year, iso_week))

            cur.execute("""
                INSERT INTO linkedin_partnership_drafts
                  (iso_year, iso_week, track_slug, headline, body, url, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (iso_year, iso_week, track["slug"], track["headline"],
                  track["body"], track["url"],
                  "approved" if auto_publish else "pending"))
            draft_id = cur.fetchone()[0]

            # If admin asked for auto-publish, fire immediately
            posted_urn = None
            if auto_publish:
                text = f"{track['body']}\n\n{track['url']}"
                result = _post_to_linkedin(text, track["url"])
                posted_urn = result.get("urn")
                cur.execute("""
                    UPDATE linkedin_partnership_drafts
                       SET posted_at=NOW(), linkedin_urn=%s, status=%s,
                           approved_at=NOW()
                     WHERE id=%s
                """, (posted_urn, "posted" if result.get("ok") else "post_failed", draft_id))
                # Also keep legacy table in sync
                _record(iso_year, iso_week, track, result)

        return jsonify({
            "ok":          True,
            "draft_id":    draft_id,
            "iso_year":    iso_year,
            "iso_week":    iso_week,
            "track":       track["slug"],
            "anchor":      track["anchor"],
            "url":         track["url"],
            "status":      "posted" if auto_publish else "pending_review",
            "linkedin_urn": posted_urn,
            "preview_url": f"https://api.dchub.cloud/api/v1/linkedin-partnership/drafts/{draft_id}",
            "approve_url": f"https://api.dchub.cloud/api/v1/linkedin-partnership/approve/{draft_id}",
            "reject_url":  f"https://api.dchub.cloud/api/v1/linkedin-partnership/reject/{draft_id}",
            "review_hint": ("Draft saved — NOT posted to LinkedIn. POST /approve/<id> with "
                            "X-Admin-Key to fire it.")
                           if not auto_publish else "Posted to LinkedIn (admin override).",
            "at":          datetime.datetime.utcnow().isoformat() + "Z",
        }), 200
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {str(e)[:160]}"}), 500


@linkedin_partnership_bp.route("/drafts", methods=["GET"])
def list_drafts():
    if not (_pg and _dsn()):
        return jsonify({"drafts": []}), 200
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT id, iso_year, iso_week, track_slug, headline, body, url,
                       created_at, approved_at, posted_at, status, linkedin_urn
                  FROM linkedin_partnership_drafts
                 ORDER BY created_at DESC LIMIT 30
            """)
            drafts = [{
                "id":           r[0],
                "iso_year":     r[1], "iso_week": r[2],
                "track":        r[3],
                "headline":     r[4],
                "body_preview": (r[5] or "")[:200],
                "url":          r[6],
                "created_at":   r[7].isoformat() if r[7] else None,
                "approved_at":  r[8].isoformat() if r[8] else None,
                "posted_at":    r[9].isoformat() if r[9] else None,
                "status":       r[10],
                "linkedin_urn": r[11],
                "approve_url":  f"https://api.dchub.cloud/api/v1/linkedin-partnership/approve/{r[0]}",
                "reject_url":   f"https://api.dchub.cloud/api/v1/linkedin-partnership/reject/{r[0]}",
            } for r in cur.fetchall()]
        return jsonify({"count": len(drafts), "drafts": drafts}), 200, {"Cache-Control": "no-store"}
    except Exception as e:
        return jsonify({"error": str(e)[:140], "drafts": []}), 200


@linkedin_partnership_bp.route("/drafts/<int:draft_id>", methods=["GET"])
def view_draft(draft_id):
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT id, iso_year, iso_week, track_slug, headline, body, url,
                       created_at, status, posted_at, linkedin_urn
                  FROM linkedin_partnership_drafts
                 WHERE id = %s
            """, (draft_id,))
            r = cur.fetchone()
            if not r:
                return jsonify({"error": "not_found"}), 404
            return jsonify({
                "id":           r[0],
                "iso_year":     r[1], "iso_week": r[2],
                "track":        r[3],
                "headline":     r[4],
                "body":         r[5],
                "url":          r[6],
                "char_count":   len(r[5] or ""),
                "created_at":   r[7].isoformat() if r[7] else None,
                "status":       r[8],
                "posted_at":    r[9].isoformat() if r[9] else None,
                "linkedin_urn": r[10],
                "approve_url":  f"https://api.dchub.cloud/api/v1/linkedin-partnership/approve/{r[0]}",
                "reject_url":   f"https://api.dchub.cloud/api/v1/linkedin-partnership/reject/{r[0]}",
            }), 200, {"Cache-Control": "no-store"}
    except Exception as e:
        return jsonify({"error": str(e)[:140]}), 500


@linkedin_partnership_bp.route("/approve/<int:draft_id>", methods=["POST"])
def approve_draft(draft_id):
    """Fire the draft to LinkedIn. Admin only."""
    if not _is_admin(request):
        return jsonify({"error": "unauthorized", "hint": "X-Admin-Key required"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT id, iso_year, iso_week, track_slug, body, url, status
                  FROM linkedin_partnership_drafts WHERE id = %s
            """, (draft_id,))
            r = cur.fetchone()
            if not r:
                return jsonify({"error": "not_found"}), 404
            if r[6] in ("posted",):
                return jsonify({"error": "already_posted",
                                "hint": "This draft was already posted to LinkedIn."}), 409

            track = next((t for t in _TRACKS if t["slug"] == r[3]), None)
            text = f"{r[4]}\n\n{r[5]}"
            result = _post_to_linkedin(text, r[5])

            cur.execute("""
                UPDATE linkedin_partnership_drafts
                   SET approved_at=NOW(),
                       posted_at=NOW(),
                       linkedin_urn=%s,
                       status=%s
                 WHERE id=%s
            """, ((result.get("urn") or None),
                  "posted" if result.get("ok") else "post_failed",
                  draft_id))

            # Mirror to legacy table for /status compatibility
            if track:
                _record(int(r[1]), int(r[2]), track, result)

            return jsonify({
                "ok":           result.get("ok"),
                "draft_id":     draft_id,
                "track":        r[3],
                "linkedin_urn": result.get("urn"),
                "error":        result.get("error", ""),
                "at":           datetime.datetime.utcnow().isoformat() + "Z",
            }), 200 if result.get("ok") else 502
    except Exception as e:
        return jsonify({"error": str(e)[:160]}), 500


@linkedin_partnership_bp.route("/reject/<int:draft_id>", methods=["POST", "DELETE"])
def reject_draft(draft_id):
    """Delete a draft without posting. Admin only."""
    if not _is_admin(request):
        return jsonify({"error": "unauthorized", "hint": "X-Admin-Key required"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                DELETE FROM linkedin_partnership_drafts
                 WHERE id = %s AND status != 'posted'
                 RETURNING id, track_slug
            """, (draft_id,))
            r = cur.fetchone()
            if not r:
                return jsonify({"error": "not_found_or_posted",
                                "hint": "Cannot reject an already-posted draft."}), 404
            return jsonify({"ok": True, "deleted_id": int(r[0]), "track": r[1]}), 200
    except Exception as e:
        return jsonify({"error": str(e)[:140]}), 500


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
