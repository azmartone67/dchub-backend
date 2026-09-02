"""routes/seven_levers_master_shell.py — Seven Levers Master Shell (#32, 2026-07-25).

WHY THIS EXISTS
===============
The 07-25 Intelligence Expansion tick (#31) turned vibes into numbers: the
zone worker served retired canon for weeks, 40% of verified fixes re-broke
with nothing consuming that signal, p99 latency was 10.8s and existed only in
logs the app can't read, prompt-cache hit-rate was 47% measured on one call
site, RAG's restored stage-2 had no quality meter, ~314 cron loops had no
dedup ledger, and the media loop was audience-half-blind on a one-word enum.

Those seven levers got seven fixes in this wave. This shell is the standing
instrument that keeps each lever MEASURED — because every one of them
degraded silently the first time, and a lever without a meter drifts back.

LANES (one per lever, ranked by the 07-25 leverage order)
  1 · zone_sync    the zone worker + MCP landing serve canon (tools + floor)
  2 · recidivism   re-broken fixes are counted AND the planner consumes them
  3 · perf         slow-request capture live; SLO within budget; worst paths
  4 · cache        usage telemetry landing; hit-rate; 7/7 call sites wired
  5 · rag_recall   anchor queries return confident results through stage-2
  6 · loops        cron/feed census + duplicate-family surfacing (list-only)
  7 · media        both follower collectors landing; goal gaps real

HOUSE RULES
  · A lane never reads PASS when it could not check — unreachable is '?'.
  · Read-only. Writes nothing but its own dead-man beat (L8). Actuation
    stays in the repaired components; retirement of loops stays HUMAN.
  · Fail-soft everywhere: a crashed lane renders '?' and never 500s.

Surface:  GET /admin/seven-levers                   (HTML)
          GET /api/v1/admin/seven-levers            (HTML)
          GET|POST /api/v1/admin/seven-levers/master-tick   (JSON)
Beat:     seven-levers-shell-daily
Kill:     SEVEN_LEVERS_SHELL_DISABLE=1
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

seven_levers_master_shell_bp = Blueprint("seven_levers_master_shell", __name__)

ORIGIN = (os.environ.get("SURFACE_TRUTH_ORIGIN") or "https://dchub.cloud").rstrip("/")
_UA = "dchub-seven-levers/1.0 (+https://dchub.cloud; internal-audit)"
# ★ 2026-08-31: the rotted range regex that lived here is gone. It banned
# 19,000-23,999, so a card carrying the live-healed 19,900+ was reported
# "retired" while the same bytes were accepted by surface_truth — and the
# exact-match canon check below then ALSO called canon "absent". One healthy
# surface, two contradictory failures. util/canon_floor.py is now the single
# rule; see its docstring for the four guards that disagreed.
from util.canon_floor import floor_verdict as _floor_verdict
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Lever 4: every brain call site that must feed brain_llm_usage.
_USAGE_SITES = (
    "routes/brain_lane_driver.py",
    "routes/brain_strategic_planner.py",
    "routes/brain_investigator.py",
    "routes/brain_feature_proposer.py",
    "routes/brain_answer_cache.py",
    "routes/analyst_note.py",
)

# Lever 5: retrieval anchors. Health probes, not curated goldens — each must
# return ≥1 result whose top cosine clears the provider's related_min gate.
# Chosen for topics the corpus provably covers (market narratives, findings,
# fix history, news). A dead index, dead embedder, or broken stage-2 fails
# ALL of them; a topically-thin day fails one, which is why the lane judges
# the set, not each anchor.
_RAG_ANCHORS = (
    "ERCOT grid headroom for data center interconnection",
    "Northern Virginia data center market power constraints",
    "hyperscaler acquisition of data center capacity",
    "fix for a stale dashboard that kept serving old numbers",
    "fiber route availability for a new data center site",
)


# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("SEVEN_LEVERS_SHELL_DISABLE") or "").strip() == "1"


# ── helpers (same contract as shells #30/#31) ─────────────────────────

def _check(cid: str, name: str, passed, detail: str,
           critical: bool = False) -> dict:
    return {"id": cid, "name": name, "pass": passed,
            "detail": detail, "critical": critical}


def _lane_verdict(checks: list[dict]) -> str:
    crits = [k for k in checks if k.get("critical")]
    if any(k["pass"] is False for k in checks):
        return "FAIL"
    if any(k["pass"] is None for k in crits):
        return "?"
    return "PASS"


def _db():
    try:
        from routes.brain_rag import _db as _rag_db
        return _rag_db()
    except Exception as e:  # noqa: BLE001
        logger.debug("[seven-levers] db unavailable: %s", e)
        return None


def _read_repo(rel: str):
    try:
        with open(os.path.join(_REPO_ROOT, rel), encoding="utf-8",
                  errors="ignore") as fh:
            return fh.read()
    except Exception:
        return None


def _fetch(path: str):
    """GET through the public edge with a cache-buster — admin/manifest GETs
    are zone-cached up to 3600s, and a cached body is exactly the stale-green
    this shell exists to kill. requests, not urllib (regression_lint)."""
    try:
        import time as _t
        import requests as _rq
        sep = "&" if "?" in path else "?"
        r = _rq.get(ORIGIN + path + sep + "cb=%d" % _t.time(),
                    headers={"User-Agent": _UA}, timeout=12)
        if r.status_code >= 400:
            return None, "HTTP %d" % r.status_code
        return r.text, None
    except Exception as e:  # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, str(e)[:110])


# ── lane 1 · zone sync ────────────────────────────────────────────────

def _lane_zone_sync() -> list[dict]:
    out: list[dict] = []
    try:
        from ai_surface_canon import PINNED
        want_tools = int(PINNED.get("tools_advertised") or 0)
        canon_floor = (PINNED.get("public") or {}).get("facilities") or ""
    except Exception as e:  # noqa: BLE001
        return [_check("zs_canon", "canon available", None,
                       "ai_surface_canon import failed: %s" % str(e)[:100],
                       critical=True)]
    # The CARD is the count-bearing surface: canon floor REQUIRED there.
    # /mcp (GET) is a status envelope that legitimately carries no floor —
    # it is scanned for stale floors only, plus its self-reported tool count.
    for cid, path, need_floor in (
            ("zs_card", "/.well-known/mcp/server-card.json", True),
            ("zs_landing", "/mcp", False)):
        body, err = _fetch(path)
        if body is None:
            out.append(_check(cid, path + " serves canon", None,
                              "unreachable: %s" % err, critical=True))
            continue
        v = _floor_verdict(body, canon_floor)
        stale = v["retired"] or []
        m = re.search(r"(\d+)\s+tools", body)
        got_tools = int(m.group(1)) if m else None
        problems = []
        if v["retired"] is None:
            problems.append("canon floor unresolvable — cannot judge")
        elif stale:
            problems.append("retired floor(s): %s" % ", ".join(stale))
        # A floor is a FLOOR: PINNED itself or any live-healed value inside the
        # band counts as present. The old exact `canon_floor not in body`
        # reported "absent" for a card that carried 19,900+ against a pin of
        # 18,500+ — correct content, failed on string equality.
        if need_floor and canon_floor and v["retired"] is not None \
                and not v["found"]:
            problems.append("no canon-family floor (accepts %s or a live-healed "
                            "value within 10%% above it)" % canon_floor)
        if want_tools and got_tools is not None and got_tools != want_tools:
            problems.append("advertises %d tools (canon %d)"
                            % (got_tools, want_tools))
        out.append(_check(cid, path + " serves canon", not problems,
                          "in sync" if not problems else "; ".join(problems),
                          critical=True))
        if path == "/mcp":
            # The envelope's "tools": N self-count vs canon — the manifest
            # chain has disagreed three ways before ([[manifests]] class).
            # 2026-07-26: envelope says 72 while manifest/card/canon say 79 —
            # somebody is wrong, and a disagreement must read RED until the
            # registry itself is recounted, not smoothed over.
            try:
                env_tools = json.loads(body).get("tools")
            except Exception:
                env_tools = None
            if isinstance(env_tools, int) and want_tools:
                out.append(_check(
                    "zs_envelope_tools", "/mcp envelope tool count vs canon",
                    env_tools == want_tools,
                    "envelope self-reports %d, canon %d%s"
                    % (env_tools, want_tools,
                       "" if env_tools == want_tools else
                       " — manifest-chain disagreement; recount the live "
                       "registry (tools/list) to settle which is lying"),
                    critical=False))
    return out


def _lane_registry_truth() -> list[dict]:
    """Registry listings are the top of the funnel — ~84% of new agents arrive
    as generic MCP clients, i.e. via directories. This lane is CRITICAL because
    the previous machinery reported healthy on a population it could not read:
    11 of 16 listings were unreadable (403/429/redirect-to-search) and every one
    recorded drift_detected=False. 'Could not check' is now its own verdict and
    never counts as clean."""
    try:
        from routes.registry_truth import read_state, UNVERIFIED_RED_DAYS
    except Exception as e:  # noqa: BLE001
        return [_check("rt_import", "registry-truth readable", None,
                       "import failed: %s" % str(e)[:100], critical=True)]
    st = read_state()
    if not st.get("ok"):
        return [_check("rt_db", "registry-truth readable", None,
                       "state unreadable: %s" % st.get("error"), critical=True)]
    counts = st.get("counts") or {}
    total = sum(counts.values())
    ok_n = counts.get("verified_ok", 0)
    out = [_check(
        "rt_broken", "no listing resolves to a non-DC-Hub page",
        not st.get("broken"),
        "all %d listings resolve to our page" % total if not st.get("broken")
        else ("BROKEN: %s — the tracked URL is wrong or the listing was "
              "removed; these are invisible arrivals" % ", ".join(st["broken"])),
        critical=True)]
    out.append(_check(
        "rt_unverified", "no listing unread for >%dd" % UNVERIFIED_RED_DAYS,
        not st.get("stale_unverified"),
        "every listing verified recently" if not st.get("stale_unverified")
        else ("UNREAD >%dd: %s — a listing we cannot read is NOT a listing we "
              "know is healthy" % (UNVERIFIED_RED_DAYS,
                                   ", ".join(st["stale_unverified"]))),
        critical=True))
    out.append(_check(
        "rt_coverage", "listing verdict coverage", True,
        "%d/%d verified_ok · %s" % (ok_n, total,
            ", ".join("%s=%d" % (k, v) for k, v in sorted(counts.items())
                      if k != "verified_ok") or "no other states"),
        critical=False))
    return out


def _lane_registry_acquisition() -> list[dict]:
    """Advisory, NOT critical. An empty submission queue is a perfectly good
    state, and a non-empty one is work-to-do rather than a defect — unlike
    registry_truth's broken/unread listings, which are real breakage."""
    try:
        from routes.registry_acquisition import read_queue
    except Exception as e:  # noqa: BLE001
        return [_check("ra_import", "acquisition queue readable", None,
                       "import failed: %s" % str(e)[:90], critical=False)]
    q = read_queue()
    if not q.get("ok"):
        return [_check("ra_db", "acquisition queue readable", None,
                       "unreadable: %s" % q.get("error"), critical=False)]
    counts = q.get("counts") or {}
    depth = q.get("queue_depth", 0)
    names = ", ".join(x["directory"] for x in (q.get("submission_queue") or [])[:6])
    return [
        _check("ra_queue", "directories we are absent from", True,
               ("none — every live candidate lists DC Hub" if not depth
                else "%d submittable: %s" % (depth, names)), critical=False),
        _check("ra_coverage", "candidate scan coverage", True,
               ", ".join("%s=%d" % (k, v) for k, v in sorted(counts.items()))
               or "never scanned", critical=False),
    ]


