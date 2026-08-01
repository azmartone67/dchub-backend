"""
routes/agreement_master_shell.py — Agreement Master Shell (#37, 2026-07-27).

Every defect the Graph-Spine wave (#36) turned up had one shape: **two things
that should agree, silently not agreeing, and nothing checking.**

  · the `deals` table was de-duped by data_flag; the RAG registry never learned
    → 2,811 chunks, incl. unit-garbage NER fragments, publicly searchable
  · `press_releases` had the identical bug two days earlier
  · `_is_real_entity` disagreed with the facilities table about what a real
    company is, and `purge-noise` would have enforced the wrong side
    DESTRUCTIVELY across every row (it rejected Google, Apple, Huawei)
  · `network_ix_ingestion` was orphaned four months — its only "wiring" was a
    docstring
  · four separate lanes I wrote asserted a floor the audited code was
    forbidden to reach

None of that is missing capability. Coverage is 15,000+ facilities, 500k assets,
80 tools, 36 shells. The constraint is AGREEMENT. And since the entire pitch is
"live, cited ground truth for agents", every one of those is a hit on the exact
thing we sell.

Five lanes, each a place where two representations of one fact can drift:

  1. DESTRUCTIVE SURFACE — statements that mutate a SET rather than a row.
  2. PREDICATE TRIPWIRES — exclusion flags whose readers assume a shape the
     data is not constrained to keep.
  3. PUBLIC CORPUS GATES — anything in PUBLIC_CORPORA is retrievable on the
     UNAUTHENTICATED /api/v1/rag/search; a publication-state column the
     registry ignores is a leak. Bitten twice.
  4. INTENT INSTRUMENTATION — we cannot answer "do callers ask global
     questions?" because we never recorded it. See
     reference_dchub_global_question_demand.
  5. SHELL HYGIENE — the shells ARE the trust instrument. A shell that returns
     5xx when disabled trips the CF worker's failover to the stale Render
     origin; a lane that asserts an unreachable floor is worse than no lane.

★ MIXED shell: lanes 1/3/5 introspect our own SOURCE (read-only, cached once
per process — the tree cannot change under a running deploy), lanes 2/4 are
pure Neon reads. No outbound requests. Every lane names an actuator and fires
NOTHING. Only write is one snapshot row.

★ WHAT THIS SHELL DELIBERATELY DOES NOT DO — and why it matters:
lane 1 does NOT report "endpoints never invoked". That was the original design,
and `api_endpoint_log` killed it: it captured 0 of ~50 admin reindex calls and
0 graph-spine ticks (56 admin rows in 548k). Building on it would have branded
~290 endpoints "never invoked" with total confidence — the precise failure this
shell exists to catch. The instrument was tested before it was trusted, it
failed, and the lane was rebuilt on a statically decidable property instead.
The observability GAP is reported as a gauge (check 1c) rather than papered
over.

Endpoints:
  GET/POST /api/v1/admin/agreement/master-tick   JSON scoreboard (5 lanes)
  GET      /admin/agreement                       HTML dashboard
  GET      /api/v1/admin/agreement                CF zone-worker bypass alias

Auth: X-Admin-Key / ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY). Kill: AGREEMENT_SHELL_DISABLE=1
"""
from __future__ import annotations

import ast
import glob
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

from util.deals import deals_quarantined

logger = logging.getLogger(__name__)

agreement_master_shell_bp = Blueprint("agreement_master_shell", __name__)

_ROUTES_DIR = os.path.dirname(os.path.abspath(__file__))


# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("AGREEMENT_SHELL_DISABLE") or "").strip() == "1"


# ── db helpers (same contract as graph_spine_master_shell) ────────────

def _connect(url):
    if not url:
        return None
    try:
        import psycopg2 as _pg
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[agreement] db connect failed: %s", e)
        return None


def _conn():
    """READ connection — replica preferred (all reads, some whole-table)."""
    return _connect(os.environ.get("NEON_REPLICA_URL")
                    or os.environ.get("DATABASE_URL")
                    or os.environ.get("NEON_DATABASE_URL"))


def _write_conn():
    """Snapshot writes ONLY, and never the replica — the replica rejects
    writes, the failure is swallowed fail-soft, and the trend table stays
    empty while every tick reports success."""
    return _connect(os.environ.get("DATABASE_URL")
                    or os.environ.get("NEON_DATABASE_URL"))


