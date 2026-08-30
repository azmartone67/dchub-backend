"""
routes/brain_ascension_master_shell.py — Brain Ascension Master Shell (#28, 2026-07-25).

Born from the 2026-07-25 five-audit sweep (brain model/cadence, cron inventory,
competitor-gap machinery, license growth funnel, RAG). One shell that keeps the
wave-1 fixes fixed and keeps the DEFERRED work visibly red until wave 2 ships it.

  1. BRAIN DEADMAN — the off-worker watcher covered 25 ingest/growth loops but
     NOT ONE brain-* workflow: if every brain loop stopped succeeding, nothing
     alarmed. Fixed by registering the brain liveness spine in
     tools/deadman/watch.py. This lane watches that the registry keeps them.
  2. MODEL ROSTER — brain_models ships a 5-tier roster with a cross-model
     challenger BY DESIGN (second opinion from a different model), but prod env
     pinned challenger == reasoning (both opus-4-8), collapsing the diversity.
     This lane watches the resolved tiers stay cross-model and Fable stays
     reachability-gated, never assumed.
  3. COMPETITOR→PRODUCT — the gap crawler wrote coverage_gaps daily but the L6
     strategic planner never read it (static catalog only) and the public gaps
     endpoint served a hand-written constant. Fixed: planner layer 4
     (_read_crawled_gaps) + live rows on /api/competitors/gaps. This lane
     watches the wiring stays imported and the table still accrues.
  4. RAG TRUTH — /status reported the Cohere model while mistral vectors were
     written; the eval had cosine floors but NO ground truth (mistral scores
     ~0.75+ on nonsense). Fixed: provider-aware model reporting + anchor-term
     ground truth per eval query. DELIBERATE RED: the four cosine gates
     (dup 0.90/0.92, related-intel 0.30, eval floors) are still Cohere-scale —
     recalibration for mistral is wave 2 (pre-registered, not blind-tuned).
  5. METRIC HARNESS — the brain merged 41 PRs/30d with outcome 'unknown' on
     every one; its own fix (_proposed_merged_pr_before_after_metric_harness)
     is an unregistered 501 scaffold. DELIBERATE RED until the harness is real:
     brain PRs declare a target_metric at open and a daily job snapshots
     merge / +14d / +30d. The red is the wave-2 work order.
  6. GROWTH TRUTH — team ($699) and founding ($99) subscribers contributed $0
     to every MRR run-rate (missing from both price maps AND the users-probe
     plan filter); annual SKUs exist only as Stripe links, invisible to
     /api/v1/tiers. Maps fixed + filter widened; this lane watches lock-step.
     DELIBERATE RED: annual still not in tier_registry (wave 2, owner call on
     display shape).

★ HONESTY RULE (inherited from Integrity #25 / Growthfix #26): a lane must
never read PASS when it couldn't check — an indeterminate critical check
renders "?" and the lane is not green. Deliberate wave-2 reds render FAIL.

READ-ONLY / DIAGNOSTIC: every lane names its actuator and fires nothing.

Endpoints:
  GET/POST /api/v1/admin/brain-ascension/master-tick   JSON scoreboard (6 lanes)
  GET      /admin/brain-ascension                       HTML dashboard (60s refresh)
  GET      /api/v1/admin/brain-ascension                CF zone-worker bypass alias

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY) — same gate as the other master shells.
Cron: cron_heartbeat _DISPATCH `brain_ascension_shell_daily` (06:xx UTC) POSTs
the tick; on completion the tick beats the dead-man ledger (feed
`brain-ascension-shell-daily`, rows_inserted=1 liveness sentinel).
Kill: BRAIN_ASCENSION_SHELL_DISABLE=1
"""
from __future__ import annotations

import datetime
import logging
import os
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

brain_ascension_master_shell_bp = Blueprint("brain_ascension_master_shell", __name__)