# ── lane 2 · recidivism ───────────────────────────────────────────────

def _lane_recidivism() -> list[dict]:
    out: list[dict] = []
    planner_src = _read_repo(os.path.join("routes",
                                          "brain_strategic_planner.py")) or ""
    wired = ("_read_recidivism" in planner_src
             and '"recidivism"' in planner_src)
    out.append(_check("rc_wired", "planner consumes recidivist clusters",
                      wired if planner_src else None,
                      "reader + ctx budget + prompt section present"
                      if wired else "NOT wired into the planner context",
                      critical=True))
    c = _db()
    if c is None:
        out.append(_check("rc_rate", "recidivism measured", None,
                          "no DB connection", critical=True))
        return out
    try:
        with c.cursor() as cur:
            # ★live schema: the stamp column is checked_at (the reconciler's
            # reconciled_at DDL never ran — table pre-existed, older shape).
            cur.execute(
                "SELECT count(*) FILTER (WHERE still_broken IS NOT NULL), "
                "count(*) FILTER (WHERE still_broken IS TRUE), "
                "count(*) FILTER (WHERE still_broken IS TRUE AND "
                "  checked_at > now() - interval '60 days') "
                "FROM brain_fix_outcomes")
            checked, recid, recent = [int(x) for x in cur.fetchone()]
        pct = (100.0 * recid / checked) if checked else 0.0
        out.append(_check("rc_rate", "recidivism measured", checked > 0,
                          "%d checked, %d recidivist (%.0f%%), %d in 60d — "
                          "the planner now sees the top clusters each tick"
                          % (checked, recid, pct, recent), critical=True))
    except Exception as e:  # noqa: BLE001
        out.append(_check("rc_rate", "recidivism measured", None,
                          "brain_fix_outcomes unreadable: %s" % str(e)[:100],
                          critical=True))
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


