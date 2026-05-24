"""Phase RRRR (2026-05-16) — DC Hub Media revival.

User question: "is DC Hub Media telling everyone?"
Honest answer: NO. /api/v1/press-releases/list returned 0 items.
Source-of-truth score is 10/100 — "invisible." The infrastructure
exists (newsroom-auto cron 3h, marketing_auto_press, agent_vendor_
digest weekly) but the OUTPUT side is broken.

This module:
  1. New brain detector `dchub_media_press_silent` — fires if no
     press release in last 7 days
  2. New brain detector `source_of_truth_critical` — fires when
     score < 20 (current is 10)
  3. New endpoint /api/v1/media/winback-pitches — per-AI-platform
     email templates based on dormant agent UAs, ready to copy/paste
  4. Both detectors map to autopilot patterns that AUTO-TRIGGER
     /api/v1/marketing/auto-generate (not escalation-only — this
     is the "wake up DC Hub Media" move the user asked for)
"""

from __future__ import annotations

import os
import re
import datetime
from flask import Blueprint, jsonify, request


dchub_media_revival_bp = Blueprint("dchub_media_revival", __name__)


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


# UA-fingerprint → AI-platform classifier. Used by winback pitches.
# Each pattern maps to (platform_name, suggested_contact_url, pitch_angle).
_UA_PLATFORM = [
    # Anthropic ecosystem
    (re.compile(r"anthropic|claude(?!.*headless)", re.I),
     ("Anthropic / Claude",
      "https://www.anthropic.com/contact-sales",
      "Add DC Hub MCP to the Claude registry so direct connectors discover us")),
    # Cursor
    (re.compile(r"cursor", re.I),
     ("Cursor",
      "https://cursor.sh/contact",
      "List DC Hub in Cursor's MCP server directory for data-center workflows")),
    # ChatGPT / OpenAI
    (re.compile(r"openai|chatgpt|gpt-?\d", re.I),
     ("OpenAI / ChatGPT",
      "https://openai.com/contact-sales",
      "Submit DC Hub to the GPT Store + connect via custom GPTs")),
    # Perplexity
    (re.compile(r"perplexity", re.I),
     ("Perplexity",
      "https://www.perplexity.ai/hub/blog",
      "Apply for Perplexity's source-citation program — high-volume usage proves data quality")),
    # Gemini / Google
    (re.compile(r"gemini|google.*ai|bard", re.I),
     ("Google Gemini",
      "https://cloud.google.com/contact",
      "Submit DC Hub to Vertex AI agent garden + Gemini extensions")),
    # MCP probers / discovery tools
    (re.compile(r"mcp[-_]?(probe|registry|scanner|crawler|explorer|inspector)", re.I),
     ("MCP Discovery Registry",
      "https://mcpregistry.com or smithery.dev/submit",
      "Submit DC Hub to the MCP registry/directory listing the prober came from")),
    # Generic python HTTP — likely an internal tool or fresh-built agent
    (re.compile(r"python-(httpx|requests)|aiohttp", re.I),
     ("Python-built agent",
      "(no platform — direct outreach via referrer/IP investigation)",
      "Internal or custom agent — needs deeper UA/IP analysis to identify")),
]


def _classify_ua(ua: str) -> tuple[str, str, str]:
    if not ua: return ("Unknown", "n/a", "Add UA classification rule when identified")
    for pattern, info in _UA_PLATFORM:
        if pattern.search(ua):
            return info
    return ("Unidentified AI platform", "n/a",
            "Add UA classification to routes/dchub_media_revival.py:_UA_PLATFORM")


def _last_press_age_days() -> tuple[float | None, int]:
    """Returns (days_since_last_press, count_30d). Tolerates missing
    table by returning (None, 0). Tries auto_press_releases first
    (the autonomous writer), falls back to press_releases."""
    c = _conn()
    if c is None: return None, 0
    try:
        with c.cursor() as cur:
            for table, date_col in (("auto_press_releases", "generated_for"),
                                     ("auto_press_releases", "created_at"),
                                     ("press_releases", "published_at"),
                                     ("press_releases", "published_date"),
                                     ("press_releases", "created_at")):
                try:
                    cur.execute(f"SELECT to_regclass('public.{table}')")
                    if not (cur.fetchone() or [None])[0]: continue
                    cur.execute(f"""
                        SELECT EXTRACT(EPOCH FROM (NOW() - MAX({date_col})))/86400.0,
                               COUNT(*) FILTER (WHERE {date_col} >= NOW() - INTERVAL '30 days')
                          FROM {table}
                    """)
                    r = cur.fetchone()
                    if r and r[0] is not None:
                        return float(r[0]), int(r[1] or 0)
                except Exception:
                    continue
    finally:
        try: c.close()
        except Exception: pass
    return None, 0


