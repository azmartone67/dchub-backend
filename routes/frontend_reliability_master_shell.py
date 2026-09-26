"""
frontend_reliability_master_shell.py — the self-driving FRONTEND-RELIABILITY
orchestrator (2026-07-14).

Sibling to reliability_master_shell (which points at the brain's fix-success
rate); this one points at the PUBLIC-PAGE reliability surface that the
consistency_radar's `frontend_endpoint_slow` / `frontend_endpoint_unreachable`
detectors keep flagging. It generalizes the #100095 fix (the /api/pipeline
stale-while-revalidate cache) so the whole CLASS of problem is caught
continuously instead of one finding at a time — and, critically, so a
scaffold-only "fix" that silently re-fires can't hide.

Every tick it scores THREE lanes, finds the weakest, and pulls exactly one
bounded action.

The three lanes:
  1. SLOW_PATH_CACHE     — do the shared public API paths behind the slow-page
                           findings actually have an ORIGIN cache now? A page is
                           only fast for real if its API path serves from cache
                           (emits X-Cache) or answers under the radar's cap. An
                           uncached path that flapped = the real, unfixed bug.
  2. FALSE_CLOSE_REFIRE  — are findings being marked resolved and then re-firing
                           (the scaffold-PR / substance-gate pathology)? A
                           resolved finding whose last_seen moves past its
                           resolved_at was closed without a code change and will
                           churn forever. Surface them for a REAL fix.
  3. EDGE_CACHEABILITY   — audit the hot PUBLIC read endpoints: are the ones that
                           are BOTH no-store AND slow being left un-cacheable at
                           every layer? (no-store is a deliberate /api/* policy —
                           this lane only dings an endpoint that is no-store AND
                           slow, i.e. fast for nobody.)

Design mirrors the house master-shell pattern exactly: admin-gated, killable
(FRONTEND_RELIABILITY_MASTER_DISABLED=1), a snapshot table, 0-100 score, one
bounded action/tick. SHADOW BY DEFAULT — it measures + scores + persists every
tick but files NO finding unless FRONTEND_RELIABILITY_MASTER_ARM=1. Every action
is individually killable (FRONTEND_RELIABILITY_LEVER_<NAME>_OFF=1) and fail-soft.

Endpoints:
  POST /api/v1/admin/frontend-reliability/master-tick  — measure → score → act
  GET  /api/v1/admin/frontend-reliability/state        — latest snapshot + trend
"""
import os
import re
import json
import time
import hmac
import urllib.request
import urllib.error
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write

frontend_reliability_master_shell_bp = Blueprint("frontend_reliability_master_shell", __name__)

# Origin base: loopback on Railway (the shell runs INSIDE the backend, so hitting
# the public URL would round-trip out through Cloudflare — which also strips the
# X-Cache header we read in lane 1). Loopback keeps origin probes honest + fast.
_ORIGIN_BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else os.environ.get("DCHUB_BACKEND_BASE", "https://dchub-backend-production.up.railway.app")
)
# Edge base: the public CF hostname — lane 3 must read the EDGE Cache-Control.
_EDGE_BASE = os.environ.get("DCHUB_PUBLIC_BASE", "https://dchub.cloud")

# The radar's frontend_endpoint_slow cap (seconds). Mirrors the probe-set cap in
# routes/brain_consistency_radar.py (5s for the pipeline pages).
_SLOW_CAP_S = float(os.environ.get("FRONTEND_RELIABILITY_SLOW_CAP_S", "5"))
_MEASURE_WINDOW_DAYS = 14
_MAX_PATH_PROBES = 5        # lane 1: bound origin probes/tick
_PROBE_TIMEOUT = 5          # seconds/probe (a cold slow path trips this → "slow")
# HTTP statuses where an anon GET probe can't represent the endpoint → excluded
# from lane 1's covered/total ratio (auth-gated, wrong-method, absent).
_UNSCOREABLE_HTTP = (401, 403, 404, 405, 410, 501)

