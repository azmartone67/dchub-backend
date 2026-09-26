"""
ai_lab_outreach.py — Phase r63 (2026-05-25).

Targeted outreach campaign module for the 9 AI labs + GPU clouds the
founder explicitly wants citing DC Hub as a resource:

  Perplexity, Groq, Gemini (Google DeepMind), Mistral,
  NVIDIA, CoreWeave, Lambda, TensorWave, Core42

Each target gets a personalized pitch surfacing what THEY would
specifically unlock from DC Hub's data:

  - Perplexity / Gemini: citation-engine value — real-time DC
    market data their answer-generation can quote with attribution
  - Groq: their inference customers need to know WHERE the chips
    physically sit + power profile of those locations
  - Mistral: European DC market data (we just added London,
    Frankfurt, Amsterdam, Paris, Marseille, Stockholm)
  - NVIDIA: "where to deploy" intel for their hyperscaler customers
  - CoreWeave / Lambda / TensorWave: interconnection queue +
    power-availability intel for their next build
  - Core42: UAE/MENA-adjacent intel (we have global ISO coverage)

Architecture mirrors mcp_registry_outreach.py — admin-keyed endpoints
to list targets, draft pitches, and (with explicit per-target confirm)
fire emails via the existing outreach plumbing.

Endpoints:

  GET  /api/v1/admin/ai-lab-outreach/targets
       — list all 9 targets + their per-company pitch state

  POST /api/v1/admin/ai-lab-outreach/draft/<slug>
       — generate a personalized draft for one target

  POST /api/v1/admin/ai-lab-outreach/draft-all?category=<category>
       — draft every active target in one category

2026-09-26: the 9 targets above are RETIRED (per-target "retired" flag) and
the lane now pitches agent builders, MCP clients and energy-agent teams.
Retired targets cannot be drafted, and a stored draft for one cannot be sent.

  GET  /api/v1/admin/ai-lab-outreach/drafts/<slug>
       — read back a previously generated draft

Drafts are persisted to ai_lab_outreach_drafts table. The founder
reviews + sends manually (these are high-touch sales pitches, not
auto-send territory). A future round can wire to /api/v1/outreach/send.
"""
from __future__ import annotations

import datetime
import json
import logging
import os

from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write


ai_lab_outreach_bp = Blueprint("ai_lab_outreach", __name__)

# ★ _perform_resend_send() logs a claim-gate block through this. It was used
# and never defined, so a blocked draft raised NameError (HTTP 500) and took
# the rest of the /auto-send loop down with it.
logger = logging.getLogger(__name__)


# ── The 9 targets ──────────────────────────────────────────────────
# Each target has:
#   slug         — URL-safe ID
#   name         — display name
#   category     — ai_lab | gpu_cloud | hyperscaler_oem
#   contact_url  — best public contact form / dev relations page
#   intent_url   — page on their site that proves they care about DC
#                  intel (their infra page, partners page, etc.)
#   value_pitch  — 1-sentence summary of what they unlock
#   integration  — recommended path (MCP server / REST API / dataset)

# ── Manual-lane slugs: NEVER sent by this module ────────────────────
# 2026-09-26 owner decision: the restarted partner outreach (agent builders,
# MCP clients, energy agents) is sent by hand from the owner's Gmail and
# tracked in HubSpot, not by the 17:17Z auto-send cron. /auto-send picks ANY
# status='draft' row with an email -- it never consults _TARGETS -- so a row
# for one of these slugs (inserted by hand, or by a future _TARGETS entry)
# would otherwise be mailed with no human step. The exclusion lives in the
# auto-send SELECT (so these rows never use up ?limit) AND in
# _perform_resend_send (the one choke point both send routes share).
_MANUAL_LANE_SLUGS = frozenset({
    "composio", "pipeworx", "typingmind", "paces", "transect",
})

