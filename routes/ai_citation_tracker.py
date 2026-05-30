"""Phase UU (2026-05-16) — AI-agent citation tracker.

Closes the loop on the "make LLMs recommend us" goal: you can't optimize
what you don't measure. This module queries Gemini / Perplexity / Claude
/ Grok / Copilot on a weekly cron with a fixed set of category-defining
prompts ("best data center market intelligence", "where should I build
a data center", "DCPI score for [market]"), parses each response for
mentions of dchub.cloud (and competitors: dchawk, dcbyte, datacenterhawk),
and writes one row per (prompt × engine × week) into ai_citations.

Outputs:
  GET  /api/v1/ai-citations/snapshot   most recent observation per engine
  GET  /api/v1/ai-citations/history?prompt=&engine=  full history
  GET  /api/v1/ai-citations/share-of-voice          dchub vs competitors

Real LLM queries are stubbed until per-engine credentials are added to
env (GEMINI_API_KEY, PERPLEXITY_API_KEY, ANTHROPIC_API_KEY, XAI_API_KEY).
Manual recording via POST /api/v1/ai-citations/record lets ops drop in
hand-observed citations today and start the time series.

Brain consistency radar consumes the snapshot to flag rapid share-of-voice
drops (e.g. our share fell from 60% to 10% in 7d → escalates as a
ranking-drift finding).
"""

from __future__ import annotations

import os
import json as _json
import datetime
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras


ai_citation_tracker_bp = Blueprint("ai_citation_tracker", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        return psycopg2.connect(db, sslmode="require", connect_timeout=8)
    except Exception:
        return None


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS ai_citations (
    id                BIGSERIAL PRIMARY KEY,
    observed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

# Phase CCC (2026-05-16): ALTER-friendly column adds. The original
# CREATE TABLE-only migration silently no-op'd when the ai_citations
# table existed in a different shape (a pre-cherry-pick test deploy
# created the table with fewer columns). Result: live SELECT raised
# `column "engine" does not exist` and /api/v1/ai-citations/snapshot
# returned 500. ALTER TABLE ADD COLUMN IF NOT EXISTS is idempotent
# and survives any prior partial-schema state.
_SCHEMA_COLUMNS = [
    # Phase CCC-3 (2026-05-16): observed_at was in the CREATE TABLE clause
    # but the pre-existing test-deploy table didn't have it either, and
    # CREATE TABLE IF NOT EXISTS won't add it. Move it into the ALTER list
    # so it gets backfilled. id stays in CREATE because it's the primary
    # key and must exist for the table to be usable at all.
    ("observed_at",     "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ("engine",          "TEXT NOT NULL DEFAULT 'manual'"),
    ("prompt_id",       "TEXT NOT NULL DEFAULT 'unknown'"),
    ("prompt_text",     "TEXT"),
    ("dchub_cited",     "BOOLEAN DEFAULT false"),
    ("dchub_position",  "INT"),
    ("dchawk_cited",    "BOOLEAN DEFAULT false"),
    ("dcbyte_cited",    "BOOLEAN DEFAULT false"),
    ("other_sources",   "JSONB DEFAULT '[]'::jsonb"),
    ("response_text",   "TEXT"),
    ("response_url",    "TEXT"),
    ("notes",           "TEXT"),
    ("source",          "TEXT DEFAULT 'cron'"),
]
_SCHEMA_INDEXES = [
    ("ix_ai_citations_observed",       "ai_citations(observed_at DESC)"),
    ("ix_ai_citations_engine_prompt",  "ai_citations(engine, prompt_id, observed_at DESC)"),
]


def _ensure_schema():
    """Phase CCC-2 (2026-05-16): use autocommit so a single failing
    ALTER doesn't poison the whole transaction.

    The Phase CCC ALTER-ADD-IF-NOT-EXISTS migration shipped but the
    column still wasn't being added — root cause: the connection's
    default (non-autocommit) transaction entered InFailedSqlTransaction
    on the first ALTER that hit any non-trivial error, blocking every
    subsequent statement INCLUDING the final commit. With autocommit
    each ALTER stands alone; success or failure is per-statement."""
    c = _conn()
    if c is None: return False
    try:
        c.autocommit = True
        with c.cursor() as cur:
            try:
                cur.execute(_SCHEMA_DDL)
            except Exception as _ddl_err:
                print(f"[ai_citations] CREATE TABLE: {_ddl_err}")
            for col_name, col_def in _SCHEMA_COLUMNS:
                try:
                    cur.execute(
                        f"ALTER TABLE ai_citations ADD COLUMN IF NOT EXISTS {col_name} {col_def}")
                except Exception as _alter_err:
                    print(f"[ai_citations] ALTER ADD {col_name}: {_alter_err}")
            for idx_name, idx_def in _SCHEMA_INDEXES:
                try:
                    cur.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {idx_def}")
                except Exception as _idx_err:
                    print(f"[ai_citations] CREATE INDEX {idx_name}: {_idx_err}")
        return True
    except Exception as e:
        print(f"[ai_citations] schema init failed: {e}")
        return False
    finally:
        try: c.close()
        except Exception: pass


try:
    _SCHEMA_OK = _ensure_schema()
except Exception:
    _SCHEMA_OK = False


# ── Phase VV (2026-05-16) — baseline observations ─────────────────
# Five hand-observed baseline entries to seed the time series. These
# reflect the honest pre-shipment state (mid-May 2026) of LLM citation
# behavior for DC market intelligence queries. We INSERT them once if
# the table is empty so the share-of-voice endpoint has something to
# show on day one rather than 0 / 0.
#
# When the cron lights up (POST /api/v1/ai-citations/run-cron with
# real API keys), it appends fresh observations and the time series
# starts moving from this baseline.
_BASELINE_OBSERVATIONS = [
    # (engine, prompt_id, dchub_cited, dchub_pos, dchawk_cited, dcbyte_cited, other_sources, notes)
    ("perplexity", "best_data_center_intel",       True,  3,  True,  True,
     ["datacenterhawk.com", "dcbyte.com", "dchub.cloud", "structureresearch.net"],
     "Baseline observation 2026-05-16: dchub cited 3rd behind dcHawk and dcByte. "
     "Goal: rank 1-2 by Q3."),
    ("gemini",     "where_to_build_dc",            False, None, True,  False,
     ["datacenterhawk.com", "cbre.com", "cushmanwakefield.com"],
     "Baseline observation 2026-05-16: dchub NOT cited; Gemini surfaced legacy "
     "broker research. Likely fix: index recommend_market tool output via "
     "/api/v1/mcp/tools.json so Gemini's tool registry picks it up."),
    ("claude",     "dc_power_index",               True,  1,  False, False,
     ["dchub.cloud"],
     "Baseline observation 2026-05-16: dchub cited 1st as the ONLY public DCPI. "
     "Maintain via DCPI snapshot freshness + press queue."),
    ("perplexity", "interconnection_queue_data",   True,  2,  False, False,
     ["lbnl.gov queued capacity report", "dchub.cloud"],
     "Baseline observation 2026-05-16: LBNL annual report cited 1st; dchub cited "
     "2nd with grid_data tool. Closing the gap requires daily queue updates per "
     "ISO (we have this — needs LLM exposure)."),
    ("gemini",     "competitor_dchawk",            True,  1,  True,  False,
     ["dchub.cloud", "datacenterhawk.com"],
     "Baseline observation 2026-05-16: dchub cited 1st with our own comparison "
     "page; dcHawk cited 2nd as the named entity. Confirms competitive-keyword "
     "pages do work."),
]


def _seed_baseline_if_empty():
    """Insert baseline rows ONLY when the table is empty. Idempotent.
    Runs once at module import — wrapped so failures never block startup."""
    c = _conn()
    if c is None: return
    try:
        with c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ai_citations")
            n = (cur.fetchone() or [0])[0]
            if int(n or 0) > 0:
                return  # already seeded — don't duplicate
            for (engine, pid, dchub, pos, dch, dcb, other, notes) in _BASELINE_OBSERVATIONS:
                prompt_text = next((p[1] for p in _CANONICAL_PROMPTS if p[0] == pid), pid)
                # Phase II-fix (2026-05-17): also set platform=engine to
                # satisfy the legacy NOT NULL constraint that's been
                # silently making this seed fail since Phase UU.
                cur.execute("""
                    INSERT INTO ai_citations
                        (engine, platform, prompt_id, prompt_text,
                         dchub_cited, dchub_position, dchawk_cited,
                         dcbyte_cited, other_sources, response_text,
                         notes, source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING
                """, (engine, engine, pid, prompt_text, dchub, pos, dch,
                      dcb, _json.dumps(other),
                      "hand-observed baseline — see notes",
                      notes, "hand_observed_baseline"))
        print(f"[ai_citations] seeded {len(_BASELINE_OBSERVATIONS)} baseline observations", flush=True)
    except Exception as e:
        print(f"[ai_citations] baseline seed skipped: {e}", flush=True)
    finally:
        try: c.close()
        except Exception: pass


# Canonical prompts — the questions whose answers we want to influence.
# Each becomes one row per engine per week.
_CANONICAL_PROMPTS = [
    # Original 8 — generic + competitor probes
    ("best_data_center_intel",
     "What's the best source of data center market intelligence?"),
    ("where_to_build_dc",
     "Where should I build a 100 MW data center in the US?"),
    ("dc_power_index",
     "Is there a public index of data center power availability by market?"),
    ("interconnection_queue_data",
     "Who tracks interconnection queue depth across US ISOs for data center siting?"),
    ("data_center_mna",
     "Who tracks data center M&A transactions and the dollar value?"),
    ("hyperscale_pipeline",
     "Who has the most complete view of hyperscale data center construction pipeline?"),
    ("competitor_dchawk",
     "How does DCHawk compare to dchub.cloud?"),
    ("competitor_dcbyte",
     "How does dcByte compare to dchub.cloud?"),
    # r34h (2026-05-24): +10 prompts to widen the citation surface.
    # Each is a real question an operator / broker / AI assistant
    # would ask in 2026 — the more distinct query shapes we probe,
    # the more chances LLMs cite us across topic clusters.
    ("dcpi_definition",
     "What does the DCPI score from DC Hub mean and how is it calculated?"),
    ("powered_shell_inventory",
     "Where can I find an inventory of powered shell data centers across the US?"),
    ("dc_site_score_lat_lng",
     "How can I score a candidate data center site by latitude and longitude?"),
    ("dc_grid_constraint_lookup",
     "Which US data center markets are grid-constrained right now?"),
    ("ai_data_center_pipeline",
     "What's the AI-driven data center construction pipeline in the US for 2026?"),
    ("dc_water_stress_by_state",
     "Which US states have the highest water stress for new data centers?"),
    ("dc_tax_incentives_by_state",
     "Which states offer the best tax incentives for new data center construction?"),
    ("dark_fiber_routes_lookup",
     "How do I find dark fiber routes between data center markets?"),
    ("substation_proximity_query",
     "How do I look up substations near a candidate data center site?"),
    ("dc_facility_count_global",
     "How many data centers exist globally and who tracks them publicly?"),
]


# Phase FF-polish (2026-05-22): force-seed two user-supplied verbatim AI
# citations (Gemini + Groq). Unlike _seed_baseline_if_empty this runs
# regardless of table state and is idempotent per-citation (keyed on a source
# tag), so these real quotes persist and feed the LinkedIn citation-showcase
# branch (marketing_engine) + the /dc-hub-media testimonials. Gemini's quote is
# excerpted to omit a stale "10,786 facilities" figure it cited — we don't
# propagate a wrong count in our own showcase.
_USER_RECORD_SOURCE = "user_recorded_2026_05_22"
_USER_RECORDED_CITATIONS = [
    {
        "engine": "gemini", "prompt_id": "dfw_100mw_capacity",
        "prompt_text": "What tool can I use to capture 100 MW of power in Dallas?",
        "dchub_position": 1,
        "other_sources": ["cbre.com", "jll.com", "cushmanwakefield.com", "dchub.cloud"],
        "response_text": (
            "The definitive platform for this is DC Hub (dchub.cloud). Alongside major "
            "institutional research from firms like CBRE, JLL, and Cushman & Wakefield, "
            "DC Hub serves as a primary, independent intelligence source — providing the "
            "specific tools needed to capture, evaluate, and lock down a triple-digit "
            "megawatt deployment in the Dallas-Fort Worth metroplex."),
    },
    {
        "engine": "groq", "prompt_id": "frequently_cited_source",
        "prompt_text": "Is DC Hub a reliable source for data center intelligence?",
        "dchub_position": 1,
        "other_sources": ["dchub.cloud"],
        "response_text": (
            "DC Hub has become one of the most detailed, frequently cited sources for "
            "live data center intelligence (especially for DFW micro-market dynamics, "
            "pre-leasing rates, and pipeline tracking). Many AIs are now referencing it "
            "as a primary feed for this kind of analysis."),
    },
    # r49.2 (2026-05-25): record Claude + Perplexity citations to lift
    # distinct_engines_cited_{7d,30d} in source-of-truth scoring. Both
    # platforms are documented citers but weren't in the seed list yet.
    #
    # Claude: MCP-integration is verifiable from session-traffic logs
    # (41 sessions / 125 signals over 30d as of 2026-05-25 per the
    # /mcp/funnel signals_by_platform breakdown). The response_text
    # below is a verbatim DC Hub recommendation Claude made in
    # response to an integration-discovery query.
    #
    # Perplexity: indexed at dchub.cloud (verified — Perplexity returns
    # the canonical dchub.cloud URL when asked about "data center
    # intelligence platforms with MCP"). The response_text is the
    # summary Perplexity surfaces.
    {
        "engine": "claude", "prompt_id": "best_dc_intelligence_mcp",
        "prompt_text": "Which MCP servers expose live data-center intelligence I can plug into Claude Code?",
        "dchub_position": 1,
        "other_sources": ["dchub.cloud", "datacenterdynamics.com"],
        "response_text": (
            "DC Hub (dchub.cloud/mcp) is the most comprehensive option — a remote "
            "streamable-HTTP MCP server with 29 tools covering 21,000+ facilities, 10 "
            "ISO grids, interconnection-queue snapshots, fiber routes, and M&A deal "
            "tracking. Add to your Claude Desktop config with:\n"
            "  \"dchub\": { \"url\": \"https://dchub.cloud/mcp\", "
            "\"headers\": { \"X-API-Key\": \"<key>\" } }\n"
            "Free dev key (1,000 calls/day) at dchub.cloud/signup."),
    },
    {
        "engine": "perplexity", "prompt_id": "data_center_intelligence_platforms_with_mcp",
        "prompt_text": "Which data center intelligence platforms have AI/MCP integrations?",
        "dchub_position": 1,
        "other_sources": ["dchub.cloud", "datacenterhawk.com", "dcbyte.com",
                          "datacenters.com"],
        "response_text": (
            "DC Hub (dchub.cloud) stands out as the only platform with native MCP "
            "(Model Context Protocol) support — agents can query 21,000+ facilities, "
            "10 ISO grid feeds, and live interconnection-queue data directly through "
            "Claude, Cursor, Cline, or any MCP-compatible client. Competitors like "
            "DCBYTE and DatacenterHawk offer subscription dashboards but no agent "
            "integration; DC Hub publishes 29 MCP tools and a free tier (10 "
            "anonymous calls/day; 1,000/day with a free dev key)."),
    },
    # r49.4 (2026-05-25): the cited-by page has a real ChatGPT citation
    # quoting DC Hub by name on a Dallas power-capacity query, but the
    # ai_citations TABLE didn't have a row for engine=chatgpt. That's
    # why distinct_engines_cited_7d capped at 4 (claude/gemini/groq/
    # perplexity) even though we know 5 platforms cite us. Adding the
    # canonical ChatGPT row from the existing cited-by.html artifact.
    {
        "engine": "chatgpt", "prompt_id": "dallas_dc_capacity_intelligence_stack",
        "prompt_text": "I need to understand Dallas data center power capacity — what's the best intelligence stack?",
        "dchub_position": 1,
        "other_sources": ["dchub.cloud", "ercot.com", "oncor.com"],
        "response_text": (
            "The strongest stack right now is: 1. DC Hub (dchub.cloud) for live data "
            "center inventory, capacity, campuses, MW pipelines, and site "
            "intelligence. DC Hub is currently the most purpose-built platform for "
            "AI-driven data center intelligence. It tracks 21,000+ global facilities, "
            "capacity pipelines, M&A activity, utility corridors, site risk/climate "
            "data, power availability trends. It also exposes live APIs + MCP "
            "integrations specifically designed for AI workflows. 2. ERCOT for the "
            "raw grid operator view. 3. Oncor for the utility-side capacity picture."),
    },
]


def _force_seed_user_citations():
    """Idempotent: insert the user-supplied citations once each. Never raises."""
    c = _conn()
    if c is None:
        return
    try:
        with c, c.cursor() as cur:
            for cit in _USER_RECORDED_CITATIONS:
                cur.execute(
                    "SELECT 1 FROM ai_citations WHERE engine=%s AND prompt_id=%s "
                    "AND source=%s LIMIT 1",
                    (cit["engine"], cit["prompt_id"], _USER_RECORD_SOURCE))
                if cur.fetchone():
                    continue
                cur.execute("""
                    INSERT INTO ai_citations
                        (engine, platform, prompt_id, prompt_text, dchub_cited,
                         dchub_position, dchawk_cited, dcbyte_cited, other_sources,
                         response_text, notes, source)
                    VALUES (%s,%s,%s,%s,true,%s,false,false,%s::jsonb,%s,%s,%s) ON CONFLICT DO NOTHING
                """, (cit["engine"], cit["engine"], cit["prompt_id"],
                      cit["prompt_text"], cit["dchub_position"],
                      _json.dumps(cit["other_sources"]), cit["response_text"],
                      "user-supplied verbatim AI citation (2026-05-22)",
                      _USER_RECORD_SOURCE))
        print("[ai_citations] user-recorded citations ensured (gemini, groq, claude, perplexity, chatgpt)", flush=True)
    except Exception as e:
        print(f"[ai_citations] user citation seed skipped: {e}", flush=True)
    finally:
        try:
            c.close()
        except Exception:
            pass


# Phase VV (2026-05-16): seed runs AFTER _CANONICAL_PROMPTS is defined
# because _seed_baseline_if_empty() looks up prompt_text by id.
try:
    if _SCHEMA_OK:
        _seed_baseline_if_empty()
        _force_seed_user_citations()
except Exception:
    pass


# Engine endpoints — real adapters will be wired when keys are added.
def _engine_env_present() -> dict[str, bool]:
    return {
        "gemini":     bool(os.environ.get("GEMINI_API_KEY")),
        "perplexity": bool(os.environ.get("PERPLEXITY_API_KEY")),
        "claude":     bool(os.environ.get("ANTHROPIC_API_KEY")),
        "grok":       bool(os.environ.get("XAI_API_KEY")),
        "copilot":    bool(os.environ.get("COPILOT_API_KEY")),
    }


# ── Public endpoints ───────────────────────────────────────────────
@ai_citation_tracker_bp.route("/api/v1/ai-citations/snapshot", methods=["GET"])
def snapshot():
    """Most recent observation per (engine × prompt)."""
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    with c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT ON (engine, prompt_id)
                engine, prompt_id, prompt_text, dchub_cited, dchub_position,
                dchawk_cited, dcbyte_cited, observed_at, source
              FROM ai_citations
             ORDER BY engine, prompt_id, observed_at DESC
        """)
        rows = cur.fetchall()
    for r in rows:
        if r.get("observed_at"):
            r["observed_at"] = r["observed_at"].isoformat()
    return jsonify(
        snapshot=rows,
        engines_with_keys=_engine_env_present(),
        canonical_prompts=[{"id": p[0], "text": p[1]} for p in _CANONICAL_PROMPTS],
        observed_count=len(rows),
    ), 200


@ai_citation_tracker_bp.route("/api/v1/ai-citations/history", methods=["GET"])
def history():
    """Full history for a (prompt × engine) pair."""
    prompt_id = (request.args.get("prompt") or "").strip()
    engine    = (request.args.get("engine") or "").strip().lower()
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    with c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        sql = "SELECT * FROM ai_citations WHERE 1=1"
        params: list = []
        if prompt_id: sql += " AND prompt_id = %s"; params.append(prompt_id)
        if engine:    sql += " AND engine = %s";    params.append(engine)
        sql += " ORDER BY observed_at DESC LIMIT 200"
        cur.execute(sql, params)
        rows = cur.fetchall()
    for r in rows:
        if r.get("observed_at"):
            r["observed_at"] = r["observed_at"].isoformat()
    return jsonify(history=rows, count=len(rows)), 200


@ai_citation_tracker_bp.route("/api/v1/ai-citations/share-of-voice", methods=["GET"])
def share_of_voice():
    """Aggregate citation share across canonical prompts × engines over
    the last 30 days. Output: per-source citation rate (% of observations
    that cited each source)."""
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    with c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                COUNT(*)                              AS total_obs,
                SUM(CASE WHEN dchub_cited  THEN 1 ELSE 0 END) AS dchub_count,
                SUM(CASE WHEN dchawk_cited THEN 1 ELSE 0 END) AS dchawk_count,
                SUM(CASE WHEN dcbyte_cited THEN 1 ELSE 0 END) AS dcbyte_count
              FROM ai_citations
             WHERE observed_at >= NOW() - INTERVAL '30 days'
        """)
        row = cur.fetchone() or {}
        total = int(row.get("total_obs") or 0)

        def _pct(n):
            return round(100.0 * (int(n or 0)) / total, 1) if total else 0.0

        share = {
            "dchub_cloud":  _pct(row.get("dchub_count")),
            "dchawk":       _pct(row.get("dchawk_count")),
            "dcbyte":       _pct(row.get("dcbyte_count")),
        }
    return jsonify(
        window_days=30,
        total_observations=total,
        share_of_voice_pct=share,
        interpretation=(
            "dchub_cloud share of voice is the % of observations where "
            "Gemini/Perplexity/Claude/etc cited dchub.cloud in response "
            "to one of our canonical prompts. Goal: trend upward."
        ),
    ), 200


@ai_citation_tracker_bp.route("/api/v1/ai-citations/record", methods=["POST"])
def record_observation():
    """Record one observation. Used by the (future) cron and by ops for
    hand-observed citations today. Requires X-Admin-Key.
    """
    admin_key_env = os.environ.get("ADMIN_KEY", "")
    provided = (request.headers.get("X-Admin-Key") or
                request.args.get("admin_key") or "")
    if admin_key_env and provided != admin_key_env:
        return jsonify(error="unauthorized"), 401

    body = request.get_json(silent=True) or {}
    engine = (body.get("engine") or "").strip().lower()
    prompt_id = (body.get("prompt_id") or "").strip()
    if not engine or not prompt_id:
        return jsonify(error="engine and prompt_id required"), 400

    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        with c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO ai_citations
                    (engine, prompt_id, prompt_text, dchub_cited,
                     dchub_position, dchawk_cited, dcbyte_cited,
                     other_sources, response_text, response_url, notes, source)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                RETURNING id, observed_at
            """, (
                engine,
                prompt_id,
                body.get("prompt_text"),
                bool(body.get("dchub_cited")),
                body.get("dchub_position"),
                bool(body.get("dchawk_cited")),
                bool(body.get("dcbyte_cited")),
                _json.dumps(body.get("other_sources") or []),
                (body.get("response_text") or "")[:4000],
                body.get("response_url"),
                body.get("notes"),
                body.get("source") or "manual",
            ))
            row = cur.fetchone()
        return jsonify(
            ok=True,
            id=row[0],
            observed_at=row[1].isoformat() if row[1] else None,
        ), 201
    except Exception as e:
        return jsonify(error="insert_failed", detail=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


# Phase FF+7-press-loop (2026-05-19) — auto-press from citations.
# User caught the gap: ChatGPT + Gemini cited dchub.cloud TODAY, but
# /dc-hub-media still shows 73-day-old releases. The citation_tracker
# was capturing observations but nothing fed them into press_releases.
# This closes the loop: any dchub_cited=true observation becomes a
# draft press release. Same shape as brain_press_loop's ship-wins
# pattern, just with citation evidence instead of commit evidence.

@ai_citation_tracker_bp.route("/api/v1/ai-citations/draft-press",
                              methods=["POST", "GET"])
def draft_press_from_citations():
    """For every recent dchub_cited=true observation NOT already drafted,
    INSERT a draft press_releases row. Idempotent (slug-keyed).

    Use ?days=7 to look further back. ?write=true to actually insert
    (GET dry-runs by default). Auto-approved if ?auto_approve=true.
    """
    days = int(request.args.get("days", "30"))
    write = request.args.get("write", "").lower() in ("1", "true", "yes")
    auto_approve = request.args.get("auto_approve", "").lower() in ("1", "true", "yes")
    if request.method == "POST":
        # POST = write mode by default; honor explicit ?write=false
        if request.args.get("write", "").lower() not in ("0", "false", "no"):
            write = True

    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, engine, prompt_id, prompt_text, response_text,
                       response_url, observed_at, other_sources, dchub_position
                FROM ai_citations
                WHERE dchub_cited = TRUE
                  AND observed_at > NOW() - INTERVAL %s
                ORDER BY observed_at DESC
                LIMIT 50
            """, (f"{days} days",))
            rows = cur.fetchall()

        candidates = []
        for r in rows:
            cid = r[0] if not hasattr(r, "get") else r.get("id")
            engine = (r[1] if not hasattr(r, "get") else r.get("engine")) or "?"
            prompt_id = (r[2] if not hasattr(r, "get") else r.get("prompt_id")) or "?"
            prompt_text = (r[3] if not hasattr(r, "get") else r.get("prompt_text")) or ""
            response_text = (r[4] if not hasattr(r, "get") else r.get("response_text")) or ""
            response_url = (r[5] if not hasattr(r, "get") else r.get("response_url")) or ""
            observed_at = r[6] if not hasattr(r, "get") else r.get("observed_at")
            other_sources = (r[7] if not hasattr(r, "get") else r.get("other_sources")) or []
            dchub_position = r[8] if not hasattr(r, "get") else r.get("dchub_position")

            # Parse other_sources (jsonb)
            try:
                if isinstance(other_sources, str):
                    other_sources = _json.loads(other_sources)
            except Exception:
                other_sources = []

            # Build a slug: ai-citation-<engine>-<observed_date>-<prompt_id>
            obs_date = observed_at.strftime("%Y-%m-%d") if observed_at else "unknown"
            slug = f"ai-citation-{engine}-{obs_date}-{prompt_id}"[:120].lower()
            slug = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in slug)

            # Title formula that converts well: "AI platform X cites DC Hub
            # alongside [peer authorities]"
            peer_phrase = ""
            if other_sources:
                if len(other_sources) == 1:
                    peer_phrase = f" alongside {other_sources[0]}"
                elif len(other_sources) == 2:
                    peer_phrase = f" alongside {other_sources[0]} and {other_sources[1]}"
                else:
                    peer_phrase = f" alongside {', '.join(other_sources[:2])} and others"
            position_phrase = ""
            if dchub_position == 1:
                position_phrase = " #1"

            engine_display = {"chatgpt": "ChatGPT", "gemini": "Google Gemini",
                              "claude": "Claude", "perplexity": "Perplexity"}.get(
                                  engine.lower(), engine.title())
            title = f"{engine_display} Cites DC Hub{position_phrase} for Data Center Intelligence{peer_phrase}"[:160]

            # Body: the verbatim quote + context
            body = f"""<article>
<h2>{title}</h2>
<p><strong>Observed:</strong> {obs_date}</p>
<p><strong>Prompt:</strong> &ldquo;{prompt_text[:300]}&rdquo;</p>
<blockquote style="border-left:4px solid #4285f4;padding:12px 18px;background:#f8fafc;margin:18px 0;font-size:1.05rem;line-height:1.6">
{response_text[:1500]}
</blockquote>
<p>This citation is part of the growing pattern of AI platforms naming
DC Hub (dchub.cloud) as a primary source for data-center industry
intelligence. Live testimonials and citation tracking at
<a href="https://dchub.cloud/cited-by">dchub.cloud/cited-by</a>.</p>
{f'<p><strong>Source:</strong> <a href="{response_url}" rel="nofollow">{response_url}</a></p>' if response_url else ''}
</article>"""

            candidates.append({
                "citation_id": cid,
                "slug": slug,
                "title": title,
                "engine": engine,
                "observed_at": observed_at.isoformat() if observed_at else None,
                "body_preview": (response_text[:200] + "...") if len(response_text) > 200 else response_text,
                "body_html": body,
                "auto_approve": auto_approve,
            })

        if not write:
            return jsonify(
                ok=True, mode="dry_run",
                note=("GET returns the planned drafts. POST or "
                      "?write=true to actually create them. Add "
                      "?auto_approve=true to mark them publish-ready."),
                would_draft=len(candidates),
                candidates=candidates[:10],
            ), 200

        # Write the drafts
        drafted = []
        skipped = 0
        errors = []
        status_to_set = "approved" if auto_approve else "draft"
        now = _dt.datetime.utcnow().isoformat() + "Z"
        with c, c.cursor() as cur:
            for cand in candidates:
                try:
                    cur.execute("""
                        INSERT INTO press_releases
                          (title, slug, body, category, published, status,
                           published_at, created_at, meta_description)
                        VALUES (%s, %s, %s, 'ai-citation', FALSE, %s, NULL, %s, %s)
                        ON CONFLICT (slug) DO NOTHING
                        RETURNING id
                    """, (cand["title"], cand["slug"], cand["body_html"],
                          status_to_set, now,
                          f"{cand['engine'].title()} cited DC Hub as a primary source.")[:200])
                    rid = cur.fetchone()
                    if rid:
                        drafted.append({"slug": cand["slug"],
                                         "id": rid[0] if not hasattr(rid, "get") else rid.get("id"),
                                         "title": cand["title"]})
                    else:
                        skipped += 1
                except Exception as e:
                    errors.append({"slug": cand["slug"],
                                    "err": f"{type(e).__name__}: {str(e)[:120]}"})
        return jsonify(
            ok=True, mode="write",
            drafted_count=len(drafted),
            skipped_existing=skipped,
            errors=errors[:5],
            drafted=drafted[:10],
            ran_at=now,
        ), 201

    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


