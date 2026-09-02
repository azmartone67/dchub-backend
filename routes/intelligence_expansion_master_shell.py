"""routes/intelligence_expansion_master_shell.py — Intelligence Expansion Master Shell (#31, 2026-07-25).

WHY THIS EXISTS
===============
The 07-25 five-audit + Surface Truth wave established two facts about the
brain's expansion machinery:

  1. Most of the "missing" capability is BUILT but either unwired, dark, or
     silently degraded — media-growth (SEE→GOAL→MANAGE) is registered and
     cron-ticked yet the audit found the brain still audience-blind; RAG lost
     its entire second retrieval stage the day the embed provider moved off
     Cohere (_rerank_on() is provider-locked); lesson corpora are embedded but
     nothing proves they are recalled; the PR-outcome harness records KPIs that
     nothing reads back.
  2. Degradation of this class is INVISIBLE: every one of those states reads
     green on some dashboard. The recurring failure is a check that verifies an
     artifact (a job ran, a file changed) instead of reality (retrieval got
     better, a follower count landed, the zone worker serves canon).

This shell is the standing instrument for that second fact, applied to the
five expansion fronts (RAG · evidence/self-healing · media growth · self-
learning · efficiency). Every check is MEASURED from live state — the DB, the
edge, or the source tree — and a lane never reads PASS when it could not
check.

LANES
  1 · rag             stage-2 rerank alive (cohere OR neutral), every
                      registered corpus actually indexing, truncation exposure
                      of the flat 1600-char cap quantified
  2 · evidence        ingest ledger reachable, evidence-free green streaks
                      surfaced, ZONE worker serves canon (tools count + floor)
  3 · media_growth    the built media-growth shell is wired, ticking, and its
                      SEE stage is actually landing audience telemetry
  4 · self_learning   fix outcomes stamped, recidivism visible, lesson corpora
                      embedded AND consumed, PR-metric harness beating
  5 · efficiency      LLM usage/cache telemetry landing, cache hit-rate,
                      capture coverage across brain call sites, gateway state

HOUSE RULES
  · A lane never reads PASS when it could not check — unreachable is '?'.
  · Read-only. Writes nothing but its own dead-man beat (L8: never
    auto-execute). The one mutation adjacent to this shell — the neutral
    rerank — lives in brain_rag.py behind BRAIN_RAG_RERANK_NEUTRAL.
  · Fail-soft everywhere: a crashed lane renders '?' and never 500s the tick.

Surface:  GET /admin/intelligence-expansion             (HTML)
          GET /api/v1/admin/intelligence-expansion      (HTML)
          GET|POST /api/v1/admin/intelligence-expansion/master-tick  (JSON)
Beat:     intelligence-expansion-shell-daily
Kill:     INTEL_EXPANSION_SHELL_DISABLE=1
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

intelligence_expansion_master_shell_bp = Blueprint(
    "intelligence_expansion_master_shell", __name__)

ORIGIN = (os.environ.get("SURFACE_TRUTH_ORIGIN") or "https://dchub.cloud").rstrip("/")
_UA = "dchub-intel-expansion/1.0 (+https://dchub.cloud; internal-audit)"

# Retired pre-dedup facility floors (same range the Surface Truth shell bans).
# ★ 2026-08-31: third copy of a rotted range ban, removed. It flagged the
# live-healed floor as retired while sibling guards accepted the same bytes.
# util/canon_floor.py is the single rule now.
from util.canon_floor import floor_verdict as _floor_verdict

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Brain call sites that build bodies via brain_llm_structured — the capture-
# coverage census greps these for record_llm_usage wiring.
_LLM_CALL_SITES = (
    "routes/brain_lane_driver.py",
    "routes/brain_strategic_planner.py",
    "routes/brain_investigator.py",
    "routes/brain_feature_proposer.py",
    "routes/brain_answer_cache.py",
    "routes/analyst_note.py",
)

# Evidence-free-success streak threshold. consecutive_zero is the ledger's own
# counter; >= this many zero-row successes in a row on a sub-daily cadence is
# worth surfacing (legit-zero feeds exist — no_new_data is EARNED — so this
# lane LISTS, it does not fail the board).
_ZERO_STREAK = 5


# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("INTEL_EXPANSION_SHELL_DISABLE") or "").strip() == "1"


# ── helpers ───────────────────────────────────────────────────────────

def _check(cid: str, name: str, passed, detail: str,
           critical: bool = False) -> dict:
    """passed: True / False / None (None = indeterminate, renders '?')."""
    return {"id": cid, "name": name, "pass": passed,
            "detail": detail, "critical": critical}


def _lane_verdict(checks: list[dict]) -> str:
    """PASS only when every critical check affirmatively passed. An
    indeterminate critical check yields '?' — never green-by-silence."""
    crits = [k for k in checks if k.get("critical")]
    if any(k["pass"] is False for k in checks):
        return "FAIL"
    if any(k["pass"] is None for k in crits):
        return "?"
    return "PASS"


def _db():
    """The brain-RAG connection factory (read-only use here). None on failure."""
    try:
        from routes.brain_rag import _db as _rag_db
        return _rag_db()
    except Exception as e:  # noqa: BLE001
        logger.debug("[intel-expansion] db unavailable: %s", e)
        return None


def _q1(cur, sql: str, params=()):
    """One-row fetch; returns the row or None. Caller owns the cursor."""
    cur.execute(sql, params)
    return cur.fetchone()


def _read_repo(rel: str):
    try:
        with open(os.path.join(_REPO_ROOT, rel), encoding="utf-8",
                  errors="ignore") as fh:
            return fh.read()
    except Exception:
        return None


def _fetch(path: str):
    """GET a live surface through the public edge. Returns (body, error).
    requests, not urllib (regression_lint urllib-request-on-railway)."""
    try:
        import requests as _rq
        r = _rq.get(ORIGIN + path, headers={"User-Agent": _UA}, timeout=12)
        if r.status_code >= 400:
            return None, "HTTP %d" % r.status_code
        return r.text, None
    except Exception as e:  # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, str(e)[:110])


# ── lane 1 · RAG ──────────────────────────────────────────────────────

def _lane_rag() -> list[dict]:
    out: list[dict] = []
    try:
        from routes.brain_rag import (CORPORA, CHUNKED_CORPORA, LESSON_CORPORA,
                                      _embed_provider, _rerank_on,
                                      _neutral_rerank_on)
    except Exception as e:  # noqa: BLE001
        return [_check("rag_import", "brain_rag importable", None,
                       "import failed: %s" % str(e)[:120], critical=True)]

    provider = _embed_provider()
    if _rerank_on():
        mode = "cohere cross-encoder"
    elif _neutral_rerank_on():
        mode = "neutral lexical (provider=%s)" % provider
    else:
        mode = "OFF"
    out.append(_check("rag_rerank", "stage-2 rerank alive", mode != "OFF",
                      "mode: %s" % mode, critical=True))

    c = _db()
    if c is None:
        out.append(_check("rag_db", "corpus store reachable", None,
                          "no DB connection", critical=True))
        return out
    try:
        with c.cursor() as cur:
            cur.execute("SELECT source_table, count(*) "
                        "FROM brain_corpus_embeddings GROUP BY 1")
            counts = {r[0]: int(r[1]) for r in cur.fetchall()}
            registered = (set(CORPORA) | set(CHUNKED_CORPORA)
                          | set(LESSON_CORPORA))
            # fix_history + other push-fed corpora register no reader-side spec;
            # judge only corpora the registry actively indexes.
            silent = sorted(s for s in (set(CORPORA) | set(CHUNKED_CORPORA))
                            if counts.get(s, 0) == 0)
            out.append(_check(
                "rag_corpora", "every registered corpus has embeddings",
                not silent,
                ("%d corpora, %d rows total"
                 % (len(registered & set(counts)), sum(counts.values())))
                if not silent else
                "ZERO embeddings for: %s (indexing dead-ends silently)"
                % ", ".join(silent),
                critical=True))

            # Truncation exposure of the flat left(text,1600) cap: how many
            # source rows exceed what indexing can see. Bounded probe per
            # corpus (stops at 500 matches); informational — this is the
            # measured case for a chunk-out, not a failure of today.
            exposed = []
            for src, spec in sorted(CORPORA.items()):
                try:
                    where = spec.get("where")
                    sql = ("SELECT count(*) FROM (SELECT 1 FROM %s t WHERE "
                           "length(%s) > 1600 %s LIMIT 500) q"
                           % (src, spec["text"],
                              ("AND (%s)" % where) if where else ""))
                    row = _q1(cur, sql)
                    n = int(row[0]) if row else 0
                    if n:
                        exposed.append("%s:%s" % (src, "500+" if n >= 500 else n))
                except Exception:
                    try:
                        c.rollback()
                    except Exception:
                        pass
            out.append(_check(
                "rag_truncation", "flat-cap truncation exposure", True,
                ("rows whose text exceeds the 1600-char index cap — "
                 + (", ".join(exposed) if exposed else "none")),
                critical=False))
    except Exception as e:  # noqa: BLE001
        out.append(_check("rag_db", "corpus store readable", None,
                          "query failed: %s" % str(e)[:120], critical=True))
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


# ── lane 2 · evidence ─────────────────────────────────────────────────

def _lane_evidence() -> list[dict]:
    out: list[dict] = []
    c = _db()
    if c is None:
        out.append(_check("ev_ledger", "ingest ledger reachable", None,
                          "no DB connection", critical=True))
    else:
        try:
            with c.cursor() as cur:
                row = _q1(cur, "SELECT count(*) FROM ingest_runs")
                out.append(_check("ev_ledger", "ingest ledger reachable", True,
                                  "%d feeds tracked" % int(row[0]),
                                  critical=True))
                cur.execute(
                    "SELECT feed, consecutive_zero FROM ingest_runs "
                    "WHERE last_status = 'success' AND consecutive_zero >= %s "
                    "ORDER BY consecutive_zero DESC LIMIT 12", (_ZERO_STREAK,))
                streaks = cur.fetchall()
                out.append(_check(
                    "ev_zero_streaks",
                    "evidence-free green streaks surfaced", True,
                    ("none at >=%d" % _ZERO_STREAK) if not streaks else
                    ("%d feed(s) green with zero rows: %s — zero can be "
                     "EARNED (supply-limited feeds), so this lists, it does "
                     "not fail" % (len(streaks),
                                   ", ".join("%s(%d)" % (f, int(z))
                                             for f, z in streaks))),
                    critical=False))
                row = _q1(cur,
                          "SELECT last_run FROM ingest_runs WHERE feed = %s",
                          ("intelligence-expansion-shell-daily",))
                if row and row[0]:
                    age_h = ((datetime.datetime.now(datetime.timezone.utc)
                              - row[0]).total_seconds() / 3600.0)
                    out.append(_check("ev_own_beat", "own beat within 26h",
                                      age_h <= 26,
                                      "last beat %.1fh ago" % age_h,
                                      critical=False))
                else:
                    out.append(_check("ev_own_beat", "own beat within 26h",
                                      None, "no beat yet (first tick?)",
                                      critical=False))
        except Exception as e:  # noqa: BLE001
            out.append(_check("ev_ledger", "ingest ledger readable", None,
                              "query failed: %s" % str(e)[:120],
                              critical=True))
        finally:
            try:
                c.close()
            except Exception:
                pass

    # ZONE worker (the .well-known shadow) — the surface nothing guarded.
    # 07-25 finding: live served "73 tools over 21,000+" against a canon of
    # 79 / 15,000+ for weeks. Red here means: redeploy the zone worker.
    body, err = _fetch("/.well-known/mcp/server-card.json")
    if body is None:
        out.append(_check("ev_zone_worker", "zone worker serves canon", None,
                          "server-card unreachable: %s" % err, critical=True))
    else:
        try:
            from ai_surface_canon import PINNED
            want_tools = int(PINNED.get("tools_advertised") or 0)
            canon_floor = (PINNED.get("public") or {}).get("facilities") or ""
            m = re.search(r"(\d+)\s+tools", body)
            got_tools = int(m.group(1)) if m else None
            _v = _floor_verdict(body, canon_floor)
            stale = _v["retired"] or []
            problems = []
            if want_tools and got_tools is not None and got_tools != want_tools:
                problems.append("advertises %d tools (canon %d)"
                                % (got_tools, want_tools))
            if stale:
                problems.append("serves retired floor(s): %s"
                                % ", ".join(stale))
            if _v["retired"] is None:
                problems.append("canon floor unresolvable — cannot judge")
            elif canon_floor and not _v["found"] and not stale:
                # A floor rounds DOWN and may lag: PINNED itself or any
                # live-healed value inside the band counts as present. Exact
                # string equality reported "absent" for correct content.
                problems.append("no canon-family floor (accepts %s or a "
                                "live-healed value within 10%% above it)"
                                % canon_floor)
            out.append(_check(
                "ev_zone_worker", "zone worker serves canon", not problems,
                "in sync (%d tools)" % (got_tools or want_tools)
                if not problems else
                "STALE — %s. No workflow deploys worker.js and no guard "
                "watches it; redeploy the zone worker." % "; ".join(problems),
                critical=True))
        except Exception as e:  # noqa: BLE001
            out.append(_check("ev_zone_worker", "zone worker serves canon",
                              None, "canon compare failed: %s" % str(e)[:110],
                              critical=True))
    return out


# ── lane 3 · media growth ─────────────────────────────────────────────

def _lane_media_growth() -> list[dict]:
    out: list[dict] = []
    main_src = _read_repo("main.py") or ""
    cron_src = _read_repo(os.path.join("routes", "cron_heartbeat.py")) or ""
    wired = ("media_growth_master_shell" in main_src
             and "media-growth/master-tick" in cron_src)
    out.append(_check("mg_wired", "media-growth shell registered + ticked",
                      wired if (main_src and cron_src) else None,
                      "registration + cron tick present in source"
                      if wired else "NOT wired (registration or cron tick "
                      "missing)", critical=True))

    c = _db()
    if c is None:
        out.append(_check("mg_see", "SEE stage landing audience telemetry",
                          None, "no DB connection", critical=True))
        return out
    try:
        with c.cursor() as cur:
            # ★live schema (introspected 2026-07-25): the timestamp column is
            # created_at — NOT snap_date (the first tick guessed from a repo
            # DDL fragment and rendered '?'; the repo-vs-live drift class).
            row = _q1(cur, "SELECT count(*) FROM media_growth_snapshots")
            total = int(row[0]) if row else 0
            latest = _q1(cur, "SELECT created_at, li_followers, x_followers "
                              "FROM media_growth_snapshots "
                              "ORDER BY created_at DESC LIMIT 1")
            if not latest:
                out.append(_check(
                    "mg_see", "SEE stage landing audience telemetry", False,
                    "media_growth_snapshots is EMPTY — the shell ticks but "
                    "audience telemetry never lands (the audit's 'brain is "
                    "audience-blind' state)", critical=True))
            else:
                ts, li, x = latest
                age_d = ((datetime.datetime.now(datetime.timezone.utc) - ts)
                         .total_seconds() / 86400.0)
                blind = li is None and x is None
                out.append(_check(
                    "mg_see", "SEE stage landing audience telemetry",
                    (age_d <= 2) and not blind,
                    "%d rows, latest %.1fd old, li=%s x=%s"
                    % (total, age_d, li, x)
                    + (" — snapshots land but BOTH follower counts are NULL: "
                       "the collectors are blind, not the loop" if blind
                       else ("" if age_d <= 2
                             else " — STALE, snapshots stopped")),
                    critical=True))
    except Exception as e:  # noqa: BLE001
        out.append(_check("mg_see", "SEE stage landing audience telemetry",
                          None, "snapshot table unreadable: %s" % str(e)[:110],
                          critical=True))
    finally:
        try:
            c.close()
        except Exception:
            pass

    armed = (os.environ.get("MEDIA_GROWTH_ACT_ENABLED") or "").strip().lower() \
        in ("1", "true", "yes", "on")
    out.append(_check("mg_mode", "actuation mode", True,
                      "ARMED — strategy actions execute" if armed
                      else "SHADOW — records what it would do (dark by "
                      "design; arm via MEDIA_GROWTH_ACT_ENABLED=1)",
                      critical=False))
    return out


# ── lane 4 · self-learning ────────────────────────────────────────────

def _lane_self_learning() -> list[dict]:
    out: list[dict] = []
    c = _db()
    if c is None:
        return [_check("sl_db", "learning stores reachable", None,
                       "no DB connection", critical=True)]
    try:
        with c.cursor() as cur:
            try:
                row = _q1(cur, "SELECT count(*) FILTER (WHERE still_broken IS NOT NULL), "
                               "count(*) FILTER (WHERE still_broken IS TRUE) "
                               "FROM brain_fix_outcomes")
                checked, recid = int(row[0]), int(row[1])
                out.append(_check("sl_outcomes", "fix outcomes stamped",
                                  checked > 0,
                                  "%d checked, %d recidivist (fix didn't "
                                  "stick)" % (checked, recid), critical=True))
            except Exception as e:  # noqa: BLE001
                c.rollback()
                out.append(_check("sl_outcomes", "fix outcomes stamped", None,
                                  "brain_fix_outcomes unreadable: %s"
                                  % str(e)[:100], critical=True))
            try:
                from routes.brain_rag import LESSON_CORPORA
                cur.execute(
                    "SELECT count(*) FROM brain_corpus_embeddings "
                    "WHERE source_table = ANY(%s)",
                    (list(LESSON_CORPORA) + ["fix_history"],))
                lessons = int(cur.fetchone()[0])
                out.append(_check("sl_lessons", "lesson corpora embedded",
                                  lessons > 0, "%d lesson/fix-history "
                                  "embeddings" % lessons, critical=True))
            except Exception as e:  # noqa: BLE001
                c.rollback()
                out.append(_check("sl_lessons", "lesson corpora embedded",
                                  None, "unreadable: %s" % str(e)[:100],
                                  critical=True))
            try:
                # ★live schema: the stamp column is captured_at, not created_at
                # (introspected 2026-07-25 after the first tick rendered '?').
                row = _q1(cur, "SELECT count(*) FROM brain_pr_metric_snapshots "
                               "WHERE captured_at > now() - interval '8 days'")
                fresh = int(row[0])
                out.append(_check("sl_harness", "PR-metric harness beating",
                                  fresh > 0,
                                  "%d snapshot rows in 8d" % fresh
                                  if fresh else
                                  "no snapshots in 8d — harness dead or GH "
                                  "token blind", critical=True))
            except Exception as e:  # noqa: BLE001
                c.rollback()
                out.append(_check("sl_harness", "PR-metric harness beating",
                                  None, "unreadable: %s" % str(e)[:100],
                                  critical=True))
    finally:
        try:
            c.close()
        except Exception:
            pass

    # Are lessons actually RECALLED anywhere? Embeddings nobody retrieves are
    # write-only memory. Source census of retrieve_lessons()/retrieve_prior_fixes().
    consumers = 0
    try:
        rdir = os.path.join(_REPO_ROOT, "routes")
        for fn in os.listdir(rdir):
            if not fn.endswith(".py") or fn == "brain_rag.py":
                continue
            src = _read_repo(os.path.join("routes", fn)) or ""
            if "retrieve_lessons(" in src or "retrieve_prior_fixes(" in src:
                consumers += 1
        out.append(_check("sl_recall", "lesson recall consumers exist",
                          consumers > 0,
                          "%d route module(s) recall lessons" % consumers
                          if consumers else
                          "NOBODY calls retrieve_lessons/retrieve_prior_fixes "
                          "— lessons are write-only memory",
                          critical=False))
    except Exception as e:  # noqa: BLE001
        out.append(_check("sl_recall", "lesson recall consumers exist", None,
                          "census failed: %s" % str(e)[:100], critical=False))
    return out


# ── lane 5 · efficiency ───────────────────────────────────────────────

def _lane_efficiency() -> list[dict]:
    out: list[dict] = []
    c = _db()
    if c is None:
        out.append(_check("eff_usage", "LLM usage telemetry landing", None,
                          "no DB connection", critical=False))
    else:
        try:
            with c.cursor() as cur:
                row = _q1(cur, "SELECT count(*), coalesce(sum(input_tokens),0), "
                               "coalesce(sum(cache_read_tokens),0) "
                               "FROM brain_llm_usage "
                               "WHERE ts > now() - interval '7 days'")
                n, inp, cread = int(row[0]), int(row[1]), int(row[2])
                if n == 0:
                    out.append(_check("eff_usage", "LLM usage telemetry "
                                      "landing", None,
                                      "no rows yet (capture ships with this "
                                      "shell — first lane-driver tick "
                                      "populates)", critical=False))
                else:
                    denom = inp + cread
                    ratio = (100.0 * cread / denom) if denom else 0.0
                    out.append(_check("eff_usage", "LLM usage telemetry "
                                      "landing", True,
                                      "%d calls 7d" % n, critical=False))
                    out.append(_check("eff_cache", "prompt-cache hit rate",
                                      True,
                                      "%.0f%% of input tokens served from "
                                      "cache (%d cached / %d fresh)"
                                      % (ratio, cread, inp),
                                      critical=False))
        except Exception as e:  # noqa: BLE001
            try:
                c.rollback()
            except Exception:
                pass
            out.append(_check("eff_usage", "LLM usage telemetry landing",
                              None, "brain_llm_usage unreadable: %s (created "
                              "on first recorded call)" % str(e)[:80],
                              critical=False))
        finally:
            try:
                c.close()
            except Exception:
                pass

    wired = []
    for rel in _LLM_CALL_SITES:
        src = _read_repo(rel) or ""
        if "record_llm_usage" in src:
            wired.append(os.path.basename(rel))
    out.append(_check("eff_coverage", "usage capture coverage",
                      bool(wired),
                      "%d/%d call sites wired (%s)"
                      % (len(wired), len(_LLM_CALL_SITES),
                         ", ".join(wired) or "none"),
                      critical=False))

    try:
        from utils.anthropic_helper import gateway_active
        out.append(_check("eff_gateway", "AI gateway state", True,
                          "gateway active (edge analytics available; note: "
                          "fable leg bypasses it by design)"
                          if gateway_active() else
                          "direct-to-API (no gateway analytics)",
                          critical=False))
    except Exception as e:  # noqa: BLE001
        out.append(_check("eff_gateway", "AI gateway state", None,
                          "helper unavailable: %s" % str(e)[:80],
                          critical=False))
    return out


# ── dead-man beat ─────────────────────────────────────────────────────

def _beat_ledger(note: str, failing: bool = False) -> None:
    """Best-effort beat into the ingest_runs ledger. NEVER raises."""
    try:
        body = json.dumps({
            "feed": "intelligence-expansion-shell-daily",
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
        import requests as _rq   # not urllib (regression_lint)
        _rq.post("http://127.0.0.1:" + str(port)
                 + "/api/v1/admin/ingest-runs/beat",
                 data=body, timeout=5,
                 headers={"Content-Type": "application/json",
                          "User-Agent": "dchub-intel-expansion-shell/1.0",
                          "X-Admin-Key": admin_key})
    except Exception as e:  # noqa: BLE001
        logger.debug("[intel-expansion] ledger beat failed: %s", e)


# ── tick ──────────────────────────────────────────────────────────────

def _safe_lane(fn) -> list[dict]:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return [_check("lane_crash", "lane ran to completion", None,
                       "lane crashed: %s: %s" % (type(e).__name__,
                                                 str(e)[:120]),
                       critical=True)]


def _run_tick(beat: bool = True) -> dict:
    # ★2026-09-02 (D5): beat=False on every GET. A dashboard view — with its
    # auto-refresh — must never stamp the daily beat, or a browser tab keeps a
    # dead cron "alive" on /api/v1/ops/deadman. Only the POST master-tick beats.
    lanes = [
        {"id": "rag", "name": "1 · RAG retrieval",
         "checks": _safe_lane(_lane_rag)},
        {"id": "evidence", "name": "2 · evidence / self-healing",
         "checks": _safe_lane(_lane_evidence)},
        {"id": "media_growth", "name": "3 · media growth loop",
         "checks": _safe_lane(_lane_media_growth)},
        {"id": "self_learning", "name": "4 · self-learning",
         "checks": _safe_lane(_lane_self_learning)},
        {"id": "efficiency", "name": "5 · efficiency",
         "checks": _safe_lane(_lane_efficiency)},
    ]
    for ln in lanes:
        ln["verdict"] = _lane_verdict(ln["checks"])
    summary = " ".join("%s=%s" % (ln["id"], ln["verdict"]) for ln in lanes)
    out = {
        "ok": True,
        "shell": "intelligence-expansion-31",
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "lanes": lanes,
        "summary": summary,
        "any_fail": any(ln["verdict"] == "FAIL" for ln in lanes),
    }
    if beat:
        _beat_ledger("lanes: " + summary, failing=out["any_fail"])
    return out


def _no_store(resp):
    # ★CF caches admin GETs (30-min stale-board trap, brain-ascension
    # ee403b40) — every response from this shell is no-store.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@intelligence_expansion_master_shell_bp.route(
    "/api/v1/admin/intelligence-expansion/master-tick",
    methods=["GET", "POST"])
def master_tick():
    if _disabled():
        return _no_store(jsonify(
            ok=False, error="INTEL_EXPANSION_SHELL_DISABLE=1")), 404
    if not _admin_ok():
        return _no_store(jsonify(ok=False, error="admin key required")), 401
    return _no_store(jsonify(_run_tick(beat=(request.method == "POST"))))


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


@intelligence_expansion_master_shell_bp.route(
    "/admin/intelligence-expansion", methods=["GET"])
@intelligence_expansion_master_shell_bp.route(
    "/api/v1/admin/intelligence-expansion", methods=["GET"])
def dashboard():
    from flask import make_response
    if _disabled():
        return _no_store(make_response(
            "<h1>Intelligence Expansion</h1>"
            "<p>INTEL_EXPANSION_SHELL_DISABLE=1</p>", 404))
    if not _admin_ok():
        return _no_store(make_response(
            "<h1>401</h1><p>admin key required</p>", 401))
    t = _run_tick(beat=False)
    color = {"PASS": "#22c55e", "FAIL": "#ef4444", "?": "#eab308"}
    rows = []
    for ln in t["lanes"]:
        rows.append("<tr><td><b>%s</b></td><td style='color:%s'><b>%s</b></td>"
                    "<td>%s</td></tr>"
                    % (_esc(ln["name"]), color.get(ln["verdict"], "#eab308"),
                       _esc(ln["verdict"]),
                       "<br>".join(
                           "%s <i>%s</i> — %s"
                           % ({True: "✓", False: "✗"}.get(k["pass"], "?"),
                              _esc(k["name"]), _esc(k["detail"]))
                           for k in ln["checks"])))
    html = ("<html><head><title>Intelligence Expansion #31</title>"
            "<meta http-equiv='refresh' content='120'></head>"
            "<body style='font-family:system-ui;max-width:1150px;margin:24px auto'>"
            "<h1>Intelligence Expansion <small>#31</small></h1>"
            "<p>%s</p>"
            "<p><small>Five expansion fronts, measured from live state — DB, "
            "edge, source tree. A lane never reads PASS when it could not "
            "check.</small></p>"
            "<table cellpadding='8' style='border-collapse:collapse;width:100%%'>"
            "<tr><th align='left'>lane</th><th align='left'>verdict</th>"
            "<th align='left'>checks</th></tr>%s</table>"
            "<p><small>refreshes 120s · kill INTEL_EXPANSION_SHELL_DISABLE=1"
            "</small></p></body></html>"
            % (_esc(t["generated_at"]), "".join(rows)))
    return _no_store(make_response(html, 200))
