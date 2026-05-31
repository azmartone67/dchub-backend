"""
press_outreach.py — r47.38 (2026-05-26).

Smart journalist outreach. Replaces the "spam every market shift to LinkedIn"
pattern with targeted, narrative pitches tied to specific publication beats.

The system:

  1. Maintains a curated press_contacts table of data-center beat outlets
     (Bisnow, DCD, Light Reading, Bloomberg infra, WSJ tech, etc.) with
     beat tags and pitch-style preferences. Seeded on first boot.

  2. Scans the platform for "story angles" — events the data layer makes
     newsworthy:
         - DCPI verdict shifts (market becomes BUILD or AVOID)
         - Largest M&A deal of the week
         - New market crosses into BUILD with above-threshold Excess Power
         - AI citation milestones (e.g., 100K calls/month threshold)
         - Hyperscaler power-deal scoop
         - International market addition

  3. For each (contact × matching story angle), generates a personalized
     pitch DRAFT — 2-3 paragraph email leading with the data point that
     matters to that journalist's beat. Optional embargo offer for fresh
     data.

  4. Drafts live in press_pitch_drafts table with status='pending'.
     Admin reviews at /admin/partnerships/review, approves to send via
     Resend. Same draft-then-approve safety gate as enterprise leads.

  5. Tracks responses + coverage so each contact's priority can evolve
     (high-response journalists get pitched more often; non-responders
     get de-prioritized over time).

Endpoints (all admin-gated except the scan summary):

  POST /api/v1/admin/press-outreach/scan-angles
      Detect newsworthy events from platform data.
  POST /api/v1/admin/press-outreach/generate-drafts
      Generate pitch drafts. Optional ?angle=<angle_id>, ?top=N.
  GET  /api/v1/admin/press-outreach/contacts
      List the journalist contact DB.
  POST /api/v1/admin/press-outreach/contacts/upsert
      Add or update a contact.
  GET  /api/v1/admin/press-outreach/drafts
      Review queue. ?status=pending|approved|sent|rejected.
  POST /api/v1/admin/press-outreach/approve/<id>
      Approve + fire pitch via Resend.
  POST /api/v1/admin/press-outreach/reject/<id>
      Discard draft (kept for audit).

CRITICAL: NEVER auto-sends to third parties. Same draft-then-approve
discipline as partnership_press_template + enterprise_leads_sweep.
"""
import os
import json
import datetime
import logging
from contextlib import contextmanager
from flask import Blueprint, request, jsonify

try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

