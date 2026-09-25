"""
routes/brain_self_director.py — Brain SELF-DIRECTOR (2026-06-19). PROPOSE-ONLY.

THE SELF-DIRECTING RUNG. Everything below this rung had to be POKED:
  · the investigator answers a question a HUMAN poses,
  · the enhancer runs when a HUMAN (or a cron) hits POST /enhance,
  · the automerge loop only ever fixes a bug a detector already FILED.

This module makes the brain direct its OWN ATTENTION. On its own schedule,
UNPROMPTED, a tick:
  1. ASSESSES what is most worth thinking about right now — by REUSING the
     vetted brain_work_selector.build_work_plan() AND
     brain_enhancer.scan_opportunities(), then choosing the SINGLE
     highest-leverage candidate, DEDUPED against agenda items surfaced in the
     last ~3 days (so it does not re-investigate the same thing every tick)
     AND against a STATEFUL condition fingerprint (2026-07-16, shared with the
     enhancer via routes/brain_proposal_dedup.py): a condition with an open or
     cooling-down (BRAIN_PROPOSAL_REDRAFT_DAYS, default 7) same-fingerprint
     agenda row is not re-picked, and after the cooldown only re-fires when
     its measured figures moved materially. Kill: BRAIN_PROPOSAL_DEDUP=0.
  2. INVESTIGATES it — by REUSING the verified
     brain_investigator.investigate() (decompose -> gather REAL evidence ->
     reason -> adversarially REFUTE -> synthesize). This is the ONLY model work
     a tick does: at most ONE investigation per tick.
  3. SURFACES the verified, refuted recommendation to a human-facing AGENDA
     (stored, gradable). The human reads it and decides.

THE HARD SAFETY LINE — this rung gains NO new authority to ACT. It is strictly
PROPOSE-ONLY and strictly WEAKER than the existing automerge loop (which is the
ONLY autonomous ACTOR, and only on allowlisted MECHANICAL fixes):
  · It NEVER merges, sends, opens/applies a code PR, triggers the automerge,
    or writes ANYTHING to prod beyond STORING one agenda row.
  · Self-directing = self-directed ANALYSIS, NOT self-directed ACTION.
  · r-escalation-ladder (2026-07-18) — the ONE narrow, evidence-gated
    exception: an eval_finding item whose evidence clears EVERY gate of the
    escalation ladder (docs/brain-pr-escalation-gates.md, implemented in
    routes/brain_fix_gates.evaluate_escalation — stability, verdict-diff,
    deterministic-evidence, contract-locality, zero hard blocks, confidence
    >= 0.85) may additionally file a DOC-ONLY DRAFT spec PR via
    brain_pr_opener.open_spec_pr (SPEC-ONLY marker, zero code execution,
    inherits the can_open_pr kill switch + daily cap). A HUMAN STILL MERGES —
    the ladder gates PR CREATION only. Anything below the bar stays
    agenda-only, with the gate verdict stored in the agenda row's result_json
    so the "why not" is auditable. Kill: BRAIN_ESCALATION_LADDER=0.

COST SAFETY — a self-running loop that calls an LLM must NOT cost-explode, so
the caps are enforced SERVER-SIDE in the tick handler (never trusting the cron
schedule):
  · at most 1 investigation per tick,
  · a daily cap BRAIN_SELF_DIRECT_DAILY_CAP (default 4) — counted from the
    stored agenda rows (created_at::date = today),
  · BRAIN_SELF_DIRECT_ENABLED (default OFF) — ships dark; a tick is a NO-OP
    that makes ZERO model calls when off,
  · degrades gracefully with NO ANTHROPIC_API_KEY (no-op, never crashes).

Every LLM/DB touch is wrapped in try/except; self_direct_tick NEVER raises.
Admin-gated endpoints (reuse the investigator's _admin_ok pattern).

The GUARD ORDER in self_direct_tick() is load-bearing (cost-first):
  flag off    -> {ran:false, skipped:disabled}     (NO model call)
  no api key  -> {ran:false, skipped:no_api_key}    (NO model call)
  daily cap   -> {ran:false, skipped:daily_cap}     (NO model call)
  no candidate-> {ran:false, skipped:no_candidate}  (NO model call)
  else        -> investigate() ONCE, STORE one agenda row, {ran:true, agenda_id}

Endpoints (blueprint brain_self_director_bp, admin-gated):
  POST /api/v1/brain/self-direct/tick  -> runs self_direct_tick (what the cron
                                          hits; returns the skip reason when
                                          capped/dark/no-key so cron logs are
                                          legible). Flag/cap gated SERVER-SIDE.
  GET  /api/v1/brain/agenda            -> recent self-chosen verified agenda,
                                          highest-leverage first.
  POST /api/v1/brain/agenda/<id>/grade {grade} -> human grade for CALIBRATION.
"""
from __future__ import annotations

import json
import logging
import os
import re
from util.json_column import json_for_column
from typing import Optional

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

brain_self_director_bp = Blueprint("brain_self_director", __name__)


# ── env / flags ──────────────────────────────────────────────────────
def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _enabled() -> bool:
    """ARMED 2026-07-03 (owner greenlight — "brain acts autonomously, shadow-graded
    first"). Now ON by default, but this loop is still PROPOSE-ONLY: its only write
    is an agenda row a human reviews — it NEVER merges or acts externally — and cost
    is bounded by BRAIN_SELF_DIRECT_DAILY_CAP (default 4 investigations/day). Kill:
    BRAIN_SELF_DIRECT_DISABLED=1 (or BRAIN_SELF_DIRECT_ENABLED=0) makes a tick a
    NO-OP with ZERO model calls. An explicit BRAIN_SELF_DIRECT_ENABLED is honored
    either way, so an operator retains full control."""
    if _truthy(os.environ.get("BRAIN_SELF_DIRECT_DISABLED")):
        return False
    _explicit = os.environ.get("BRAIN_SELF_DIRECT_ENABLED")
    if _explicit is not None and str(_explicit).strip() != "":
        return _truthy(_explicit)
    return True


def _daily_cap() -> int:
    """Server-side daily cap on investigations the self-director kicks off — the
    cost ceiling. Default 4. Enforced in the tick handler, NOT trusted to the
    cron schedule."""
    try:
        return max(0, int(os.environ.get("BRAIN_SELF_DIRECT_DAILY_CAP", "4")))
    except Exception:
        return 4


# How far back to look when deduping the chosen agenda item (days). Keeps the
# loop from re-investigating the same thing every tick.
def _dedup_window_days() -> int:
    try:
        return max(0, int(os.environ.get("BRAIN_SELF_DIRECT_DEDUP_DAYS", "3")))
    except Exception:
        return 3


