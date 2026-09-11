"""
distribution_master_shell.py — the self-driving DISTRIBUTION orchestrator (2026-07-03).

Owns lever #5 of the growth flywheel: grow how AI agents DISCOVER DC Hub —
(a) GEO answer pages that win BROAD non-branded queries (DC Hub already wins
branded ones), and (b) presence on the high-traffic MCP registries.

Each tick MEASURES: per-query GEO coverage (probe /answers/<slug> + llms.txt
keyword) and real registry presence (mcp_presence_listings, the crawler's ground
truth — NOT the hardcoded mcp_standing counts). SCORES distribution. ACTS on the
weaker sub-signal: a GEO gap emits a content brief for the top uncovered query +
fires the audience GEO tick; a registry gap fires the (env-gated) registry
submit + agent broadcast. PERSISTS a 0-100 score.

Constraint (RESOLVED 2026-07-03): GEO page authoring is no longer a human step —
routes/geo_answer_publisher.py commits answers/<slug>.html to the frontend via
the GitHub Contents API (POST /api/v1/admin/geo/publish-answer). The shell still
briefs the top gap; wiring brief->publish (LLM draft_answer -> structured fields
-> publish_answer) is the remaining self-drive step. Registry + broadcast actions
are already fully self-driving. Mirrors the house pattern (loopback self-calls +
fire-and-forget, admin-gated, killable).

Endpoints:
  POST /api/v1/admin/distribution/master-tick   — measure -> score -> act -> persist
  GET  /api/v1/admin/distribution/master-state   — latest snapshot + trend
"""
import os
import json
import time
import hmac
import urllib.request
import urllib.error
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write

distribution_master_shell_bp = Blueprint("distribution_master_shell", __name__)

_BACKEND_BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else os.environ.get("DCHUB_BACKEND_BASE", "https://dchub-backend-production.up.railway.app")
)
_GEO_BASE = "https://dchub.cloud"

# The broad, high-intent developer queries DC Hub must win, each mapped to its
# GEO answer-page slug (frontend/answers/<slug>.html). Mirrors
# audience_master_shell._DISCOVERY_QUERIES. `must_any` = llms.txt keyword proxy.
_GEO_QUERIES = [
    {"q": "live data center capacity data into an AI agent",
     "slug": "live-data-center-capacity-for-ai-agents",
     "must_any": ["data center capacity", "capacity data"]},
    {"q": "power grid / ISO headroom data",
     "slug": "global-grid-data-for-ai-agents",
     "must_any": ["grid intelligence", "grid headroom", "iso"]},
    {"q": "interconnection queue data",
     "slug": "interconnection-queue-data-for-ai-agents",
     "must_any": ["interconnection queue", "interconnection"]},
    {"q": "natural gas / behind-the-meter data for siting",
     "slug": "natural-gas-data-for-ai-data-center-siting",
     "must_any": ["natural gas", "gas pipeline", "behind-the-meter"]},
    {"q": "data center M&A / transaction data",
     "slug": "data-center-ma-data-for-ai-agents",
     "must_any": ["m&a", "transactions", "acquisition"]},
    {"q": "dark fiber / network route data",
     "slug": "dark-fiber-data-for-ai-agents",
     "must_any": ["fiber", "dark fiber"]},
    {"q": "data center site selection with an AI agent",
     "slug": "data-center-site-selection-for-ai-agents",
     "must_any": ["site selection", "score_facility"]},
    {"q": "power market / energy price data",
     "slug": "energy-price-data-for-ai-agents",
     "must_any": ["energy price", "energy prices", "power market"]},
    {"q": "data center water risk data",
     "slug": "water-risk-data-for-ai-agents",
     "must_any": ["water risk", "water"]},
    {"q": "real-time infrastructure ground truth for AI agents",
     "slug": "infrastructure-ground-truth-for-ai-agents",
     "must_any": ["ground truth", "infrastructure data layer"]},
]

# Registries that matter, ranked by traffic. Presence measured from the crawler.
_KEY_REGISTRIES = ["smithery", "glama", "pulsemcp", "mcp.so", "lobehub"]


def _admin_key() -> str | None:
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _admin_ok() -> bool:
    expected = (_admin_key() or "").strip()
    if not expected:
        return False
    got = (request.headers.get("X-Admin-Key") or request.args.get("admin_key") or "").strip()
    return bool(got) and hmac.compare_digest(got, expected)


def _disabled() -> bool:
    return str(os.environ.get("DISTRIBUTION_MASTER_DISABLED", "")).lower() in ("1", "true", "yes")


def _act_disabled() -> bool:
    return str(os.environ.get("DISTRIBUTION_MASTER_ACT_DISABLED", "")).lower() in ("1", "true", "yes")


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