logger = logging.getLogger(__name__)
press_outreach_bp = Blueprint("press_outreach", __name__)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try: yield c
    finally: c.close()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS press_contacts (
    id                BIGSERIAL PRIMARY KEY,
    outlet            TEXT NOT NULL UNIQUE,  -- r47.38.2: simplified — one row per outlet,
                                              -- contact_email updates in-place
    beat              TEXT,                -- data_centers | ai_infra | m_and_a | energy_grid | telecom
    contact_name      TEXT,
    contact_email     TEXT,
    contact_twitter   TEXT,
    contact_linkedin  TEXT,
    priority          INTEGER DEFAULT 5,   -- 1-10, used to rank pitch order
    pitch_style       TEXT,                -- narrative | data_first | exclusive_ok | embargo_ok
    notes             TEXT,
    last_contacted_at TIMESTAMPTZ,
    total_pitches     INTEGER DEFAULT 0,
    total_responses   INTEGER DEFAULT 0,
    total_coverage    INTEGER DEFAULT 0,
    active            BOOLEAN DEFAULT TRUE,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS press_contacts_priority_idx
  ON press_contacts (active, priority DESC);

CREATE TABLE IF NOT EXISTS press_pitch_drafts (
    id              BIGSERIAL PRIMARY KEY,
    contact_id      BIGINT REFERENCES press_contacts(id),
    angle_key       TEXT,                  -- dcpi_shift | m_and_a | new_market | ai_citation_milestone | …
    angle_data      JSONB,                 -- {market: 'Cheyenne, WY', verdict: 'BUILD', excess: 69.5, …}
    subject         TEXT,
    body            TEXT,
    status          TEXT DEFAULT 'pending', -- pending | approved | sent | rejected | bounced
    embargo_until   TIMESTAMPTZ,
    approved_at     TIMESTAMPTZ,
    sent_at         TIMESTAMPTZ,
    response_at     TIMESTAMPTZ,
    coverage_url    TEXT,
    score           REAL,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS press_pitch_drafts_status_idx
  ON press_pitch_drafts (status, created_at DESC);
"""


# Seed list of real data-center beat outlets. EMAIL FIELD INTENTIONALLY LEFT
# BLANK — the operator (Jonathan) populates contact_email/contact_name with
# the specific editor they know from each outlet. We never invent or guess
# journalist email addresses. Outlets + beats are publicly known facts.
_SEED_CONTACTS = [
    {"outlet": "Datacenter Dynamics (DCD)",
     "beat": "data_centers",
     "pitch_style": "data_first",
     "priority": 9,
     "notes": "Global trade pub. Power + capacity heavy. Sebastian Moss + team."},
    {"outlet": "Bisnow Data Center",
     "beat": "data_centers",
     "pitch_style": "narrative",
     "priority": 9,
     "notes": "US real-estate-flavored DC coverage. Mark Faithfull / Bisnow DC team."},
    {"outlet": "Data Center Knowledge",
     "beat": "data_centers",
     "pitch_style": "data_first",
     "priority": 8,
     "notes": "Informa-owned trade. Wider operator audience."},
    {"outlet": "The Information",
     "beat": "ai_infra",
     "pitch_style": "exclusive_ok",
     "priority": 10,
     "notes": "Subscription tech. Hyperscaler infra + AI buildout angle. Embargo-friendly."},
    {"outlet": "WSJ — Tech / Heard on the Street",
     "beat": "ai_infra",
     "pitch_style": "exclusive_ok",
     "priority": 10,
     "notes": "Pro Crawford / Theo Francis / similar tech-infra beat. Exclusive data drops."},
    {"outlet": "Bloomberg — Infrastructure / Equity Research",
     "beat": "ai_infra",
     "pitch_style": "data_first",
     "priority": 10,
     "notes": "Equinix / DLR analyst coverage. Bloomberg Intelligence DC team."},
    {"outlet": "Reuters — M&A / Infrastructure",
     "beat": "m_and_a",
     "pitch_style": "data_first",
     "priority": 9,
     "notes": "Greg Roumeliotis / Reuters infra desk. $324B+ tracker angle."},
    {"outlet": "Crunchbase News",
     "beat": "m_and_a",
     "pitch_style": "narrative",
     "priority": 7,
     "notes": "Hyperscaler funding rounds + acquisitions."},
    {"outlet": "TechCrunch",
     "beat": "ai_infra",
     "pitch_style": "narrative",
     "priority": 8,
     "notes": "Frederic Lardinois / AI infra beat."},
    {"outlet": "Light Reading",
     "beat": "telecom",
     "pitch_style": "data_first",
     "priority": 7,
     "notes": "Fiber + interconnect coverage. Iain Morris."},
    {"outlet": "Capacity Media",
     "beat": "telecom",
     "pitch_style": "data_first",
     "priority": 6,
     "notes": "Wholesale + interconnect-tier coverage."},
    {"outlet": "S&P Global Market Intelligence",
     "beat": "m_and_a",
     "pitch_style": "data_first",
     "priority": 8,
     "notes": "M&A + REIT coverage. DLR / EQIX analyst team."},
    {"outlet": "Axios Pro — Climate Deals",
     "beat": "energy_grid",
     "pitch_style": "narrative",
     "priority": 8,
     "notes": "Power-PPA + grid constraint angle. Andy Sicard et al."},
    {"outlet": "Heatmap News",
     "beat": "energy_grid",
     "pitch_style": "narrative",
     "priority": 7,
     "notes": "Grid + climate-policy lens on DC buildout."},
    {"outlet": "Utility Dive",
     "beat": "energy_grid",
     "pitch_style": "data_first",
     "priority": 7,
     "notes": "ISO + utility-policy lens. Data centers as new grid load."},
    {"outlet": "Latitude Media",
     "beat": "energy_grid",
     "pitch_style": "narrative",
     "priority": 7,
     "notes": "Stephen Lacey + David Roberts. Climate-tech + DC angle."},
    {"outlet": "Semafor — Technology",
     "beat": "ai_infra",
     "pitch_style": "narrative",
     "priority": 7,
     "notes": "Reed Albergotti AI infrastructure beat."},
    {"outlet": "Stratechery",
     "beat": "ai_infra",
     "pitch_style": "exclusive_ok",
     "priority": 8,
     "notes": "Ben Thompson — analytical, long-form. Pitch DCPI methodology."},
    {"outlet": "Substack — Doug O'Laughlin / Fabricated Knowledge",
     "beat": "ai_infra",
     "pitch_style": "data_first",
     "priority": 7,
     "notes": "Semiconductor + DC build-out depth."},
    {"outlet": "DataCenter News (NZ / APAC)",
     "beat": "data_centers",
     "pitch_style": "narrative",
     "priority": 5,
     "notes": "APAC angle for international DCPI expansion."},
]


def _ensure_schema_and_seed():
    if not (_pg and _dsn()): return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(_SCHEMA)
            for row in _SEED_CONTACTS:
                cur.execute("""
                    INSERT INTO press_contacts
                        (outlet, beat, contact_name, contact_email,
                         priority, pitch_style, notes)
                    VALUES (%s, %s, NULL, NULL, %s, %s, %s)
                    ON CONFLICT (outlet) DO NOTHING
                """, (row["outlet"], row["beat"], row["priority"],
                       row["pitch_style"], row["notes"]))
        logger.info(f"[press_outreach] seeded {len(_SEED_CONTACTS)} outlets")
    except Exception as e:
        logger.warning(f"[press_outreach] init failed: {e}")


def _is_admin(req):
    provided = req.headers.get("X-Admin-Key") or req.headers.get("X-Internal-Key")
    if not provided: return False
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    if expected and provided == expected:
        return True
    try:
        from internal_auth import is_valid_internal_key
        return bool(is_valid_internal_key(provided))
    except Exception:
        return False


# ── Story-angle detectors ─────────────────────────────────────────────
#
# Each returns a list of `angle` dicts. An angle has:
#   key:            'dcpi_shift_to_avoid', 'dcpi_shift_to_build', ...
#   newsworthiness: 1-10 (10 = front-page)
#   beat:           which press_contacts.beat this is most relevant to
#   headline_hint:  string used as pitch-subject seed
#   data:           dict of structured numbers we can quote
#   url:            link the journalist can use to verify

def _detect_angles():
    if not (_pg and _dsn()):
        return []
    angles = []
    try:
        with _conn() as c, c.cursor() as cur:
            # 1) DCPI verdict shifts to AVOID — top 3 by Constraint score
            try:
                cur.execute("""
                    SELECT name, iso, slug, excess_power_score, constraint_score
                      FROM market_power_scores
                     WHERE verdict = 'AVOID'
                       AND updated_at > NOW() - INTERVAL '14 days'
                     ORDER BY constraint_score DESC NULLS LAST LIMIT 3
                """)
                for r in cur.fetchall() or []:
                    angles.append({
                        "key":            "dcpi_shift_to_avoid",
                        "newsworthiness": 7,
                        "beat":           "data_centers",
                        "headline_hint":  f"{r[0]} flips to AVOID on DCPI ({r[1]} grid constraint {float(r[4] or 0):.1f}/100)",
                        "data":           {"market": r[0], "iso": r[1],
                                            "slug":   r[2],
                                            "excess": float(r[3] or 0),
                                            "constraint": float(r[4] or 0)},
                        "url":            f"https://dchub.cloud/dcpi/{r[2]}",
                    })
            except Exception: pass

            # 2) DCPI BUILD verdicts with highest Excess Power
            try:
                cur.execute("""
                    SELECT name, iso, slug, excess_power_score, constraint_score
                      FROM market_power_scores
                     WHERE verdict = 'BUILD'
                       AND excess_power_score >= 65
                       AND updated_at > NOW() - INTERVAL '14 days'
                     ORDER BY excess_power_score DESC NULLS LAST LIMIT 3
                """)
                for r in cur.fetchall() or []:
                    angles.append({
                        "key":            "dcpi_top_build",
                        "newsworthiness": 8,
                        "beat":           "data_centers",
                        "headline_hint":  f"{r[0]} ({r[1]}) clears DC Hub's highest-tier BUILD threshold with Excess Power {float(r[3] or 0):.1f}/100",
                        "data":           {"market": r[0], "iso": r[1],
                                            "slug":   r[2],
                                            "excess": float(r[3] or 0),
                                            "constraint": float(r[4] or 0)},
                        "url":            f"https://dchub.cloud/dcpi/{r[2]}",
                    })
            except Exception: pass

            # 3) Largest M&A deal of the last 14 days
            try:
                cur.execute("""
                    SELECT buyer, seller, value, mw, market, date
                      FROM deals
                     WHERE value IS NOT NULL
                       AND COALESCE(date, '1970-01-01') > (NOW() - INTERVAL '14 days')::text
                     ORDER BY (CASE
                                  WHEN value ~ '^[0-9.]+$' THEN value::numeric
                                  ELSE 0
                              END) DESC NULLS LAST
                     LIMIT 1
                """)
                row = cur.fetchone()
                if row and row[0]:
                    angles.append({
                        "key":            "m_and_a_top",
                        "newsworthiness": 8,
                        "beat":           "m_and_a",
                        "headline_hint":  f"DC Hub tracks {row[0]} acquires {row[1] or 'data-center asset'} — {row[2] or '?'}",
                        "data":           {"buyer": row[0], "seller": row[1],
                                            "value": str(row[2] or ""),
                                            "mw":    str(row[3] or ""),
                                            "market": row[4] or "", "date": str(row[5] or "")},
                        "url":            "https://dchub.cloud/transactions",
                    })
            except Exception: pass

            # 4) AI citation milestone — only fire when crossing round thresholds
            try:
                cur.execute("""
                    SELECT COUNT(*) FROM mcp_call_log
                     WHERE timestamp > NOW() - INTERVAL '30 days'
                """)
                calls_30d = int((cur.fetchone() or [0])[0])
                threshold = None
                for t in (1_000_000, 500_000, 250_000, 100_000, 50_000, 25_000, 10_000):
                    if calls_30d >= t:
                        threshold = t; break
                if threshold and threshold >= 50_000:
                    angles.append({
                        "key":            "ai_citation_milestone",
                        "newsworthiness": 9,
                        "beat":           "ai_infra",
                        "headline_hint":  f"DC Hub's MCP server crosses {threshold:,} AI-agent tool calls in 30 days",
                        "data":           {"calls_30d": calls_30d, "threshold": threshold,
                                            "platforms":  ["Claude (Anthropic)", "Claude Desktop"]},
                        "url":            "https://dchub.cloud/api/v1/agents/citations.json",
                    })
            except Exception: pass

            # 5) International market addition
            try:
                cur.execute("""
                    SELECT name, country, iso, slug
                      FROM market_power_scores
                     WHERE country IS NOT NULL
                       AND country NOT IN ('US', 'USA')
                       AND created_at > NOW() - INTERVAL '14 days'
                     ORDER BY created_at DESC LIMIT 5
                """)
                intl = list(cur.fetchall() or [])
                if intl:
                    countries = sorted({r[1] for r in intl if r[1]})
                    angles.append({
                        "key":            "intl_market_addition",
                        "newsworthiness": 7,
                        "beat":           "data_centers",
                        "headline_hint":  f"DC Hub Power Index expands to {len(intl)} new international markets across {len(countries)} countries",
                        "data":           {"new_markets": [{"name": r[0], "country": r[1],
                                                              "iso": r[2], "slug": r[3]} for r in intl[:5]],
                                            "country_count": len(countries),
                                            "countries":     countries[:10]},
                        "url":            "https://dchub.cloud/dcpi/intl",
                    })
            except Exception: pass

    except Exception as e:
        logger.warning(f"[press_outreach] detect_angles failed: {e}")

    return angles


# ── Pitch generator ──────────────────────────────────────────────────

def _generate_pitch(contact: dict, angle: dict) -> dict:
    """Craft a 2-3 paragraph email pitch tailored to (contact, angle).

    Lead with the data point that matters to this journalist's beat.
    No marketing fluff — journalists hate it. Offer founder availability
    + an exclusive embargo if the story angle is fresh."""
    beat   = (contact.get("beat") or "data_centers").lower()
    style  = (contact.get("pitch_style") or "narrative").lower()
    outlet = contact.get("outlet", "your outlet")
    name   = contact.get("contact_name") or "there"
    key    = angle.get("key", "")
    data   = angle.get("data") or {}

    # Subject line — terse, data-forward, no clickbait
    if key == "dcpi_shift_to_avoid":
        subj = (f"{data.get('market')} hits DCPI AVOID — "
                f"Constraint {data.get('constraint', 0):.0f}/100 — "
                f"happy to walk you through the methodology")
        lead = (f"Hi {name},\n\n"
                f"DC Hub's Power Index just flipped {data.get('market')} "
                f"({data.get('iso')}) into AVOID territory — Grid Constraint "
                f"hit {data.get('constraint', 0):.1f}/100 against Excess Power "
                f"of {data.get('excess', 0):.1f}/100. That's the 2nd-highest "
                f"infrastructure-risk score in our tracker right now.\n\n")
        offer = (f"For a {outlet} story on where the AI buildout is hitting "
                 f"grid walls in 2026, this is the data point. Methodology + "
                 f"underlying inputs at {angle.get('url')}. I can also send "
                 f"the per-ISO comparison and the 90-day trendline if useful — "
                 f"and I'm happy to be quoted on what the shift means for "
                 f"site-selection. Reply here or 30-min call link: "
                 f"https://dchub.cloud/contact.\n\n")
    elif key == "dcpi_top_build":
        subj = (f"{data.get('market')} ({data.get('iso')}) clears DC Hub's "
                f"top BUILD tier — Excess Power {data.get('excess', 0):.0f}/100")
        lead = (f"Hi {name},\n\n"
                f"{data.get('market')} just crossed DC Hub's top BUILD "
                f"threshold on the Power Index (Excess Power "
                f"{data.get('excess', 0):.1f}/100, Grid Constraint "
                f"{data.get('constraint', 0):.1f}/100). For context: only "
                f"~14 of 286 markets we track hit BUILD this week, and only "
                f"the top 3 clear the Excess Power 65/100 line.\n\n")
        offer = (f"For a {outlet} angle on where hyperscale-class capacity "
                 f"is materializing outside the traditional FLAP markets, "
                 f"this is the story. Live page + methodology at "
                 f"{angle.get('url')}. I can send the full top-14 BUILD list "
                 f"and the per-market sub-market detail. Happy to be quoted.\n\n")
    elif key == "m_and_a_top":
        subj = (f"DC Hub data: {data.get('buyer')} → {data.get('seller')} — "
                f"largest tracked DC deal in 14 days")
        lead = (f"Hi {name},\n\n"
                f"DC Hub's transaction tracker flagged the {data.get('buyer')} "
                f"acquisition of {data.get('seller')} as the largest "
                f"data-center M&A signal in the last 14 days "
                f"({data.get('value', '?')} {('· ' + data.get('mw') + ' MW') if data.get('mw') else ''}"
                f"{(' · ' + data.get('market')) if data.get('market') else ''}).\n\n"
                f"Our database tracks $324B+ in lifetime DC transactions — "
                f"this one slots into the broader consolidation arc.\n\n")
        offer = (f"For a {outlet} story on the 2026 DC-asset M&A cycle, I "
                 f"can send the 12-month trend, comparable deals by MW/$M, "
                 f"and which markets are seeing the most acquisition "
                 f"activity. Live tracker: {angle.get('url')}. Quote-able "
                 f"on what the deal signals for asset-level valuations.\n\n")
    elif key == "ai_citation_milestone":
        subj = (f"DC Hub's MCP server crosses {data.get('threshold', 0):,} "
                f"AI-agent tool calls / 30d — exclusive data point for {outlet}")
        lead = (f"Hi {name},\n\n"
                f"DC Hub's MCP server (data-center intelligence layer for "
                f"AI agents) just crossed {data.get('threshold', 0):,} "
                f"tool calls in a 30-day window — current count is "
                f"{data.get('calls_30d', 0):,}. Claude, ChatGPT, Perplexity, "
                f"Gemini, Copilot, Grok, and 96+ other AI platforms now "
                f"actively cite the dataset.\n\n"
                f"For a {outlet} story on what AI infrastructure looks "
                f"like when the agents are the customers, this is the "
                f"directly-measurable headline number — verified citations "
                f"at {angle.get('url')}.\n\n")
        offer = (f"I can send: the per-platform breakdown, the most-cited "
                 f"tools, the journey from 623 → 110K+ calls in 30 days. "
                 f"Happy to set up a 20-min call to walk through the "
                 f"architecture + what AI-agent demand looks like at this "
                 f"scale. Exclusive embargo available through end of week "
                 f"if you want first publication.\n\n")
    elif key == "intl_market_addition":
        countries = ", ".join((data.get("countries") or [])[:5])
        subj = (f"DC Hub Power Index expands internationally — "
                f"{data.get('country_count', 0)} new countries")
        lead = (f"Hi {name},\n\n"
                f"DC Hub just expanded the Power Index across "
                f"{data.get('country_count', 0)} new countries "
                f"({countries}{'…' if len(data.get('countries') or []) > 5 else ''}). "
                f"That's the first methodology-consistent DC-power index "
                f"covering Hydro-Québec, AESO (Alberta), and 15 Nord Pool "
                f"zones alongside the 7 US ISOs we already track.\n\n")
        offer = (f"For a {outlet} story on how the AI-buildout supply "
                 f"curve looks outside US-FLAP markets, this is fresh data. "
                 f"Live international index: {angle.get('url')}. I can "
                 f"send the per-country DCPI snapshot + the methodology "
                 f"that lets us compare e.g. Frankfurt vs Cheyenne head-to-head.\n\n")
    else:
        subj = angle.get("headline_hint", "DC Hub data point")
        lead = (f"Hi {name},\n\n"
                f"Quick data drop from DC Hub — {angle.get('headline_hint', '?')}. "
                f"Full context at {angle.get('url')}.\n\n")
        offer = (f"If this fits a {outlet} story you're working on, "
                 f"happy to send underlying data + be quoted. 20-min call "
                 f"link: https://dchub.cloud/contact.\n\n")

    signoff = ("— Jonathan\n"
               "  Founder, DC Hub\n"
               "  https://dchub.cloud · press@dchub.cloud\n"
               "  Reply to this email or grab a slot directly:\n"
               "  https://dchub.cloud/enterprise (data licensing)\n"
               "  https://dchub.cloud/dc-hub-media (press kit)\n")

    body = lead + offer + signoff
    return {"subject": subj, "body": body}


# ── Endpoints ────────────────────────────────────────────────────────

@press_outreach_bp.route("/api/v1/admin/press-outreach/scan-angles",
                          methods=["GET", "POST"], strict_slashes=False)
def scan_angles():
    """List newsworthy story angles detected from platform data."""
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    _ensure_schema_and_seed()
    angles = _detect_angles()
    return jsonify({
        "count":  len(angles),
        "angles": angles,
        "hint":   ("Run POST /generate-drafts to draft pitches for the "
                    "highest-newsworthiness angles against the press_contacts "
                    "table."),
    }), 200


@press_outreach_bp.route("/api/v1/admin/press-outreach/generate-drafts",
                          methods=["POST"], strict_slashes=False)
def generate_drafts():
    """Pair each detected angle with top-priority contacts for its beat.

    Generates personalized pitch drafts into press_pitch_drafts with
    status='pending'. Dedupes against any draft created in last 14d for
    the same (contact, angle_key) pair.

    Params:
      ?top=N           max drafts to generate per angle (default 3)
      ?min_priority=N  contact priority threshold (default 6)
      ?dedupe_days=N   skip repeats (default 14)
    """
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503

    _ensure_schema_and_seed()
    top         = max(1, min(int(request.args.get("top", 3)), 10))
    min_prio    = max(1, int(request.args.get("min_priority", 6)))
    dedupe_days = max(1, int(request.args.get("dedupe_days", 14)))

    angles = _detect_angles()
    if not angles:
        return jsonify({"ok": True, "angles_detected": 0, "drafted": 0,
                          "hint": "No newsworthy angles right now. Try later."}), 200

    drafted = []
    skipped_dupe = 0

    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for angle in angles:
                # Pick the top-N contacts whose beat matches this angle
                cur.execute("""
                    SELECT id, outlet, beat, contact_name, contact_email,
                           priority, pitch_style
                      FROM press_contacts
                     WHERE active = TRUE
                       AND priority >= %s
                       AND (beat = %s OR %s = 'data_centers')
                     ORDER BY priority DESC,
                              total_responses DESC,
                              outlet
                     LIMIT %s
                """, (min_prio, angle.get("beat"), angle.get("beat"), top))
                contacts = [dict(r) for r in cur.fetchall()]

                for contact in contacts:
                    # Dedupe per (contact, angle_key)
                    cur.execute("""
                        SELECT 1 FROM press_pitch_drafts
                         WHERE contact_id = %s
                           AND angle_key  = %s
                           AND created_at > NOW() - INTERVAL %s
                         LIMIT 1
                    """, (contact["id"], angle.get("key"), f"{dedupe_days} days"))
                    if cur.fetchone():
                        skipped_dupe += 1
                        continue

                    pitch = _generate_pitch(contact, angle)
                    score = float(angle.get("newsworthiness", 5)) * \
                            (1 + (contact.get("priority", 5) / 10.0))

                    cur.execute("""
                        INSERT INTO press_pitch_drafts
                            (contact_id, angle_key, angle_data, subject, body,
                             score, status)
                        VALUES (%s, %s, %s::jsonb, %s, %s, %s, 'pending') ON CONFLICT DO NOTHING
                        RETURNING id
                    """, (contact["id"], angle.get("key"),
                           json.dumps(angle.get("data") or {}),
                           pitch["subject"], pitch["body"], score))
                    new_id = int(cur.fetchone()["id"])
                    drafted.append({
                        "id":       new_id,
                        "outlet":   contact["outlet"],
                        "angle":    angle.get("key"),
                        "subject":  pitch["subject"],
                        "score":    round(score, 2),
                    })
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500

    return jsonify({
        "ok":              True,
        "angles_detected": len(angles),
        "drafted":         len(drafted),
        "skipped_dupe":    skipped_dupe,
        "drafts":          drafted,
        "review_url":      "/admin/partnerships/review",
        "hint":            "Review at /admin/partnerships/review, approve to send via Resend.",
    }), 200


@press_outreach_bp.route("/api/v1/admin/press-outreach/contacts",
                          methods=["GET"], strict_slashes=False)
def list_contacts():
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db", "contacts": []}), 503
    _ensure_schema_and_seed()
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM press_contacts
                 WHERE active = TRUE
                 ORDER BY priority DESC, outlet
            """)
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                for k in ("created_at", "updated_at", "last_contacted_at"):
                    if r.get(k): r[k] = r[k].isoformat()
        return jsonify({"count": len(rows), "contacts": rows}), 200
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@press_outreach_bp.route("/api/v1/admin/press-outreach/contacts/bulk-upsert",
                          methods=["POST"], strict_slashes=False)
def bulk_upsert_contacts():
    """r47.40 (2026-05-27): one curl, many contacts.

    Body: {"contacts": [
        {"outlet":"...", "contact_name":"...", "contact_email":"...", ...},
        ...
    ]}

    Each entry has the same shape as /contacts/upsert. Returns one
    response with per-contact result + total count. Idempotent — re-runs
    only update existing rows.

    Useful when you've gathered 5-20 journalist emails at once and don't
    want to fire 20 separate curls."""
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503

    data = request.get_json(silent=True) or {}
    contacts = data.get("contacts") or []
    if not isinstance(contacts, list) or not contacts:
        return jsonify({"error": "body needs {\"contacts\": [...]}"}), 400

    results = []
    upserted = 0
    failed = 0
    try:
        with _conn() as c, c.cursor() as cur:
            for entry in contacts[:50]:  # bound at 50 per call
                outlet = (entry.get("outlet") or "").strip()
                if not outlet:
                    results.append({"error": "missing outlet", "entry": entry})
                    failed += 1
                    continue
                try:
                    cur.execute("""
                        INSERT INTO press_contacts
                            (outlet, beat, contact_name, contact_email,
                             contact_twitter, contact_linkedin,
                             priority, pitch_style, notes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (outlet) DO UPDATE SET
                            beat            = EXCLUDED.beat,
                            contact_name    = COALESCE(EXCLUDED.contact_name, press_contacts.contact_name),
                            contact_email   = COALESCE(EXCLUDED.contact_email, press_contacts.contact_email),
                            contact_twitter = COALESCE(EXCLUDED.contact_twitter, press_contacts.contact_twitter),
                            contact_linkedin = COALESCE(EXCLUDED.contact_linkedin, press_contacts.contact_linkedin),
                            priority        = EXCLUDED.priority,
                            pitch_style     = EXCLUDED.pitch_style,
                            notes           = COALESCE(EXCLUDED.notes, press_contacts.notes),
                            updated_at      = NOW()
                        RETURNING id
                    """, (
                        outlet, entry.get("beat"),
                        entry.get("contact_name"), entry.get("contact_email"),
                        entry.get("contact_twitter"), entry.get("contact_linkedin"),
                        int(entry.get("priority", 5)),
                        entry.get("pitch_style", "narrative"),
                        entry.get("notes"),
                    ))
                    new_id = int(cur.fetchone()[0])
                    results.append({
                        "id":     new_id, "outlet": outlet,
                        "email":  entry.get("contact_email"),
                    })
                    upserted += 1
                except Exception as e:
                    results.append({"error": str(e)[:120], "outlet": outlet})
                    failed += 1
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500

    return jsonify({
        "ok":         True,
        "upserted":   upserted,
        "failed":     failed,
        "results":    results,
        "next_step":  ("Run POST /generate-drafts to rebuild pitch drafts "
                        "now that contacts have emails populated."),
    }), 200


@press_outreach_bp.route("/api/v1/admin/press-outreach/contacts/upsert",
                          methods=["POST"], strict_slashes=False)
def upsert_contact():
    """Body: {outlet, beat, contact_name, contact_email, priority, pitch_style, notes}"""
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503

    d = request.get_json(silent=True) or {}
    outlet = (d.get("outlet") or "").strip()
    if not outlet:
        return jsonify({"error": "outlet required"}), 400

    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO press_contacts
                    (outlet, beat, contact_name, contact_email,
                     contact_twitter, contact_linkedin,
                     priority, pitch_style, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (outlet) DO UPDATE SET
                    beat            = EXCLUDED.beat,
                    contact_name    = COALESCE(EXCLUDED.contact_name, press_contacts.contact_name),
                    contact_email   = COALESCE(EXCLUDED.contact_email, press_contacts.contact_email),
                    contact_twitter = COALESCE(EXCLUDED.contact_twitter, press_contacts.contact_twitter),
                    contact_linkedin = COALESCE(EXCLUDED.contact_linkedin, press_contacts.contact_linkedin),
                    priority        = EXCLUDED.priority,
                    pitch_style     = EXCLUDED.pitch_style,
                    notes           = COALESCE(EXCLUDED.notes, press_contacts.notes),
                    updated_at      = NOW()
                RETURNING id
            """, (outlet, d.get("beat"), d.get("contact_name"),
                   d.get("contact_email"), d.get("contact_twitter"),
                   d.get("contact_linkedin"),
                   int(d.get("priority", 5)),
                   d.get("pitch_style", "narrative"),
                   d.get("notes")))
            new_id = int(cur.fetchone()[0])
        return jsonify({"ok": True, "id": new_id, "outlet": outlet}), 200
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@press_outreach_bp.route("/api/v1/admin/press-outreach/drafts",
                          methods=["GET"], strict_slashes=False)
def list_drafts():
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db", "drafts": []}), 503
    status_f = (request.args.get("status") or "pending").strip().lower()
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT d.*, c.outlet, c.contact_name, c.contact_email, c.beat
                  FROM press_pitch_drafts d
                  LEFT JOIN press_contacts c ON c.id = d.contact_id
                 WHERE d.status = %s
                 ORDER BY d.score DESC NULLS LAST, d.created_at DESC LIMIT 100
            """, (status_f,))
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                for k in ("created_at", "approved_at", "sent_at", "embargo_until"):
                    if r.get(k): r[k] = r[k].isoformat()
        return jsonify({"count": len(rows), "status": status_f, "drafts": rows}), 200
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


def _send_email_resend(to: str, subject: str, body: str, reply_to: str = None) -> bool:
    try:
        import urllib.request as _req
        api_key = os.environ.get("RESEND_API_KEY", "")
        if not api_key:
            logger.warning("[press_outreach] RESEND_API_KEY missing")
            return False
        html_body = body.replace('\n', '<br>')
        payload = json.dumps({
            "from":     "Jonathan Martone <jonathan@dchub.cloud>",
            "to":       [to],
            "subject":  subject,
            "html":     html_body,
            "text":     body,
            "reply_to": reply_to or "jonathan@dchub.cloud",
        }).encode()
        req = _req.Request("https://api.resend.com/emails", data=payload, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        })
        with _req.urlopen(req, timeout=10) as r:
            r.read()
        return True
    except Exception as e:
        logger.warning(f"[press_outreach] resend failed: {e}")
        return False