# ── lane 3 · perf ─────────────────────────────────────────────────────

def _lane_perf() -> list[dict]:
    out: list[dict] = []
    main_src = _read_repo("main.py") or ""
    wired = "perf_timing_bp" in main_src
    out.append(_check("pf_wired", "slow-request capture registered",
                      wired if main_src else None,
                      "perf_timing_bp registered" if wired
                      else "NOT registered in main.py", critical=True))

    body, err = _fetch("/api/v1/slo/error-budget")
    if body is None:
        out.append(_check("pf_slo", "SLO within budget", None,
                          "error-budget unreachable: %s" % err, critical=True))
    else:
        try:
            d = json.loads(body)
            verdict = d.get("verdict") or "unknown"
            out.append(_check("pf_slo", "SLO within budget",
                              verdict == "within_budget",
                              "verdict=%s err_pct=%s worst=%s"
                              % (verdict, d.get("global_err_pct"),
                                 ((d.get("top_5xx_paths") or [{}])[0]
                                  .get("pattern", "-"))),
                              critical=True))
        except Exception as e:  # noqa: BLE001
            out.append(_check("pf_slo", "SLO within budget", None,
                              "error-budget parse failed: %s" % str(e)[:80],
                              critical=True))

    c = _db()
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT count(*), max(dt_ms), "
                    "(ARRAY_AGG(path ORDER BY dt_ms DESC))[1] "
                    "FROM slow_requests WHERE ts > now() - interval '24 hours'")
                n, worst_ms, worst_path = cur.fetchone()
                n = int(n or 0)
                out.append(_check(
                    "pf_slow", "slow requests (>2s) in 24h", True,
                    "none captured" if not n else
                    "%d captured, worst %sms on %s"
                    % (n, worst_ms, worst_path), critical=False))
        except Exception as e:  # noqa: BLE001
            try:
                c.rollback()
            except Exception:
                pass
            out.append(_check("pf_slow", "slow requests (>2s) in 24h", None,
                              "slow_requests unreadable: %s (created on "
                              "first slow capture)" % str(e)[:70],
                              critical=False))
        finally:
            try:
                c.close()
            except Exception:
                pass
    return out