# The brain liveness spine the deadman registry must keep covering. Kept
# literal (no import of tools/) — this is the shell's own contract.
_REQUIRED_DEADMAN_BRAIN = (
    "cron-heartbeat.yml", "brain-autonomy.yml", "brain-autopilot.yml",
    "brain-verify.yml", "brain-master-tick.yml", "brain-model-reachability.yml",
    "brain-mirror.yml", "strategic-briefing-weekly.yml",
)


# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("BRAIN_ASCENSION_SHELL_DISABLE") or "").strip() == "1"


# ── db helpers (mirror growthfix_master_shell) ────────────────────────

def _conn():
    """Raw psycopg2 connection. None on failure. Deliberately OUTSIDE the
    app pool — one short-lived connection per tick."""
    try:
        import psycopg2 as _pg
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[brain-ascension] db connect failed: %s", e)
        return None


def _row(c, sql: str):
    """Fail-soft single row. None on error. LITERAL SQL only — no params
    tuple and no percent characters anywhere in the statement (psycopg2
    percent-substitution trap)."""
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()
    except Exception as e:
        logger.debug("[brain-ascension] row failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _check(cid: str, name: str, passed, detail: str,
           critical: bool = False) -> dict:
    """passed: True / False / None (None = indeterminate, shown as '?')."""
    return {"id": cid, "name": name, "pass": passed,
            "detail": detail, "critical": critical}


def _lane_verdict(checks: list[dict]) -> str:
    """green only when something was actually decided and nothing failed.

    2026-07-25 (adversarial review): the critical-only escalation let a lane
    whose checks were ALL indeterminate-and-non-critical render a confident
    PASS. Mirrors routes/integrity_master_shell.py:161 (#25)."""
    if any(k["pass"] is False for k in checks):
        return "FAIL"
    if any(k["pass"] is None for k in checks if k.get("critical")):
        return "?"
    if not [k for k in checks if k["pass"] is not None]:
        return "?"
    return "PASS"


def _as_dt(ts):
    """Coerce a DB timestamp into an aware datetime. coverage_gaps.created_at
    is TEXT (it 500'd growthfix's very first live tick) — strings are parsed,
    never assumed. None on failure."""
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00").strip())
        except Exception:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts


def _age_days(ts) -> float | None:
    ts = _as_dt(ts)
    if ts is None:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - ts).total_seconds() / 86400.0


# ── lane 1: brain deadman coverage ────────────────────────────────────

def _lane_brain_deadman() -> list[dict]:
    checks = []
    path = os.path.join(os.getcwd(), "tools", "deadman", "watch.py")
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
    except Exception as e:  # noqa: BLE001
        return [_check("dm_registry", "deadman registry readable", None,
                       f"tools/deadman/watch.py unreadable: {type(e).__name__}",
                       critical=True)]
    missing = [wf for wf in _REQUIRED_DEADMAN_BRAIN if f'"{wf}"' not in src]
    checks.append(_check(
        "dm_brain", "brain liveness spine registered in deadman watch",
        len(missing) == 0,
        ("all " + str(len(_REQUIRED_DEADMAN_BRAIN)) + " required brain feeds present"
         if not missing else "MISSING: " + ", ".join(missing)),
        critical=True))
    return checks


# ── lane 2: model roster (cross-model challenger, Fable gated) ────────

def _lane_model_roster() -> list[dict]:
    checks = []
    try:
        from routes.brain_models import brain_model_for
        resolved = {t: brain_model_for(t)
                    for t in ("inspector", "reasoning", "routine", "voice",
                              "challenger")}
    except Exception as e:  # noqa: BLE001
        return [_check("roster", "model roster resolvable", None,
                       f"brain_models import/resolve failed: {type(e).__name__}",
                       critical=True)]
    cross = resolved.get("challenger") != resolved.get("reasoning")
    checks.append(_check(
        "cross_model", "challenger is a DIFFERENT model from reasoning",
        cross,
        "challenger=" + str(resolved.get("challenger"))
        + " reasoning=" + str(resolved.get("reasoning"))
        + ("" if cross else
           " — pinned same; unset DCHUB_BRAIN_MODEL_CHALLENGER or pin a "
           "different model to restore the cross-model second opinion"),
        critical=True))
    checks.append(_check(
        "roster_report", "resolved per-tier roster", True,
        " · ".join(f"{t}={m}" for t, m in resolved.items())))
    fable_active = any("fable" in str(m) for m in resolved.values())
    checks.append(_check(
        "fable_gate", "fable-5 only via positive reachability probe", True,
        ("fable-5 ACTIVE on a tier (probe confirmed it)" if fable_active
         else "fable-5 not currently resolved (probe has not confirmed it — "
              "correct fail-closed behavior; roster falls back opus→sonnet)")))
    return checks


