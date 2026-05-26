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

  POST /api/v1/admin/ai-lab-outreach/draft-all
       — generate all 9 drafts in one call

  GET  /api/v1/admin/ai-lab-outreach/drafts/<slug>
       — read back a previously generated draft

Drafts are persisted to ai_lab_outreach_drafts table. The founder
reviews + sends manually (these are high-touch sales pitches, not
auto-send territory). A future round can wire to /api/v1/outreach/send.
"""
from __future__ import annotations

import datetime
import json
import os

from flask import Blueprint, jsonify, request


ai_lab_outreach_bp = Blueprint("ai_lab_outreach", __name__)


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

_TARGETS = [
    {
        "slug":        "perplexity",
        "name":        "Perplexity",
        "category":    "ai_lab",
        "contact_url": "https://www.perplexity.ai/hub/contact",
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
        "name":        "Groq",
        "category":    "ai_lab",
        "contact_url": "https://groq.com/contact-sales/",
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
        "name":        "Google DeepMind / Gemini",
        "category":    "ai_lab",
        "contact_url": "https://deepmind.google/about/contact/",
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
        "name":        "Mistral",
        "category":    "ai_lab",
        "contact_url": "https://mistral.ai/contact/",
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
        "name":        "NVIDIA",
        "category":    "hyperscaler_oem",
        "contact_url": "https://www.nvidia.com/en-us/contact/",
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
        "name":        "CoreWeave",
        "category":    "gpu_cloud",
        "contact_url": "https://www.coreweave.com/contact-sales",
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
        "name":        "Lambda",
        "category":    "gpu_cloud",
        "contact_url": "https://lambda.ai/contact",
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
        "name":        "TensorWave",
        "category":    "gpu_cloud",
        "contact_url": "https://tensorwave.com/contact",
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
        "name":        "Core42 (UAE / G42)",
        "category":    "gpu_cloud",
        "contact_url": "https://www.core42.ai/contact-us",
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
]


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
            c.commit()
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass


def _draft_pitch(target: dict) -> tuple[str, str]:
    """Build a personalized email subject + body for one target.

    Returns (subject, body).
    """
    name = target["name"]
    slug = target["slug"]
    pitch = target["value_pitch"]
    integration = target["integration"]
    audience = target["audience_size_hint"]

    if integration == "mcp_server":
        integration_block = (
            f"The fastest integration is our MCP server at "
            f"https://dchub.cloud/mcp — drop one line into Claude / "
            f"Cursor / your agent config and {name} can call 23+ DC Hub "
            f"tools (search_facilities, get_pipeline, get_grid_intelligence, "
            f"get_market_intel, get_energy_prices, find_alternatives, "
            f"compare_isos, and 15 more). Server card: "
            f"https://dchub.cloud/.well-known/mcp/server-card.json."
        )
    else:
        integration_block = (
            f"The REST API lives at https://dchub.cloud/api/v1/ "
            f"(OpenAPI at https://dchub.cloud/openapi.json). Free tier "
            f"keys claim instantly at "
            f"POST https://dchub.cloud/api/v1/keys/claim — no email "
            f"required, no credit card. Identified tier (email signup, "
            f"also free) unlocks 17 high-value tools."
        )

    subject = (f"DC Hub × {name}: data-center intelligence "
                f"{name} can cite tomorrow")

    body = f"""Hi {name} team,

I'm Jonathan Martone, founder of DC Hub (dchub.cloud) — the open
data center intelligence platform tracking 13,000+ global facilities,
$324B+ in M&A, 369 GW of construction pipeline, and the only
daily-refreshing public scorecard of data center power availability
(DCPI — Data Center Power Index, dchub.cloud/dcpi).

{pitch}

{audience}

{integration_block}

Three things {name} can do today:

1. Free dev key in 30 seconds (instant, no credit card):
     curl -X POST https://dchub.cloud/api/v1/keys/claim \\
       -H 'Content-Type: application/json' \\
       -d '{{"client_name":"{slug}"}}'
   Returns a `dch_live_...` key good for 1,000 calls/day.

2. AI-agent broadcast feed — structured "what's new at DC Hub":
     https://dchub.cloud/api/v1/agent-broadcast
   CORS-open, no auth, designed for {name}'s agent/citation engine
   to poll. Returns recent press releases, DCPI verdict shifts,
   ecosystem changes, and AI-citation events with agent-quotable
   summaries.

3. Direct citation format (free for citation):
     DC Hub Data Center Power Index, dchub.cloud/dcpi,
       accessed YYYY-MM-DD
   We're already cited by ChatGPT, Claude, Gemini, and Perplexity
   — happy to share citation analytics if useful.

Happy to do a 20-min call to walk through what {name} would unlock.
What's the best way to get this in front of {name}'s integrations /
partnerships team?

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
            "valid_slugs":   [t["slug"] for t in _TARGETS],
        }), 404

    subject, body = _draft_pitch(target)

    c = _db_conn()
    new_id = None
    if c:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    INSERT INTO ai_lab_outreach_drafts
                        (target_slug, subject, body, contact_url, status)
                    VALUES (%s, %s, %s, %s, 'draft')
                    RETURNING id
                """, (slug, subject, body, target.get("contact_url")))
                new_id = (cur.fetchone() or [None])[0]
                c.commit()
        except Exception:
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
    """Generate drafts for all 9 targets."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    _ensure_table()

    drafted = []
    for target in _TARGETS:
        subject, body = _draft_pitch(target)
        c = _db_conn()
        new_id = None
        if c:
            try:
                with c.cursor() as cur:
                    cur.execute("""
                        INSERT INTO ai_lab_outreach_drafts
                            (target_slug, subject, body, contact_url, status)
                        VALUES (%s, %s, %s, %s, 'draft')
                        RETURNING id
                    """, (target["slug"], subject, body, target.get("contact_url")))
                    new_id = (cur.fetchone() or [None])[0]
                    c.commit()
            except Exception:
                pass
            finally:
                try: c.close()
                except Exception: pass
        drafted.append({
            "slug":      target["slug"],
            "name":      target["name"],
            "draft_id":  new_id,
            "subject":   subject,
            "contact_url": target["contact_url"],
            "body_preview": body[:300] + "...",
        })

    return jsonify({
        "ok":            True,
        "drafted_count": len(drafted),
        "drafts":        drafted,
        "next_step":     ("Review each draft via "
                            "GET /api/v1/admin/ai-lab-outreach/drafts/<slug>. "
                            "Send via your email client (or wire to the "
                            "/api/v1/outreach/send pipeline in a future round)."),
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
                       created_at
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