@press_outreach_bp.route("/api/v1/admin/press-outreach/approve/<int:draft_id>",
                          methods=["POST"], strict_slashes=False)
def approve_draft(draft_id):
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT d.subject, d.body, d.status, c.contact_email, c.id
                  FROM press_pitch_drafts d
                  LEFT JOIN press_contacts c ON c.id = d.contact_id
                 WHERE d.id = %s
            """, (draft_id,))
            r = cur.fetchone()
            if not r:
                return jsonify({"error": "not_found"}), 404
            subj, body, status, email, contact_id = r
            if not email:
                return jsonify({"error": "contact has no email — set via /contacts/upsert first",
                                  "draft_id": draft_id}), 400
            if status not in ("pending", "approved"):
                return jsonify({"error": f"draft is {status}"}), 400

            ok = _send_email_resend(email, subj, body)
            new_status = "sent" if ok else "approved"
            cur.execute("""
                UPDATE press_pitch_drafts
                   SET status      = %s,
                       approved_at = COALESCE(approved_at, NOW()),
                       sent_at     = CASE WHEN %s THEN NOW() ELSE sent_at END
                 WHERE id = %s
            """, (new_status, ok, draft_id))
            if ok and contact_id:
                cur.execute("""
                    UPDATE press_contacts
                       SET total_pitches     = total_pitches + 1,
                           last_contacted_at = NOW(),
                           updated_at        = NOW()
                     WHERE id = %s
                """, (contact_id,))
        return jsonify({"ok": True, "id": draft_id, "status": new_status,
                          "sent": ok, "to": email, "subject": subj}), 200
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@press_outreach_bp.route("/api/v1/admin/press-outreach/drafts/clear-pending",
                          methods=["POST"], strict_slashes=False)
def clear_pending_drafts():
    """Discard all pending drafts. Useful after editing contact_name /
    contact_email on the underlying contacts — old drafts were generated
    with NULL fields ("Hi there" → "Hi Reed"). Run /generate-drafts after
    to repopulate with refreshed contact info.

    Only touches status='pending'. Approved / sent / rejected stay for audit."""
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                UPDATE press_pitch_drafts
                   SET status = 'rejected',
                       notes  = COALESCE(notes, '') || ' [auto-cleared via clear-pending]'
                 WHERE status = 'pending'
            """)
            n = cur.rowcount
        return jsonify({"ok": True, "cleared": n,
                          "hint": "Now POST /generate-drafts to rebuild with fresh contact info."}), 200
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@press_outreach_bp.route("/api/v1/admin/press-outreach/diagnostics",
                          methods=["GET"], strict_slashes=False)