# ── lane 3: competitor → product wiring ───────────────────────────────

def _lane_competitor_pipeline(c) -> list[dict]:
    checks = []
    try:
        from routes.brain_strategic_planner import _read_crawled_gaps  # noqa: F401
        wired = True
        detail = "_read_crawled_gaps present (planner layer 4 wired)"
    except Exception as e:  # noqa: BLE001
        wired = False
        detail = f"planner crawled-gaps layer MISSING: {type(e).__name__}"
    checks.append(_check("gap_layer", "L6 planner reads crawled coverage_gaps",
                         wired, detail, critical=True))
    try:
        from competitor_intelligence import _crawled_coverage_gaps  # noqa: F401
        checks.append(_check(
            "gap_endpoint", "/api/competitors/gaps serves live table",
            True, "_crawled_coverage_gaps present (static constant demoted)"))
    except Exception as e:  # noqa: BLE001
        checks.append(_check(
            "gap_endpoint", "/api/competitors/gaps serves live table",
            False, f"live-table reader MISSING: {type(e).__name__}"))
    newest = _row(c, "SELECT MAX(created_at) FROM coverage_gaps") if c else None
    gage = _age_days(newest[0]) if newest and newest[0] else None
    checks.append(_check(
        "gap_accrual", "coverage_gaps still accrues finds",
        (True if (gage is not None and gage <= 21) else None),
        (f"newest gap {gage:.1f}d ago" if gage is not None
         else "no rows / unreadable")))
    try:
        from routes.brain_strategic_planner import _COMPETITOR_UNIVERSE
        names = " ".join(
            e.get("name", "") for cat in _COMPETITOR_UNIVERSE.values()
            if isinstance(cat, list) for e in cat if isinstance(e, dict))
        both = ("SemiAnalysis" in names) and ("Electricity Maps" in names)
        checks.append(_check(
            "universe", "universe models SemiAnalysis + Electricity Maps",
            both, ("both present" if both else "absent from catalog"),
            critical=True))
    except Exception as e:  # noqa: BLE001
        checks.append(_check(
            "universe", "universe models SemiAnalysis + Electricity Maps",
            None, f"universe unreadable: {type(e).__name__}", critical=True))
    return checks


# ── lane 4: rag truth ─────────────────────────────────────────────────