# ── lane 4 · cache ────────────────────────────────────────────────────

def _lane_cache() -> list[dict]:
    out: list[dict] = []
    wired = []
    for rel in _USAGE_SITES:
        if "record_llm_usage" in (_read_repo(rel) or ""):
            wired.append(os.path.basename(rel).replace(".py", ""))
    out.append(_check("ca_coverage", "usage capture on all call sites",
                      len(wired) == len(_USAGE_SITES),
                      "%d/%d wired (%s)" % (len(wired), len(_USAGE_SITES),
                                            ", ".join(wired) or "none"),
                      critical=True))
    c = _db()
    if c is None:
        out.append(_check("ca_rate", "cache hit-rate measured", None,
                          "no DB connection", critical=False))
        return out
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT count(*), count(DISTINCT component), "
                "coalesce(sum(input_tokens),0), "
                "coalesce(sum(cache_read_tokens),0) "
                "FROM brain_llm_usage WHERE ts > now() - interval '7 days'")
            n, comps, inp, cread = cur.fetchone()
            n, comps, inp, cread = int(n), int(comps), int(inp), int(cread)
        if n == 0:
            out.append(_check("ca_rate", "cache hit-rate measured", None,
                              "no usage rows in 7d", critical=False))
        else:
            denom = inp + cread
            ratio = (100.0 * cread / denom) if denom else 0.0
            out.append(_check("ca_rate", "cache hit-rate measured", True,
                              "%.0f%% cached (%d calls, %d components "
                              "reporting)" % (ratio, n, comps),
                              critical=False))
    except Exception as e:  # noqa: BLE001
        out.append(_check("ca_rate", "cache hit-rate measured", None,
                          "brain_llm_usage unreadable: %s" % str(e)[:80],
                          critical=False))
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