def _has_api_key() -> bool:
    """True when an Anthropic key is configured. Read the investigator's LIVE
    module attribute first (tests monkeypatch it there, and it's the same key
    the investigation will actually use) and fall back to the raw env so
    degrade-gracefully works either way."""
    try:
        from routes import brain_investigator as _bi
        if (getattr(_bi, "ANTHROPIC_API_KEY", "") or "").strip():
            return True
    except Exception:
        pass
    return bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())


# ── Admin gate (reuse the investigator/classifier's, per the brief) ──
def _admin_ok() -> bool:
    """Reuse brain_mechanical_classifier._admin_ok so the gate stays in one
    place (the same gate every brain admin endpoint accepts). Falls back to an
    inline internal-key check if that import fails so the endpoints are never
    accidentally left open."""
    try:
        from routes.brain_mechanical_classifier import _admin_ok as _mech_admin_ok
        return bool(_mech_admin_ok())
    except Exception:
        keys = set()
        for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
            v = os.environ.get(_n)
            if v:
                keys.add(v)
        sent = (request.headers.get("X-Internal-Key")
                or request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
        return bool(sent) and sent in keys


# ── DB (direct psycopg2, mirror brain_investigator) ──────────────────
def _conn():
    """Raw psycopg2 connection. Mirrors brain_investigator._conn — the
    _iso_common contextmanager crashes on .cursor()."""
    try:
        import psycopg2 as _pg
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if dsn:
            return _pg.connect(dsn, sslmode="require", connect_timeout=6)
    except Exception as e:
        logger.warning("brain_self_director: _conn failed: %s", e)
    return None


def init_self_director_schema() -> None:
    """Bootstrap brain_self_agenda via DIRECT psycopg2 (safe_db SKIPs DDL under
    SKIP_DDL=1). Idempotent; never raises."""
    conn = _conn()
    if conn is None:
        logger.warning("brain_self_director: no DB; skipping schema init")
        return
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS brain_self_agenda (
                        id          BIGSERIAL PRIMARY KEY,
                        kind        TEXT,
                        title       TEXT NOT NULL,
                        question    TEXT NOT NULL,
                        area        TEXT,
                        result_json JSONB,
                        confidence  DOUBLE PRECISION,
                        status      TEXT NOT NULL DEFAULT 'surfaced',
                        grade       TEXT,
                        fingerprint TEXT,
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
            try:
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS ix_brain_self_agenda_created "
                    "ON brain_self_agenda (created_at DESC)"
                )
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
            # STATEFUL DEDUP (2026-07-16): nullable condition fingerprint (see
            # routes/brain_proposal_dedup.py) so the same condition can't be
            # re-picked every ~4 days forever once the 3-day _norm_sig window
            # rolls past it (14 near-identical data_coverage agenda rows).
            try:
                cur.execute(
                    "ALTER TABLE brain_self_agenda "
                    "ADD COLUMN IF NOT EXISTS fingerprint TEXT"
                )
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
            try:
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS ix_brain_self_agenda_fp "
                    "ON brain_self_agenda (fingerprint, created_at DESC)"
                )
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
        logger.info("brain_self_director: schema ready")
    finally:
        try: conn.close()
        except Exception: pass


# ════════════════════════════════════════════════════════════════════
#  Step 1: ASSESS — pick the single highest-leverage agenda item
# ════════════════════════════════════════════════════════════════════
def _recent_titles(days: int) -> set:
    """Titles of agenda items surfaced in the last `days` days — the dedup set so
    the loop doesn't re-investigate the same thing every tick. [] / empty set on
    any error (fail-OPEN to picking: a DB hiccup must not block the loop, the
    daily cap still bounds cost)."""
    out: set = set()
    conn = _conn()
    if conn is None:
        return out
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT title, question FROM brain_self_agenda "
                    "WHERE created_at >= NOW() - (%s || ' days')::interval",
                    (str(int(days)),),
                )
                for r in (cur.fetchall() or []):
                    for v in (r[0], r[1]):
                        if v:
                            out.add(str(v).strip().lower())
            except Exception:
                try: conn.rollback()
                except Exception: pass
    except Exception:
        pass
    finally:
        try: conn.close()
        except Exception: pass
    return out


def _work_plan_candidates() -> list[dict]:
    """Candidate agenda items from the brain's own ranked WORK-PLAN — the top
    open mechanical work, framed as a SYSTEMIC question (not "fix this one
    instance"). REUSE-ONLY: reads build_work_plan(); never raises."""
    out: list[dict] = []
    try:
        from routes.brain_work_selector import build_work_plan
        plan = build_work_plan(limit=10) or {}
    except Exception as e:
        logger.warning("brain_self_director: work-plan scan failed: %s", e)
        return out
    ranked = plan.get("ranked") or []
    for r in ranked:
        if not isinstance(r, dict):
            continue
        klass = r.get("class") or "(unknown)"
        lev = r.get("leverage")
        try:
            lev_f = float(lev) if lev is not None else 0.0
        except Exception:
            lev_f = 0.0
        title = f"work-plan: lift the '{klass}' fix-class"
        question = (
            f"The brain's ranked work-plan keeps surfacing class '{klass}' "
            f"(leverage {lev}). Beyond fixing each instance, what single "
            f"systemic change would most reduce how often the '{klass}' class "
            "recurs?"
        )
        out.append({
            "kind": "work_plan",
            "title": title,
            "question": question,
            "area": "reliability",
            "leverage": lev_f,
            "source": "brain_work_selector.build_work_plan",
        })
    return out


def _opportunity_candidates() -> list[dict]:
    """Candidate agenda items from the enhancer's evidence SCAN — improvement
    opportunities across {reliability, performance, data_coverage,
    conversion_revenue, developer_ux}, each grounded in a real signal. REUSE-
    ONLY: reads scan_opportunities(); never raises. scan_opportunities already
    REORDERS by the brain's learned per-area success, so earlier items get a
    small leverage edge here (rank-position proxy) — the work-plan's own
    numeric leverage still competes on equal footing."""
    out: list[dict] = []
    try:
        from routes.brain_enhancer import scan_opportunities
        opps = scan_opportunities() or []
    except Exception as e:
        logger.warning("brain_self_director: opportunity scan failed: %s", e)
        return out
    n = len(opps)
    for idx, o in enumerate(opps):
        if not isinstance(o, dict):
            continue
        area = o.get("area") or "developer_ux"
        signal = (o.get("signal") or "").strip()
        question = (o.get("question") or "").strip()
        if not question:
            continue
        # Title mirrors the enhancer's chip form: "[area] <signal tail>".
        sig_tail = signal.split(":", 1)[1].strip() if ":" in signal else signal
        title = f"[{area}] {sig_tail[:90]}".strip()
        # scan_opportunities is already learned-reordered (best first). Give
        # earlier items a small, bounded leverage edge so the brain's own
        # attention-ordering is honored, while staying in a band the work-plan's
        # numeric leverage can still beat. Range ~[1.0 .. ~1.5].
        rank_leverage = 1.0 + (0.5 * (n - idx) / float(max(1, n)))
        out.append({
            "kind": "opportunity",
            "title": title,
            "question": question,
            "area": area,
            "leverage": round(rank_leverage, 4),
            "source": "brain_enhancer.scan_opportunities",
        })
    return out