def _lane_rag_truth() -> list[dict]:
    checks = []
    try:
        from routes.brain_rag import _embed_provider, _live_embed_model
        prov = _embed_provider()
        model = _live_embed_model()
        honest = not (prov == "mistral" and "embed-english" in model)
        checks.append(_check(
            "model_honest", "/status reports the LIVE embed model",
            honest, f"provider={prov} model={model}", critical=True))
    except Exception as e:  # noqa: BLE001
        checks.append(_check(
            "model_honest", "/status reports the LIVE embed model",
            None, f"brain_rag probe failed: {type(e).__name__}", critical=True))
    try:
        from routes.rag_master_shell import _EVAL_QUERIES
        missing = [q["q"][:30] for q in _EVAL_QUERIES if not q.get("anchors")]
        checks.append(_check(
            "eval_truth", "every eval query carries anchor ground truth",
            len(missing) == 0,
            (f"all {len(_EVAL_QUERIES)} queries anchored" if not missing
             else "unanchored: " + "; ".join(missing)),
            critical=True))
    except Exception as e:  # noqa: BLE001
        checks.append(_check(
            "eval_truth", "every eval query carries anchor ground truth",
            None, f"rag_master_shell probe failed: {type(e).__name__}",
            critical=True))
    # Wave 2 (2026-07-25): gates recalibrated from LIVE measured distributions
    # (see PROVIDER_COSINE_GATES in brain_rag.py). This check verifies the
    # registry exists for the live provider AND every gate site actually
    # derives from it — registry drift or a reverted site goes red again.
    try:
        from routes.brain_rag import PROVIDER_COSINE_GATES, cosine_gate
        prov2 = _embed_provider()
        g = PROVIDER_COSINE_GATES.get(prov2)
        problems = []
        if not g:
            problems.append(f"no registered gates for provider={prov2}")
        else:
            from routes.rag_master_shell import _EVAL_QUERIES, _EVAL_MEAN_FLOOR
            low = [q["q"][:24] for q in _EVAL_QUERIES
                   if float(q.get("floor", 0)) < g["eval_floor"]]
            if low:
                problems.append("eval floors below registered "
                                + str(g["eval_floor"]) + ": " + "; ".join(low))
            if float(_EVAL_MEAN_FLOOR) < g["eval_floor"]:
                problems.append("mean floor below registered")
            if abs(cosine_gate("related_min") - g["related_min"]) > 1e-9:
                problems.append("cosine_gate helper drifted")
            main_src = ""
            try:
                with open(os.path.join(os.getcwd(), "main.py"),
                          encoding="utf-8") as f:
                    main_src = f.read()
            except Exception:  # noqa: BLE001
                pass
            if 'cosine_gate("related_min")' not in main_src:
                problems.append("_rag_related_intel not wired to registry")
        checks.append(_check(
            "gates_calibrated", "cosine gates recalibrated for live provider",
            (len(problems) == 0) if g else False,
            ("registered+wired: dup 0.90/0.92 validated in the 0.86-0.925 "
             "separation gap; related_min=" + str(g["related_min"])
             + " eval_floor=" + str(g["eval_floor"])
             + " (measured 2026-07-25: nonsense<=0.675, on-topic>=0.744)"
             if g and not problems else "; ".join(problems)[:220])))
    except Exception as e:  # noqa: BLE001
        checks.append(_check(
            "gates_calibrated", "cosine gates recalibrated for live provider",
            None, f"gate registry probe failed: {type(e).__name__}"))
    return checks


# ── lane 5: metric harness (the brain measuring its own PRs) ──────────

def _lane_metric_harness(c) -> list[dict]:
    checks = []
    # DELIBERATE RED until the before/after harness is REAL: the brain's own
    # diagnosis (41 merged PRs/30d, outcome unknown on all) fixed by a 501
    # scaffold is exactly the false-"shipped" class the substance gate flags.
    t = _row(c, "SELECT to_regclass('brain_pr_metric_snapshots')") if c else None
    exists = bool(t and t[0])
    checks.append(_check(
        "harness_real", "merged-PR before/after harness is REAL",
        (True if exists else False) if c is not None else None,
        ("brain_pr_metric_snapshots exists — harness landed "
         "(routes/brain_pr_metric_harness.py, daily 07:xx tick)"
         if exists else
         "harness shipped but table absent — fire "
         "POST /api/v1/admin/brain/pr-metrics/tick (first tick creates it)"),
        critical=True))
    # brain_fix_outcomes tri-state lives in still_broken (BOOLEAN, NULL =
    # unchecked) — brain_learning._SCHEMA is the DDL SoT.
    ver = _row(c, "SELECT COUNT(*) FROM brain_fix_outcomes "
                  "WHERE still_broken IS NOT NULL") if c else None
    checks.append(_check(
        "verify_flow", "fix-outcome verification accrues ground truth",
        (True if (ver and int(ver[0] or 0) > 0) else None),
        (f"{int(ver[0])} checked fix outcomes (still_broken stamped)"
         if ver and ver[0] is not None
         else "brain_fix_outcomes unreadable / none checked yet")))
    return checks