# ── lane 5 · rag recall ───────────────────────────────────────────────

def _lane_rag_recall() -> list[dict]:
    try:
        from routes.brain_rag import retrieve_context, cosine_gate
    except Exception as e:  # noqa: BLE001
        return [_check("rr_import", "brain_rag importable", None,
                       "import failed: %s" % str(e)[:100], critical=True)]
    gate = None
    try:
        gate = float(cosine_gate("related_min"))
    except Exception:
        pass
    if gate is None:
        return [_check("rr_gate", "provider gate available", None,
                       "cosine_gate unavailable", critical=True)]
    hits, details = 0, []
    for q in _RAG_ANCHORS:
        try:
            res = retrieve_context(q, k=6) or []
            top = res[0] if res else None
            top_cos = float(top.get("cosine") or 0.0) if top else 0.0
            ok = bool(res) and top_cos >= gate
            hits += 1 if ok else 0
            details.append("%s『%s…』top=%.2f/%s"
                           % ("✓" if ok else "✗", q[:34], top_cos,
                              (top or {}).get("source_table", "-")))
        except Exception as e:  # noqa: BLE001
            details.append("✗『%s…』error %s" % (q[:34], type(e).__name__))
    passed = hits >= max(1, len(_RAG_ANCHORS) - 1)   # tolerate one thin topic
    return [_check("rr_anchors",
                   "anchor retrieval confident (gate %.2f)" % gate,
                   passed,
                   "%d/%d anchors clear the gate · %s"
                   % (hits, len(_RAG_ANCHORS), " | ".join(details)),
                   critical=True)]


# ── lane 6 · loops ────────────────────────────────────────────────────

def _norm_feed(name: str) -> str:
    n = (name or "").lower()
    for suf in ("-daily", "-weekly", "-hourly", "_daily", "_weekly",
                "-v2", "_v2", "-sync", "_sync"):
        if n.endswith(suf):
            n = n[: -len(suf)]
    return n