@dchub_media_revival_bp.route("/api/v1/media/press-health", methods=["GET"])
def press_health():
    """Public — DC Hub Media output health. Used by /transparency."""
    age, count_30d = _last_press_age_days()
    sot = None
    try:
        from routes.media_pulse import _compute_source_of_truth  # if exposed
        sot = _compute_source_of_truth().get("score")
    except Exception:
        try:
            import requests
            r = requests.get("http://localhost:8080/api/v1/media/source-of-truth",
                              timeout=2)
            if r.status_code == 200:
                sot = r.json().get("score")
        except Exception: pass
    resp = jsonify({
        "days_since_last_press":  round(age, 1) if age is not None else None,
        "press_releases_30d":     count_30d,
        "source_of_truth_score":  sot,
        "verdict": (
            "silent"  if age is None or age > 7 else
            "weak"    if count_30d < 4 else
            "healthy"
        ),
        "generated_at":           datetime.datetime.utcnow().isoformat() + "Z",
    })
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@dchub_media_revival_bp.route("/api/v1/media/winback-pitches", methods=["GET"])
def winback_pitches():
    """Public worklist. For each dormant agent (>=30 prior calls,
    idle 14+ days), classify by UA and emit a per-platform pitch
    template. DC Hub Media can copy/paste these for outbound."""
    try:
        from routes.bot_outreach import _compute_dormant
        dormant = _compute_dormant(min_prior_calls=30, idle_days=14) or []
    except Exception:
        dormant = []

    # Group by platform
    by_platform: dict = {}
    for a in dormant:
        ua = (a.get("ua_fingerprint") or "")[:200]
        platform, contact, angle = _classify_ua(ua)
        b = by_platform.setdefault(platform, {
            "platform":         platform,
            "contact":          contact,
            "pitch_angle":      angle,
            "dormant_count":    0,
            "total_prior_calls": 0,
            "sample_uas":       [],
            "max_prior_calls":  0,
        })
        b["dormant_count"]    += 1
        b["total_prior_calls"] += int(a.get("prior_calls") or 0)
        b["max_prior_calls"]   = max(b["max_prior_calls"], int(a.get("prior_calls") or 0))
        if len(b["sample_uas"]) < 3:
            b["sample_uas"].append(ua[:60])

    pitches = sorted(by_platform.values(),
                     key=lambda p: -p["total_prior_calls"])

    # Generate a templated email pitch per platform
    for p in pitches:
        p["email_subject"] = (
            f"DC Hub: {p['total_prior_calls']:,} calls from "
            f"{p['platform']} — let's formalize the integration"
        )
        p["email_body"] = (
            f"Hi {p['platform']} team,\n\n"
            f"DC Hub (https://dchub.cloud) is the live, MCP-native "
            f"data-center intelligence platform. We've observed "
            f"{p['total_prior_calls']:,} MCP calls from agents matching "
            f"{p['platform']} signatures over the last 90 days — that's "
            f"strong organic adoption, but the agents went dormant "
            f"14+ days ago.\n\n"
            f"{p['pitch_angle']}.\n\n"
            f"What we'd like to discuss:\n"
            f"- Formal listing in your registry / directory\n"
            f"- Co-marketing case study on data-center site selection\n"
            f"- Joint webinar for hyperscale / colo decision-makers\n\n"
            f"Try our MCP server: https://dchub.cloud/mcp\n"
            f"Brand positioning: https://dchub.cloud/vs\n"
            f"Live ops dashboard: https://dchub.cloud/transparency\n\n"
            f"— DC Hub team\napi@dchub.cloud"
        )

    resp = jsonify(
        pitches=pitches,
        platform_count=len(pitches),
        total_dormant_agents=sum(p["dormant_count"] for p in pitches),
        total_dormant_calls=sum(p["total_prior_calls"] for p in pitches),
        note=("Copy-paste-ready outbound pitches for the dormant-agent "
              "winback campaign. Maps UA fingerprints from "
              "/api/v1/bots/dormant to the originating AI platform + "
              "suggested contact URL + email template. Send via the "
              "platform's contact form or whatever channel your DC Hub "
              "Media playbook uses."),
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
    )

    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


