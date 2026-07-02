"""Brain Fast-QA loop — the cheap, frequent half of QA.

WHY THIS EXISTS (2026-06-16): the brain's QA all rode the single ~6h
consistency-radar / Inspector cadence, so site breakage (a 404, a frozen
data feed) could sit unnoticed for up to 6 hours. This module is the
*cheap, fast* complement: a small set of read-only checks that run every
~30 min so breakage is caught in ~1h instead of ~6h, WITHOUT the cost of
the heavy Opus analysis. The heavy radar stays exactly as-is.

Two check classes, both read-only:

  1. URL health — a curated set of high-value public URLs (homepage,
     /pricing, /mcp, top market + dcpi pages, key JSON endpoints) fetched
     end-to-end against https://dchub.cloud. Anything >=400 (or a
     connection error) is a `fast_qa_url_down` finding. Redirects (3xx)
     and 2xx are healthy.

  2. Freshness drift — calls the existing /api/v1/freshness/radar
     (own origin, GET-only) and flags any surface whose age exceeds its
     own declared SLA as `fast_qa_freshness_drift`.

Findings are written through the canonical upsert_brain_finding and
RESOLVED-on-absence (a URL that recovers, a feed that refreshes, clears
its finding automatically). So the action-queue stays honest.

Calibration feed (the honest half of "warm the calibration gate"): for
each problem found, we record ONE genuinely-falsifiable resolution
prediction in brain_predictions_log — "this problem will be resolved
within 24h", confidence 0.5. The existing L16 verify loop scores it
true/false over the next day. Because the autopilot only really fixes a
minority of things, most of these will score FALSE — which is exactly the
point: it gives the brain real, un-gamed data about its own fix rate
instead of the inflated self-report. No fabrication.

Endpoints:
  POST /api/v1/brain/qa/fast-sweep        — admin: run the sweep + persist
  GET  /api/v1/brain/qa/fast-sweep/status — public: last run summary
"""
import datetime
import json
import logging
import os

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

brain_fast_qa_bp = Blueprint("brain_fast_qa", __name__)

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()

# Curated high-value public URLs. Kept SMALL (≤12) and tight-timeout so a
# single sweep holds one worker for <~20s on the 1-replica backend — a cron
# every 30 min is not "hammering". Each is a surface whose breakage would be
# customer- or revenue-visible. Mix of HTML pages + JSON endpoints + the two
# CF-worker-served paths (/mcp, /.well-known/mcp.json) so the sweep exercises
# the full edge→origin chain, not just Flask.
_PUBLIC_URLS = [
    "/",
    "/pricing",
    "/mcp",
    "/.well-known/mcp.json",
    "/enterprise",
    "/markets/northern-virginia",
    "/dcpi/ashburn",
    "/api/v1/stats",
    "/api/v1/mcp/funnel",
    "/sitemap.xml",
    "/daily",
    "/api/v1/brain/action-queue",
]

_BASE = "https://dchub.cloud"
_PER_URL_TIMEOUT = 4  # seconds; total worst-case ~ len(_PUBLIC_URLS) * timeout


# ── persistence of last-run summary (process-local, cheap) ──────────────
_LAST_RUN: dict = {"ran_at": None, "summary": None}


def _admin_ok() -> bool:
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    return bool(_ADMIN_KEY) and provided == _ADMIN_KEY


# ── check 1: URL health ─────────────────────────────────────────────────

def _check_urls() -> tuple[list[dict], list[dict]]:
    """Fetch each curated URL end-to-end. Return (problems, rate_limited):
    problems = [{path, status, detail}] for any non-healthy URL (2xx/3xx =
    healthy); rate_limited = HTTP 429 responses, which are self-inflicted
    throttling, NOT outages, and never become findings."""
    problems: list[dict] = []
    rate_limited: list[dict] = []
    try:
        import requests
    except Exception as e:  # pragma: no cover
        logger.warning("fast_qa: requests import failed: %s", e)
        return problems, rate_limited
    sess = requests.Session()
    # 2026-07-01: identify as an internal probe on BOTH rails the rate
    # limiter recognises (rate_limiter.rate_limit_before): the dchub.cloud
    # Origin exemption and the X-DC-Probe marker whitelist ('brain-radar'
    # is an accepted value — same mechanism site_qa.py uses with
    # 'self-heal'). Without these, the sweep 429'd itself through the edge
    # and filed its own throttling as HIGH fast_qa_url_down outages
    # (/sitemap.xml, /enterprise, /dcpi/ashburn, /api/v1/brain/action-queue
    # sat in the action-queue for 2 weeks as false HTTP 429 findings).
    sess.headers.update({
        "User-Agent": "dchub-brain-fastqa/1.0",
        "Origin": _BASE,
        "X-DC-Probe": "brain-radar",
    })
    for path in _PUBLIC_URLS:
        url = _BASE + path
        try:
            r = sess.get(url, timeout=_PER_URL_TIMEOUT, allow_redirects=True)
            status = r.status_code
            if status == 429:
                # Defense in depth: even if the exemption headers stop
                # matching some day, a 429 is the platform rate-limiting
                # its own probe — record it as 'rate_limited' (visible in
                # the run summary) but NEVER file it as an outage finding.
                rate_limited.append({
                    "path": path,
                    "status": status,
                    "detail": f"[INFO] HTTP 429 on {path} — probe "
                              f"rate-limited, not an outage",
                })
                continue
            if status >= 400:
                sev = "CRIT" if path in ("/", "/pricing", "/mcp") else "HIGH"
                problems.append({
                    "path": path,
                    "status": status,
                    "detail": f"[{sev}] HTTP {status} on {path} (end-to-end)",
                })
        except Exception as e:
            problems.append({
                "path": path,
                "status": "ERR",
                "detail": f"[HIGH] fetch error on {path}: {str(e)[:120]}",
            })
    return problems, rate_limited


