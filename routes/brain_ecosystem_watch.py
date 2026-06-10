"""
brain_ecosystem_watch.py — Phase r46 (2026-05-25).

Watches the MCP ecosystem for competitive pressure. The L23 lifecycle
curator audits OUR moat-health; this layer watches THEIR moves. When
a new MCP server appears in our category (data-center / energy /
infrastructure) or when our position changes on a directory's
leaderboard, we want to know.

Approach: lightweight HEAD/GET probes of public registry index pages.
We don't scrape full catalogs — we look for the small set of signals
that indicate competition or position change. Findings persist in
brain_ecosystem_watch table for L23 to consume via a new audit dim.

Endpoints:
  POST /api/v1/brain/ecosystem/watch        admin: kick a watch cycle
  GET  /api/v1/brain/ecosystem/findings     latest findings + summary

Schema is auto-created on import. Cron (daily) fires the watch cycle.
"""
from __future__ import annotations

import datetime
import json
import os
import urllib.request
import urllib.error
from typing import Any

from flask import Blueprint, jsonify, request


brain_ecosystem_watch_bp = Blueprint("brain_ecosystem_watch", __name__)


ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
             or os.environ.get("DCHUB_INTERNAL_KEY") or "")


# ── Watch targets ──────────────────────────────────────────────────
# We probe with a real-browser UA because most registries 403 bare curl.
# audit_signal is a case-insensitive substring; presence → we're listed.
# competition_signal is a substring → competitor entry exists.