def _row(c, sql: str):
    """Fail-soft single row; None on error, NEVER a zero-filled tuple —
    a probe must distinguish "query broke" from "count is zero"."""
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()
    except Exception as e:
        logger.debug("[agreement] row failed: %s -- %s", sql[:90], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _pct(part, whole):
    try:
        p, w = float(part), float(whole)
    except (TypeError, ValueError):
        return None
    return round(p * 100.0 / w, 1) if w else None


def _check(cid, name, passed, detail, critical=False) -> dict:
    return {"id": cid, "name": name, "pass": passed,
            "detail": (detail or "")[:340], "critical": critical}


def _lane_verdict(checks):
    if any(ch["pass"] is None and ch.get("critical") for ch in checks):
        return None
    decided = [ch for ch in checks if ch["pass"] is not None]
    if not decided:
        return None
    return all(ch["pass"] for ch in decided)


# ── source introspection (computed ONCE per process) ──────────────────
# The source tree cannot change under a running deploy, so a full AST scan of
# routes/*.py (~669 files, ~2s) is paid once and cached. Never re-scan per
# tick — that would put 2s of CPU on an admin page refresh.

_SCAN_LOCK = threading.Lock()
_SCAN: dict | None = None

_MUTATES = re.compile(r"^\s*(DELETE\s+FROM|UPDATE\s+|TRUNCATE\s+)", re.I)


def _is_route(dec) -> bool:
    """True when a decorator is a Flask route registration."""
    try:
        return "route(" in ast.unparse(dec)
    except Exception:
        return False


def _static_sql(node):
    """Reconstruct a statement from a literal / concat / f-string. None when
    the SQL is assembled dynamically (those are counted, never judged —
    guessing at a query we cannot see is how a lane starts lying)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str)
                       else " ? " for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _static_sql(node.left), _static_sql(node.right)
        if left is None and right is None:
            return None
        return (left or "") + (right or "")
    return None


def _scan_routes() -> dict:
    """One pass over routes/*.py feeding lanes 1 and 5."""
    out = {"setwide": [], "scoped": 0, "dynamic": 0, "handlers": 0,
           "shells": 0, "shells_5xx": [], "shells_nokill": [], "files": 0,
           "scan_ms": 0}
    t0 = time.time()
    for path in sorted(glob.glob(os.path.join(_ROUTES_DIR, "*.py"))):
        base = os.path.basename(path)
        try:
            src = open(path, encoding="utf-8").read()
            tree = ast.parse(src)
        except Exception:
            continue
        out["files"] += 1

        # A file is only a SHELL if it actually serves something. `_brand_shell`
        # is an HTML template helper with ZERO routes and matched the filename
        # glob — the first cut of lane 5 reported it as "no kill switch", which
        # is meaningless for a module you cannot call. Route count, not name.
        route_fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                     and any(_is_route(d) for d in n.decorator_list)]

        # ── lane 5: shell hygiene ──
        if ("master_shell" in base or base.endswith("_shell.py")) and route_fns:
            out["shells"] += 1
            if re.search(r"error=[\"']disabled[\"']\s*\)\s*,\s*5\d\d", src):
                out["shells_5xx"].append(base)
            if "DISABLE" not in src and "_disabled()" not in src:
                out["shells_nokill"].append(base)

        # ── lane 1: set-wide mutations inside route handlers ──
        for fn in route_fns:
            out["handlers"] += 1
            for call in ast.walk(fn):
                if not (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "execute" and call.args):
                    continue
                sql = _static_sql(call.args[0])
                if sql is None:
                    out["dynamic"] += 1
                    continue
                stmt = " ".join(sql.split())
                if not _MUTATES.match(stmt):
                    continue
                # A bound parameter means the statement is scoped to whatever
                # the caller identified. No placeholder anywhere => it acts on
                # a SET the caller never named.
                if len(call.args) > 1 or "%s" in stmt or "%(" in stmt:
                    out["scoped"] += 1
                else:
                    out["setwide"].append(
                        {"file": base, "handler": fn.name, "sql": stmt[:120]})
    out["scan_ms"] = int((time.time() - t0) * 1000)
    return out


def _scan() -> dict:
    global _SCAN
    with _SCAN_LOCK:
        if _SCAN is None:
            try:
                _SCAN = _scan_routes()
            except Exception as e:
                logger.warning("[agreement] source scan failed: %s", e)
                _SCAN = {"error": str(e)[:200]}
        return _SCAN


# ── lane 1 · destructive surface ──────────────────────────────────────

def _lane_destructive(c, ctx) -> list[dict]:
    out = []
    s = _scan()
    if s.get("error"):
        out.append(_check("ds_setwide", "no unreviewed set-wide mutation",
                          None, f"source scan failed: {s['error']}", critical=True))
        return out

    setwide = s["setwide"]
    ctx["setwide_n"] = len(setwide)

    # 1a — the reviewed baseline. 18 statements across 10 handlers on
    # 2026-07-27, each inspected by hand: bulk deal purges, dedup_undo
    # (reverts the WHOLE fleet), geo_undo, news date clamps, a market
    # backfill, TTL cleanups. The assertion is NOT "zero set-wide statements"
    # — several are legitimate — it is "no NEW one appeared unreviewed".
    # A count-only floor of 0 would be unreachable and therefore useless.
    baseline = 18
    out.append(_check(
        "ds_setwide", f"no unreviewed set-wide mutation (baseline {baseline})",
        len(setwide) <= baseline,
        f"{len(setwide)} statements mutate a SET with no bound parameter, "
        f"across {len({(x['file'], x['handler']) for x in setwide})} route "
        f"handlers · {s['scoped']} scoped · {s['dynamic']} assembled "
        "dynamically (not judged)", critical=True))

    # 1b — the worst offenders, named on the page so a reviewer sees WHICH.
    worst = sorted({(x["file"], x["handler"]) for x in setwide})[:6]
    out.append(_check(
        "ds_named", "set-wide handlers are named, not just counted", None,
        " · ".join(f"{f.replace('.py','')}:{h}" for f, h in worst) or "none"))

    # 1c — ★THE HONEST GAP. We cannot say which of these has ever RUN.
    # api_endpoint_log captured 0 of ~50 admin reindex calls and 0 graph-spine
    # ticks on 2026-07-27 (56 admin rows in 548k). purge-noise proved the
    # hazard class: catastrophically wrong predicate, never fired, pure luck.
    # ★ Now a real LIVENESS assertion, not a gauge (r-admin-observability
    # 2026-07-27). The tracker used to record a request only if it carried an
    # X-API-Key, and admin calls authenticate with X-Admin-Key — so admin
    # traffic was invisible and this check could only describe the hole.
    # api_usage_tracker now stamps a credential CLASS ('admin'/'cron'/
    # 'internal', never the secret), so the hole is closed and the check can
    # assert.
    #
    # It is satisfied by THIS SHELL'S OWN TICK, which is itself an admin call —
    # deliberately. That is not circular: if the tracker regresses, the tick
    # stops being logged and the check goes red. It is a genuine
    # can-we-still-see-ourselves probe, and the cheapest possible one.
    r = _row(c, "SELECT count(*) FROM api_endpoint_log"
                " WHERE api_key_prefix IN ('admin','cron','internal')"
                "   AND called_at > now() - interval '7 days'")
    if r is None or r[0] is None:
        out.append(_check("ds_observability", "admin traffic is observable",
                          None, "query failed", critical=True))
    else:
        seen = int(r[0])
        out.append(_check(
            "ds_observability", "admin traffic is observable",
            seen > 0,
            f"{seen:,} admin/cron/internal calls logged in 7d. While this is "
            "0, 'which destructive endpoint has never run' is UNMEASURABLE and "
            "no lane may claim an endpoint is dead — the state that let "
            "purge-noise sit armed and unnoticed", critical=True))
    return out


# ── lane 2 · predicate tripwires ──────────────────────────────────────

def _lane_predicates(c, ctx) -> list[dict]:
    """Exclusion flags whose readers assume a shape the schema does not
    enforce. `is_duplicate` is the live example: 134 call sites, 79 of them
    using the NULL-unsafe bare `is_duplicate = 0`. That is SAFE TODAY only
    because the column happens to hold no NULLs — verified, not assumed. The
    day one writer leaves a NULL, 79 sites silently start serving duplicates
    out of a table that is 75% duplicates."""
    out = []

    # ★ ASSERT THE GUARANTEE, NOT TODAY'S DATA (r-isdup-notnull 2026-07-27).
    # v1 checked "0 NULLs right now", which was true by LUCK: nothing in the
    # schema stopped a writer from inserting one, and 79 call sites use the
    # bare `is_duplicate = 0` form that silently drops NULL rows out of a table
    # that is 75% duplicates. The column is now NOT NULL DEFAULT 0 on live, so
    # the check moved up a level — a data check can pass while the invariant is
    # one careless writer away; a schema check cannot.
    # (With NOT NULL enforced the bare form is CORRECT, so the 79 call sites
    # need no churn — COALESCE there is now redundant, not load-bearing.)
    r = _row(c, "SELECT is_nullable, coalesce(column_default,'')"
                " FROM information_schema.columns"
                " WHERE table_name = 'discovered_facilities'"
                "   AND column_name = 'is_duplicate'")
    if not r:
        out.append(_check("pt_isdup_null", "is_duplicate is NOT NULL in the schema",
                          None, "column not found", critical=True))
    else:
        nullable, default = str(r[0]), str(r[1])
        ok = nullable == "NO" and default.startswith("0")
        out.append(_check(
            "pt_isdup_null", "is_duplicate is NOT NULL in the schema",
            ok,
            f"is_nullable={nullable} default={default or '(none)'} — the fleet "
            "filter is enforced by the SCHEMA, so the 79 bare "
            "`is_duplicate = 0` call sites cannot silently drop rows",
            critical=True))

    r = _row(c, "SELECT count(*), count(*) FILTER (WHERE is_duplicate = 0)"
                " FROM discovered_facilities")
    if r:
        total, live = int(r[0] or 0), int(r[1] or 0)
        out.append(_check(
            "pt_isdup_share", "fleet filter share", None,
            f"{live:,} of {total:,} rows pass the fleet filter — "
            f"{_pct(total - live, total)}% are duplicates, which is why a NULL "
            "leaking into this column would be so expensive"))

    # data_flag — the quarantine that the RAG registry failed to learn.
    r = _row(c, "SELECT count(*), count(*) FILTER (WHERE "
                + deals_quarantined() + ") FROM deals")
    if r:
        total, q = int(r[0] or 0), int(r[1] or 0)
        out.append(_check(
            "pt_dataflag", "deals quarantine is load-bearing", None,
            f"{q:,} of {total:,} rows ({_pct(q, total)}%) are quarantined — any "
            "COUNT(*) over `deals` that omits this filter measures history, "
            "not what we serve"))

    # in_facilities vs status='rejected' — the purge-noise landmine invariant.
    r = _row(c, "SELECT count(*) FROM news_discovered_entities"
                " WHERE in_facilities = TRUE AND COALESCE(status,'') = 'rejected'")
    if r is None or r[0] is None:
        out.append(_check("pt_reject_real", "no resolved entity is marked rejected",
                          None, "query failed", critical=True))
    else:
        bad = int(r[0])
        out.append(_check(
            "pt_reject_real", "no resolved entity is marked rejected",
            bad == 0,
            f"{bad:,} entities resolve to a facility AND carry "
            "status='rejected' — ground truth must beat the heuristic",
            critical=True))
    return out


# ── lane 3 · public corpus gates ──────────────────────────────────────

def _lane_public_gates(c, ctx) -> list[dict]:
    """Anything in PUBLIC_CORPORA is retrievable, with a citation stamp, on the
    UNAUTHENTICATED /api/v1/rag/search. If a corpus table carries a
    PUBLICATION-state column and the registry `where` ignores it, unpublished
    rows are public. Bitten twice: press_releases (07-25), deals (07-27).

    ★ The registry is IMPORTED, never restated — a copy here would drift from
    the thing it audits, which is the whole failure this shell is about.
    ★ A project-lifecycle column is NOT a publication gate: construction_permits
    and capacity_pipeline carry status = announced/under_construction/planned,
    which describes the WORLD, not our editorial state. Treating those as
    leaks would be a false alarm, so the check looks only for genuine
    publication vocabulary."""
    out = []
    try:
        from routes.brain_rag import CORPORA, PUBLIC_CORPORA
    except Exception as e:
        out.append(_check("pg_gates", "public corpora gate their unpublished rows",
                          None, f"could not import the registry: {str(e)[:90]}",
                          critical=True))
        return out

    # ★ EDITORIAL vocabulary only, and NEVER a bare `status`.
    #   · `published_at` / `published_date` record WHEN something happened, not
    #     whether we may show it. Matching on the substring "published" flags
    #     news_articles and announcements as leaks — the first cut of this lane
    #     did exactly that. Filtering by data_type does NOT save you either:
    #     both columns are TEXT on live (the LIVE-≠-repo-DDL class), so the
    #     discriminator has to be the NAME SUFFIX.
    #   · bare `status` is excluded deliberately. On construction_permits and
    #     capacity_pipeline it holds announced / under_construction / planned —
    #     that describes the WORLD, not our editorial state, and asserting on
    #     it would be a permanent false alarm. `row_status` and `data_flag` are
    #     unambiguous, so those stay.
    PUB_WORDS = ("published", "is_published", "draft", "approved", "row_status",
                 "data_flag", "is_duplicate", "visibility", "hidden")

    def _is_gate_col(col: str) -> bool:
        col = col.strip().lower()
        if col.endswith("_at") or col.endswith("_date"):
            return False
        return any(w in col for w in PUB_WORDS)

    ungated = []
    checked = 0
    for name in PUBLIC_CORPORA:
        spec = CORPORA.get(name)
        if not spec:
            continue
        where = str(spec.get("where") or "")
        checked += 1
        cols = _row(c, "SELECT string_agg(column_name, ',') FROM"
                       " information_schema.columns WHERE table_name = "
                       "'" + name.replace("'", "''") + "'")
        gate_cols = [x for x in ((cols[0] or "") if cols else "").split(",")
                     if _is_gate_col(x)]
        gated = any(w in where for w in PUB_WORDS)
        if gate_cols and not gated:
            ungated.append(f"{name}({gate_cols[0].strip()})")

    ctx["ungated"] = len(ungated)
    out.append(_check(
        "pg_gates", "public corpora gate their unpublished rows",
        len(ungated) == 0,
        f"{len(ungated)} of {checked} PUBLIC_CORPORA carry a publication-state "
        f"column their registry `where` ignores{': ' + ', '.join(ungated) if ungated else ''}"
        " — anything here is retrievable unauthenticated", critical=True))

    out.append(_check(
        "pg_size", "public corpus inventory", None,
        f"{len(PUBLIC_CORPORA)} corpora are public: "
        + ", ".join(sorted(PUBLIC_CORPORA))[:190]))
    return out


# ── lane 4 · intent instrumentation ───────────────────────────────────

def _lane_intent(c, ctx) -> list[dict]:
    """We could not answer "do callers ask GLOBAL questions?" — the only thing
    a knowledge graph beats vector RAG at — because we never recorded it. The
    2026-07-27 read had to reverse-engineer intent from raw params and was
    under-powered anyway (every free-text front door was 2-24 days old).
    This lane exists so the next read is a lookup."""
    out = []

    r = _row(c, "SELECT count(*), count(*) FILTER (WHERE params ? 'question_class')"
                " FROM mcp_call_log WHERE timestamp > now() - interval '30 days'")
    if not r:
        out.append(_check("in_instrument", "free-text intent is classified at the call site",
                          None, "query failed", critical=True))
    else:
        total, classed = int(r[0] or 0), int(r[1] or 0)
        ctx["intent_classed"] = classed
        out.append(_check(
            "in_instrument", "free-text intent is classified at the call site",
            classed > 0,
            f"{classed:,} of {total:,} calls in 30d carry a question_class — "
            "without it, 'is there global-question demand?' can only be "
            "reverse-engineered from raw params", critical=True))

    r = _row(c, "SELECT count(*) FILTER (WHERE COALESCE(params->>'intent',"
                " params->>'query', params->>'q') IS NOT NULL), count(*)"
                " FROM mcp_call_log WHERE timestamp > now() - interval '30 days'")
    if r:
        ft, total = int(r[0] or 0), int(r[1] or 0)
        out.append(_check(
            "in_freetext", "free-text share (gauge)", None,
            f"{ft:,} of {total:,} calls in 30d ({_pct(ft, total)}%) carry free "
            "text. 2026-07-27 baseline: 0 of 1,032 EXTERNAL free-text calls "
            "were global-synthesis; 65% were single-entity lookups"))
    return out


# ── lane 5 · shell hygiene ────────────────────────────────────────────

def _lane_shell_hygiene(c, ctx) -> list[dict]:
    """The shells are the trust instrument. Two ways they betray it:
      · a kill switch that returns 5xx — the CF worker's proxyWithRetry reads
        ANY 5xx from Railway as a dead origin and fails over to the STALE
        Render backend; two within 10s trip the breaker site-wide for 30s. So
        disabling a diagnostic shell can take the SITE down.
      · no kill switch at all — nothing to pull when it misbehaves."""
    out = []
    s = _scan()
    if s.get("error"):
        out.append(_check("sh_5xx", "no shell 5xxs on its kill switch", None,
                          f"source scan failed: {s['error']}", critical=True))
        return out

    bad = s["shells_5xx"]
    ctx["shells_5xx"] = len(bad)
    out.append(_check(
        "sh_5xx", "no shell 5xxs on its kill switch",
        len(bad) == 0,
        f"{len(bad)} of {s['shells']} shell modules return 5xx when disabled"
        + (": " + ", ".join(x.replace('.py', '') for x in bad[:6]) if bad else "")
        + " — a 5xx here fails the whole site over to the stale Render origin",
        critical=True))

    nokill = s["shells_nokill"]
    out.append(_check(
        "sh_killswitch", "every shell has a kill switch",
        len(nokill) == 0,
        f"{len(nokill)} of {s['shells']} shell modules expose no DISABLE env"
        + (": " + ", ".join(x.replace('.py', '') for x in nokill[:6]) if nokill else "")))

    out.append(_check(
        "sh_scan", "source scan cost", None,
        f"{s['files']} route files, {s['handlers']} route handlers, "
        f"{s['scan_ms']}ms — computed ONCE per process, never per tick"))
    return out


# ── tick orchestration ────────────────────────────────────────────────

_LANES = [
    ("destructive", "1 · Destructive surface (set-wide writes)", _lane_destructive,
     "log admin traffic (api_endpoint_log misses it) so 'never invoked' becomes "
     "measurable; then dry-run-default the set-wide handlers"),
    ("predicates", "2 · Predicate tripwires", _lane_predicates,
     "add NOT NULL / DEFAULT 0 to discovered_facilities.is_duplicate so the 79 "
     "bare-form call sites cannot break, then normalise them to COALESCE"),
    ("public_gates", "3 · Public corpus gates", _lane_public_gates,
     "none while green — this is the press_releases/deals leak class; the check "
     "exists to catch the NEXT corpus added to PUBLIC_CORPORA"),
    ("intent", "4 · Intent instrumentation", _lane_intent,
     "log a question_class on the free-text tools (semantic_search, "
     "search_intelligence, plan_query, execute_plan) — unblocks the Oct re-read"),
    ("shell_hygiene", "5 · Shell hygiene", _lane_shell_hygiene,
     "change the 5xx kill-switch returns to 404 — a disabled diagnostic must "
     "never look like a dead origin to the CF worker"),
]

_cache: dict = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()
_TICK_TTL = 180.0


def _ensure_snapshots(c) -> None:
    try:
        with c.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS agreement_snapshots ("
                " id BIGSERIAL PRIMARY KEY,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                " lanes_pass INT, lanes_total INT, payload JSONB)")
    except Exception as e:
        logger.debug("[agreement] snapshot ddl skipped: %s", e)


def _run_tick() -> dict:
    c = _conn()
    ctx: dict = {}
    lanes = []
    for key, label, fn, actuator in _LANES:
        t0 = time.time()
        try:
            checks = fn(c, ctx)
        except Exception as e:
            checks = [_check(f"{key}_error", "lane crashed", None, str(e)[:200])]
        decided = [ch for ch in checks if ch["pass"] is not None]
        lanes.append({
            "lane": key, "label": label, "pass": _lane_verdict(checks),
            "actuator": actuator, "checks": checks,
            "ms": int((time.time() - t0) * 1000),
            "progress": f"{sum(1 for ch in decided if ch['pass'])}/{len(decided)}"
            if decided else "0/0"})

    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lanes_pass": sum(1 for l in lanes if l["pass"]),
        "lanes_total": len(lanes),
        "lanes": lanes,
        "note": "read-only DIAGNOSTIC; names an actuator per lane and fires "
                "nothing. Source scan cached per process. See "
                "routes/agreement_master_shell.py",
    }
    if c is not None:
        try:
            c.close()
        except Exception:
            pass

    payload["persisted"] = False
    w = _write_conn()
    if w is not None:
        try:
            _ensure_snapshots(w)
            with w.cursor() as cur:
                cur.execute("INSERT INTO agreement_snapshots"
                            " (lanes_pass, lanes_total, payload)"
                            " VALUES (%s, %s, %s)",
                            (payload["lanes_pass"], payload["lanes_total"],
                             json.dumps(payload)))
            payload["persisted"] = True
        except Exception as e:
            logger.warning("[agreement] snapshot insert failed: %s", e)
        try:
            w.close()
        except Exception:
            pass
    return payload


def _tick_cached() -> dict:
    with _cache_lock:
        if _cache["payload"] is not None and time.time() - _cache["ts"] < _TICK_TTL:
            return _cache["payload"]
    payload = _run_tick()
    with _cache_lock:
        _cache["ts"] = time.time()
        _cache["payload"] = payload
    return payload


# ── routes ────────────────────────────────────────────────────────────

@agreement_master_shell_bp.route("/api/v1/admin/agreement/master-tick",
                                 methods=["GET", "POST"])
def agreement_master_tick():
    if _disabled():
        # 404, never 5xx — see lane 5. A kill switch that 5xxs takes the site
        # down via the CF worker's failover breaker.
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if (request.args.get("fresh") or "") == "1":
        with _cache_lock:
            _cache["payload"] = None
    return jsonify(_tick_cached())


@agreement_master_shell_bp.route("/admin/agreement", methods=["GET"])
@agreement_master_shell_bp.route("/api/v1/admin/agreement", methods=["GET"])
def agreement_dashboard():
    if _disabled():
        return Response("agreement shell disabled", status=404)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _tick_cached()

    def _chip(v):
        if v is True:
            return '<span style="color:#22c55e">✓</span>'
        if v is False:
            return '<span style="color:#ef4444">✗</span>'
        return '<span style="color:#eab308">?</span>'

    cards = []
    for lane in p["lanes"]:
        rows = "".join(
            f"<tr><td style='padding:4px 8px;vertical-align:top'>{_chip(ch['pass'])}</td>"
            f"<td style='padding:4px 8px;vertical-align:top'>{_esc(ch['name'])}"
            f"{' <b style=color:#f59e0b>*</b>' if ch.get('critical') else ''}</td>"
            f"<td style='padding:4px 8px;color:#94a3b8'>{_esc(ch['detail'])}</td></tr>"
            for ch in lane["checks"])
        border = "#22c55e" if lane["pass"] else (
            "#ef4444" if lane["pass"] is False else "#334155")
        cards.append(
            f"<div style='background:#0f172a;border:1px solid {border};border-radius:12px;"
            f"padding:16px;margin:12px 0'><div style='font-weight:700;font-size:15px'>"
            f"{_chip(lane['pass'])} {_esc(lane['label'])} "
            f"<span style='color:#64748b;font-weight:400'>({lane['progress']} "
            f"checks green · {lane.get('ms',0)}ms)</span></div>"
            f"<table style='margin-top:8px;font-size:13px;border-collapse:collapse'>{rows}</table>"
            f"<div style='margin-top:8px;font-size:12px;color:#64748b'>⚡ actuator "
            f"(not fired): {_esc(lane.get('actuator',''))}</div></div>")

    green = p["lanes_pass"] == p["lanes_total"]
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='300'>"
        "<title>Agreement Master Shell · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,"
        "Roboto,sans-serif;max-width:920px;margin:24px auto;padding:0 16px'>"
        f"<h2 style='margin:0 0 4px'>Agreement Master Shell "
        f"<span style='color:{'#22c55e' if green else '#eab308'}'>"
        f"{p['lanes_pass']}/{p['lanes_total']} lanes green</span></h2>"
        f"<div style='color:#64748b;font-size:12px'>#37 · 07-27 · read-only "
        f"DIAGNOSTIC (names an actuator per lane, fires nothing) · source scan "
        f"cached per process · {int(_TICK_TTL)}s tick cache · generated "
        f"{_esc(p['generated_at'])} · JSON: "
        f"/api/v1/admin/agreement/master-tick</div>"
        "<div style='background:#0f172a;border:1px solid #334155;border-radius:12px;"
        "padding:14px;margin:14px 0;color:#94a3b8;font-size:13px'>"
        "Every lane watches one place where <b>two representations of a fact "
        "can drift apart</b> — a table and the index built from it, a heuristic "
        "and the ground truth it overrules, a shell's threshold and the code it "
        "audits. <b style='color:#f59e0b'>*</b> marks the check a lane exists "
        "for; an undetermined one renders the lane '?', never green."
        "</div>" + "".join(cards) + "</body>")
    return Response(html, mimetype="text/html")