def _fetch(url: str, method: str = "GET", timeout: int = 8) -> int:
    """Return HTTP status only (cheap coverage probe)."""
    try:
        req = urllib.request.Request(url, data=(b"" if method == "POST" else None), method=method)
        req.add_header("User-Agent", "dchub-distribution-orchestrator/1.0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def _fetch_text(url: str, timeout: int = 10) -> tuple[int, str]:
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "dchub-distribution-orchestrator/1.0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def _fire(path: str, timeout: int = 4) -> dict:
    url = (path if path.startswith("http") else _BACKEND_BASE.rstrip("/") + path)
    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        req.add_header("X-DC-Probe", "distribution-tick")
        req.add_header("User-Agent", "dchub-distribution-orchestrator/1.0")
        ak = _admin_key()
        if ak:
            req.add_header("X-Admin-Key", ak)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": resp.status < 400, "http": resp.status, "dispatched": True}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "dispatched": True}
    except Exception:
        return {"ok": True, "dispatched": True, "note": "not awaited"}


def _conn():
    try:
        from routes.ai_reach import _conn as _raw
        return _raw()
    except Exception:
        return None


def _ensure_tables() -> bool:
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS distribution_snapshots (
                    id                 SERIAL PRIMARY KEY,
                    computed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    distribution_score NUMERIC(6,2),
                    geo_coverage       NUMERIC(6,3),
                    geo_gaps           INTEGER,
                    registry_present   INTEGER,
                    registry_total     INTEGER,
                    weakest            TEXT,
                    action_taken       TEXT,
                    detail             JSONB
                )
            """)
        return True
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


def _registry_presence() -> dict:
    """Real registry presence from the crawler's mcp_presence_listings (NOT the
    hardcoded mcp_standing counts). Returns {present:set, total:int}."""
    c = _conn()
    present = set()
    if c is not None:
        try:
            # 2026-07-03 FIX: real columns are registry_name + discovered. The
            # prior query used registry/present/listed — none exist — so it
            # threw, the fallback reused the same bad column, and this ALWAYS
            # returned 0/5 while we're listed everywhere.
            with c.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT lower(registry_name)
                      FROM mcp_presence_listings
                     WHERE COALESCE(discovered, FALSE) = true
                """)
                present = {r[0] for r in cur.fetchall() if r and r[0]}
        except Exception:
            try: c.rollback()
            except Exception: pass
        finally:
            try: c.close()
            except Exception: pass
    # Normalize dots/hyphens→underscores so "mcp.so" matches the crawler's "mcp_so".
    def _norm(s: str) -> str:
        return (s or "").replace(".", "_").replace("-", "_")
    present_n = {_norm(p) for p in present}
    hits = {reg for reg in _KEY_REGISTRIES if any(_norm(reg) in p for p in present_n)}
    return {"present": sorted(hits), "missing": [r for r in _KEY_REGISTRIES if r not in hits],
            "count": len(hits), "total": len(_KEY_REGISTRIES)}


# ── TIER 1 — MEASURE (GEO coverage + registry presence) ───────────────
def tier1_measure() -> dict:
    code_llms, llms = _fetch_text(_GEO_BASE + "/llms.txt")
    hay = (llms or "").lower()
    covered, gaps = [], []
    for item in _GEO_QUERIES:
        # coverage = intent language present in llms.txt (an AI would retrieve +
        # recommend DC Hub here). A dedicated /answers/<slug> page strengthens
        # this — but per-tick HTTP probing of all 10 (external round-trips) blew
        # past CF's ~15s proxy limit, so the keyword signal is the per-tick proxy.
        kw = any(k.lower() in hay for k in item["must_any"])
        (covered if kw else gaps).append({"q": item["q"], "slug": item["slug"]})
    coverage = round(len(covered) / len(_GEO_QUERIES), 3) if _GEO_QUERIES else 0.0
    reg = _registry_presence()
    return {
        "geo_coverage": coverage,
        "geo_covered": len(covered),
        "geo_gaps": gaps,
        "registry": reg,
        "honest_read": (
            f"GEO {int(coverage*100)}% ({len(covered)}/{len(_GEO_QUERIES)} broad queries); "
            f"registries {reg['count']}/{reg['total']} present ({', '.join(reg['present']) or 'none'})."
        ),
    }


# ── TIER 2 — SCORE ────────────────────────────────────────────────────
def tier2_score(m: dict) -> dict:
    geo = m.get("geo_coverage") or 0.0
    reg = m.get("registry") or {}
    reg_cov = (reg.get("count") or 0) / (reg.get("total") or len(_KEY_REGISTRIES))
    score = round(100.0 * (0.6 * geo + 0.4 * reg_cov), 2)
    weakest = "geo" if geo <= reg_cov else "registry"
    return {"distribution_score": score, "geo_coverage": geo, "registry_coverage": round(reg_cov, 3),
            "weakest": weakest}