def _lane_loops() -> list[dict]:
    out: list[dict] = []
    try:
        wf = len([f for f in os.listdir(
            os.path.join(_REPO_ROOT, ".github", "workflows"))
            if f.endswith((".yml", ".yaml"))])
    except Exception:
        wf = None
    hb_src = _read_repo(os.path.join("routes", "cron_heartbeat.py")) or ""
    hb = hb_src.count('("') if hb_src else None
    feeds, dupes = None, []
    c = _db()
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("SELECT feed FROM ingest_runs")
                names = [r[0] for r in cur.fetchall()]
            feeds = len(names)
            fam = {}
            for n in names:
                fam.setdefault(_norm_feed(n), []).append(n)
            dupes = sorted(["%s (%s)" % (k, ", ".join(v))
                            for k, v in fam.items() if len(v) > 1])
        except Exception:
            pass
        finally:
            try:
                c.close()
            except Exception:
                pass
    census_ok = (wf is not None and hb is not None and feeds is not None)
    out.append(_check(
        "lp_census", "loop census computed",
        True if census_ok else None,
        "%s workflows · ~%s heartbeat entries · %s ledger feeds"
        % (wf, hb, feeds), critical=True))
    out.append(_check(
        "lp_dupes", "duplicate feed families surfaced", True,
        ("none" if not dupes else
         "%d families share a normalized name: %s — RETIREMENT IS HUMAN "
         "(the eia landmine: several look-alikes are deliberately distinct)"
         % (len(dupes), "; ".join(dupes[:6]))),
        critical=False))
    return out


# ── lane 7 · media followers ──────────────────────────────────────────