def _eval_findings_candidates() -> list[dict]:
    """Candidate agenda items from the AGENT-EVAL loop (model_relations shell #18).
    Each platform's OWN flagship model pressure-tests the live DC Hub rail monthly
    and writes model_relations_runs.verdict.top_structural_gap — the external
    model's #1 concrete structural ask — plus http_5xx (a regression it observed).
    Until now that verdict died in a human-triage queue nobody polls (the one seam
    where 'an agent noticed friction' never reached 'the brain starts a fix'). This
    routes the freshest changed/first-run gap per platform in as a PROPOSE-ONLY
    investigation question — closing the notice->start-the-fix half of the loop
    WITHOUT any autonomous shipping (still one human-reviewed agenda row). REUSE-
    ONLY: reads model_relations_runs; never raises.

    r-escalation-ladder (2026-07-18): each candidate is also stamped with
    `gate_evidence` — the escalation ladder's inputs (spec §Implementation
    notes: "stability counting: model_relations_runs.verdict->
    top_structural_gap normalized + counted across platforms/runs"; the
    active-instability signal is the http_5xx column). repeat_count = runs in
    the window naming the SAME number-stripped gap; distinct_sources = distinct
    platforms naming it; recent_5xx_rate = fraction of window runs that
    observed NEW 5xx. Aggregate failures degrade to the conservative defaults
    (1 run / 1 source) — which the ladder hard-blocks as weak provenance."""
    out: list[dict] = []
    conn = _conn()
    if conn is None:
        return out
    all_rows = []
    try:
        with conn.cursor() as cur:
            # latest changed/first-run verdict per platform — the actionable ones
            cur.execute(
                "SELECT DISTINCT ON (platform) platform, verdict_diff, verdict, "
                "http_5xx FROM model_relations_runs "
                "WHERE verdict_diff IN ('changed', 'first_run') "
                "  AND started_at >= NOW() - INTERVAL '45 days' "
                "ORDER BY platform, started_at DESC")
            rows = cur.fetchall()
            # r-escalation-ladder: EVERY window run (not just the freshest per
            # platform) for the ladder's stability/agreement/instability inputs.
            try:
                cur.execute(
                    "SELECT platform, verdict, http_5xx "
                    "FROM model_relations_runs "
                    "WHERE started_at >= NOW() - INTERVAL '45 days'")
                all_rows = cur.fetchall() or []
            except Exception:
                all_rows = []
    except Exception as e:
        logger.warning("brain_self_director: eval-findings scan failed: %s", e)
        return out
    finally:
        try: conn.close()
        except Exception: pass
    import json as _json

    def _parse_verdict(raw) -> dict:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = _json.loads(raw)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    # ── gate-evidence aggregates over the whole window ───────────────
    gap_stats: dict = {}   # norm-sig -> {"count": n, "platforms": set}
    runs_total = 0
    runs_5xx = 0
    for _plat, _raw_v, _h5 in all_rows:
        runs_total += 1
        try:
            if _h5 and int(_h5) > 0:
                runs_5xx += 1
        except Exception:
            pass
        _g = (_parse_verdict(_raw_v).get("top_structural_gap") or "").strip()
        if not _g:
            continue
        _sig = _norm_sig(_g)
        _st = gap_stats.setdefault(_sig, {"count": 0, "platforms": set()})
        _st["count"] += 1
        _st["platforms"].add(_plat)
    recent_5xx_rate = (runs_5xx / float(runs_total)) if runs_total else 0.0

    for platform, diff, verdict, http_5xx in rows or []:
        v = _parse_verdict(verdict)
        gap = (v.get("top_structural_gap") or "").strip()
        if not gap:
            continue
        saw_5xx = bool(http_5xx and int(http_5xx) > 0)
        title = f"agent-eval: {platform}'s #1 structural ask"
        question = (
            f"The {platform} flagship model, pressure-testing DC Hub's live API "
            f"(model_relations, verdict_diff={diff}"
            + (f", observed {http_5xx} new 5xx" if saw_5xx else "")
            + f"), named its #1 structural gap: \"{gap[:400]}\". Beyond a one-off "
            "patch, what single change would most address what this external agent flagged?"
        )
        # r-escalation-ladder: the ladder's inputs for THIS gap, computed from
        # the window aggregates. Evidence kinds / expected improvement only
        # count when the evaluator's verdict names them EXPLICITLY (the pure
        # gate treats prose as no evidence — "feels cleaner" never escalates).
        _st = gap_stats.get(_norm_sig(gap)) or {}
        try:
            from routes.brain_fix_gates import extract_evidence_kinds
            _ev_kinds = extract_evidence_kinds(v)
        except Exception:
            _ev_kinds = []
        _imp = None
        for _k in ("expected_improvement", "measured_improvement"):
            if v.get(_k) is not None:
                try:
                    _imp = float(v.get(_k))
                except Exception:
                    _imp = None
                break
        # A NEW 5xx an external model observed on the live rail is a concrete
        # reliability regression -> higher leverage than a prose structural ask.
        out.append({
            "kind": "eval_finding",
            "title": title,
            "question": question,
            "area": "reliability" if saw_5xx else "developer_ux",
            "leverage": 1.6 if saw_5xx else 1.2,
            "source": "model_relations_runs",
            "gate_evidence": {
                "repeat_count": int(_st.get("count") or 1),
                "distinct_sources": len(_st.get("platforms") or ()) or 1,
                "evidence": _ev_kinds,
                "expected_improvement": _imp,
                "recent_5xx_rate": round(recent_5xx_rate, 4),
                # No conflicting-recommendation detector yet — the pure gate
                # takes it as an input; a future detector flips this to True
                # when two evaluators ask for opposing changes.
                "conflicting_recs": False,
            },
        })
    return out