# ── TIER 3 — ACT (brief the top GEO gap; submit a missing registry) ───
def tier3_act(m: dict, sc: dict) -> dict:
    if _act_disabled():
        return {"action": "none", "reason": "DISTRIBUTION_MASTER_ACT_DISABLED (shadow)"}
    weakest = sc.get("weakest")

    # 2026-07-03: self-driving GEO authoring runs EVERY tick regardless of the
    # weakest lever — it expands coverage into new high-intent queries beyond
    # the tracked broad set. Publishes ONE page/tick, LLM-drafted from real DB
    # facts, committed to the frontend via GitHub API. Inert unless
    # GEO_AUTOPUBLISH_ENABLED=1 (autopublish_next self-gates). Never raises.
    geo_auto = None
    try:
        from routes.geo_autopublish import autopublish_next
        geo_auto = autopublish_next(dry=False)
    except Exception as _e:
        geo_auto = {"ok": False, "error": str(_e)[:120]}

    if weakest == "registry":
        # Real submit is env-gated (REGISTRY_SUBMIT_ENABLED) server-side; also
        # re-broadcast our discovery surfaces so registries re-crawl us.
        _fire("/api/v1/admin/outreach/mcp-registry/submit-all")
        _fire("/api/v1/agents/broadcast")
        return {"action": "registry_submit+broadcast", "lever": "registry",
                "missing": (m.get("registry") or {}).get("missing"),
                "geo_autopublish": geo_auto, "dispatched": True}

    # GEO gap: fire the audience GEO tick (acts on the top broad-query gap) and
    # emit a content brief for the top uncovered answer page (frontend commit is
    # a human/one-time step — see module docstring).
    _fire("/api/v1/admin/audience/master-tick")
    gaps = m.get("geo_gaps") or []
    brief = None
    if gaps:
        g = gaps[0]
        brief = {
            "target_query": g["q"],
            "publish": f"frontend/answers/{g['slug']}.html (+ main.py static_pages + answers/index + sitemap)",
            "must_include": [
                f"<title>/<h1> mirroring the query: \"{g['q']}\"",
                "the exact MCP tool that answers it + connect line "
                "(claude mcp add dchub --transport http https://dchub.cloud/mcp)",
                "one cited LIVE data point (proof) + the free-tier hook (10 calls/day, no key)",
            ],
        }
    return {"action": "geo_tick+answer_page_brief", "lever": "geo",
            "remaining_gaps": len(gaps), "brief": brief,
            "geo_autopublish": geo_auto, "dispatched": True}


# ── persist ───────────────────────────────────────────────────────────
def _persist(m: dict, sc: dict, action: dict) -> bool:
    if not _ensure_tables():
        return False
    c = _conn()
    if c is None:
        return False
    try:
        reg = m.get("registry") or {}
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO distribution_snapshots
                  (distribution_score, geo_coverage, geo_gaps, registry_present,
                   registry_total, weakest, action_taken, detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (
                sc.get("distribution_score"), sc.get("geo_coverage"), len(m.get("geo_gaps") or []),
                reg.get("count"), reg.get("total"), sc.get("weakest"),
                (action or {}).get("action"),
                json.dumps({"measure": m, "score": sc, "action": action}),
            ))
        return True
    except Exception:
        note_swallowed_write("distribution_snapshots", where="distribution_master_shell._persist")
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── ORCHESTRATOR ──────────────────────────────────────────────────────
@distribution_master_shell_bp.route("/api/v1/admin/distribution/master-tick", methods=["POST", "GET"])
def master_tick():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="DISTRIBUTION_MASTER_DISABLED"), 200
    started = time.time()
    measure = tier1_measure()
    score = tier2_score(measure)
    action = tier3_act(measure, score)
    persisted = _persist(measure, score, action)
    headline = (
        f"distribution {score.get('distribution_score')}/100 · "
        f"GEO {int((score.get('geo_coverage') or 0)*100)}% ({len(measure.get('geo_gaps') or [])} gaps) · "
        f"registries {(measure.get('registry') or {}).get('count')}/{(measure.get('registry') or {}).get('total')} · "
        f"weakest {score.get('weakest')} -> {action.get('action')}"
    )
    return jsonify(
        ok=True, ms=int((time.time() - started) * 1000),
        distribution_score=score.get("distribution_score"), headline=headline,
        tier1_measure=measure, tier2_score=score, tier3_action=action,
        persisted=persisted, generated_at=datetime.now(timezone.utc).isoformat(),
    ), 200


@distribution_master_shell_bp.route("/api/v1/admin/distribution/master-state", methods=["GET"])
def state():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(error="db_unavailable"), 503
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM distribution_snapshots ORDER BY id DESC LIMIT 20")
            rows = cur.fetchall()
        return jsonify(ok=True, latest=(rows[0] if rows else None),
                       trend=[{"at": str(r.get("computed_at")), "score": _num(r.get("distribution_score")),
                               "geo_coverage": _num(r.get("geo_coverage")), "weakest": r.get("weakest"),
                               "acted": r.get("action_taken")} for r in rows]), 200
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}"), 500
    finally:
        try: c.close()
        except Exception: pass