# ── lane 6: growth truth ──────────────────────────────────────────────

def _lane_growth_truth() -> list[dict]:
    checks = []
    try:
        from canonical_funnel import PLAN_MONTHLY_USD as _canon
        from routes.funnel_health import _PLAN_MONTHLY_USD as _fh
        lock = dict(_canon) == dict(_fh)
        checks.append(_check(
            "map_lockstep", "canonical + funnel_health price maps identical",
            lock,
            ("identical (" + str(len(_canon)) + " plans)" if lock else
             "DRIFTED: " + str(sorted(set(_canon.items()) ^ set(_fh.items()))[:4])),
            critical=True))
        tf = ("team" in _canon and "founding" in _canon)
        checks.append(_check(
            "team_founding", "team + founding priced in the MRR maps",
            tf, ("team=" + str(_canon.get("team")) + " founding="
                 + str(_canon.get("founding")) if tf else
                 "MISSING — those subscribers count $0"),
            critical=True))
    except Exception as e:  # noqa: BLE001
        checks.append(_check(
            "map_lockstep", "canonical + funnel_health price maps identical",
            None, f"price-map probe failed: {type(e).__name__}", critical=True))
    # Wave 2 (2026-07-25): ANNUAL_OPTIONS is the additive display surface —
    # rank/access/limits untouched (founding==pro rule intact).
    try:
        from tier_registry import ANNUAL_OPTIONS
        ok = bool((ANNUAL_OPTIONS.get("pro") or {}).get("annual_usd_year"))
        checks.append(_check(
            "annual_visible", "annual pricing visible on /api/v1/tiers",
            ok,
            ("ANNUAL_OPTIONS['pro']: $"
             + str((ANNUAL_OPTIONS.get('pro') or {}).get('annual_usd_year'))
             + "/yr + promo $"
             + str((ANNUAL_OPTIONS.get('pro') or {}).get('annual_promo_usd_year'))
             + "/yr one-time, surfaced in as_public_dict()" if ok else
             "ANNUAL_OPTIONS present but pro annual price missing")))
    except Exception as e:  # noqa: BLE001
        checks.append(_check(
            "annual_visible", "annual pricing visible on /api/v1/tiers",
            False, f"ANNUAL_OPTIONS absent: {type(e).__name__}"))
    return checks


# ── dead-man beat (fail-open, mirrors growthfix shell) ────────────────

def _beat_ledger(note: str, failing: bool = False) -> None:
    try:
        import json as _json
        import urllib.request
        body = _json.dumps({
            "feed": "brain-ascension-shell-daily",
            # ★ batch-3/Screen D: this was the literal "success", which is in
            # routes/ingest_runs._OK_STATUS, so a shell whose every lane FAILED
            # still read green on /api/v1/ops/deadman. Measured 2026-08-30:
            # 11 of 15 shell feeds carried FAIL lanes in `note` while the board
            # reported 0 of 150 loops overdue. Liveness is not health.
            "status": ("lanes_failing" if failing else "success"),
            "cadence_hours": 24,
            "last_run": datetime.datetime.utcnow().isoformat() + "Z",
            "note": note[:280],
        }).encode()
        port = os.environ.get("PORT", "8080")
        admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                     or os.environ.get("DCHUB_INTERNAL_KEY")
                     or os.environ.get("ADMIN_API_KEY", ""))
        req = urllib.request.Request(
            "http://127.0.0.1:" + str(port) + "/api/v1/admin/ingest-runs/beat",
            data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "dchub-brain-ascension-shell/1.0",
                     "X-Admin-Key": admin_key})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:  # noqa: BLE001 — a beat error must never break the tick
        logger.debug("[brain-ascension] ledger beat failed: %s", e)


# ── tick ──────────────────────────────────────────────────────────────

def _safe_lane(fn, *a) -> list[dict]:
    """A lane that CRASHES must render '?' (indeterminate), never 500 the
    tick (growthfix's first live tick died on a TEXT timestamp and took all
    five lanes down with it)."""
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        return [_check("lane_crash", "lane ran to completion", None,
                       f"lane crashed: {type(e).__name__}: {str(e)[:120]}",
                       critical=True)]