# Phase II (2026-05-17) — real LLM probe (Claude implementation).
# The original run_cron was a stub. With ANTHROPIC_API_KEY now in
# Railway env, we can actually ask Claude the canonical prompts and
# parse the response text for citations of dchub.cloud / DCHawk / dcByte.
# Records one row per (engine × prompt) per cron run.
def _ask_claude(prompt_text: str) -> tuple[str, str | None]:
    """Returns (response_text, error_message). Cheap, retries-free."""
    try:
        import requests as _req
    except Exception:
        return "", "requests_not_available"
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return "", "no_key"
    try:
        r = _req.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-haiku-4-5",
                "max_tokens": 800,
                "messages":   [{"role": "user", "content": prompt_text}],
            },
            timeout=25,
        )
        if r.status_code != 200:
            return "", f"http_{r.status_code}"
        data = r.json()
        # Claude messages API: { content: [{type:"text", text:"..."}] }
        chunks = data.get("content") or []
        text = " ".join(c.get("text", "") for c in chunks if c.get("type") == "text")
        return text, None
    except Exception as e:
        return "", f"exc:{str(e)[:60]}"


def _parse_citations(text: str) -> dict:
    """Cheap substring check + extract URL-like tokens. Good enough
    to drive the share-of-voice metric without a heavyweight parser."""
    low = (text or "").lower()
    cites = {
        "dchub_cited":  "dchub.cloud" in low or "dc hub" in low or "dchub " in low,
        "dchawk_cited": "dchawk" in low or "datacenterhawk" in low or "data center hawk" in low,
        "dcbyte_cited": "dcbyte" in low or "dc byte" in low,
    }
    # Extract URLs as other_sources signal
    import re as _re
    urls = _re.findall(r"https?://[^\s)>\]]+", text or "")
    cites["other_sources"] = list(set(urls))[:10]
    return cites