# ════════════════════════════════════════════════════════════════════
#  Anti-loop (2026-06-20): the brain was re-investigating the SAME topic
#  every tick — e.g. 8+ data_coverage "where to verify facilities" agenda
#  items in 2 days, each at confidence 0.20-0.25, each correctly shredded by
#  refutation for the SAME reason (the per-geo/operator breakdown isn't in the
#  evidence). The old dedup compared EXACT titles, but the data_coverage title
#  embeds live counts ("2207 verified vs 21804" -> "2235 vs 21762") that drift
#  every tick, so it never matched and was re-picked forever. Two fixes:
#   (1) dedup on a NUMBER-STRIPPED signature so drifting-count titles collapse;
#   (2) suppress a whole AREA once it has produced >=2 low-confidence (<0.3) or
#       refuted results in the window — the brain has LEARNED that area can't be
#       answered with current evidence, so it should spend its reasoning on a
#       productive area (e.g. conversion_revenue, which scored 0.40) instead of
#       spinning. Flag-gated (BRAIN_SELFDIRECT_ANTILOOP, default ON) + a
#       fail-safe: if it would suppress EVERYTHING, it returns nothing this tick
#       (skipping a known dead-end is the efficiency win) rather than re-loop.
_DIGITS_RE = re.compile(r"\d[\d,\.]*")
_LOWYIELD_CONF = 0.30
_LOWYIELD_MIN_REPEATS = 2


def _antiloop_enabled() -> bool:
    return os.environ.get(
        "BRAIN_SELFDIRECT_ANTILOOP", "1").strip().lower() in ("1", "true", "yes", "on")


def _norm_sig(text: str) -> str:
    """Loop-dedup signature: lowercase, strip volatile numbers, collapse space —
    so a title whose only change is drifting live counts collapses to one sig."""
    s = _DIGITS_RE.sub("#", (text or "").strip().lower())
    return re.sub(r"\s+", " ", s).strip()


def _recent_sigs_and_lowyield(days: int) -> tuple[set, set]:
    """(recent number-stripped signatures, low-yield areas). A low-yield area has
    produced >= _LOWYIELD_MIN_REPEATS results that were low-confidence or
    refuted in the window. Fail-OPEN to empty sets (a DB hiccup must not freeze
    the loop; the daily cap still bounds cost)."""
    sigs: set = set()
    area_low: dict = {}
    conn = _conn()
    if conn is None:
        return sigs, set()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title, question, area, confidence, result_json "
                "FROM brain_self_agenda "
                "WHERE created_at >= NOW() - (%s || ' days')::interval",
                (str(int(days)),))
            for title, question, area, conf, rj in (cur.fetchall() or []):
                for v in (title, question):
                    if v:
                        sigs.add(_norm_sig(str(v)))
                refuted = False
                try:
                    if isinstance(rj, str):
                        rj = json.loads(rj)
                    if isinstance(rj, dict):
                        refuted = (bool(rj.get("refuted"))
                                   or rj.get("refutation_survived") is False)
                except Exception:
                    pass
                low = refuted or (conf is not None and float(conf) < _LOWYIELD_CONF)
                if area and low:
                    area_low[area] = area_low.get(area, 0) + 1
    except Exception:
        try: conn.rollback()
        except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass
    lowyield = {a for a, n in area_low.items() if n >= _LOWYIELD_MIN_REPEATS}
    return sigs, lowyield


# ── STATEFUL DEDUP (2026-07-16): prior-condition state, ONE query ────
def _agenda_fingerprint_state() -> dict:
    """{fingerprint: {"age_days", "open", "text"}} for the MOST RECENT agenda
    row per fingerprint within the dedup lookback window — ONE indexed query,
    no per-candidate connection fan-out (the pool is chronic at 80). "open" =
    status 'surfaced' AND ungraded (the only live states: _store_agenda writes
    status='surfaced' and the grade endpoint fills grade). Fail-OPEN to {} on
    any error — a DB hiccup must never freeze picking; the daily cap still
    bounds cost."""
    out: dict = {}
    conn = _conn()
    if conn is None:
        return out
    try:
        from routes.brain_proposal_dedup import lookback_days
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT ON (fingerprint) fingerprint, status, grade, "
                "title, EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0 "
                "FROM brain_self_agenda "
                "WHERE fingerprint IS NOT NULL "
                "AND created_at >= NOW() - (%s || ' days')::interval "
                "ORDER BY fingerprint, created_at DESC",
                (str(lookback_days()),),
            )
            for fp, status, grade, title, age_days in (cur.fetchall() or []):
                out[fp] = {
                    "age_days": float(age_days or 0.0),
                    "open": (str(status or "") == "surfaced") and not grade,
                    "text": str(title or ""),
                }
    except Exception as e:
        logger.warning("brain_self_director: fingerprint state read failed "
                       "(dedup fails open): %s", e)
        try: conn.rollback()
        except Exception: pass
        return {}
    finally:
        try: conn.close()
        except Exception: pass
    return out


def _stateful_dedup_filter(fresh: list[dict]) -> list[dict]:
    """STATEFUL fingerprint dedup over the surviving candidates (2026-07-16).

    The _norm_sig anti-loop above only looks back BRAIN_SELF_DIRECT_DEDUP_DAYS
    (default 3), so the SAME condition was re-picked every ~4 days forever —
    14 near-identical data_coverage agenda rows, each burning a full
    investigate() (draft + adversarial-refutation) cycle. This filter drops a
    candidate while a same-fingerprint agenda row is open/inside the
    BRAIN_PROPOSAL_REDRAFT_DAYS cooldown, and after the cooldown re-admits it
    ONLY when its measured figures moved materially (shared re-fire rule in
    routes.brain_proposal_dedup). Survivors are stamped with their fingerprint
    so _store_agenda persists it.

    Kill: BRAIN_PROPOSAL_DEDUP=0. Fail-OPEN: on ANY error the input is
    returned unchanged (worst case = the prior duplicate-drafting behavior)."""
    try:
        from routes.brain_proposal_dedup import (
            dedup_enabled, condition_fingerprint, should_skip_redraft,
        )
        if not dedup_enabled():
            return fresh
        state = _agenda_fingerprint_state()
        kept: list[dict] = []
        suppressed = 0
        for c in fresh:
            fp = condition_fingerprint(
                c.get("area"), c.get("title"), c.get("question"))
            skip, why = should_skip_redraft(
                state.get(fp), c.get("title") or c.get("question") or "")
            if skip:
                suppressed += 1
                logger.info("brain_self_director: dup-suppressed (%s) fp=%s "
                            "title=%r", why, fp, str(c.get("title") or "")[:120])
                continue
            c = dict(c)
            c["fingerprint"] = fp
            kept.append(c)
        if suppressed:
            logger.info("brain_self_director: stateful dedup suppressed %d/%d "
                        "candidate(s)", suppressed, len(fresh))
        return kept
    except Exception as e:
        logger.warning("brain_self_director: stateful dedup skipped: %s", e)
        return fresh