# ── check 2: freshness drift ────────────────────────────────────────────

def _internal(path: str, timeout: int = 8) -> dict:
    try:
        import requests
        r = requests.get(f"http://localhost:8080{path}", timeout=timeout)
        if r.status_code != 200:
            return {}
        return r.json() or {}
    except Exception:
        return {}


def _iter_freshness_items(radar: dict):
    """Defensive: the freshness radar response has changed shape over time.
    Yield (name, age_hours, sla_hours, breached) tuples from whatever
    list-of-surfaces / domains structure is present."""
    if not isinstance(radar, dict):
        return
    # collect candidate item lists from common keys
    buckets = []
    for key in ("surfaces", "items", "breaches", "sources", "domains"):
        v = radar.get(key)
        if isinstance(v, list):
            buckets.append(v)
        elif isinstance(v, dict):
            # domain -> {surfaces:[...]} or domain -> item
            for dv in v.values():
                if isinstance(dv, list):
                    buckets.append(dv)
                elif isinstance(dv, dict):
                    buckets.append([dv])
    _healthy = {"fresh", "ok", "healthy", "green", "live", "good", "pass"}
    for bucket in buckets:
        for it in bucket:
            if not isinstance(it, dict):
                continue
            # live shape (verified 2026-06-16): items are keyed `domain` +
            # `source_table` + `status` ('fresh' when healthy). Keep the older
            # surface/name fallbacks for robustness across shape changes.
            name = (it.get("domain") or it.get("source_table")
                    or it.get("surface") or it.get("name") or it.get("source")
                    or it.get("table") or "")
            age = it.get("age_hours")
            sla = (it.get("sla_hours") or it.get("sla")
                   or it.get("sla_target_hours"))
            status = (it.get("status") or "").strip().lower()
            breached = it.get("breached")
            if breached is None and status:
                # an explicit non-healthy status is the strongest signal
                breached = status not in _healthy
            if breached is None and age is not None and sla:
                try:
                    breached = float(age) > float(sla)
                except Exception:
                    breached = None
            if name and breached:
                yield name, age, sla, True


def _check_freshness() -> list[dict]:
    """Flag surfaces past their own SLA. Read-only, own-origin."""
    problems: list[dict] = []
    radar = _internal("/api/v1/freshness/radar")
    if not radar:
        return problems
    for name, age, sla, _ in _iter_freshness_items(radar):
        problems.append({
            "surface": name,
            "detail": f"[MED] {name} freshness age={age}h exceeds SLA={sla}h",
        })
    return problems


# ── calibration feed: honest, falsifiable resolution predictions ────────

def _predictions_columns(cur) -> set:
    """Introspect brain_predictions_log live columns (the repo DDL has lied
    before — see brain_findings trap). Insert only the intersection."""
    try:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='brain_predictions_log'")
        return {r[0] for r in cur.fetchall()}
    except Exception:
        return set()


def _record_resolution_predictions(cur, problem_issues: list[str]) -> int:
    """For each currently-open QA problem, record ONE falsifiable prediction
    that it will be resolved within 24h (confidence 0.5 — genuinely
    uncertain). Deduped by chain_title within 3 days so we don't swamp the
    L16 verify budget. Returns count recorded. Caller commits."""
    if not problem_issues:
        return 0
    cols = _predictions_columns(cur)
    if not {"chain_title", "prediction"} <= cols:
        return 0
    recorded = 0
    for issue in problem_issues[:5]:  # cap per run
        chain_title = f"qa-resolve:{issue}"
        try:
            cur.execute(
                "SELECT 1 FROM brain_predictions_log "
                "WHERE chain_title = %s "
                "AND predicted_at > NOW() - INTERVAL '3 days' LIMIT 1",
                (chain_title,),
            )
            if cur.fetchone():
                continue
            row = {
                "source_layer": "QA",
                "chain_title": chain_title,
                "confidence": 0.5,
                "verification_criterion": (
                    f"GET /api/v1/brain/action-queue — the finding "
                    f"'{issue}' should be absent (resolved) within 24h"),
                "prediction": (
                    f"The fast-QA problem {issue} will be resolved within "
                    f"24h by the brain/operator."),
            }
            use = {c: v for c, v in row.items() if c in cols}
            colnames = list(use)
            collist = ", ".join(colnames)
            placeholders = ", ".join(["%s"] * len(colnames))
            # ON CONFLICT DO NOTHING is idempotent (the SELECT-dedup above is the
            # primary guard); kept in one quote-free literal so the regression
            # lint sees the clause in the same statement.
            sql = f"INSERT INTO brain_predictions_log ({collist}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"  # noqa: E501
            cur.execute(sql, [use[c] for c in colnames])
            recorded += 1
        except Exception as e:
            logger.warning("fast_qa: prediction insert failed for %s: %s",
                           issue, e)
    return recorded