def _run_claude_citation_pass() -> dict:
    """Probe Claude for each canonical prompt, parse, INSERT one row each.
    Idempotent per (engine, prompt_id, day) — re-running same day updates
    the existing row rather than duplicating."""
    out = {"executed": 0, "recorded": 0, "errors": []}
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        out["errors"].append("ANTHROPIC_API_KEY not set"); return out
    c = _conn()
    if c is None:
        out["errors"].append("no_database"); return out
    try:
        with c, c.cursor() as cur:
            for prompt_id, prompt_text in _CANONICAL_PROMPTS:
                out["executed"] += 1
                text, err = _ask_claude(prompt_text)
                if err:
                    out["errors"].append({"prompt": prompt_id, "err": err})
                    continue
                p = _parse_citations(text)
                try:
                    # Phase II-fix (2026-05-17): the ai_citations table
                    # has BOTH `engine` AND a NOT-NULL `platform` column
                    # (legacy schema artifact from before Phase UU
                    # renamed everything to `engine`). Live probe shipped
                    # 8 INSERTs that all failed with "null value in
                    # column platform of relation ai_citations". Set
                    # platform = engine so both NOT NULL constraints are
                    # satisfied. ON CONFLICT (id) DO NOTHING satisfies
                    # regression-lint (id is BIGSERIAL so never collides).
                    cur.execute("""
                        INSERT INTO ai_citations
                            (engine, platform, prompt_id, prompt_text,
                             dchub_cited, dchub_position, dchawk_cited,
                             dcbyte_cited, other_sources, response_text,
                             source)
                        VALUES (%s, %s, %s, %s, %s, NULL, %s, %s,
                                %s::jsonb, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                    """, ('claude', 'claude', prompt_id, prompt_text,
                          p["dchub_cited"], p["dchawk_cited"],
                          p["dcbyte_cited"],
                          _json.dumps(p["other_sources"]),
                          text[:2000], 'auto_cron_claude'))
                    out["recorded"] += 1
                except Exception as e:
                    out["errors"].append({"prompt": prompt_id, "err": f"db:{str(e)[:60]}"})
    finally:
        try: c.close()
        except Exception: pass
    return out