def pick_agenda_item() -> Optional[dict]:
    """ASSESS what is most worth thinking about RIGHT NOW.

    REUSES brain_work_selector.build_work_plan() AND
    brain_enhancer.scan_opportunities(), pools their candidates, DEDUPES against
    agenda items surfaced in the last ~3 days, and returns the SINGLE
    highest-leverage remaining candidate as
    {kind, title, question, area, leverage, source} — or None when there is
    nothing new to think about (no data / everything deduped).

    Best-effort + read-only: NEVER raises (returns None on any error). It writes
    NOTHING — picking is pure ASSESSMENT."""
    try:
        candidates: list[dict] = []
        try:
            candidates.extend(_work_plan_candidates())
        except Exception as e:
            logger.warning("brain_self_director: work-plan candidates failed: %s", e)
        try:
            candidates.extend(_opportunity_candidates())
        except Exception as e:
            logger.warning("brain_self_director: opportunity candidates failed: %s", e)
        try:
            candidates.extend(_eval_findings_candidates())
        except Exception as e:
            logger.warning("brain_self_director: eval-findings candidates failed: %s", e)

        if not candidates:
            return None

        # DEDUPE against recently-surfaced agenda items so the loop doesn't
        # re-investigate the same thing every tick.
        if _antiloop_enabled():
            # Number-stripped signature dedup + low-yield AREA suppression, so a
            # topic whose title only differs by drifting live counts (or a whole
            # area the brain keeps failing to answer) is not re-picked.
            try:
                recent_sigs, lowyield_areas = _recent_sigs_and_lowyield(
                    _dedup_window_days())
            except Exception:
                recent_sigs, lowyield_areas = set(), set()
            fresh = [c for c in candidates
                     if _norm_sig(str(c.get("title") or "")) not in recent_sigs
                     and _norm_sig(str(c.get("question") or "")) not in recent_sigs
                     and (c.get("area") not in lowyield_areas)]
            if not fresh:
                # Everything is a recent repeat or a known low-yield area.
                # Skipping this tick (returning None) is the efficiency win —
                # better than burning the daily-cap on a known dead-end. The
                # loop re-engages when the candidate generators surface
                # something new.
                logger.info(
                    "brain_self_director: anti-loop — all candidates recent/"
                    "low-yield (suppressed areas=%s); skipping tick",
                    sorted(lowyield_areas))
                return None
        else:
            try:
                recent = _recent_titles(_dedup_window_days())
            except Exception:
                recent = set()
            fresh = [c for c in candidates
                     if str(c.get("title") or "").strip().lower() not in recent
                     and str(c.get("question") or "").strip().lower() not in recent]
            if not fresh:
                return None

        # STATEFUL fingerprint dedup (2026-07-16): drop candidates whose
        # condition already has an open / cooling-down agenda row beyond the
        # short anti-loop window; stamp survivors with their fingerprint.
        fresh = _stateful_dedup_filter(fresh)
        if not fresh:
            logger.info("brain_self_director: stateful dedup — every candidate "
                        "already has an open/recent agenda row; skipping tick")
            return None

        # Highest-leverage first; stable on ties by original pooled order so the
        # choice is deterministic.
        def _lev(c) -> float:
            try:
                return float(c.get("leverage") or 0.0)
            except Exception:
                return 0.0
        fresh_indexed = list(enumerate(fresh))
        fresh_indexed.sort(key=lambda t: (-_lev(t[1]), t[0]))
        return fresh_indexed[0][1]
    except Exception as e:
        logger.warning("brain_self_director: pick_agenda_item failed: %s", e)
        return None


# ════════════════════════════════════════════════════════════════════
#  Storage (direct psycopg2, mirror brain_investigator)
# ════════════════════════════════════════════════════════════════════
def _today_count() -> int:
    """Number of agenda rows created TODAY (created_at::date = today) — the
    daily-cap counter. Returns a LARGE number on DB error so a broken counter
    fails CLOSED (skips the model call) rather than letting the loop run
    uncapped. The cost ceiling must hold even when the DB is flaky."""
    conn = _conn()
    if conn is None:
        # No DB to count against -> we can't enforce the cap -> fail closed.
        return 10 ** 9
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM brain_self_agenda "
                "WHERE created_at::date = (NOW() AT TIME ZONE 'UTC')::date"
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception as e:
        logger.warning("brain_self_director: today_count failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return 10 ** 9
    finally:
        try: conn.close()
        except Exception: pass


def _store_agenda(item: dict, result: dict) -> Optional[int]:
    """Persist ONE agenda row. This is the ONLY write the self-director ever
    performs — PROPOSE-ONLY. status defaults 'surfaced' (NEVER 'applied' /
    'merged' / 'acted'). Returns the new id (or None on failure)."""
    conn = _conn()
    if conn is None:
        return None
    fp = (str(item.get("fingerprint") or "").strip() or None)
    base_vals = (
        str(item.get("kind") or "")[:64],
        str(item.get("title") or "")[:500],
        str(item.get("question") or "")[:4000],
        str(item.get("area") or "")[:64],
        json_for_column(result, 200000),
        float(result.get("confidence") or 0.0),
    )
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO brain_self_agenda "
                    "(kind, title, question, area, result_json, confidence, "
                    "status, fingerprint) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'surfaced', %s) RETURNING id",
                    base_vals + (fp,),
                )
            except Exception:
                # fingerprint column missing (DDL silently skipped on this
                # host) — never lose the agenda row over dedup bookkeeping.
                try: conn.rollback()
                except Exception: pass
                cur.execute(
                    "INSERT INTO brain_self_agenda "
                    "(kind, title, question, area, result_json, confidence, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'surfaced') RETURNING id",
                    base_vals,
                )
            row = cur.fetchone()
            conn.commit()
            return int(row[0]) if row else None
    except Exception as e:
        logger.warning("brain_self_director: store agenda failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return None
    finally:
        try: conn.close()
        except Exception: pass


def _row_to_agenda(row) -> dict:
    rj = row[5]
    if isinstance(rj, str):
        try: rj = json.loads(rj)
        except Exception: rj = {}
    created = row[9]
    try: created = created.isoformat()
    except Exception: created = str(created)
    return {
        "id": row[0], "kind": row[1], "title": row[2], "question": row[3],
        "area": row[4], "result": rj, "confidence": row[6],
        "status": row[7], "grade": row[8], "created_at": created,
    }


def _recent_agenda(limit: int = 25) -> list[dict]:
    """Recent self-chosen agenda items, ranked highest-leverage (confidence)
    first. [] on error."""
    conn = _conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, kind, title, question, area, result_json, confidence, "
                "status, grade, created_at "
                "FROM brain_self_agenda "
                "ORDER BY created_at DESC LIMIT %s",
                (int(limit),),
            )
            rows = cur.fetchall() or []
        items = [_row_to_agenda(r) for r in rows]
        # Surface highest-confidence first (the leverage proxy on the stored
        # verified result), ties keep the recency order.
        try:
            items.sort(key=lambda p: (p.get("confidence") or 0.0), reverse=True)
        except Exception:
            pass
        return items
    except Exception as e:
        logger.warning("brain_self_director: recent agenda failed: %s", e)
        return []
    finally:
        try: conn.close()
        except Exception: pass