# r-widen (2026-06-05): expanded from a 3-registry competition probe into a
# full ecosystem COVERAGE map for the MCP Growth Sentinel (widen arm). Each
# target carries:
#   kind          'official' (registry.modelcontextprotocol.io JSON — reliable) |
#                 'registry' (3rd-party HTML directory — best-effort, may 403) |
#                 'self'     (our own agent-facing surface — reliable, we control it)
#   submit_method how we get/stay listed: 'ci-auto' (mcp-registry-publish.yml) |
#                 'auto-index' (re-crawls the official registry) | 'claim' |
#                 'form' | 'pr' | 'owner' | 'native' (our own surface)
#   submit_url    where the listing lives / where to submit
# 'official' + 'self' targets are checked via _probe_self (reliable); the rest
# via _probe (browser-UA HTML, best-effort). competition_signal still feeds L23.
_WATCH_TARGETS = [
    # ── official registry: clean JSON API; mcp.so/PulseMCP/Glama auto-index it ──
    {"key": "official_registry", "name": "Official MCP Registry", "kind": "official",
     "url": "https://registry.modelcontextprotocol.io/v0/servers?search=dchub",
     "self_signal": "cloud.dchub", "competition_signal": "",
     "submit_method": "ci-auto",
     "submit_url": "https://registry.modelcontextprotocol.io"},

    # ── 3rd-party registries (best-effort HTML probe; many auto-index official) ──
    {"key": "mcp_so", "name": "mcp.so", "kind": "registry",
     "url": "https://mcp.so/servers", "self_signal": "dc-hub",
     "competition_signal": "data center", "submit_method": "auto-index",
     "submit_url": "https://mcp.so/server/dchub"},
    {"key": "glama", "name": "Glama", "kind": "registry",
     "url": "https://glama.ai/mcp/servers", "self_signal": "dchub",
     "competition_signal": "data-center", "submit_method": "auto-index",
     "submit_url": "https://glama.ai/mcp/servers"},
    {"key": "pulsemcp", "name": "PulseMCP", "kind": "registry",
     "url": "https://www.pulsemcp.com/servers", "self_signal": "dchub",
     "competition_signal": "data center", "submit_method": "auto-index",
     "submit_url": "https://www.pulsemcp.com"},
    {"key": "smithery", "name": "Smithery", "kind": "registry",
     "url": "https://smithery.ai/category/data", "self_signal": "dchub",
     "competition_signal": "data-center", "submit_method": "claim",
     "submit_url": "https://smithery.ai/server/azmartone67/dchub"},

    # ── native discoverability: our OWN surfaces every agent fetches (reliable) ──
    {"key": "self_mcp_manifest", "name": "Self · MCP manifest", "kind": "self",
     "url": "https://api.dchub.cloud/api/v1/mcp/manifest", "self_signal": "dchub",
     "competition_signal": "", "submit_method": "native", "submit_url": ""},
    {"key": "self_capabilities", "name": "Self · agent capabilities", "kind": "self",
     "url": "https://api.dchub.cloud/api/v1/agents/capabilities.json",
     "self_signal": "mcp", "competition_signal": "", "submit_method": "native",
     "submit_url": ""},
    {"key": "self_ai_agents_json", "name": "Self · ai-agents.json", "kind": "self",
     "url": "https://dchub.cloud/api/v1/ai-agents.json", "self_signal": "dchub",
     "competition_signal": "", "submit_method": "native", "submit_url": ""},
    {"key": "self_llms_txt", "name": "Self · llms.txt", "kind": "self",
     "url": "https://dchub.cloud/llms.txt", "self_signal": "dc hub",
     "competition_signal": "", "submit_method": "native", "submit_url": ""},
    {"key": "self_agents_md", "name": "Self · AGENTS.md", "kind": "self",
     "url": "https://dchub.cloud/AGENTS.md", "self_signal": "dc hub",
     "competition_signal": "", "submit_method": "native", "submit_url": ""},

    # ── expansion targets (the GAPS to widen into — manual/PR submits) ──
    {"key": "awesome_mcp", "name": "awesome-mcp-servers", "kind": "registry",
     "url": "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md",
     "self_signal": "dchub", "competition_signal": "data center",
     "submit_method": "pr",
     "submit_url": "https://github.com/punkpeye/awesome-mcp-servers"},
    {"key": "lobehub", "name": "LobeHub", "kind": "registry",
     "url": "https://lobehub.com/mcp/dchub", "self_signal": "dchub",
     "competition_signal": "data center", "submit_method": "form",
     "submit_url": "https://github.com/lobehub/lobe-chat-agents/issues"},
    {"key": "cursor_directory", "name": "Cursor Directory", "kind": "registry",
     "url": "https://cursor.directory/mcp", "self_signal": "dchub",
     "competition_signal": "data center", "submit_method": "form",
     "submit_url": "https://cursor.directory/mcp"},
    {"key": "mcp_get", "name": "mcp-get", "kind": "registry",
     "url": "https://mcp-get.com/packages", "self_signal": "dchub",
     "competition_signal": "data center", "submit_method": "pr",
     "submit_url": "https://github.com/michaellatman/mcp-get"},
    # r-widen-2 (2026-06-05): high-traffic GitHub-list registries — reliably
    # checkable via raw README (no SPA/bot-block), all verified ABSENT = real
    # PR-able gaps. Cline Marketplace = a top MCP client's directory.
    {"key": "cline_marketplace", "name": "Cline Marketplace", "kind": "registry",
     "url": "https://raw.githubusercontent.com/cline/mcp-marketplace/main/README.md",
     "self_signal": "dchub", "competition_signal": "data center",
     "submit_method": "pr", "submit_url": "https://github.com/cline/mcp-marketplace"},
    {"key": "appcypher_awesome", "name": "awesome-mcp-servers (appcypher)",
     "kind": "registry",
     "url": "https://raw.githubusercontent.com/appcypher/awesome-mcp-servers/main/README.md",
     "self_signal": "dchub", "competition_signal": "data center",
     "submit_method": "pr",
     "submit_url": "https://github.com/appcypher/awesome-mcp-servers"},
    {"key": "wong2_awesome", "name": "awesome-mcp-servers (wong2)",
     "kind": "registry",
     "url": "https://raw.githubusercontent.com/wong2/awesome-mcp-servers/main/README.md",
     "self_signal": "dchub", "competition_signal": "data center",
     "submit_method": "pr",
     "submit_url": "https://github.com/wong2/awesome-mcp-servers"},
    {"key": "habitoai_awesome", "name": "awesome-mcp-servers (HabitoAI)",
     "kind": "registry",
     "url": "https://raw.githubusercontent.com/HabitoAI/awesome-mcp-servers/main/README.md",
     "self_signal": "dchub", "competition_signal": "data center",
     "submit_method": "pr",
     "submit_url": "https://github.com/HabitoAI/awesome-mcp-servers"},
]


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS brain_ecosystem_watch (
    id BIGSERIAL PRIMARY KEY,
    at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    target_key TEXT NOT NULL,
    target_name TEXT NOT NULL,
    we_present BOOLEAN,
    competition_seen BOOLEAN,
    http_status INTEGER,
    page_bytes INTEGER,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_brain_ecosystem_at
    ON brain_ecosystem_watch (at DESC);
CREATE INDEX IF NOT EXISTS ix_brain_ecosystem_target
    ON brain_ecosystem_watch (target_key, at DESC);
"""


def _conn():
    db = (os.environ.get("DATABASE_URL")
          or os.environ.get("NEON_DATABASE_URL"))
    if not db:
        return None
    try:
        import psycopg2
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _ensure_schema():
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA_DDL)
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass


_ensure_schema()


def _admin_ok() -> bool:
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
    return bool(ADMIN_KEY) and provided == ADMIN_KEY


def _probe(target: dict) -> dict:
    """Single target probe — returns presence + competition findings."""
    req = urllib.request.Request(
        target["url"],
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read(200_000)  # cap at 200KB so we don't OOM
            status = r.status
    except urllib.error.HTTPError as e:
        return {
            "target_key": target["key"], "target_name": target["name"],
            "we_present": None, "competition_seen": None,
            "http_status": e.code, "page_bytes": 0,
            "detail": f"http_{e.code}",
        }
    except Exception as e:
        return {
            "target_key": target["key"], "target_name": target["name"],
            "we_present": None, "competition_seen": None,
            "http_status": 0, "page_bytes": 0,
            "detail": f"{type(e).__name__}: {str(e)[:80]}",
        }

    body_lower = body.lower()
    we_present = (target["self_signal"].lower().encode("utf-8")
                  in body_lower)
    competition_seen = (target["competition_signal"].lower().encode("utf-8")
                        in body_lower)

    return {
        "target_key":       target["key"],
        "target_name":      target["name"],
        "we_present":       we_present,
        "competition_seen": competition_seen,
        "http_status":      status,
        "page_bytes":       len(body),
        "detail":           "probed ok",
    }


def _read_local_server_version() -> str:
    """Local server.json version — used to flag a STALE official-registry
    listing (published version behind the repo) so the sentinel catches the
    'I bumped server.json but the registry still shows the old one' case."""
    try:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "server.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("version") or "")
    except Exception:
        return ""


def _probe_self(target: dict) -> dict:
    """Reliable probe for surfaces we control (kind='self') and the official
    registry JSON (kind='official'). HTTP 200 + signal present = we're live.
    For the official registry we also compare the published version to the
    local server.json and flag drift. Unlike _probe (best-effort HTML on
    bot-protected 3rd-party sites), these checks are deterministic."""
    req = urllib.request.Request(
        target["url"],
        headers={"User-Agent": "dchub-ecosystem-sentinel/1.0",
                 "Accept": "application/json, text/plain, */*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read(300_000)
            status = r.status
    except urllib.error.HTTPError as e:
        status, body = e.code, b""
    except Exception as e:
        return {"target_key": target["key"], "target_name": target["name"],
                "we_present": False, "competition_seen": None,
                "http_status": 0, "page_bytes": 0,
                "detail": f"{type(e).__name__}: {str(e)[:60]}"}

    text = body.decode("utf-8", "replace")
    low = text.lower()
    sig = (target.get("self_signal") or "").lower()
    present = bool(status == 200 and len(body) > 40 and (not sig or sig in low))
    detail = "live" if present else f"http_{status}_or_signal_miss"

    if target.get("kind") == "official" and status == 200:
        try:
            data = json.loads(text)
            servers = data.get("servers", []) if isinstance(data, dict) else []
            vers = [s.get("server", {}).get("version") for s in servers
                    if s.get("server", {}).get("name") == "cloud.dchub/mcp-server"]
            present = bool(vers)
            local_v = _read_local_server_version()
            pub_v = vers[-1] if vers else None
            if not pub_v:
                detail = "NOT in official registry"
            elif local_v and pub_v != local_v:
                detail = (f"STALE: published v{pub_v} != local v{local_v} "
                          f"(bump server.json + push to republish)")
            else:
                detail = f"live v{pub_v} (current)"
        except Exception as e:
            detail = f"json_parse: {str(e)[:50]}"

    return {"target_key": target["key"], "target_name": target["name"],
            "we_present": present, "competition_seen": None,
            "http_status": status, "page_bytes": len(body), "detail": detail}


def _record(finding: dict) -> None:
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO brain_ecosystem_watch
                  (target_key, target_name, we_present, competition_seen,
                   http_status, page_bytes, detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            """, (
                finding["target_key"], finding["target_name"],
                finding.get("we_present"), finding.get("competition_seen"),
                finding.get("http_status"), finding.get("page_bytes"),
                finding.get("detail"),
            ))
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass


@brain_ecosystem_watch_bp.route(
    "/api/v1/brain/ecosystem/watch", methods=["POST"])
def ecosystem_watch():
    """Run a watch cycle against all targets. Admin-keyed."""
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401

    _ensure_schema()
    findings = []
    for t in _WATCH_TARGETS:
        f = (_probe_self(t) if t.get("kind") in ("self", "official")
             else _probe(t))
        _record(f)
        findings.append(f)
    return jsonify({
        "ok": True,
        "findings": findings,
        "targets_count": len(_WATCH_TARGETS),
        "we_present_count": sum(1 for f in findings if f.get("we_present")),
        "competition_seen_count": sum(1 for f in findings if f.get("competition_seen")),
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }), 200


@brain_ecosystem_watch_bp.route(
    "/api/v1/brain/ecosystem/findings", methods=["GET"])
def ecosystem_findings():
    """Latest finding per target — what's the brain's current view of
    the ecosystem? Consumed by the L23 ecosystem_position audit dim."""
    _ensure_schema()
    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "db_unreachable",
                        "by_target": {}}), 200
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (target_key)
                  target_key, target_name, at, we_present,
                  competition_seen, http_status, detail
                FROM brain_ecosystem_watch
                ORDER BY target_key, at DESC
            """)
            rows = cur.fetchall()
        by_target = {}
        for r in rows:
            by_target[r[0]] = {
                "target_name":     r[1],
                "at":              r[2].isoformat() if r[2] else None,
                "we_present":      r[3],
                "competition_seen": r[4],
                "http_status":     r[5],
                "detail":          r[6],
            }
        # Summary
        present_count = sum(1 for v in by_target.values() if v.get("we_present"))
        return jsonify({
            "ok": True,
            "by_target": by_target,
            "summary": {
                "targets_known":   len(by_target),
                "we_present_in":   present_count,
                "expected_total":  len(_WATCH_TARGETS),
            },
            "purpose": (
                "MCP ecosystem competitive watch. POST /watch to refresh; "
                "GET /findings to read latest. L23 audit reads this for "
                "the ecosystem_position dim — flags weak if we're absent "
                "from more than 1 of the watched targets."
            ),
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
    finally:
        try: c.close()
        except Exception: pass


def compute_coverage_scorecard() -> dict:
    """MCP Growth Sentinel — WIDEN-arm coverage scorecard. Shared by the
    /api/v1/brain/ecosystem/coverage endpoint AND the brain heartbeat's
    growth_sentinel block (one source of truth). Reads the latest watch result
    per target (the every-6h cron populates it) — DB-read only, fast,
    single-replica-safe."""
    _ensure_schema()
    meta = {t["key"]: t for t in _WATCH_TARGETS}
    latest = {}
    c = _conn()
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT ON (target_key)
                      target_key, we_present, http_status, detail, at
                    FROM brain_ecosystem_watch
                    ORDER BY target_key, at DESC
                """)
                for r in cur.fetchall():
                    latest[r[0]] = {"we_present": r[1], "http_status": r[2],
                                    "detail": r[3],
                                    "at": r[4].isoformat() if r[4] else None}
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass

    # Official-registry presence drives auto-index inference: mcp.so / Glama /
    # PulseMCP crawl the official registry, so being live there ⇒ covered there
    # even when their JS/paginated index pages defeat a direct slug probe (a
    # bare probe shows them 'missing' = a FALSE negative). Don't call covered
    # surfaces gaps.
    official_live = bool(latest.get("official_registry", {}).get("we_present"))

    targets = []
    for t in _WATCH_TARGETS:
        lat = latest.get(t["key"], {})
        present = lat.get("we_present")
        method = t.get("submit_method", "")
        detail = lat.get("detail")
        if present:
            status = "live"                       # verified present
        elif method == "auto-index" and official_live:
            status = "auto-indexed"               # covered by design, probe can't confirm
            detail = "auto-indexed from official registry (direct probe inconclusive)"
        elif present is None:
            status = "unknown"                    # probe blocked (403/404/redirect)
        else:
            status = "gap"                        # verified-absent / manual submit pending
        targets.append({
            "key": t["key"], "name": t["name"], "kind": t.get("kind", "registry"),
            "we_present": present, "status": status,
            "submit_method": method, "submit_url": t.get("submit_url", ""),
            "http_status": lat.get("http_status"),
            "detail": detail, "last_checked": lat.get("at"),
        })

    live = [t for t in targets if t["status"] == "live"]
    auto = [t for t in targets if t["status"] == "auto-indexed"]
    gaps = [t for t in targets if t["status"] == "gap"]
    unknown = [t for t in targets if t["status"] == "unknown"]
    covered = len(live) + len(auto)
    denom = covered + len(gaps)  # exclude 'unknown' (probe-blocked) from the %
    # actionable: a verified gap on a surface with a manual submit path
    owner_actions = [
        {"name": t["name"], "submit_method": t["submit_method"],
         "submit_url": t["submit_url"], "detail": t["detail"]}
        for t in gaps
        if t["submit_method"] in ("form", "pr", "owner", "claim")
    ]
    return {
        "ok": True,
        "scorecard": {
            "total_targets": len(targets),
            "verified_live": len(live),
            "auto_indexed": len(auto),
            "covered": covered,
            "gaps": len(gaps),
            "unknown": len(unknown),
            "coverage_pct": round(100.0 * covered / denom, 1) if denom else None,
        },
        "by_kind": {
            k: {"covered": sum(1 for t in targets if t["kind"] == k
                               and t["status"] in ("live", "auto-indexed")),
                "total": sum(1 for t in targets if t["kind"] == k)}
            for k in ("official", "registry", "self")
        },
        "owner_actions": owner_actions,
        "targets": targets,
        "note": ("3rd-party registries are best-effort (403/redirect/JS defeat a "
                 "bare probe); 'official' + 'self' are deterministic. "
                 "'auto-indexed' = covered via the official-registry crawl. "
                 "'unknown' = probe blocked, not absent."),
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


@brain_ecosystem_watch_bp.route(
    "/api/v1/brain/ecosystem/coverage", methods=["GET"])
def ecosystem_coverage():
    """GET the coverage scorecard (thin wrapper over compute_coverage_scorecard
    so the brain heartbeat reuses the same computation). Public: it's 'where are
    we listed', not secret."""
    resp = jsonify(compute_coverage_scorecard())
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp, 200