# Hot PUBLIC, non-personalized read endpoints (the API paths behind public pages,
# from the consistency_radar probe set). Lane 3 audits these for no-store+slow.
_HOT_PUBLIC_PATHS = tuple(
    p.strip() for p in os.environ.get(
        "FRONTEND_RELIABILITY_HOT_PATHS",
        "/api/pipeline,/api/v1/news?limit=5,/api/v1/dcpi/scores?limit=300,"
        "/api/v1/status,/api/v1/tax-incentives?limit=50,/api/v1/listings"
    ).split(",") if p.strip()
)


# ── auth (mirrors reliability_master_shell) ───────────────────────────
def _admin_key() -> str | None:
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _admin_ok() -> bool:
    expected = (_admin_key() or "").strip()
    if not expected:
        return False
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(got) and hmac.compare_digest(got, expected)


def _disabled() -> bool:
    return str(os.environ.get("FRONTEND_RELIABILITY_MASTER_DISABLED", "")).lower() in ("1", "true", "yes")


def _act_enabled() -> bool:
    """SHADOW by default: measure + score + persist every tick but file NO finding
    unless the operator explicitly arms it (FRONTEND_RELIABILITY_MASTER_ARM=1)."""
    return str(os.environ.get("FRONTEND_RELIABILITY_MASTER_ARM", "")).lower() in ("1", "true", "yes")


def _lever_off(name: str) -> bool:
    """Per-lane kill switch, e.g. FRONTEND_RELIABILITY_LEVER_SLOW_PATH_CACHE_OFF=1."""
    return str(os.environ.get(f"FRONTEND_RELIABILITY_LEVER_{name.upper()}_OFF", "")).lower() in ("1", "true", "yes")


# ── db (mirror the house shells) ──────────────────────────────────────
def _conn():
    """autocommit=True conn — reads + the single-statement snapshot INSERT."""
    try:
        from routes.ai_reach import _conn as _raw
        return _raw()
    except Exception:
        return None


def _txn_conn():
    """autocommit=False conn — REQUIRED for upsert_brain_finding (SAVEPOINT-
    wrapped; silently no-ops under autocommit — the documented trap)."""
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        import psycopg2
        c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        c.autocommit = False
        return c
    except Exception:
        return None


# ── probe helper ──────────────────────────────────────────────────────
def _probe(base: str, path: str, timeout: int = _PROBE_TIMEOUT) -> dict:
    """GET base+path, returning latency + the cache headers. Reads the full body
    (capped) so the latency reflects time-to-full-response — exactly what the
    radar's requests.get measures. Never raises."""
    url = path if path.startswith("http") else base.rstrip("/") + path
    started = time.time()
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "dchub-frontend-reliability/1.0")
        req.add_header("X-DC-Probe", "frontend-reliability-tick")  # rate-limiter bypass
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(2_000_000)  # pull the body → latency includes server work
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return {"ok": True, "http": resp.status,
                    "ms": int((time.time() - started) * 1000),
                    "xcache": hdrs.get("x-cache"),
                    "cache_control": (hdrs.get("cache-control") or "").lower()}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "ms": int((time.time() - started) * 1000)}
    except Exception as e:
        # A timeout on a cold slow path is itself the signal — record it as slow.
        return {"ok": False, "http": None, "ms": int((time.time() - started) * 1000),
                "error": f"{type(e).__name__}: {str(e)[:80]}"}


def _probe_many(specs: dict, timeout: int = _PROBE_TIMEOUT, workers: int = 8) -> dict:
    """Concurrently probe {key: (base, path)} → {key: result}. Bounded thread pool
    so the whole tick is capped at ~one slow probe (~5s), not the serial sum.
    Never raises — a failed probe becomes an error result under its key."""
    out = {}
    if not specs:
        return out
    import concurrent.futures as _cf
    try:
        with _cf.ThreadPoolExecutor(max_workers=min(workers, len(specs))) as ex:
            futs = {ex.submit(_probe, base, path, timeout): k
                    for k, (base, path) in specs.items()}
            for fut in _cf.as_completed(futs):
                k = futs[fut]
                try:
                    out[k] = fut.result()
                except Exception as e:
                    out[k] = {"ok": False, "http": None, "error": f"{type(e).__name__}: {str(e)[:60]}"}
    except Exception:
        # pool failure → fall back to serial (still correct, just slower)
        for k, (base, path) in specs.items():
            out[k] = _probe(base, path, timeout)
    return out