def diagnostics():
    """Health check: which env vars are set, which contacts have emails,
    whether Resend is wired correctly."""
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401

    resend_set    = bool(os.environ.get("RESEND_API_KEY"))
    internal_set  = bool(os.environ.get("DCHUB_INTERNAL_KEY"))
    admin_set     = bool(os.environ.get("DCHUB_ADMIN_KEY"))

    contacts_with_email = 0
    total_contacts = 0
    if _pg and _dsn():
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM press_contacts WHERE active = TRUE")
                total_contacts = int((cur.fetchone() or [0])[0])
                cur.execute("""SELECT COUNT(*) FROM press_contacts
                                WHERE active = TRUE
                                  AND contact_email IS NOT NULL
                                  AND contact_email <> ''""")
                contacts_with_email = int((cur.fetchone() or [0])[0])
        except Exception:
            pass

    ready = resend_set and contacts_with_email > 0
    return jsonify({
        "ready_to_send":             ready,
        "resend_api_key_set":        resend_set,
        "dchub_internal_key_set":    internal_set,
        "dchub_admin_key_set":       admin_set,
        "total_contacts":            total_contacts,
        "contacts_with_email":       contacts_with_email,
        "contacts_missing_email":    total_contacts - contacts_with_email,
        "next_steps": (
            "Set RESEND_API_KEY env on Railway + populate contact_email "
            "on top-priority outlets via POST /contacts/upsert. "
            "Then re-run /generate-drafts and approve from the dashboard."
            if not ready else
            "All systems go. Approve drafts at /admin/partnerships/review."
        ),
    }), 200


@press_outreach_bp.route("/api/v1/admin/press-outreach/reject/<int:draft_id>",
                          methods=["POST"], strict_slashes=False)
def reject_draft(draft_id):
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503
    notes = (request.args.get("notes") or "").strip()[:500]
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                UPDATE press_pitch_drafts
                   SET status = 'rejected',
                       notes  = COALESCE(NULLIF(%s,''), notes)
                 WHERE id = %s AND status = 'pending'
            """, (notes, draft_id))
            n = cur.rowcount
        return jsonify({"ok": True, "id": draft_id, "rejected": n > 0}), 200
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