# ── the sweep ───────────────────────────────────────────────────────────

def run_fast_sweep() -> dict:
    """Run both checks, persist findings (upsert + resolve-on-absence),
    feed calibration. Returns a summary dict. Safe to call repeatedly."""
    url_problems, url_rate_limited = _check_urls()
    fresh_problems = _check_freshness()

    # Build the canonical (issue -> detail) map for this run.
    current: dict[str, dict] = {}
    for p in url_problems:
        issue = f"fast_qa_url_down:{p['path']}"
        current[issue] = {
            "url": _BASE + p["path"],
            "detail": p["detail"],
        }
    for p in fresh_problems:
        issue = f"fast_qa_freshness_drift:{p['surface']}"
        current[issue] = {
            "url": f"dchub://freshness/{p['surface']}",
            "detail": p["detail"],
        }

    persisted = 0
    resolved = 0
    predictions = 0
    persist_error = None
    try:
        from main import get_db
        from routes.brain_findings_writer import upsert_brain_finding
        conn = get_db()
        if conn is None:
            raise RuntimeError("no db")
        try:
            cur = conn.cursor()
            # 1. upsert current problems
            for issue, meta in current.items():
                try:
                    upsert_brain_finding(
                        cur, issue, url=meta["url"], count=1,
                        detail=meta["detail"], detector="fast_qa",
                        status="open")
                    persisted += 1
                except Exception as e:
                    logger.warning("fast_qa: upsert failed for %s: %s", issue, e)
            # 2. resolve-on-absence: any open fast_qa finding not failing now
            try:
                cur.execute(
                    "SELECT issue FROM brain_findings "
                    "WHERE detector = 'fast_qa' AND status = 'open'")
                open_issues = {r[0] for r in cur.fetchall()}
            except Exception:
                open_issues = set()
            for stale in (open_issues - set(current)):
                try:
                    upsert_brain_finding(
                        cur, stale, status="resolved",
                        detail="[RESOLVED] fast-QA: recovered on a later sweep",
                        detector="fast_qa")
                    resolved += 1
                except Exception as e:
                    logger.warning("fast_qa: resolve failed for %s: %s",
                                   stale, e)
            # 3. calibration feed (honest falsifiable predictions)
            try:
                predictions = _record_resolution_predictions(
                    cur, list(current.keys()))
            except Exception as e:
                logger.warning("fast_qa: prediction feed failed: %s", e)
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        persist_error = str(e)[:200]
        logger.warning("fast_qa: persist failed: %s", e)

    summary = {
        "ran_at": datetime.datetime.utcnow().isoformat() + "Z",
        "urls_checked": len(_PUBLIC_URLS),
        "url_problems": url_problems,
        "url_rate_limited": url_rate_limited,
        "freshness_problems": fresh_problems,
        "problems_total": len(current),
        "findings_upserted": persisted,
        "findings_resolved": resolved,
        "predictions_recorded": predictions,
        "persist_error": persist_error,
        "healthy": len(current) == 0,
    }
    _LAST_RUN["ran_at"] = summary["ran_at"]
    _LAST_RUN["summary"] = summary
    return summary


# ── endpoints ───────────────────────────────────────────────────────────

@brain_fast_qa_bp.route("/api/v1/brain/qa/fast-sweep", methods=["POST"])
def fast_sweep():
    """Admin: run the fast-QA sweep now and persist findings. Fail-closed."""
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin key required"}), 403
    try:
        summary = run_fast_sweep()
        return jsonify({"ok": True, **summary}), 200, {
            "Cache-Control": "no-store, max-age=0",
            "X-DC-Hub-Surface": "brain-fast-qa",
        }
    except Exception as e:
        logger.exception("fast_qa: sweep crashed")
        return jsonify({"ok": False, "error": str(e)[:300]}), 500


@brain_fast_qa_bp.route("/api/v1/brain/qa/fast-sweep/status", methods=["GET"])
def fast_sweep_status():
    """Public read-only: the last fast-QA run summary (no secrets)."""
    return jsonify({
        "ok": True,
        "last_run": _LAST_RUN.get("summary"),
        "note": ("Fast-QA runs every ~30 min via .github/workflows/"
                 "brain-fast-qa.yml. POST /api/v1/brain/qa/fast-sweep "
                 "(admin) to run now."),
    }), 200, {"Cache-Control": "no-store, max-age=0"}