def _confirm_probes(results: dict, specs: dict, timeout: int = _PROBE_TIMEOUT) -> dict:
    """Serially re-probe any result that came back slow (>= the cap) or errored,
    to distinguish a GENUINELY slow path from one that only timed out under the
    tick's own concurrent probe burst (self-contention: the shell probes the very
    backend serving it). The parallel pass is done, so the retry is uncontended.
    Keep whichever is better — a real slow path stays slow; a contention blip or an
    auth/method status (401/405) that the timeout hid is recovered. Bounded: only
    the already-bad probes are retried, one at a time."""
    cap_ms = _SLOW_CAP_S * 1000
    for k, pr in list(results.items()):
        bad = (not pr.get("ok")) or ((pr.get("ms") or 0) >= cap_ms)
        if not bad or k not in specs:
            continue
        base, path = specs[k]
        retry = _probe(base, path, timeout)
        if retry.get("ok") and (not pr.get("ok") or (retry.get("ms") or 9e9) < (pr.get("ms") or 9e9)):
            results[k] = retry                       # contention blip → the fast truth
        elif (not pr.get("ok")) and retry.get("http") is not None:
            results[k] = retry                       # timeout hid a real status (401/405/…)
    return results


def _ensure_tables() -> bool:
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS frontend_reliability_snapshots (
                    id                 SERIAL PRIMARY KEY,
                    computed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    score              NUMERIC(6,2),
                    slow_path_cache    NUMERIC(6,3),
                    false_close_refire NUMERIC(6,3),
                    edge_cacheability  NUMERIC(6,3),
                    weakest_lever      TEXT,
                    action_taken       TEXT,
                    lever_scores       JSONB,
                    detail             JSONB
                )
            """)
        return True
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── TIER 1 — MEASURE (three lanes, straight from brain_findings + probes) ──
_API_PATH_RE = re.compile(r"API call to `([^`]+)`")
_PAGE_RE = re.compile(r"Public page `([^`]+)`")
_TOOK_RE = re.compile(r"took ([0-9.]+)s")


def _measure_slow_path_cache() -> dict:
    """Group open/recent frontend_endpoint_slow findings by the SHARED API path
    they blame, then probe each distinct path at the ORIGIN: a path is 'covered'
    if it serves from an SWR cache (emits X-Cache) or answers under the cap."""
    out = {"slow_paths": [], "total_paths": 0, "covered_paths": 0, "error": None}
    c = _conn()
    if c is None:
        out["error"] = "no_db"
        return out
    by_path = {}
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT detail, url, status, COALESCE(seen_count, 0), resolved_at
                  FROM brain_findings
                 WHERE issue IN ('frontend_endpoint_slow', 'frontend_endpoint_unreachable')
                   AND last_seen >= NOW() - INTERVAL '%s days'
                 ORDER BY COALESCE(seen_count,0) DESC LIMIT 60
            """ % int(_MEASURE_WINDOW_DAYS))
            for detail, url, status, seen, resolved_at in cur.fetchall():
                d = str(detail or "")
                mp = _API_PATH_RE.search(d)
                api = (mp.group(1) if mp else None)
                if not api or not api.startswith("/"):
                    continue
                pg = _PAGE_RE.search(d)
                page = pg.group(1) if pg else (url or "")
                mt = _TOOK_RE.search(d)
                took = float(mt.group(1)) if mt else None
                e = by_path.setdefault(api, {"pages": set(), "max_seen": 0,
                                             "worst_took_s": None, "open": False})
                e["pages"].add(page)
                e["max_seen"] = max(e["max_seen"], int(seen or 0))
                if took is not None:
                    e["worst_took_s"] = max(e["worst_took_s"] or 0, took)
                if (status or "").lower() not in ("resolved", "wont_fix", "closed"):
                    e["open"] = True
    except Exception as ex:
        out["error"] = f"{type(ex).__name__}: {str(ex)[:120]}"
        try: c.close()
        except Exception: pass
        return out
    finally:
        try: c.close()
        except Exception: pass

    # probe the worst N distinct paths at the origin (bounded, concurrent)
    ranked = sorted(by_path.items(), key=lambda kv: kv[1]["max_seen"], reverse=True)[:_MAX_PATH_PROBES]
    l1_specs = {api: (_ORIGIN_BASE, api) for api, _ in ranked}
    probes = _confirm_probes(_probe_many(l1_specs), l1_specs)
    for api, e in ranked:
        pr = probes.get(api) or {}
        # Only a DEFINITIVE public 200 is scoreable. Everything else is excluded
        # from the covered/total ratio because it can't safely support "file a
        # finding": a non-2xx status (401/403 auth-gated — a cache would leak;
        # 405 wrong-method like the POST-only /api/v1/demo/ask; 404/410/501 absent),
        # OR a probe that errored/timed out (http is None → ambiguous: auth, backend
        # load, or genuine — even after the serial confirmation re-probe). Counting
        # an ambiguous timeout as "uncovered slow" is what made /api/v1/news +
        # /api/v1/usage flap; being conservative here keeps the shell from filing a
        # bogus finding. A genuinely slow PUBLIC path still counts (200 + slow + no
        # cache); the radar catches timeout-only paths independently.
        _st = pr.get("http")
        excluded = (_st in _UNSCOREABLE_HTTP) or (_st is None)
        has_cache = bool(pr.get("xcache"))
        fast = pr.get("ok") and (pr.get("ms") or 0) < _SLOW_CAP_S * 1000
        covered = bool(has_cache or fast)
        out["slow_paths"].append({
            "api_path": api, "pages": sorted(e["pages"])[:4], "max_seen": e["max_seen"],
            "worst_took_s": e["worst_took_s"], "open": e["open"],
            "probe_ms": pr.get("ms"), "probe_http": pr.get("http"),
            "x_cache": pr.get("xcache"), "covered": covered, "excluded": excluded,
        })
    scoreable = [p for p in out["slow_paths"] if not p["excluded"]]
    out["total_paths"] = len(scoreable)
    out["covered_paths"] = sum(1 for p in scoreable if p["covered"])
    return out