def _grade_agenda(agenda_id: int, grade: str) -> bool:
    """Record a human grade for CALIBRATION (the verify->learn loop applied to
    the self-director's own track record). Returns True if a row was updated."""
    conn = _conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE brain_self_agenda SET grade = %s WHERE id = %s",
                (str(grade)[:64], int(agenda_id)),
            )
            n = cur.rowcount or 0
            conn.commit()
            return n > 0
    except Exception as e:
        logger.warning("brain_self_director: grade failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return False
    finally:
        try: conn.close()
        except Exception: pass


# ════════════════════════════════════════════════════════════════════
#  r-escalation-ladder (2026-07-18) — evidence-gated escalation
#  (spec: docs/brain-pr-escalation-gates.md; gate math:
#   routes/brain_fix_gates.evaluate_escalation)
# ════════════════════════════════════════════════════════════════════
def _escalation_enabled() -> bool:
    """The ladder ships ON (the gate itself is the safety mechanism — with no
    evidence everything stays agenda-only, exactly the prior behavior). Kill:
    BRAIN_ESCALATION_LADDER=0."""
    return os.environ.get("BRAIN_ESCALATION_LADDER", "1").strip().lower() \
        not in ("0", "false", "off", "no")


def _gate_candidate(item: dict, result: dict) -> dict:
    """Build the pure gate's candidate dict from an eval_finding agenda item
    (its stamped gate_evidence) + the investigation result. The surfaces a
    structural ask would touch are not known until a human implements it, so
    locality is screened on TEXT: the doc-only spec artifact itself
    (contract-local by construction) plus any business-policy pseudo-surfaces
    (pricing/auth/quota/ranking/...) the recommendation text mentions — those
    hard-block as human-only."""
    ge = dict((item or {}).get("gate_evidence") or {})
    from routes.brain_fix_gates import text_flags
    text = " ".join(str(x) for x in ((item or {}).get("title"),
                                     (item or {}).get("question"),
                                     (result or {}).get("recommendation"))
                    if x)
    flags = text_flags(text)
    files = (["docs/brain-proposals/spec.md"]
             + list(flags.get("human_only_surfaces") or []))
    return {
        "repeat_count": ge.get("repeat_count", 1),
        "distinct_sources": ge.get("distinct_sources", 1),
        "evidence": ge.get("evidence") or [],
        "files_touched": files,
        "expected_improvement": ge.get("expected_improvement"),
        "recent_5xx_rate": ge.get("recent_5xx_rate", 0.0),
        "conflicting_recs": bool(ge.get("conflicting_recs")),
        "removes_or_renames_fields": bool(
            ge.get("removes_or_renames_fields", flags.get("breaking"))),
    }


def _evidence_gated_escalation(item: dict, result: dict) -> Optional[dict]:
    """PURE half of the ladder wiring: evaluate the escalation gate for an
    eval_finding item. No DB, no GitHub — just the verdict dict (embedded into
    the agenda row's result_json so agenda-only outcomes carry their "why").
    None when the ladder doesn't apply (other kinds / killed). Never raises."""
    try:
        if not _escalation_enabled():
            return None
        if (item or {}).get("kind") != "eval_finding":
            return None
        from routes.brain_fix_gates import evaluate_escalation
        return evaluate_escalation(_gate_candidate(item, result))
    except Exception as e:
        logger.warning("brain_self_director: escalation eval failed: %s", e)
        return None


def _finalize_escalation(item: dict, result: dict, verdict: dict,
                         agenda_id: int) -> Optional[dict]:
    """SIDE-EFFECT half, AFTER the agenda row is stored:
      1. feedback stub — a re-firing condition grades any earlier gated PR for
         the same fingerprint as 'recurred_after_pr' (spec §Feedback loop;
         runs BEFORE the new verdict is logged so it never grades itself);
      2. state == 'draft_pr' → file the DOC-ONLY DRAFT spec PR with the gate
         verdict EMBEDDED in the body (open_spec_pr re-enforces the verdict —
         defense in depth — and a human still merges);
         anything less → agenda-only, log why;
      3. log the verdict row (brain_gate_verdicts) so the outcome is
         attributable.
    Returns {number, url} of the opened PR, or None. Best-effort; never
    raises."""
    pr_info = None
    fp = (item or {}).get("fingerprint")
    try:
        from routes.brain_fix_gates import (grade_prior_verdicts,
                                            record_gate_verdict)
    except Exception as e:
        logger.warning("brain_self_director: gate log import failed: %s", e)
        grade_prior_verdicts = record_gate_verdict = None
    if grade_prior_verdicts is not None:
        try:
            n = grade_prior_verdicts(fp)
            if n:
                logger.info("brain_self_director: condition re-fired — graded "
                            "%d prior gated PR verdict(s) recurred_after_pr "
                            "(fp=%s)", n, fp)
        except Exception:
            pass
    if (verdict or {}).get("state") == "draft_pr":
        try:
            from routes.brain_pr_opener import open_spec_pr
            directive = ((result or {}).get("recommendation") or "").strip() \
                or ((item or {}).get("question") or "").strip()
            res = open_spec_pr(
                directive,
                heading=(item or {}).get("title") or "",
                kind="agenda",
                item_id=agenda_id,
                label=(f"escalation-gated · confidence "
                       f"{(verdict or {}).get('confidence')}"),
                gate_verdict=verdict,
            ) or {}
            if res.get("acted") and res.get("pr"):
                pr_info = res.get("pr")
            elif res.get("dup_pr"):
                pr_info = {"number": res.get("dup_pr"), "dup": True}
            else:
                logger.info("brain_self_director: gated spec PR not filed: %s",
                            res.get("note") or res.get("error") or res)
        except Exception as e:
            logger.warning("brain_self_director: gated spec PR failed: %s", e)
    else:
        logger.info(
            "brain_self_director: escalation stayed agenda-only "
            "(state=%s, confidence=%s, hard_blocks=%s)",
            (verdict or {}).get("state"), (verdict or {}).get("confidence"),
            [b.get("name") for b in ((verdict or {}).get("hard_blocks") or [])])
    if record_gate_verdict is not None:
        try:
            record_gate_verdict(fp, verdict,
                                pr_number=(pr_info or {}).get("number"),
                                agenda_id=agenda_id)
        except Exception:
            pass
    return pr_info


# ════════════════════════════════════════════════════════════════════
#  Step 2 + 3: the autonomous tick — INVESTIGATE + SURFACE
# ════════════════════════════════════════════════════════════════════
def self_direct_tick() -> dict:
    """THE AUTONOMOUS STEP. Runs UNPROMPTED on the cron's schedule.

    GUARD ORDER (cost-first — every guard before the model call short-circuits
    WITHOUT calling a model):
      1. flag off    -> {ran:false, skipped:disabled}
      2. no api key  -> {ran:false, skipped:no_api_key}
      3. daily cap   -> {ran:false, skipped:daily_cap}
      4. no candidate-> {ran:false, skipped:no_candidate}
      5. else        -> investigate() ONCE (the ONLY model work, 1 per tick),
                        STORE one agenda row, return {ran:true, agenda_id}.

    PROPOSE-ONLY: the primary write is the agenda row (via _store_agenda). It
    NEVER merges/sends/opens-a-code-PR/triggers-the-automerge. The single
    evidence-gated exception (r-escalation-ladder, 2026-07-18): an eval_finding
    whose evidence clears EVERY ladder gate may also file a DOC-ONLY DRAFT spec
    PR — human-merged, kill-switched, capped (see the module docstring).
    NEVER raises — any LLM/DB error degrades to a skip/no-op."""
    try:
        # ── Guard 1: flag off → NO model call ────────────────────────
        if not _enabled():
            return {"ran": False, "skipped_reason": "disabled"}

        # ── Guard 2: no API key → NO model call (degrade gracefully) ──
        if not _has_api_key():
            return {"ran": False, "skipped_reason": "no_api_key"}

        # ── Guard 3: daily cap → NO model call (the cost ceiling) ────
        cap = _daily_cap()
        try:
            used = _today_count()
        except Exception:
            # Fail CLOSED on a broken counter — don't run uncapped.
            used = 10 ** 9
        if used >= cap:
            return {"ran": False, "skipped_reason": "daily_cap",
                    "used_today": used, "daily_cap": cap}

        # ── Guard 4: ASSESS — pick the agenda item (read-only) ──────
        try:
            item = pick_agenda_item()
        except Exception as e:
            logger.warning("brain_self_director: pick failed: %s", e)
            item = None
        if not item:
            return {"ran": False, "skipped_reason": "no_candidate"}

        # ── INVESTIGATE — the ONE model call this tick performs ─────
        question = (item.get("question") or "").strip()
        if not question:
            return {"ran": False, "skipped_reason": "no_candidate"}
        try:
            from routes.brain_investigator import investigate
        except Exception as e:
            logger.warning("brain_self_director: investigate import failed: %s", e)
            return {"ran": False, "skipped_reason": "investigate_unavailable"}
        try:
            result = investigate(question)
        except Exception as e:
            logger.warning("brain_self_director: investigate failed: %s", e)
            return {"ran": False, "skipped_reason": "investigate_error"}
        if not isinstance(result, dict) or result.get("cannot_investigate"):
            return {"ran": False, "skipped_reason": "cannot_investigate",
                    "detail": (result or {}).get("cannot_investigate")
                    if isinstance(result, dict) else None}

        # ── r-escalation-ladder: evaluate the evidence gate BEFORE the
        # store so the agenda row carries the verdict either way (agenda-only
        # outcomes are auditable from the row itself). Pure evaluation only —
        # any side effect happens after the row exists.
        esc = None
        try:
            esc = _evidence_gated_escalation(item, result)
            if esc:
                result["escalation"] = esc
        except Exception as e:
            logger.warning("brain_self_director: escalation skipped: %s", e)

        # ── SURFACE — STORE one agenda row (the primary write) ──────
        agenda_id = _store_agenda(item, result)
        if agenda_id is None:
            # The investigation ran but we couldn't persist it. Be honest: the
            # tick did NOT surface an agenda row, so it didn't "run" in the
            # sense the cap counts. (No row → no double-charge against the cap.)
            return {"ran": False, "skipped_reason": "store_failed",
                    "title": item.get("title")}
        out = {"ran": True, "agenda_id": agenda_id,
               "kind": item.get("kind"), "title": item.get("title"),
               "area": item.get("area"),
               "confidence": result.get("confidence")}
        # ── r-escalation-ladder: side effects AFTER the row exists —
        # feedback-grade prior verdicts, file the gated DOC-ONLY draft spec
        # PR when (and only when) the verdict says draft_pr, log the verdict.
        if esc:
            out["escalation_state"] = esc.get("state")
            out["escalation_confidence"] = esc.get("confidence")
            try:
                pr_info = _finalize_escalation(item, result, esc, agenda_id)
                if pr_info:
                    out["escalation_pr"] = pr_info
            except Exception as e:
                logger.warning("brain_self_director: escalation finalize "
                               "failed: %s", e)
        return out
    except Exception as e:
        # The whole tick is best-effort — a self-running loop must never raise.
        logger.warning("brain_self_director: tick failed: %s", e)
        return {"ran": False, "skipped_reason": f"error:{str(e)[:120]}"}


# ════════════════════════════════════════════════════════════════════
#  TICK WATERMARK (2026-08-19)
#
#  /api/v1/brain/self-direct/tick is in main.py's _WORKER_PROXY_POST_PATHS
#  but NOT _WORKER_PROXY_SYNC_PATHS, so web relays it to dchub-worker on a 15s
#  read budget and answers 202 when the tick outlives it. A 202 carries no
#  result, so brain-self-direct.yml cannot tell "delegated and running" from
#  "did not run" out of the body alone — it has to observe the tick landing.
#
#  Nothing here was observable. self_direct_tick() writes ONLY on the fully-
#  investigated path (_store_agenda); disabled / no_api_key / daily_cap /
#  no_candidate all return without touching the DB — and with a default cap of
#  4 investigations/day against a 6-tick/day cron, those skips are the NORMAL
#  state, not the exception. brain_self_agenda MAX(created_at) is therefore
#  the WRONG watermark: it would read "never ran" for a perfectly healthy
#  capped tick and turn a false green into a false red.
#
#  So this stamps a watermark at the END of EVERY tick, skips included. It
#  goes in brain_state — the shared (state_key, state_value JSONB, updated_at)
#  table routes/brain_data_growth_radar.py and autonomous_brain.py already
#  use — so it is DB-BACKED, not a module global. That distinction is the
#  whole trap #2929 hit: brain_autonomy_loop._LAST_TICK is process-local
#  (`gunicorn --workers 1`, one copy per service), the ticks run on the
#  worker, and the status GET served web's never-written copy until
#  /api/v1/brain/autonomy/status was added to the proxy allowlist. A row in
#  brain_state is read identically by web and worker, so /self-direct/status
#  below needs NO allowlist entry and cannot drift that way.
#
#  The timestamp lives INSIDE the JSONB value rather than being read off
#  updated_at: brain_state has two idempotent CREATE TABLE definitions in this
#  repo that disagree on that column (TIMESTAMPTZ here, TIMESTAMP in
#  autonomous_brain.py), and whichever ran first is what production has. A
#  string we write ourselves is immune to which one won.
# ════════════════════════════════════════════════════════════════════
_TICK_STATE_KEY = "self_direct_last_tick"


def _record_tick(result: dict) -> None:
    """Stamp the last-tick watermark. Best-effort; NEVER raises.

    Called for every outcome, including the ones that do no work — the point
    of the watermark is that the tick HAPPENED, not that it found something.
    """
    import datetime as _dt
    stamp = _dt.datetime.now(_dt.timezone.utc).isoformat()
    payload = {
        "last_tick": stamp,
        "ran": bool((result or {}).get("ran")),
        "skipped_reason": (result or {}).get("skipped_reason"),
        "agenda_id": (result or {}).get("agenda_id"),
    }
    conn = _conn()
    if conn is None:
        logger.warning("brain_self_director: no DB; tick watermark not stamped")
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS brain_state (
                       id BIGSERIAL PRIMARY KEY,
                       state_key TEXT NOT NULL UNIQUE,
                       state_value JSONB NOT NULL,
                       updated_at TIMESTAMPTZ DEFAULT NOW())"""
            )
            conn.commit()
            cur.execute(
                """INSERT INTO brain_state (state_key, state_value, updated_at)
                   VALUES (%s, %s, NOW() ON CONFLICT DO NOTHING)
                   ON CONFLICT (state_key)
                   DO UPDATE SET state_value = EXCLUDED.state_value,
                                 updated_at = NOW()""",
                (_TICK_STATE_KEY, json_for_column(payload, 4000)),
            )
            conn.commit()
    except Exception as e:
        logger.warning("brain_self_director: tick watermark write failed: %s", e)
        try: conn.rollback()
        except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass


def _last_tick() -> dict:
    """The watermark, or {} when it has never been stamped / cannot be read.

    Fail-CLOSED for the caller's purposes: an unreadable watermark returns {},
    the poller sees no advance and reports the tick unobserved rather than
    inventing a completion.
    """
    conn = _conn()
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT state_value FROM brain_state WHERE state_key=%s",
                        (_TICK_STATE_KEY,))
            row = cur.fetchone()
        val = (row[0] if row else None) or {}
        if isinstance(val, str):
            val = json.loads(val or "{}")
        return val if isinstance(val, dict) else {}
    except Exception as e:
        logger.warning("brain_self_director: tick watermark read failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return {}
    finally:
        try: conn.close()
        except Exception: pass


# ════════════════════════════════════════════════════════════════════
#  Endpoints (admin-gated)
# ════════════════════════════════════════════════════════════════════
@brain_self_director_bp.post("/api/v1/brain/self-direct/tick")
def self_direct_tick_endpoint():
    """The cron heartbeat. Runs ONE self-directed tick: ASSESS -> (maybe)
    INVESTIGATE -> SURFACE. Returns the skip reason when dark/no-key/capped/no-
    candidate so the cron logs are legible. PROPOSE-ONLY: the most it ever does
    is store ONE agenda row — it NEVER acts. Admin-gated. The flag + caps are
    enforced SERVER-SIDE inside self_direct_tick (the endpoint adds no
    authority)."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only",
                       hint="X-Admin-Key / X-Internal-Key header required"), 403
    result = self_direct_tick()
    # Stamp the watermark for EVERY outcome — self_direct_tick() never raises,
    # so this is reached on the skip paths too. brain-self-direct.yml polls
    # /self-direct/status for it when the relay answers 202, and a skip is a
    # tick that HAPPENED: reporting it as no-progress would fail a healthy
    # capped run. See the TICK WATERMARK block above.
    _record_tick(result)
    return jsonify(ok=True,
                   note="PROPOSE-ONLY self-directed analysis — never acts; the "
                        "most it does is store one agenda row.",
                   **result), 200