_TARGETS = [
    {
        "slug":        "perplexity",
        "retired":     True,
        "name":        "Perplexity",
        "category":    "ai_lab",
        "contact_url": "https://www.perplexity.ai/hub/contact",
        "target_email": "partnerships@perplexity.ai",
        "intent_url":  "https://www.perplexity.ai/hub",
        "value_pitch": ("Real-time data center market data Perplexity can cite "
                         "with attribution when users ask 'where are AI data centers "
                         "being built' — answers we already see Perplexity hallucinating."),
        "integration": "mcp_server",
        "audience_size_hint": "Perplexity has 22M+ MAU; even 0.1% of answer-citation "
                                "share = 22k impressions/month for DC Hub.",
    },
    {
        "slug":        "groq",
        "retired":     True,
        "name":        "Groq",
        "category":    "ai_lab",
        "contact_url": "https://groq.com/contact-sales/",
        "target_email": "partnerships@groq.com",
        "intent_url":  "https://groq.com/about-us/",
        "value_pitch": ("Groq's customers buy inference but care WHERE the LPUs sit. "
                         "DC Hub's per-facility power, water, fiber, and grid intel makes "
                         "Groq's location commitments quotable + verifiable."),
        "integration": "rest_api",
        "audience_size_hint": "Groq powers Llama, Mixtral, and Whisper inference at "
                                "scale — every inference token routes through a DC Hub-tracked facility.",
    },
    {
        "slug":        "gemini",
        "retired":     True,
        "name":        "Google DeepMind / Gemini",
        "category":    "ai_lab",
        "contact_url": "https://deepmind.google/about/contact/",
        "target_email": "press@deepmind.com",
        "intent_url":  "https://deepmind.google/discover/",
        "value_pitch": ("Gemini already has the world's best data center expertise "
                         "(Google's own infra). What it LACKS is competitive intel: "
                         "hyperscaler M&A, power-pipeline tracking outside Google, "
                         "interconnection queues at non-Google sites. DC Hub fills "
                         "the not-Google blind spot."),
        "integration": "mcp_server",
        "audience_size_hint": "Gemini API + Vertex AI = millions of dev sessions/day. "
                                "Tool-use surface is the leverage point.",
    },
    {
        "slug":        "mistral",
        "retired":     True,
        "name":        "Mistral",
        "category":    "ai_lab",
        "contact_url": "https://mistral.ai/contact/",
        "target_email": "partnerships@mistral.ai",
        "intent_url":  "https://mistral.ai/news/",
        "value_pitch": ("DC Hub just shipped 16 international markets (London, Paris, "
                         "Frankfurt, Amsterdam, Stockholm, Marseille, Dublin) — Mistral's "
                         "home turf. We're the only daily-refreshing scorecard of "
                         "European data center power availability."),
        "integration": "mcp_server",
        "audience_size_hint": ("Mistral's customers are largely European enterprises "
                                "deploying GenAI on-continent due to GDPR + data "
                                "residency. Power-availability intel for EU = decision-grade."),
    },
    {
        "slug":        "nvidia",
        "retired":     True,
        "name":        "NVIDIA",
        "category":    "hyperscaler_oem",
        "contact_url": "https://www.nvidia.com/en-us/contact/",
        "target_email": "partnerships@nvidia.com",
        "intent_url":  "https://www.nvidia.com/en-us/data-center/",
        "value_pitch": ("Every GPU NVIDIA ships ends up in a data center DC Hub "
                         "tracks. NVIDIA's hyperscaler customers (CoreWeave, Lambda, "
                         "Microsoft, Meta, Oracle) need to choose markets — DC Hub's "
                         "DCPI tells them which markets have the power, queue, and "
                         "fiber to actually host their next deployment."),
        "integration": "rest_api",
        "audience_size_hint": "NVIDIA's DGX Cloud, Inception, and partner ecosystems "
                                "all touch site selection. Their CSP partners need this.",
    },
    {
        "slug":        "coreweave",
        "retired":     True,
        "name":        "CoreWeave",
        "category":    "gpu_cloud",
        "contact_url": "https://www.coreweave.com/contact-sales",
        "target_email": "partnerships@coreweave.com",
        "intent_url":  "https://www.coreweave.com/data-centers",
        "value_pitch": ("CoreWeave is in active hyperscale build mode — Plano, Las "
                         "Vegas, Chicago, Atlanta, Las Cruces all under construction "
                         "or recently announced. DC Hub tracks every ISO interconnection "
                         "queue + DCPI verdict for every market CoreWeave is in OR could "
                         "be in next. Pre-build site selection signal."),
        "integration": "mcp_server",
        "audience_size_hint": "CoreWeave is publicly committed to $1B+/quarter in new "
                                "DC capacity. Every site decision rides on power data.",
    },
    {
        "slug":        "lambda",
        "retired":     True,
        "name":        "Lambda",
        "category":    "gpu_cloud",
        "contact_url": "https://lambda.ai/contact",
        "target_email": "partnerships@lambda.ai",
        "intent_url":  "https://lambda.ai/blog",
        "value_pitch": ("Lambda's 1-Click Clusters and on-demand H100/H200 capacity "
                         "depend on facility-level uptime, fiber, and power-availability "
                         "data. DC Hub tracks the underlying facilities Lambda colocates "
                         "in — verifiable transparency for their enterprise customers."),
        "integration": "rest_api",
        "audience_size_hint": "Lambda just raised $480M Series D, scaling out aggressively. "
                                "Every new region needs DCPI input.",
    },
    {
        "slug":        "tensorwave",
        "retired":     True,
        "name":        "TensorWave",
        "category":    "gpu_cloud",
        "contact_url": "https://tensorwave.com/contact",
        "target_email": "partnerships@tensorwave.com",
        "intent_url":  "https://tensorwave.com/about",
        "value_pitch": ("TensorWave is the AMD MI300X-first cloud — differentiates on "
                         "supply availability + power efficiency. DC Hub tracks which "
                         "markets have the spare power and fiber to host high-density "
                         "AMD deployments without grid-constraint bottlenecks. "
                         "Stockholm, Montréal, Cheyenne are TensorWave-grade today."),
        "integration": "rest_api",
        "audience_size_hint": "TensorWave's pitch is 'we're not just AMD, we're AMD "
                                "in the right places.' DCPI proves which places are right.",
    },
    {
        "slug":        "core42",
        "retired":     True,
        "name":        "Core42 (UAE / G42)",
        "category":    "gpu_cloud",
        "contact_url": "https://www.core42.ai/contact-us",
        "target_email": "info@core42.ai",
        "intent_url":  "https://www.core42.ai/about-us",
        "value_pitch": ("Core42 operates massive AI compute capacity across UAE + "
                         "expanding globally. DC Hub's recent international expansion "
                         "(Singapore, Sydney, Frankfurt, London, plus deep US coverage) "
                         "gives Core42 the global site-selection comparable-set their "
                         "investors and partners ask for."),
        "integration": "mcp_server",
        "audience_size_hint": "Core42 is one of the most strategically important "
                                "non-US AI infrastructure players. The strategic "
                                "narrative needs global comparables that we now ship.",
    },
    # ── 2026-09-26 restart: agent builders, MCP clients, energy agents ──
    # Owner-approved first batch. Every target_email was read off the
    # company's own page (contact_url), never inferred from a pattern.
    # value_pitch is prose only: figures come from canon in _draft_pitch().
    {
        "slug":        "composio",
        "name":        "Composio",
        "category":    "agent_builder",
        "contact_url": "https://composio.dev/partnerships",
        "target_email": "partnerships@composio.dev",
        "value_pitch": ("We'd like to be a Composio toolkit. Your partnership "
                         "page has a Toolkit Partnership track, and we didn't "
                         "find a toolkit in your catalog that covers power "
                         "availability, data-center siting or grid questions."),
        "integration": "openapi_and_mcp",
    },
    {
        "slug":        "pipeworx",
        "name":        "Pipeworx",
        "category":    "agent_builder",
        "contact_url": "https://pipeworx.io/contact",
        "target_email": "support@pipeworx.io",
        "value_pitch": ("This is a data-coverage request, the kind your FAQ "
                         "invites: Pipeworx already carries a grid pack, and a "
                         "DC Hub pack would add the data-center facilities, fiber, "
                         "gas and siting layers that sit on top of it."),
        "integration": "openapi_and_mcp",
    },
    {
        "slug":        "typingmind",
        "name":        "TypingMind",
        "category":    "mcp_client",
        "contact_url": "https://custom.typingmind.com/contact",
        "target_email": "partner@typingmind.com",
        "value_pitch": ("We'd like to be in TypingMind's MCP Store. It is a "
                         "remote streamable-HTTP server, it works keyless on a "
                         "free tier, and it answers questions your users "
                         "otherwise get stale training-data answers to."),
        "integration": "mcp_server",
    },
    {
        "slug":        "paces",
        "name":        "Paces",
        "category":    "energy_agent",
        "contact_url": "https://www.paces.com/products/ai",
        "target_email": "sales@paces.com",
        "value_pitch": ("Paces Agent works for the same developers we serve. "
                         "DC Hub could sit alongside it as a data layer for the "
                         "parts of a siting question it may not cover: live grid "
                         "telemetry, fiber lead-in, gas economics and the existing "
                         "data-center footprint around a site."),
        "integration": "openapi_and_mcp",
    },
    {
        "slug":        "transect",
        "name":        "Transect",
        "category":    "energy_agent",
        "contact_url": "https://www.transect.com/company/contact-us/",
        "target_email": "info@transect.com",
        "value_pitch": ("Transect already covers environmental, permitting and "
                         "community risk for a site. DC Hub adds the power, fiber "
                         "and grid layers a data-center siting call also turns on."),
        "integration": "openapi_and_mcp",
    },
]