def _lane_media() -> list[dict]:
    out: list[dict] = []
    src = _read_repo("linkedin_poster.py") or ""
    enum_ok = ("COMPANY_FOLLOWED_BY_MEMBER" in src
               and "edgeType=CompanyFollowedByMember" not in src)
    out.append(_check("md_enum", "LI follower call uses the valid enum",
                      enum_ok if src else None,
                      "COMPANY_FOLLOWED_BY_MEMBER" if enum_ok else
                      "stale CompanyFollowedByMember enum still in source",
                      critical=True))
    c = _db()
    if c is None:
        out.append(_check("md_landing", "both follower collectors landing",
                          None, "no DB connection", critical=True))
        return out
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (platform) platform, snap_date, followers
                  FROM social_audience
                 WHERE platform IN ('linkedin', 'x')
                 ORDER BY platform, snap_date DESC""")
            rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        today = datetime.datetime.now(datetime.timezone.utc).date()
        probs, parts = [], []
        for p in ("linkedin", "x"):
            d, f = rows.get(p, (None, None))
            fresh = d is not None and (today - d).days <= 2
            if not fresh:
                probs.append("%s: no snapshot ≤2d" % p)
            elif f is None:
                probs.append("%s: snapshot lands but followers NULL "
                             "(collector blind)" % p)
            else:
                parts.append("%s=%d" % (p, int(f)))
        out.append(_check("md_landing", "both follower collectors landing",
                          not probs,
                          ", ".join(parts) if not probs
                          else "; ".join(probs + parts), critical=True))
    except Exception as e:  # noqa: BLE001
        out.append(_check("md_landing", "both follower collectors landing",
                          None, "social_audience unreadable: %s"
                          % str(e)[:90], critical=True))
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


# ── dead-man beat ─────────────────────────────────────────────────────

def _beat_ledger(note: str, failing: bool = False) -> None:
    try:
        body = json.dumps({
            "feed": "seven-levers-shell-daily",
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
                          "User-Agent": "dchub-seven-levers-shell/1.0",
                          "X-Admin-Key": admin_key})
    except Exception as e:  # noqa: BLE001
        logger.debug("[seven-levers] ledger beat failed: %s", e)


# ── tick ──────────────────────────────────────────────────────────────

def _safe_lane(fn) -> list[dict]:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return [_check("lane_crash", "lane ran to completion", None,
                       "lane crashed: %s: %s"
                       % (type(e).__name__, str(e)[:120]), critical=True)]


def _run_tick(beat: bool = True) -> dict:
    # ★2026-09-02 (D5): beat=False on every GET. A dashboard view — with its
    # auto-refresh — must never stamp the daily beat, or a browser tab keeps a
    # dead cron "alive" on /api/v1/ops/deadman. Only the POST master-tick beats.
    lanes = [
        {"id": "zone_sync", "name": "1 · zone worker sync",
         "checks": _safe_lane(_lane_zone_sync)},
        {"id": "registry_truth", "name": "1b · registry listings (top of funnel)",
         "checks": _safe_lane(_lane_registry_truth)},
        {"id": "registry_acquisition", "name": "1c · directory acquisition queue",
         "checks": _safe_lane(_lane_registry_acquisition)},
        {"id": "recidivism", "name": "2 · recidivism loop",
         "checks": _safe_lane(_lane_recidivism)},
        {"id": "perf", "name": "3 · performance tail",
         "checks": _safe_lane(_lane_perf)},
        {"id": "cache", "name": "4 · cache efficiency",
         "checks": _safe_lane(_lane_cache)},
        {"id": "rag_recall", "name": "5 · RAG recall anchors",
         "checks": _safe_lane(_lane_rag_recall)},
        {"id": "loops", "name": "6 · loop census",
         "checks": _safe_lane(_lane_loops)},
        {"id": "media", "name": "7 · media followers",
         "checks": _safe_lane(_lane_media)},
    ]
    for ln in lanes:
        ln["verdict"] = _lane_verdict(ln["checks"])
    summary = " ".join("%s=%s" % (ln["id"], ln["verdict"]) for ln in lanes)
    out = {
        "ok": True,
        "shell": "seven-levers-32",
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "lanes": lanes,
        "summary": summary,
        "any_fail": any(ln["verdict"] == "FAIL" for ln in lanes),
    }
    if beat:
        _beat_ledger("lanes: " + summary, failing=out["any_fail"])
    return out


def _no_store(resp):
    # ★CF caches admin GETs (30-min stale-board trap) — always no-store.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@seven_levers_master_shell_bp.route("/api/v1/admin/seven-levers/master-tick",
                                    methods=["GET", "POST"])
def master_tick():
    if _disabled():
        return _no_store(jsonify(
            ok=False, error="SEVEN_LEVERS_SHELL_DISABLE=1")), 404
    if not _admin_ok():
        return _no_store(jsonify(ok=False, error="admin key required")), 401
    return _no_store(jsonify(_run_tick(beat=(request.method == "POST"))))


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


@seven_levers_master_shell_bp.route("/admin/seven-levers", methods=["GET"])
@seven_levers_master_shell_bp.route("/api/v1/admin/seven-levers",
                                    methods=["GET"])
def dashboard():
    from flask import make_response
    if _disabled():
        return _no_store(make_response(
            "<h1>Seven Levers</h1><p>SEVEN_LEVERS_SHELL_DISABLE=1</p>", 404))
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
    html = ("<html><head><title>Seven Levers #32</title>"
            "<meta http-equiv='refresh' content='120'></head>"
            "<body style='font-family:system-ui;max-width:1150px;margin:24px auto'>"
            "<h1>Seven Levers <small>#32</small></h1>"
            "<p>%s</p>"
            "<p><small>One lane per 07-25 leverage rank. Measured from live "
            "state; a lane never reads PASS when it could not check.</small></p>"
            "<table cellpadding='8' style='border-collapse:collapse;width:100%%'>"
            "<tr><th align='left'>lane</th><th align='left'>verdict</th>"
            "<th align='left'>checks</th></tr>%s</table>"
            "<p><small>refreshes 120s · kill SEVEN_LEVERS_SHELL_DISABLE=1"
            "</small></p></body></html>"
            % (_esc(t["generated_at"]), "".join(rows)))
    return _no_store(make_response(html, 200))