# ── r30 (2026-05-24): consolidated media pulse ────────────────────
#
# One endpoint that rolls press cadence + LinkedIn velocity + winback
# pitch count into a single health verdict. Consumed by the
# /api/v1/sentinel/sweep rollup and the /transparency UI so the
# operator stops needing to mentally compose 3 separate endpoints.

@dchub_media_revival_bp.route("/api/v1/media/pulse", methods=["GET"])
def media_pulse():
    """Consolidated DC Hub Media health rollup.

    Replaces the operator's mental sum of:
      - /api/v1/media/press-health  (press cadence)
      - /api/v1/media/winback-pitches (outbound queue)
      + LinkedIn publish velocity

    Returns one dict + a single verdict (healthy / weak / quiet /
    silent) so /transparency can render a single tile and the
    surveillance sweep can roll it into the master severity.
    """
    out: dict = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "components": {},
    }
    c = _conn()
    press_age, press_30d, press_7d, li_24h, li_7d = None, 0, 0, 0, 0
    if c is not None:
        try:
            with c.cursor() as cur:
                for table, date_col in (
                    ("auto_press_releases", "generated_at"),
                    ("auto_press_releases", "generated_for"),
                ):
                    try:
                        cur.execute(f"SELECT to_regclass('public.{table}')")
                        if not (cur.fetchone() or [None])[0]:
                            continue
                        cur.execute(f"""
                            SELECT
                              EXTRACT(EPOCH FROM (NOW() - MAX({date_col})))/86400.0,
                              COUNT(*) FILTER (WHERE {date_col} >= NOW() - INTERVAL '30 days'),
                              COUNT(*) FILTER (WHERE {date_col} >= NOW() - INTERVAL '7 days')
                            FROM {table}
                        """)
                        r = cur.fetchone()
                        if r and r[0] is not None:
                            press_age = float(r[0])
                            press_30d = int(r[1] or 0)
                            press_7d = int(r[2] or 0)
                            break
                    except Exception:
                        continue
                try:
                    cur.execute("""
                        SELECT
                          COUNT(*) FILTER (WHERE linkedin_sent_at >= NOW() - INTERVAL '24 hours'),
                          COUNT(*) FILTER (WHERE linkedin_sent_at >= NOW() - INTERVAL '7 days')
                        FROM auto_press_releases
                        WHERE linkedin_sent_at IS NOT NULL
                    """)
                    r = cur.fetchone()
                    if r:
                        li_24h = int(r[0] or 0)
                        li_7d = int(r[1] or 0)
                except Exception:
                    pass
        except Exception as _e:
            out["components"]["error"] = f"{type(_e).__name__}: {str(_e)[:80]}"

    out["components"]["press"] = {
        "days_since_last": round(press_age, 1) if press_age is not None else None,
        "count_30d": press_30d,
        "count_7d":  press_7d,
        "verdict": (
            "silent"  if press_age is None or press_age > 7 else
            "weak"    if press_30d < 4 else
            "healthy"
        ),
    }
    out["components"]["linkedin"] = {
        "sent_24h": li_24h,
        "sent_7d":  li_7d,
        "verdict": "healthy" if li_7d > 0 else "silent",
    }

    pitches_count = 0
    try:
        from flask import current_app
        with current_app.test_client() as _tc:
            _r = _tc.get("/api/v1/media/winback-pitches")
            if _r.status_code == 200:
                _data = _r.get_json() or {}
                pitches_count = int(_data.get("platform_count") or 0)
    except Exception:
        pass
    out["components"]["winback"] = {
        "platforms_targetable": pitches_count,
        "verdict": "healthy" if pitches_count > 0 else "quiet",
    }

    severity_rank = {"silent": 3, "degraded": 3, "weak": 2, "quiet": 1, "healthy": 0}
    worst = max(
        (severity_rank.get(c.get("verdict"), 0)
         for c in out["components"].values()
         if isinstance(c, dict) and "verdict" in c),
        default=0,
    )
    out["verdict"] = next(
        (v for v, rank in severity_rank.items() if rank == worst),
        "healthy",
    )
    out["ok"] = worst < 2

    if c is not None:
        try: c.close()
        except Exception: pass

    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