def _measure_false_close_refire() -> dict:
    """The scaffold-PR / substance-gate pathology, measured from the ledger. A
    finding that keeps getting "closed" without a code change re-fires forever, so
    a healthy resolution is one that STICKS. We flag a resolved finding as churny
    when it is chronic-recurring — seen many times over a long span — OR it was
    literally seen again after being resolved. Those are the ones a spec-doc close
    (like brain-spec PR #1607 did to #100095) leaves broken."""
    out = {"resolved_total": 0, "churny": 0, "seen_again": 0, "top": [], "error": None}
    c = _conn()
    if c is None:
        out["error"] = "no_db"
        return out
    _CHRONIC_SEEN = int(os.environ.get("FRONTEND_RELIABILITY_CHRONIC_SEEN", "20"))
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT
                  COUNT(*) FILTER (WHERE resolved_at IS NOT NULL),
                  COUNT(*) FILTER (WHERE resolved_at IS NOT NULL
                                    AND last_seen > resolved_at + INTERVAL '1 hour'),
                  COUNT(*) FILTER (WHERE resolved_at IS NOT NULL
                                    AND COALESCE(seen_count,0) >= %s
                                    AND last_seen - first_seen >= INTERVAL '7 days')
                  FROM brain_findings
                 WHERE last_seen >= NOW() - INTERVAL '30 days'
            """, (_CHRONIC_SEEN,))
            row = cur.fetchone() or (0, 0, 0)
            out["resolved_total"] = int(row[0] or 0)
            out["seen_again"] = int(row[1] or 0)
            out["churny"] = int(row[2] or 0)
            cur.execute("""
                SELECT issue, url, COALESCE(seen_count,0),
                       EXTRACT(DAY FROM (last_seen - first_seen))::int
                  FROM brain_findings
                 WHERE resolved_at IS NOT NULL
                   AND last_seen >= NOW() - INTERVAL '30 days'
                   AND ( (last_seen > resolved_at + INTERVAL '1 hour')
                         OR (COALESCE(seen_count,0) >= %s
                             AND last_seen - first_seen >= INTERVAL '7 days') )
                 ORDER BY COALESCE(seen_count,0) DESC LIMIT 6
            """, (_CHRONIC_SEEN,))
            out["top"] = [{"issue": r[0], "url": r[1], "seen_count": int(r[2] or 0),
                           "span_days": int(r[3] or 0)} for r in cur.fetchall()]
    except Exception as ex:
        out["error"] = f"{type(ex).__name__}: {str(ex)[:120]}"
    finally:
        try: c.close()
        except Exception: pass
    return out


def _measure_edge_cacheability() -> dict:
    """Audit the hot PUBLIC read endpoints at the EDGE. An endpoint is UNhealthy
    only if it is BOTH no-store (edge/browser can't cache) AND slow at the origin
    (no SWR cache either) — i.e. fast for nobody. no-store on a FAST endpoint is a
    deliberate, fine /api/* policy and is NOT dinged."""
    out = {"audited": 0, "healthy": 0, "no_store": 0, "endpoints": [], "error": None}
    hot = _HOT_PUBLIC_PATHS[:8]
    specs = {}
    for path in hot:
        specs[("edge", path)] = (_EDGE_BASE, path)
        specs[("origin", path)] = (_ORIGIN_BASE, path)
    probes = _confirm_probes(_probe_many(specs), specs)
    for path in hot:
        edge = probes.get(("edge", path)) or {}
        cc = edge.get("cache_control") or ""
        no_store = ("no-store" in cc) or ("max-age=0" in cc and "private" in cc)
        edge_cacheable = edge.get("ok") and not no_store
        # origin latency (loopback). Only a CONFIRMED slow 200 counts as slow — a
        # timed-out/errored origin probe is ambiguous (backend load, not necessarily
        # this endpoint) and must not flip the audit. So an endpoint is unhealthy
        # ONLY when it is no-store AND a definitive slow 200 AND not edge-cacheable.
        origin = probes.get(("origin", path)) or {}
        origin_fast = origin.get("ok") and (origin.get("ms") or 0) < 2000
        origin_slow_confirmed = origin.get("ok") and (origin.get("ms") or 0) >= 2000
        healthy = not (no_store and origin_slow_confirmed and not edge_cacheable)
        out["audited"] += 1
        if no_store:
            out["no_store"] += 1
        if healthy:
            out["healthy"] += 1
        out["endpoints"].append({
            "path": path, "edge_cache_control": cc[:60] or None,
            "no_store": no_store, "edge_cacheable": edge_cacheable,
            "origin_ms": origin.get("ms"), "origin_fast": origin_fast,
            "x_cache": origin.get("xcache"), "healthy": healthy,
        })
    return out


def tier1_measure() -> dict:
    slow = _measure_slow_path_cache() if not _lever_off("slow_path_cache") else {"skipped": True}
    refire = _measure_false_close_refire() if not _lever_off("false_close_refire") else {"skipped": True}
    edge = _measure_edge_cacheability() if not _lever_off("edge_cacheability") else {"skipped": True}
    m = {"slow_path_cache": slow, "false_close_refire": refire, "edge_cacheability": edge}
    m["honest_read"] = (
        f"slow-paths {slow.get('covered_paths')}/{slow.get('total_paths')} cached · "
        f"non-sticky resolves {max(refire.get('churny') or 0, refire.get('seen_again') or 0)}"
        f"/{refire.get('resolved_total')} · "
        f"hot-public {edge.get('healthy')}/{edge.get('audited')} fast-or-cacheable "
        f"({edge.get('no_store')} no-store)"
    )
    return m


# ── TIER 2 — SCORE THE THREE LANES (0..1 each; higher = healthier) ────
def tier2_score_levers(m: dict) -> dict:
    slow = m.get("slow_path_cache") or {}
    refire = m.get("false_close_refire") or {}
    edge = m.get("edge_cacheability") or {}

    # 1. SLOW_PATH_CACHE — fraction of flagged slow API paths now covered (cache
    # or under cap). No slow findings at all → perfect.
    tp = slow.get("total_paths") or 0
    cp = slow.get("covered_paths") or 0
    slow_score = round(cp / tp, 3) if tp else 1.0

    # 2. FALSE_CLOSE_REFIRE — fraction of resolved findings that STAYED resolved
    # (not chronic-recurring / seen-again = a close that didn't stick).
    rt = refire.get("resolved_total") or 0
    rf = max(refire.get("churny") or 0, refire.get("seen_again") or 0)
    refire_score = round(max(0.0, (rt - rf) / rt), 3) if rt else 1.0

    # 3. EDGE_CACHEABILITY — fraction of hot public endpoints fast-or-cacheable.
    au = edge.get("audited") or 0
    he = edge.get("healthy") or 0
    edge_score = round(he / au, 3) if au else 1.0

    scores = {
        "slow_path_cache": slow_score,
        "false_close_refire": refire_score,
        "edge_cacheability": edge_score,
    }
    ranked = sorted(scores.items(), key=lambda kv: kv[1])
    weakest = next((name for name, _ in ranked if not _lever_off(name)), ranked[0][0])
    return {"scores": scores, "weakest": weakest}


# ── TIER 3 — ACT (one bounded action on the weakest lane) ─────────────
def _file_finding(issue: str, url: str, detail: str) -> bool:
    try:
        from routes.brain_findings_writer import upsert_brain_finding
    except Exception:
        return False
    conn = _txn_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            upsert_brain_finding(cur, issue=issue, url=url, count=1,
                                 detail=detail, detector="frontend_reliability_master")
        conn.commit()
        return True
    except Exception:
        try: conn.rollback()
        except Exception: pass
        note_swallowed_write("brain_findings", where="frontend_reliability_master._file_finding")
        return False
    finally:
        try: conn.close()
        except Exception: pass


def tier3_act(m: dict, levers: dict) -> dict:
    if not _act_enabled():
        return {"action": "none", "reason": "SHADOW (set FRONTEND_RELIABILITY_MASTER_ARM=1 to file findings)"}
    lever = levers.get("weakest")
    if _lever_off(lever):
        return {"action": "none", "reason": f"lever '{lever}' killed"}
    scores = levers.get("scores") or {}
    if (scores.get(lever) or 0) >= 0.999:
        return {"action": "none", "reason": f"weakest lever '{lever}' already healthy"}

    if lever == "slow_path_cache":
        slow = m.get("slow_path_cache") or {}
        worst = next((p for p in (slow.get("slow_paths") or [])
                      if not p.get("covered") and not p.get("excluded")), None)
        if not worst:
            return {"action": "none", "reason": "no uncached slow path"}
        detail = (
            f"Public API path `{worst['api_path']}` (backs {', '.join(worst.get('pages') or [])}) "
            f"tripped the {int(_SLOW_CAP_S)}s frontend_endpoint_slow cap "
            f"(worst {worst.get('worst_took_s')}s, seen {worst.get('max_seen')}×) and STILL has no "
            f"origin cache — current probe {worst.get('probe_ms')}ms, X-Cache={worst.get('x_cache')}. "
            f"Fix: add a stale-while-revalidate origin cache (redis_cache cache_get/cache_set + "
            f"background single-flight refresh), the /api/pipeline pattern (routes/deals_routes.py "
            f"get_public_pipeline, commit 06a57042). A scaffold/doc 'fix' will re-fire — the scan must "
            f"never run on the hot request path."
        )
        filed = _file_finding("frontend_slow_path_uncached", worst["api_path"], detail)
        return {"action": "file_finding", "lever": lever, "issue": "frontend_slow_path_uncached",
                "target": worst["api_path"], "filed": filed}

    if lever == "false_close_refire":
        refire = m.get("false_close_refire") or {}
        top = refire.get("top") or []
        top_str = "; ".join(
            "{} @ {} (×{}, {}d span)".format(t.get("issue"), t.get("url"),
                                             t.get("seen_count"), t.get("span_days"))
            for t in top[:3]
        )
        bad = max(refire.get("churny") or 0, refire.get("seen_again") or 0)
        detail = (
            f"{bad}/{refire.get('resolved_total')} findings resolved in the last 30d did NOT stick — "
            f"chronic-recurring or seen again after resolution (the scaffold-PR / substance-gate "
            f"pathology: closed without a code change → re-fires). Top: {top_str}. "
            f"These need a REAL runtime fix, not a spec-doc close; make brain-pr-substance-gate a "
            f"required check so a scaffold PR can't auto-close them."
        )
        filed = _file_finding("finding_false_closed_refired",
                              "/api/v1/admin/frontend-reliability/master-tick", detail)
        return {"action": "file_finding", "lever": lever, "issue": "finding_false_closed_refired",
                "refired": refire.get("refired"), "filed": filed}

    if lever == "edge_cacheability":
        edge = m.get("edge_cacheability") or {}
        bad = [e for e in (edge.get("endpoints") or []) if not e.get("healthy")]
        if not bad:
            return {"action": "none", "reason": "no no-store+slow endpoint"}
        detail = (
            f"{len(bad)} hot public read endpoint(s) are BOTH no-store AND slow at the origin "
            f"(fast for nobody): {', '.join(e['path'] for e in bad[:4])}. Either add an origin SWR "
            f"cache (preferred — /api/* keeps its no-store policy; see the CF cache-leak history) or, "
            f"for genuinely public non-personalized data, exempt the path from the no-store "
            f"after_request so CF can edge-cache it."
        )
        filed = _file_finding("public_endpoint_no_store_and_slow",
                              bad[0]["path"], detail)
        return {"action": "file_finding", "lever": lever, "issue": "public_endpoint_no_store_and_slow",
                "count": len(bad), "filed": filed}

    return {"action": "none", "reason": f"unknown lever {lever}"}


# ── SCORE, PERSIST ────────────────────────────────────────────────────
def frontend_reliability_score(levers: dict) -> float:
    s = levers.get("scores") or {}
    return round(100.0 * (sum(s.values()) / len(s)), 2) if s else 0.0


def _persist(m: dict, levers: dict, score: float, action: dict) -> bool:
    if not _ensure_tables():
        return False
    c = _conn()
    if c is None:
        return False
    s = levers.get("scores") or {}
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO frontend_reliability_snapshots
                  (score, slow_path_cache, false_close_refire, edge_cacheability,
                   weakest_lever, action_taken, lever_scores, detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (
                score, s.get("slow_path_cache"), s.get("false_close_refire"),
                s.get("edge_cacheability"), levers.get("weakest"),
                (action or {}).get("action"),
                json.dumps(s),
                json.dumps({"measure": m, "levers": levers, "action": action}, default=str),
            ))
        return True
    except Exception:
        note_swallowed_write("frontend_reliability_snapshots",
                             where="frontend_reliability_master._persist")
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── ORCHESTRATOR ──────────────────────────────────────────────────────
@frontend_reliability_master_shell_bp.route(
    "/api/v1/admin/frontend-reliability/master-tick", methods=["POST", "GET"])
def master_tick():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="FRONTEND_RELIABILITY_MASTER_DISABLED"), 200
    started = time.time()
    measure = tier1_measure()
    levers = tier2_score_levers(measure)
    action = tier3_act(measure, levers)
    score = frontend_reliability_score(levers)
    persisted = _persist(measure, levers, score, action)

    s = levers.get("scores") or {}
    weak = levers.get("weakest")
    headline = (
        f"frontend-reliability {score}/100 · {measure.get('honest_read')} · "
        f"weakest → {weak} ({s.get(weak)}) · acted: {action.get('action')}"
    )
    return jsonify(
        ok=True,
        ms=int((time.time() - started) * 1000),
        frontend_reliability_score=score,
        headline=headline,
        tier1_measure=measure,
        tier2_levers=levers,
        tier3_action=action,
        persisted=persisted,
        mode=("armed" if _act_enabled() else "shadow"),
        generated_at=datetime.now(timezone.utc).isoformat(),
    ), 200


@frontend_reliability_master_shell_bp.route(
    "/api/v1/admin/frontend-reliability/state", methods=["GET"])
def state():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(error="db_unavailable"), 503
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM frontend_reliability_snapshots ORDER BY id DESC LIMIT 20")
            rows = cur.fetchall()
        return jsonify(
            ok=True,
            latest=(rows[0] if rows else None),
            history=rows,
            mode=("armed" if _act_enabled() else "shadow"),
        ), 200
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {str(e)[:160]}"), 500
    finally:
        try: c.close()
        except Exception: pass
