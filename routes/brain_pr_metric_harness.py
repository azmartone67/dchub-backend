"""
routes/brain_pr_metric_harness.py — merged-PR before/after metric harness
(brain-ascension #28 wave 2, 2026-07-25).

THE REAL IMPLEMENTATION of the brain's own diagnosis
(routes/_proposed_merged_pr_before_after_metric_harness.py, now deleted):

    "The brain merged 41 PRs in 30d with outcome 'unknown' on every one —
     before/after both null, success_rate 0.0 ... The brain is shipping
     blind... it cannot learn what works and keeps re-proposing variants of
     the same monetization ideas."

What it does, per daily tick:
  1. HARVEST — GitHub REST for recently-merged PRs whose head branch is
     brain-authored (brain/, brain-spec/, brain-l6/, brain-l22/ prefixes).
     Parses an optional `target_metric: <key>` line from the PR body (PR
     writers can adopt this; absent → fleet-level snapshot only).
  2. SNAPSHOT (phase=merge) — captures the canonical metric set at merge
     time, one row per (pr, phase, metric).
  3. RE-STAMP — for PRs whose merge snapshot is >=14d / >=30d old and whose
     d14/d30 phase is missing, captures the same metrics again. The delta
     merge→d14→d30 is the before/after the strategic ledger never had.

Canonical metric set — read from canonical_funnel.get_canonical_funnel()
(the KPI single source of truth), NEVER hand-rolled SQL, so the harness can
not drift from the honest numbers: active_dev_keys, paid_keys,
mrr_invoiced_usd, conversions_30d_real, tool_calls_7d_real.

HONESTY RULES:
  - A metric that cannot be read is stored as NULL, never 0 — absence of
    evidence is not a zero.
  - The harness ATTRIBUTES NOTHING: a delta is fleet-level context around a
    merge, not proof the PR caused it. The summary endpoint says so.
  - Snapshots are append-only; UNIQUE(pr_number, phase, metric_key) makes
    the tick idempotent.

Endpoints:
  GET/POST /api/v1/admin/brain/pr-metrics/tick   harvest + snapshot + re-stamp
  GET      /api/v1/admin/brain/pr-metrics        per-PR merge/d14/d30 deltas

Cron: cron_heartbeat `brain_pr_metrics_daily` (07:xx UTC).
Kill: BRAIN_PR_METRICS_DISABLE=1
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import urllib.request

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

brain_pr_metric_harness_bp = Blueprint("brain_pr_metric_harness", __name__)

_REPO = os.environ.get("GH_REPO", "azmartone67/dchub-backend")
_BRAIN_BRANCH_PREFIXES = ("brain/", "brain-spec/", "brain-l6/", "brain-l22/",
                          "brain-autofix-")
# canonical_funnel keys snapshotted per PR — the KPI SoT, never local SQL.
_METRIC_KEYS = ("active_dev_keys", "paid_keys", "mrr_invoiced_usd",
                "conversions_30d_real", "tool_calls_7d_real")
_PHASES = (("merge", 0), ("d14", 14), ("d30", 30))

_DDL = """CREATE TABLE IF NOT EXISTS brain_pr_metric_snapshots (
    id           BIGSERIAL PRIMARY KEY,
    pr_number    INT NOT NULL,
    pr_title     TEXT,
    head_branch  TEXT,
    merged_at    TIMESTAMPTZ,
    target_metric TEXT,
    phase        TEXT NOT NULL,
    metric_key   TEXT NOT NULL,
    metric_value DOUBLE PRECISION,
    captured_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (pr_number, phase, metric_key)
)"""


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("BRAIN_PR_METRICS_DISABLE") or "").strip() == "1"


def _conn():
    try:
        import psycopg2 as _pg
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:  # noqa: BLE001
        logger.warning("[pr-metrics] db connect failed: %s", e)
        return None


def _gh_json(path: str):
    """GitHub REST GET. None on any failure (callers treat None as
    'watcher blind', never as 'no PRs')."""
    tok = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
           or "").strip()
    if not tok:
        return None
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Authorization": "Bearer " + tok,
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "dchub-pr-metric-harness/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        logger.debug("[pr-metrics] gh %s failed: %s", path, e)
        return None


_TARGET_RE = re.compile(r"^\s*target_metric:\s*([a-z0-9_]+)\s*$",
                        re.I | re.M)


def _harvest_merged(days: int = 3) -> list[dict]:
    """Recently-merged brain-authored PRs. Small window — the daily tick
    only needs to catch what merged since the last run."""
    d = _gh_json(f"/repos/{_REPO}/pulls?state=closed&sort=updated"
                 f"&direction=desc&per_page=50")
    if not isinstance(d, list):
        return []
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=days))
    out = []
    for pr in d:
        merged = pr.get("merged_at")
        head = ((pr.get("head") or {}).get("ref") or "")
        if not merged or not any(head.startswith(p)
                                 for p in _BRAIN_BRANCH_PREFIXES):
            continue
        try:
            mdt = datetime.datetime.fromisoformat(merged.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            continue
        if mdt < cutoff:
            continue
        m = _TARGET_RE.search(pr.get("body") or "")
        out.append({"number": int(pr["number"]),
                    "title": (pr.get("title") or "")[:300],
                    "head": head[:120], "merged_at": merged,
                    "target_metric": (m.group(1).lower() if m else None)})
    return out


def _canonical_metrics() -> dict:
    """Current canonical KPI values. NULL (absent key) is stored as NULL —
    never fabricated as 0."""
    try:
        from canonical_funnel import get_canonical_funnel
        d = get_canonical_funnel() or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("[pr-metrics] canonical funnel read failed: %s", e)
        return {}
    out = {}
    for k in _METRIC_KEYS:
        v = d.get(k)
        try:
            out[k] = float(v) if v is not None else None
        except (TypeError, ValueError):
            out[k] = None
    return out


def _snapshot(cur, pr: dict, phase: str, metrics: dict) -> int:
    n = 0
    for k in _METRIC_KEYS:
        cur.execute(
            """INSERT INTO brain_pr_metric_snapshots
                 (pr_number, pr_title, head_branch, merged_at, target_metric,
                  phase, metric_key, metric_value)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (pr_number, phase, metric_key) DO NOTHING""",
            (pr["number"], pr.get("title"), pr.get("head"),
             pr.get("merged_at"), pr.get("target_metric"), phase, k,
             metrics.get(k)))
        n += cur.rowcount
    return n


def _run_tick() -> dict:
    c = _conn()
    if c is None:
        return {"ok": False, "error": "db unavailable"}
    out = {"ok": True, "harvested": 0, "merge_rows": 0, "d14_rows": 0,
           "d30_rows": 0, "gh_blind": False}
    try:
        with c.cursor() as cur:
            cur.execute(_DDL)
        merged = _harvest_merged()
        if not merged:
            # distinguish "no brain merges" from "GitHub unreadable"
            out["gh_blind"] = _gh_json(f"/repos/{_REPO}") is None
        metrics = _canonical_metrics()
        with c.cursor() as cur:
            out["harvested"] = len(merged)
            for pr in merged:
                out["merge_rows"] += _snapshot(cur, pr, "merge", metrics)
            # re-stamp: merge-phase PRs whose d14/d30 window opened
            for phase, age_d in _PHASES[1:]:
                cur.execute(
                    """SELECT DISTINCT pr_number, pr_title, head_branch,
                              merged_at, target_metric
                         FROM brain_pr_metric_snapshots
                        WHERE phase = 'merge'
                          AND merged_at <= NOW() - make_interval(days => %s)
                          AND pr_number NOT IN (
                              SELECT pr_number FROM brain_pr_metric_snapshots
                               WHERE phase = %s)
                        LIMIT 50""", (age_d, phase))
                due = [{"number": r[0], "title": r[1], "head": r[2],
                        "merged_at": r[3], "target_metric": r[4]}
                       for r in (cur.fetchall() or [])]
                for pr in due:
                    out[f"{phase}_rows"] += _snapshot(cur, pr, phase, metrics)
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


@brain_pr_metric_harness_bp.route("/api/v1/admin/brain/pr-metrics/tick",
                                  methods=["GET", "POST"])
def tick():
    if _disabled():
        return jsonify(ok=False, error="BRAIN_PR_METRICS_DISABLE=1"), 503
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    return jsonify(_run_tick())


@brain_pr_metric_harness_bp.route("/api/v1/admin/brain/pr-metrics",
                                  methods=["GET"])
def summary():
    if _disabled():
        return jsonify(ok=False, error="BRAIN_PR_METRICS_DISABLE=1"), 503
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="db unavailable"), 200
    try:
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('brain_pr_metric_snapshots')")
            if not (cur.fetchone() or [None])[0]:
                return jsonify(ok=True, prs=[], note="no snapshots yet "
                               "(first tick creates the table)"), 200
            cur.execute(
                """SELECT pr_number, MAX(pr_title), MAX(merged_at::text),
                          MAX(target_metric), phase, metric_key,
                          MAX(metric_value)
                     FROM brain_pr_metric_snapshots
                    GROUP BY pr_number, phase, metric_key
                    ORDER BY pr_number DESC LIMIT 600""")
            rows = cur.fetchall() or []
    finally:
        try:
            c.close()
        except Exception:
            pass
    prs: dict = {}
    for num, title, merged_at, target, phase, key, val in rows:
        p = prs.setdefault(num, {"pr": num, "title": title,
                                 "merged_at": merged_at,
                                 "target_metric": target, "phases": {}})
        p["phases"].setdefault(phase, {})[key] = val
    for p in prs.values():
        ph = p["phases"]
        deltas = {}
        for later in ("d14", "d30"):
            if "merge" in ph and later in ph:
                deltas[later] = {
                    k: (round(ph[later][k] - ph["merge"][k], 4)
                        if ph[later].get(k) is not None
                        and ph["merge"].get(k) is not None else None)
                    for k in _METRIC_KEYS}
        p["deltas"] = deltas
    return jsonify(ok=True, prs=sorted(prs.values(),
                                       key=lambda x: -x["pr"]),
                   note="deltas are FLEET-LEVEL context around each merge, "
                        "not per-PR attribution"), 200


def register_brain_pr_metric_harness(app):
    app.register_blueprint(brain_pr_metric_harness_bp)