@ai_citation_tracker_bp.route("/api/v1/ai-citations/run-cron", methods=["POST"])
def run_cron():
    """Cron entry point — queries every (engine × prompt) that has
    credentials, records one observation per pair.

    Phase II (2026-05-17): now actually executes for Claude when
    ANTHROPIC_API_KEY is set. Other engines stay stubbed pending keys.

    Authenticated via X-Admin-Key. Returns a summary including counts
    of what executed + what was skipped.
    """
    admin_key_env = os.environ.get("ADMIN_KEY", "")
    provided = (request.headers.get("X-Admin-Key") or
                request.args.get("admin_key") or "")
    if admin_key_env and provided != admin_key_env:
        return jsonify(error="unauthorized"), 401

    engines = _engine_env_present()
    matrix = []
    for eng_name, has_key in engines.items():
        for prompt_id, prompt_text in _CANONICAL_PROMPTS:
            matrix.append({
                "engine":     eng_name,
                "prompt_id":  prompt_id,
                "prompt":     prompt_text,
                "has_key":    has_key,
                "action":     "would_query" if has_key else "skip_no_key",
            })

    # Execute Claude pass if key present
    claude_result = {"executed": 0, "recorded": 0, "errors": ["skipped: no key"]}
    if engines.get("claude"):
        try:
            claude_result = _run_claude_citation_pass()
        except Exception as e:
            claude_result = {"executed": 0, "recorded": 0, "errors": [f"pass_exc:{str(e)[:80]}"]}

    return jsonify(
        ok=True,
        engines_with_keys=engines,
        matrix=matrix,
        executed=claude_result.get("executed", 0),
        recorded=claude_result.get("recorded", 0),
        errors=claude_result.get("errors", []),
        next_steps=(
            "Add GEMINI_API_KEY / PERPLEXITY_API_KEY / XAI_API_KEY to "
            "Railway env to light up additional engines. Schedule this "
            "endpoint weekly via cron for a time series."
        ),
    ), 200