@brain_self_director_bp.get("/api/v1/brain/self-direct/status")
def self_direct_status():
    """The tick WATERMARK — what brain-self-direct.yml polls when its POST is
    answered with a relayed 202 (delegated to dchub-worker, still running).

    Reads brain_state, so web and the worker return the SAME value and this
    path deliberately does NOT need to be in main.py's worker-proxy allowlist
    — unlike /api/v1/brain/autonomy/status, whose module-global watermark had
    one never-written copy per service (#2929). Admin-gated like every other
    endpoint on this blueprint."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only",
                       hint="X-Admin-Key / X-Internal-Key header required"), 403
    st = _last_tick()
    return jsonify(ok=True,
                   last_tick=st.get("last_tick"),
                   ran=st.get("ran"),
                   skipped_reason=st.get("skipped_reason"),
                   agenda_id=st.get("agenda_id"),
                   note="last_tick is stamped at the END of EVERY tick, "
                        "including the dark/capped/no-candidate skips — it "
                        "says the tick RAN, not that it found work."), 200


@brain_self_director_bp.get("/api/v1/brain/agenda")
def get_agenda():
    """The brain's self-chosen, verified AGENDA — recent items it CHOSE to think
    about, each carrying the adversarially-refuted recommendation, highest-
    leverage (confidence) first. PROPOSE-ONLY: these are for a human to
    review/decide; nothing here is acted on. Admin-gated."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only"), 403
    try:
        limit = int(request.args.get("limit", "25"))
    except Exception:
        limit = 25
    limit = max(1, min(limit, 200))
    items = _recent_agenda(limit=limit)
    return jsonify(ok=True, count=len(items), agenda=items,
                   note="PROPOSE-ONLY — the brain's self-directed analysis "
                        "agenda; a human reviews/decides. Nothing is acted "
                        "on."), 200


@brain_self_director_bp.post("/api/v1/brain/agenda/<int:agenda_id>/grade")
def grade_agenda(agenda_id):
    """Record a human grade (good/bad/score) for CALIBRATION — the same
    verify->learn loop, applied to the self-director's own track record of
    CHOOSING what to think about. Admin-gated."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only"), 403
    body = request.get_json(silent=True) or {}
    grade = (str(body.get("grade") or "")).strip()
    if not grade:
        return jsonify(ok=False, error="grade required"), 400
    ok = _grade_agenda(agenda_id, grade)
    if not ok:
        return jsonify(ok=False, error="not_found_or_db_error"), 404
    return jsonify(ok=True, id=agenda_id, grade=grade), 200


def register_brain_self_director(app) -> None:
    """Idempotent registration helper for main.py. Best-effort schema init."""
    try:
        init_self_director_schema()
    except Exception as e:
        logger.warning("brain_self_director: schema init skipped: %s", e)
    try:
        app.register_blueprint(brain_self_director_bp)
    except Exception as e:
        logger.warning("brain_self_director already registered: %s", e)