# ★ The 9 original AI-lab / GPU-cloud targets are retired (2026-09-26 owner
# decision): several had 5 emails each, some carrying false figures. They
# span THREE categories (ai_lab, gpu_cloud, hyperscaler_oem), so retirement
# is a per-target flag, not a category filter. A retired target cannot be
# drafted, and a draft already stored for one cannot be sent.
_ACTIVE_TARGETS = [t for t in _TARGETS if not t.get("retired")]
_ACTIVE_SLUGS = frozenset(t["slug"] for t in _ACTIVE_TARGETS)
_ACTIVE_CATEGORIES = frozenset(t["category"] for t in _ACTIVE_TARGETS)


# ── Helpers ─────────────────────────────────────────────────────────

def _db_conn():
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        return psycopg2.connect(url, connect_timeout=5) if url else None
    except Exception:
        return None


def _admin_authorized() -> bool:
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    return bool(expected) and provided == expected


def _ensure_table():
    c = _db_conn()
    if not c: return
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_lab_outreach_drafts (
                    id          SERIAL PRIMARY KEY,
                    target_slug TEXT NOT NULL,
                    subject     TEXT,
                    body        TEXT NOT NULL,
                    contact_url TEXT,
                    status      TEXT NOT NULL DEFAULT 'draft',
                    sent_at     TIMESTAMPTZ,
                    response_at TIMESTAMPTZ,
                    response_text TEXT,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (target_slug, created_at)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ai_lab_outreach_slug_idx
                    ON ai_lab_outreach_drafts (target_slug)
            """)
            # 2026-06-03: hybrid send path — Resend for the 4 labs with public
            # partnerships emails, contact forms for the rest. target_email is
            # nullable; if NULL, /send-via-resend returns 400 with "use form".
            cur.execute("ALTER TABLE ai_lab_outreach_drafts ADD COLUMN IF NOT EXISTS target_email TEXT")
            cur.execute("ALTER TABLE ai_lab_outreach_drafts ADD COLUMN IF NOT EXISTS resend_id TEXT")
            # ★2026-08-30 — ACCEPTED IS NOT SENT IS NOT DELIVERED, and this
            # table collapsed all three into status='sent'. Resend returns
            # HTTP 200 WITH A MESSAGE ID for a SUPPRESSED recipient and never
            # attempts delivery. Measured that day: six of the nine AI-lab
            # targets were on the suppression list (origin Bounce, five of them
            # added in one batch 3 months ago), so ~30 of the 45 drafts marked
            # 'sent' never left Resend. `status` is left alone because
            # auto_send filters on 'draft' and the 24h gate keys on sent_at;
            # the truth rides here instead.
            cur.execute("ALTER TABLE ai_lab_outreach_drafts "
                        "ADD COLUMN IF NOT EXISTS delivery_state TEXT")
            cur.execute("ALTER TABLE ai_lab_outreach_drafts "
                        "ADD COLUMN IF NOT EXISTS delivery_state_at TIMESTAMPTZ")
            # Backfill target_email on existing rows where it's still NULL —
            # idempotent (only touches NULL rows) and cheap (table is small).
            # The 4 hybrid-eligible labs as of 2026-06-03 get populated; the
            # other 5 stay NULL → /send-via-resend will refuse with 'no_target_email'
            # and route the operator to the contact form.
            for _t in _TARGETS:
                if _t.get("target_email"):
                    cur.execute(
                        "UPDATE ai_lab_outreach_drafts "
                        "   SET target_email = %s "
                        " WHERE target_slug = %s AND target_email IS NULL",
                        (_t["target_email"], _t["slug"]),
                    )
            c.commit()
    except Exception:
        note_swallowed_write("ai_lab_outreach_drafts", where="ai_lab_outreach._ensure_table")
        pass
    finally:
        try: c.close()
        except Exception: pass


def _draft_pitch(target: dict) -> tuple[str, str]:
    """Build the subject + body for one target. Returns (subject, body).

    ★2026-09-26 rewrite (owner-approved copy). What was removed, and why:
      - A developer key minted per target and printed in the body. A cold
        email is not where a live credential belongs; the recipient claims
        their own key from /signup?ref=partner-<slug> instead (not /claim,
        which 301s to /upgrade, and not /api/v1/keys/claim, which is
        POST-only and answers a clicked link with 405). The ref attributes
        the signup to the partner; /connect stays as the setup guide.
      - The https://dchub.cloud/partners/<slug> link. Partner pages exist only
        for the retired lab slugs; every new slug 404s there.
      - Literal counts ("23+ tools", "17 high-value tools", "500 calls/day")
        and unverifiable claims ("already cited by Claude and Cursor", "the
        only daily-refreshing public scorecard"). The claim gate catches only
        figures ABOVE canon, so an understated or qualitative claim sails
        through it. The only figures left are canon floors from
        _canon_public(), which the gate checks.
    """
    name = target["name"]
    category = target.get("category") or ""
    pitch = target["value_pitch"]
    integration = target.get("integration") or "mcp_server"
    _pub = _canon_public()

    if category == "mcp_client":
        subject = f"A data-center and power-grid MCP server for {name}"
    elif category == "agent_builder":
        subject = f"DC Hub for {name}: data-center, grid and energy data for agents"
    else:
        subject = f"Power, fiber and grid layers for {name}"

    mcp_line = ("Remote MCP server (streamable HTTP): https://dchub.cloud/mcp\n"
                "  Server card: https://dchub.cloud/.well-known/mcp/server-card.json\n"
                "  Official MCP Registry: cloud.dchub/mcp-server")
    rest_line = "OpenAPI spec: https://dchub.cloud/openapi.json"
    access = (mcp_line if integration == "mcp_server"
              else f"{mcp_line}\n  {rest_line}")

    body = f"""Hi {name} team,

I'm Jonathan Martone, founder of DC Hub (dchub.cloud). We answer questions
about the physical infrastructure behind AI: {_pub['facilities']} data-center
facilities, {_pub['markets']} scored markets, live ISO grid telemetry,
interconnection queues, fiber, gas and water risk. Every answer carries its
source and says what it does not cover.

{pitch}

How to try it:
  {access}
  Setup guide, free tier included: https://dchub.cloud/connect
  A key for your team to test with: https://dchub.cloud/signup?ref=partner-{target['slug']}

If your users build agents on top of DC Hub, they can register them at
https://dchub.cloud/ai-agents and we'll help them get set up.

What would {name} need from us to take this further: a config snippet, a
logo, a test account for your team?

Best,
Jonathan
Founder, DC Hub
jonathan@dchub.cloud · dchub.cloud
"""
    return subject, body


# ── Endpoints ───────────────────────────────────────────────────────

@ai_lab_outreach_bp.route(
    "/api/v1/admin/ai-lab-outreach/targets", methods=["GET"]
)
def list_targets():
    """List all 9 outreach targets + their current draft state."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    _ensure_table()

    # Pull latest draft per target
    latest_drafts = {}
    c = _db_conn()
    if c:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT ON (target_slug)
                           target_slug, status, created_at, sent_at,
                           response_at, id
                      FROM ai_lab_outreach_drafts
                     ORDER BY target_slug, created_at DESC
                """)
                for r in cur.fetchall() or []:
                    latest_drafts[r[0]] = {
                        "draft_id":    r[5],
                        "status":      r[1],
                        "drafted_at":  r[2].isoformat() if r[2] else None,
                        "sent_at":     r[3].isoformat() if r[3] else None,
                        "responded_at": r[4].isoformat() if r[4] else None,
                    }
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass

    out = []
    for t in _TARGETS:
        d = dict(t)
        d["latest_draft"] = latest_drafts.get(t["slug"])
        out.append(d)
    return jsonify({
        "ok":            True,
        "target_count":  len(out),
        "categories":    sorted({t["category"] for t in _TARGETS}),
        "targets":       out,
        "draft_one":     "POST /api/v1/admin/ai-lab-outreach/draft/<slug>",
        "draft_all":     "POST /api/v1/admin/ai-lab-outreach/draft-all",
    }), 200


@ai_lab_outreach_bp.route(
    "/api/v1/admin/ai-lab-outreach/draft/<slug>", methods=["POST"]
)
def draft_one(slug):
    """Generate a personalized draft for one target."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    _ensure_table()

    target = next((t for t in _TARGETS if t["slug"] == slug), None)
    if not target:
        return jsonify({
            "ok":            False,
            "error":         "unknown_target",
            "valid_slugs":   sorted(_ACTIVE_SLUGS),
        }), 404
    # A stored draft is mailed by the 17:17Z autopilot with no further human
    # step, so drafting a retired target would be re-mailing it.
    if target.get("retired"):
        return jsonify({
            "ok":            False,
            "error":         "target_retired",
            "slug":          slug,
            "valid_slugs":   sorted(_ACTIVE_SLUGS),
        }), 410

    subject, body = _draft_pitch(target)

    c = _db_conn()
    new_id = None
    if c:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    INSERT INTO ai_lab_outreach_drafts
                        (target_slug, subject, body, contact_url, target_email, status)
                    VALUES (%s, %s, %s, %s, %s, 'draft')
                    RETURNING id
                """, (slug, subject, body, target.get("contact_url"), target.get("target_email")))
                new_id = (cur.fetchone() or [None])[0]
                c.commit()
        except Exception:
            note_swallowed_write("ai_lab_outreach_drafts", where="ai_lab_outreach.draft_one")
            pass
        finally:
            try: c.close()
            except Exception: pass

    return jsonify({
        "ok":          True,
        "draft_id":    new_id,
        "target":      target,
        "subject":     subject,
        "body":        body,
        "next_step":   (f"Review the body. To send: paste into your email client "
                          f"+ POST to /api/v1/admin/ai-lab-outreach/sent/{slug} "
                          f"to mark sent (future feature: wire to outreach/send)."),
    }), 200


@ai_lab_outreach_bp.route(
    "/api/v1/admin/ai-lab-outreach/draft-all", methods=["POST"]
)
def draft_all():
    """Generate drafts for every active target in ONE category.

    ★ ?category= is required. Every stored draft is mailed by the 17:17Z
    autopilot with no further human step, so "draft all" used to mean "mail
    everyone we have ever listed". One category per call bounds a batch to
    what was reviewed, and retired targets are never drafted."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    category = (request.args.get("category") or "").strip()
    if category not in _ACTIVE_CATEGORIES:
        return jsonify({
            "ok":               False,
            "error":            "category_required",
            "valid_categories": sorted(_ACTIVE_CATEGORIES),
        }), 400
    _ensure_table()

    drafted = []
    for target in (t for t in _ACTIVE_TARGETS if t["category"] == category):
        subject, body = _draft_pitch(target)
        c = _db_conn()
        new_id = None
        if c:
            try:
                with c.cursor() as cur:
                    cur.execute("""
                        INSERT INTO ai_lab_outreach_drafts
                            (target_slug, subject, body, contact_url, target_email, status)
                        VALUES (%s, %s, %s, %s, %s, 'draft')
                        RETURNING id
                    """, (target["slug"], subject, body, target.get("contact_url"), target.get("target_email")))
                    new_id = (cur.fetchone() or [None])[0]
                    c.commit()
            except Exception:
                note_swallowed_write("ai_lab_outreach_drafts", where="ai_lab_outreach.draft_all")
                pass
            finally:
                try: c.close()
                except Exception: pass
        drafted.append({
            "slug":         target["slug"],
            "name":         target["name"],
            "draft_id":     new_id,
            "subject":      subject,
            "contact_url":  target["contact_url"],
            "target_email": target.get("target_email"),
            "send_path":    ("resend" if target.get("target_email") else "form"),
            "body_preview": body[:300] + "...",
        })

    return jsonify({
        "ok":            True,
        "category":      category,
        "drafted_count": len(drafted),
        "drafts":        drafted,
        "next_step":     ("Drafts with target_email: send via "
                            "POST /api/v1/admin/ai-lab-outreach/send-via-resend/<draft_id>. "
                            "Drafts without (send_path='form'): copy body into the contact_url, "
                            "then POST /api/v1/admin/ai-lab-outreach/sent/<draft_id> to bookkeep."),
    }), 200


@ai_lab_outreach_bp.route(
    "/api/v1/admin/ai-lab-outreach/drafts/<slug>", methods=["GET"]
)
def get_drafts(slug):
    """Read back drafts for one target (latest first)."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    c = _db_conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, target_slug, subject, body, contact_url,
                       status, sent_at, response_at, response_text,
                       created_at, target_email, resend_id
                  FROM ai_lab_outreach_drafts
                 WHERE target_slug = %s
                 ORDER BY created_at DESC
                 LIMIT 5
            """, (slug,))
            rows = cur.fetchall() or []
    except Exception as e:
        try: c.close()
        except Exception: pass
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
    finally:
        try: c.close()
        except Exception: pass

    out = [{
        "id":            r[0], "target_slug": r[1], "subject": r[2],
        "body":          r[3], "contact_url": r[4],
        "status":        r[5],
        "sent_at":       r[6].isoformat() if r[6] else None,
        "response_at":   r[7].isoformat() if r[7] else None,
        "response_text": r[8], "created_at": r[9].isoformat() if r[9] else None,
        "target_email":  r[10] if len(r) > 10 else None,
        "resend_id":     r[11] if len(r) > 11 else None,
    } for r in rows]
    return jsonify({
        "ok":     True,
        "slug":   slug,
        "drafts": out,
    }), 200


@ai_lab_outreach_bp.route(
    "/api/v1/admin/ai-lab-outreach/sent/<int:draft_id>", methods=["POST"]
)
def mark_sent(draft_id):
    """Mark a draft as sent (after the operator manually emails it)."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    c = _db_conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200
    try:
        with c.cursor() as cur:
            cur.execute("""
                UPDATE ai_lab_outreach_drafts
                   SET status = 'sent', sent_at = NOW()
                 WHERE id = %s
             RETURNING target_slug, subject, sent_at
            """, (draft_id,))
            row = cur.fetchone()
            c.commit()
    except Exception as e:
        try: c.close()
        except Exception: pass
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
    finally:
        try: c.close()
        except Exception: pass

    if not row:
        return jsonify({"ok": False, "error": "draft_not_found"}), 404

    return jsonify({
        "ok":          True,
        "draft_id":    draft_id,
        "target_slug": row[0],
        "subject":     row[1],
        "sent_at":     row[2].isoformat() if row[2] else None,
    }), 200


# r-hybrid-send (2026-06-03): actually deliver an AI-lab outreach draft
# via Resend. Caller fires this with a draft_id; we look up, validate, send,
# and mark sent on 2xx.
#
# r-guard (2026-06-03 +): added 24h per-target rate limit. After Perplexity/
# Mistral/CoreWeave/Lambda each received the same body twice (7min apart
# due to fresh-draft re-send), we now refuse if ANY draft for the same
# target_slug was sent in the last 24h. Pass ?force=1 to override (e.g.
# intentional follow-up with different copy).
#
# r-auto (2026-06-03 +): factored core logic into _perform_resend_send()
# so the new /auto-send cron-fireable endpoint can reuse it.


def _canon_public() -> dict:
    """Canon floor phrases for outbound copy. Floors only — never a live count.

    A floor phrase ("18,500+") stays true as the real number grows, so copy
    built from it cannot age into an over-claim. resolve_canon() is still
    deliberately NOT used: it DEGRADES rather than raising (observed returning
    facilities=400 against a floor of 18,500), and a 45x under-claim in a
    partner email is not an improvement on a stale one.

    ★2026-09-19: the floors are RESOLVED, through resolve_public_floors_cached().
    That resolver is the opposite shape to resolve_canon(): its overlay ONLY
    EVER RAISES, so it cannot produce the 45x under-claim above, and it never
    blocks or raises. A dead resolver leaves the pin standing.

    ★ _claim_gate() READS THE SAME FLOORS, and it has to. The gate refuses to
    send any figure ABOVE its canon, so copy built here from a risen floor and
    checked there against the pin would block every draft — the outreach lane
    would go dark refusing its own correct numbers. The two must move together
    or not at all.
    """
    import ai_surface_canon as _c
    pub = dict(_c.PINNED.get("public") or {})
    try:
        pub.update(_c.resolve_public_floors_cached() or {})
    except Exception:          # documented never to raise; belt and braces
        pass
    return {
        "facilities": pub.get("facilities", "20,100+"),
        "deals":      pub.get("deals", "2,000+"),
        "markets":    pub.get("markets", "300+"),
    }


def _claim_gate(body: str) -> list:
    """Every over-claim in `body`, or [] if it is safe to send.

    ★ FAILS CLOSED. Canon or the live total being unreadable is NOT permission
    to send — an unverifiable claim is precisely the one worth stopping.
    resolve_canon() is documented to DEGRADE rather than raise (observed
    returning facilities=400 against a floor of 18,500), so a resolver hiccup
    must never be read as "no violations found".

    ★2026-09-19: canon is the RESOLVED floor, the same one _canon_public()
    writes the copy from. Not a loosening of the gate and not optional: the
    gate blocks any figure ABOVE canon, so a pinned ceiling under risen copy
    would mark every draft `blocked_claims` and the lane would go dark
    refusing its own correct numbers.

    The ceiling still cannot fall below the pin — the overlay only RAISES —
    so a dead resolver leaves the STRICTER pinned ceiling in place. That is
    the fail-closed direction. The canon import failing is still a refusal.

    ★ KNOWN, ACCEPTED: a body is built at draft time and checked at send time.
    If the live floor falls back toward the pin between the two (a dedup pass
    lowering a count past a rounding boundary), the stored copy now exceeds
    canon and the draft is marked `blocked_claims` — which is exactly what the
    gate is for. Regenerate the draft; do not widen the gate.
    """
    from routes.outreach_claim_gate import verify_claims
    try:
        import ai_surface_canon as _c
        canon = dict(_c.PINNED.get("public") or {})
        try:
            canon.update(_c.resolve_public_floors_cached() or {})
        except Exception:      # documented never to raise; belt and braces
            pass
        markers = _c.PINNED.get("stale_markers") or ()
    except Exception as e:
        return [{"kind": "unverifiable", "noun": "canon",
                 "detail": f"canon unreadable ({str(e)[:80]}) — refusing to "
                           f"send unverified outbound copy"}]
    all_time = None
    try:
        from ai_tracking import get_total_requests_all_time as _tot
        all_time = _tot()
    except Exception:
        try:
            c2 = _db_conn()
            if c2:
                with c2.cursor() as cur2:
                    cur2.execute("SELECT COALESCE(SUM(total_requests),0) "
                                 "FROM ai_cumulative")
                    all_time = int(cur2.fetchone()[0] or 0) or None
                c2.close()
        except Exception:
            all_time = None
    return verify_claims(body, canon, all_time, markers)


def _perform_resend_send(draft_id: int, force: bool = False) -> tuple:
    """Send one draft via Resend. Returns (response_dict, http_status).
    Used by both /send-via-resend/<id> and /auto-send."""
    resend_key = (os.environ.get("DCHUB_RESEND_API_KEY") or "").strip()
    if not resend_key:
        return {
            "ok":    False,
            "error": "resend_not_configured",
            "hint":  "Set DCHUB_RESEND_API_KEY in Railway env.",
        }, 503

    from_email = os.environ.get(
        "DCHUB_FROM_EMAIL",
        "Jonathan Martone <jonathan@dchub.cloud>",
    )

    c = _db_conn()
    if not c:
        return {"ok": False, "error": "db_unavailable"}, 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, target_slug, subject, body, contact_url,
                       status, sent_at, target_email, resend_id
                  FROM ai_lab_outreach_drafts
                 WHERE id = %s
            """, (draft_id,))
            row = cur.fetchone()
    except Exception as e:
        try: c.close()
        except Exception: pass
        return {"ok": False, "error": str(e)[:200]}, 500

    if not row:
        try: c.close()
        except Exception: pass
        return {"ok": False, "error": "draft_not_found", "draft_id": draft_id}, 404

    (_id, target_slug, subject, body, contact_url,
     status, sent_at, target_email, prior_resend_id) = row

    # 00. Manual-lane slugs are sent by hand from Gmail, never from here.
    #     Checked before the claim gate and NOT bypassed by force=1.
    if target_slug in _MANUAL_LANE_SLUGS:
        try: c.close()
        except Exception: pass
        return {
            "ok":       False,
            "error":    "manual_lane_slug",
            "draft_id": draft_id,
            "target":   target_slug,
            "hint":     ("This target is contacted by hand (Gmail + HubSpot). "
                         "Remove it from _MANUAL_LANE_SLUGS to hand it back "
                         "to the Resend lane."),
        }, 409

    # 01. Retired or unlisted target — refuse before anything else, and leave
    #     the row untouched. Drafts written for the 9 retired lab targets can
    #     still sit in the table; neither /send-via-resend nor /auto-send may
    #     mail one, and force=1 does not change that.
    if target_slug not in _ACTIVE_SLUGS:
        try: c.close()
        except Exception: pass
        return {
            "ok":          False,
            "error":       "target_retired",
            "draft_id":    draft_id,
            "target_slug": target_slug,
        }, 410

    # 0. THE CLAIM GATE — before anything can leave. This is the one choke
    #    point /send-via-resend/<id> and /auto-send both flow through, which is
    #    why it lives here and not at either call site.
    #    ★ `force=1` does NOT bypass it. force exists to re-send a draft inside
    #    the 24h window; it was never meant to authorise a false claim, and a
    #    gate any caller can wave through is not a gate.
    _violations = _claim_gate(body)
    if _violations:
        try:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE ai_lab_outreach_drafts SET status = 'blocked_claims' "
                    " WHERE id = %s AND status = 'draft'", (draft_id,))
            c.commit()
        except Exception:
            try: c.rollback()
            except Exception: pass
        try: c.close()
        except Exception: pass
        logger.warning("ai_lab_outreach: draft %s BLOCKED — %s", draft_id,
                       "; ".join(v.get("detail", "") for v in _violations)[:400])
        return {
            "ok":         False,
            "error":      "claims_failed_verification",
            "draft_id":   draft_id,
            "target":     target_slug,
            "violations": _violations,
            "hint":       ("The draft asserts figures above canon, or one that "
                           "is impossible. Regenerate it from canon "
                           "(POST /draft/<slug>) and send the new draft — do "
                           "not edit the number and re-send the old one."),
        }, 409

    # 1. Form-only (no target_email).
    if not target_email:
        try: c.close()
        except Exception: pass
        return {
            "ok":          False,
            "error":       "no_target_email",
            "draft_id":    draft_id,
            "target_slug": target_slug,
            "send_path":   "form",
            "hint":        ("This target has no public partnerships email. "
                              "Paste the body into the contact form at "
                              + (contact_url or "<contact_url missing>") +
                              ", then POST /sent/" + str(draft_id) +
                              " to bookkeep."),
        }, 422

    # 2. This specific draft already sent (per-draft dup guard).
    if status == "sent" or sent_at:
        try: c.close()
        except Exception: pass
        return {
            "ok":          False,
            "error":       "already_sent",
            "draft_id":    draft_id,
            "target_slug": target_slug,
            "sent_at":     sent_at.isoformat() if sent_at else None,
            "resend_id":   prior_resend_id,
            "hint":        ("If you want to send again, create a fresh draft "
                              "with POST /draft-all or /draft/" + target_slug),
        }, 409

    # 3. 24h per-target rate limit (r-guard). Refuses if ANY draft for the
    #    same target_slug was sent in the last 24h. force=True overrides.
    if not force:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT id, sent_at FROM ai_lab_outreach_drafts
                     WHERE target_slug = %s
                       AND sent_at IS NOT NULL
                       AND sent_at > NOW() - INTERVAL '24 hours'
                     ORDER BY sent_at DESC
                     LIMIT 1
                """, (target_slug,))
                recent = cur.fetchone()
        except Exception:
            recent = None
        if recent:
            try: c.close()
            except Exception: pass
            return {
                "ok":              False,
                "error":           "rate_limited_24h",
                "draft_id":        draft_id,
                "target_slug":     target_slug,
                "recent_draft_id": recent[0],
                "recent_sent_at":  recent[1].isoformat() if recent[1] else None,
                "hint":            ("Already sent to this target within last "
                                       "24h. Pass ?force=1 to override (e.g. "
                                       "intentional follow-up with new copy)."),
            }, 429

    # 4. Fire Resend.
    try:
        import requests as _rq
        rr = _rq.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type":  "application/json",
            },
            json={
                "from":     from_email,
                "to":       [target_email],
                "reply_to": "jonathan@dchub.cloud",
                "subject":  subject,
                "text":     body,
            },
            timeout=15,
        )
    except Exception as e:
        try: c.close()
        except Exception: pass
        return {"ok": False, "error": "resend_call_failed",
                  "detail": str(e)[:200]}, 502

    if rr.status_code >= 400:
        try: c.close()
        except Exception: pass
        return {
            "ok":             False,
            "error":          "resend_rejected",
            "resend_status":  rr.status_code,
            "resend_body":    (rr.text or "")[:500],
        }, 502

    resend_data = {}
    try:
        resend_data = rr.json() or {}
    except Exception:
        pass
    resend_id = resend_data.get("id")

    # 5. Mark sent + record resend_id.
    try:
        with c.cursor() as cur:
            # ★ delivery_state='submitted' — Resend ACCEPTED it. That is all
            # a 200 means. It is promoted to 'delivered' (or 'bounced') only by
            # a webhook event in routes/resend_webhook.py. A row that sits at
            # 'submitted' with no event is the suppression signal: Resend never
            # attempts a suppressed address, so it emits nothing at all.
            cur.execute("""
                UPDATE ai_lab_outreach_drafts
                   SET status = 'sent', sent_at = NOW(), resend_id = %s,
                       delivery_state = 'submitted', delivery_state_at = NOW()
                 WHERE id = %s
            """, (resend_id, draft_id))
            c.commit()
    except Exception:
        note_swallowed_write("ai_lab_outreach_drafts", where="ai_lab_outreach._perform_resend_send")
        pass
    finally:
        try: c.close()
        except Exception: pass

    return {
        "ok":           True,
        "draft_id":     draft_id,
        "target_slug":  target_slug,
        "target_email": target_email,
        "from":         from_email,
        "subject":      subject,
        "resend_id":    resend_id,
        "next_step":    "Watch jonathan@dchub.cloud inbox for a reply.",
    }, 200


@ai_lab_outreach_bp.route(
    "/api/v1/admin/ai-lab-outreach/send-via-resend/<int:draft_id>",
    methods=["POST"]
)
def send_via_resend(draft_id):
    """Send one drafted AI-lab outreach email via Resend.

    Form-only targets return 422. Recently-sent targets (same slug within
    24h) return 429 unless ?force=1. Same draft already sent returns 409.
    """
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    _ensure_table()
    force = (request.args.get("force") == "1"
             or request.args.get("force", "").lower() == "true")
    resp, status = _perform_resend_send(draft_id, force=force)
    return jsonify(resp), status


@ai_lab_outreach_bp.route(
    "/api/v1/admin/ai-lab-outreach/auto-send", methods=["POST"]
)
def auto_send():
    """Cron-fireable. For each target_slug, send the LATEST draft if:
      - status == 'draft' (not yet sent)
      - target_email is populated (resend-eligible; form-only targets skipped)
      - no draft for the same slug was sent in the last 24h
    Caps at ?limit=N (default 20) to stay polite.
    Returns per-target attempt log with status."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    _ensure_table()

    limit = min(int(request.args.get("limit", "20") or "20"), 100)
    only_slugs_arg = (request.args.get("slugs") or "").strip()
    only_slugs = ([s.strip() for s in only_slugs_arg.split(",") if s.strip()]
                   if only_slugs_arg else None)
    force = (request.args.get("force") == "1"
             or request.args.get("force", "").lower() == "true")

    # Find candidates: latest draft per slug, status='draft', has email.
    # The 24h gate is applied inside _perform_resend_send so the response
    # log clearly shows which slugs got rate-limited.
    c = _db_conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 503
    candidates = []
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (target_slug)
                       id, target_slug, target_email
                  FROM ai_lab_outreach_drafts
                 WHERE status = 'draft'
                   AND target_email IS NOT NULL
                   AND target_email <> ''
                   AND NOT (target_slug = ANY(%s))
                   AND target_slug = ANY(%s)
                 ORDER BY target_slug, created_at DESC
                 LIMIT %s
            """, (sorted(_MANUAL_LANE_SLUGS), sorted(_ACTIVE_SLUGS), limit))
            candidates = cur.fetchall() or []
    except Exception as e:
        try: c.close()
        except Exception: pass
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    finally:
        try: c.close()
        except Exception: pass

    if only_slugs:
        candidates = [r for r in candidates if r[1] in only_slugs]

    sent_log, skipped_log = [], []
    for (draft_id, slug, email) in candidates:
        resp, http_status = _perform_resend_send(draft_id, force=force)
        entry = {
            "draft_id":    draft_id,
            "target_slug": slug,
            "target_email": email,
            "http_status": http_status,
            "ok":          bool(resp.get("ok")),
            "resend_id":   resp.get("resend_id"),
            "error":       resp.get("error"),
        }
        (sent_log if resp.get("ok") else skipped_log).append(entry)

    return jsonify({
        "ok":            True,
        "as_of":         datetime.datetime.utcnow().isoformat() + "Z",
        "candidates":    len(candidates),
        "sent":          len(sent_log),
        "skipped":       len(skipped_log),
        "sent_log":      sent_log,
        "skipped_log":   skipped_log[:25],
        "next_step":     ("Run again in 24h+ (or pass ?force=1) to drive "
                           "the next round. Wire this to cron for autopilot."),
    }), 200


# r66-b (2026-05-26): bulk mark-sent for the workflow where you email
# all 9 targets in one sitting and want to flag them sent in one call
# instead of firing 9 separate POSTs. Marks the LATEST per-slug draft
# of each target as 'sent' — or, if ?slugs=a,b,c is passed, only
# those targets. Useful after a session of "I just blasted 9 emails."

@ai_lab_outreach_bp.route(
    "/api/v1/admin/ai-lab-outreach/sent-all", methods=["POST"]
)
def mark_sent_all():
    """Mark the latest draft for each (or specified) target as sent."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    _ensure_table()

    only_slugs_arg = (request.args.get("slugs") or "").strip()
    only_slugs = ([s.strip() for s in only_slugs_arg.split(",") if s.strip()]
                   if only_slugs_arg else None)
    if only_slugs:
        valid = {t["slug"] for t in _TARGETS}
        invalid = [s for s in only_slugs if s not in valid]
        if invalid:
            return jsonify({
                "ok":     False,
                "error":  "unknown_slugs",
                "invalid_slugs": invalid,
                "valid_slugs":   sorted(valid),
            }), 400

    target_slugs = only_slugs or [t["slug"] for t in _TARGETS]

    c = _db_conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200

    marked = []
    skipped = []
    try:
        with c.cursor() as cur:
            for slug in target_slugs:
                cur.execute("""
                    WITH latest AS (
                        SELECT id FROM ai_lab_outreach_drafts
                         WHERE target_slug = %s
                         ORDER BY created_at DESC
                         LIMIT 1
                    )
                    UPDATE ai_lab_outreach_drafts d
                       SET status = 'sent', sent_at = NOW()
                      FROM latest
                     WHERE d.id = latest.id
                       AND d.status != 'sent'
                 RETURNING d.id, d.target_slug, d.subject, d.sent_at
                """, (slug,))
                row = cur.fetchone()
                if row:
                    marked.append({
                        "draft_id":    row[0],
                        "target_slug": row[1],
                        "subject":     (row[2] or "")[:80],
                        "sent_at":     row[3].isoformat() if row[3] else None,
                    })
                else:
                    # Either no draft exists, or it was already sent
                    cur.execute("""
                        SELECT id, status, sent_at FROM ai_lab_outreach_drafts
                         WHERE target_slug = %s
                         ORDER BY created_at DESC
                         LIMIT 1
                    """, (slug,))
                    existing = cur.fetchone()
                    if existing:
                        skipped.append({
                            "target_slug": slug,
                            "draft_id":    existing[0],
                            "status":      existing[1],
                            "reason":      "already_sent" if existing[1] == "sent"
                                              else f"status={existing[1]}",
                            "sent_at":     existing[2].isoformat() if existing[2] else None,
                        })
                    else:
                        skipped.append({
                            "target_slug": slug,
                            "reason":      "no_draft_exists",
                            "hint":        f"POST /api/v1/admin/ai-lab-outreach/draft/{slug} first",
                        })
            c.commit()
    except Exception as e:
        try: c.close()
        except Exception: pass
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
    finally:
        try: c.close()
        except Exception: pass

    return jsonify({
        "ok":            True,
        "marked_count":  len(marked),
        "skipped_count": len(skipped),
        "marked":        marked,
        "skipped":       skipped,
        "ran_at":        datetime.datetime.utcnow().isoformat() + "Z",
        "scoped":        ("all 9 targets" if not only_slugs
                            else f"{len(only_slugs)} selected: {only_slugs}"),
        "next_step":     ("To track responses, hit "
                            "POST /api/v1/admin/ai-lab-outreach/respond/<id> "
                            "with body {\"response_text\":\"...\"}. "
                            "(That endpoint is queued for a future round.)"),
    }), 200