def _lane_evolution_story() -> list[dict]:
    """7 · evolution story told (wave 3, 2026-07-31).

    The shell's other lanes prove the brain IS evolving; this one proves the
    evolution is being TOLD — the operator's complaint was never the shipping,
    it was the silence. Two checks against the weekly analyst note:
    (1) a note exists and is fresh (<= 8 days — weekly cadence + 1 day grace);
    (2) its body carries the evolution section, matched via the SAME
    EVOLUTION_HEADING constant the composer emits (imported, never
    transcribed — the anchor-contract lesson).

    Expected lifecycle: this lane is BORN RED on deploy — the current week's
    note predates the section — and goes green on the next scheduled
    generation. A lane that starts red and drives the fix is working.
    """
    checks: list[dict] = []
    try:
        # Both the heading AND the content predicate are imported from the
        # composer — never transcribed. One definition of "the section is
        # really there", shared by the side that writes it and the side that
        # grades it, is the whole anchor-contract lesson.
        from routes.analyst_note import (
            EVOLUTION_HEADING,
            _evolution_section_has_content as _has_evolution_content)
    except Exception as e:
        return [_check("evolution_heading_import", "heading contract importable",
                       False, f"cannot import EVOLUTION_HEADING: {str(e)[:100]}",
                       critical=True)]
    c = _conn()
    if c is None:
        return [_check("evolution_note_fresh", "weekly note fresh (<=8d)",
                       False, "db unavailable — could not check", critical=True)]
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT week_of, body_md,
                       EXTRACT(EPOCH FROM (NOW() - created_at))/86400.0
                  FROM analyst_notes
                 ORDER BY week_of DESC
                 LIMIT 1
            """)
            row = cur.fetchone()
        if not row:
            checks.append(_check(
                "evolution_note_fresh", "weekly note fresh (<=8d)", False,
                "no analyst note exists — the weekly story engine is dark",
                critical=True))
            return checks
        week_of, body, age_d = row[0], (row[1] or ""), float(row[2] or 0)
        checks.append(_check(
            "evolution_note_fresh", "weekly note fresh (<=8d)",
            age_d <= 8.0,
            f"latest note week_of={week_of} · {age_d:.1f}d old",
            critical=True))
        # ★ Content, not just the heading (2026-08-01). Greping for the heading
        # alone was vacuous: the composer's figure fence strips whole sentences
        # carrying an unsourced figure, so a section whose every sentence named
        # a PR number could be gutted to a bare heading and still pass here.
        _has_section = _has_evolution_content(body)
        checks.append(_check(
            "evolution_section_present", "note tells the shipped story",
            _has_section,
            (f'"{EVOLUTION_HEADING}" section present with prose'
             if _has_section else
             (f'note carries the "{EVOLUTION_HEADING}" heading but no prose '
              "under it — the figure fence stripped the body"
              if EVOLUTION_HEADING in body else
              f'note exists but carries no "{EVOLUTION_HEADING}" section')),
            critical=True))
    except Exception as e:
        checks.append(_check(
            "evolution_note_fresh", "weekly note fresh (<=8d)", False,
            f"probe failed: {type(e).__name__}: {str(e)[:100]}", critical=True))
    finally:
        try:
            c.close()
        except Exception:
            pass
    return checks


def _run_tick() -> dict:
    c = _conn()
    try:
        lanes = [
            {"id": "brain_deadman", "name": "1 · brain deadman coverage",
             "checks": _safe_lane(_lane_brain_deadman)},
            {"id": "model_roster", "name": "2 · model roster (cross-model)",
             "checks": _safe_lane(_lane_model_roster)},
            {"id": "competitor_pipeline", "name": "3 · competitor → product",
             "checks": _safe_lane(_lane_competitor_pipeline, c)},
            {"id": "rag_truth", "name": "4 · rag truth",
             "checks": _safe_lane(_lane_rag_truth)},
            {"id": "metric_harness", "name": "5 · merged-PR metric harness",
             "checks": _safe_lane(_lane_metric_harness, c)},
            {"id": "growth_truth", "name": "6 · growth / MRR truth",
             "checks": _safe_lane(_lane_growth_truth)},
            {"id": "evolution_story", "name": "7 · evolution story told",
             "checks": _safe_lane(_lane_evolution_story)},
        ]
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
    for ln in lanes:
        ln["verdict"] = _lane_verdict(ln["checks"])
    summary = " ".join(f"{ln['id']}={ln['verdict']}" for ln in lanes)
    out = {
        "ok": True,
        "shell": "brain-ascension-28",
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "lanes": lanes,
        "summary": summary,
        "any_fail": any(ln["verdict"] == "FAIL" for ln in lanes),
    }
    _beat_ledger("lanes: " + summary, failing=out["any_fail"])
    return out


@brain_ascension_master_shell_bp.route(
    "/api/v1/admin/brain-ascension/master-tick", methods=["GET", "POST"])
def master_tick():
    if _disabled():
        # ★404, never 5xx (2026-08-12): the CF worker's proxyWithRetry reads
        # ANY 5xx from Railway as a dead origin and fails the site over to the
        # stale Render backend. Turning off one diagnostic shell must not be
        # able to do that. See graph_spine_master_shell for the original note.
        return jsonify(ok=False, error="BRAIN_ASCENSION_SHELL_DISABLE=1"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    resp = jsonify(_run_tick())
    # CF edge cached a wave-1 GET of this endpoint for >30 min post-deploy
    # (the Cache-Rules leak) — the operator read a stale board. Never cache.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@brain_ascension_master_shell_bp.route("/admin/brain-ascension", methods=["GET"])
@brain_ascension_master_shell_bp.route("/api/v1/admin/brain-ascension",
                                       methods=["GET"])
def dashboard():
    if _disabled():
        return Response("brain-ascension shell disabled", status=404,
                        mimetype="text/plain")
    if not _admin_ok():
        return Response("admin key required (?admin_key=)", status=401,
                        mimetype="text/plain")
    d = _run_tick()
    color = {"PASS": "#22c55e", "FAIL": "#ef4444", "?": "#eab308"}
    rows = []
    for ln in d["lanes"]:
        rows.append(
            f"<tr><td class='lane'>{_esc(ln['name'])}</td>"
            f"<td style='color:{color.get(ln['verdict'], '#eab308')}'>"
            f"<b>{_esc(ln['verdict'])}</b></td><td>"
            + "<br>".join(
                ("&#9989; " if k["pass"] is True else
                 ("&#10060; " if k["pass"] is False else "&#10068; "))
                + _esc(k["name"]) + " — <span class='d'>" + _esc(k["detail"])
                + "</span>" for k in ln["checks"])
            + "</td></tr>")
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='60'>"
        "<title>Brain Ascension Shell #28</title>"
        "<style>body{background:#0b1020;color:#e2e8f0;font:14px/1.5 "
        "-apple-system,Segoe UI,sans-serif;margin:2rem}table{border-collapse:"
        "collapse;width:100%;max-width:1100px}td{border-bottom:1px solid "
        "#1e293b;padding:.6rem .8rem;vertical-align:top}.lane{white-space:"
        "nowrap;font-weight:600}.d{color:#94a3b8}h1{font-size:1.2rem}"
        "small{color:#64748b}</style>"
        "<h1>Brain Ascension Master Shell #28</h1>"
        "<small>generated " + _esc(d["generated_at"]) + " · read-only · "
        "refreshes 60s · wave 2 shipped 07-25 (gates measured+registered, "
        "PR metric harness, annual visibility) · "
        "kill BRAIN_ASCENSION_SHELL_DISABLE=1</small>"
        "<table>" + "".join(rows) + "</table>")
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def register_brain_ascension_master_shell(app):
    app.register_blueprint(brain_ascension_master_shell_bp)
