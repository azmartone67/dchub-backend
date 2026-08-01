"""
routes/route_auth_master_shell.py — Route-Auth Hardening Master Shell (#41, 2026-07-31).

Born from the 2026-07-31 route-security audit
(~/Downloads/dchub_route_security_audit_2026-07-31.md). The audit found the same
shape five different ways: a Flask route that TOUCHES something dangerous —
server-held credentials, an ingestion job, the official LinkedIn/X/Moltbook
accounts, the outbound mailer, a client traceback — with NO gate in front of it.
The app-wide @app.before_request chain only RATE-LIMITS; it is not an access
gate, so "the blueprint is registered" never means "the endpoint is
authenticated". Only an in-handler or in-decorator check clears a route.

Five lanes, each one AUTH-GAP CLASS this shell MEASURES statically over the route
files (repo root *.py + routes/*.py — the two dirs the surface actually lives
in). Each lane produces a real count + sample file:line offenders, NAMES the fix
actuator, and files its offenders into brain_findings for a human to triage. It
FIRES NOTHING: it opens no PR, rotates no key, applies no decorator, sends no
message, and never marks a finding resolved.

  1. SECRET-LEAK — a credential compared against the request instead of read
     from os.environ: a hardcoded key literal inside an auth fn, an env-value
     preview handler (the /env-snoop class), or a credential pulled from the
     query string into an auth compare. The canonical control is
     internal_auth.is_valid_internal_key (env-only, constant-time) — it must
     NEVER match. (#2049 already deleted the two headline offenders — the
     sources.py literal and /env-snoop — so lane 1's critical count reads clean;
     the shell reports the honest current state, not the stale audit.)
  2. UNAUTH JOB TRIGGER — a run/sync/ingest/extract/refresh/prune/crawl handler
     that mutates a set, spawns a thread, or calls an ingest fn, with no gate.
     The control is network_ix_ingestion.py's sync routes: they carry a
     LOCALLY-DEFINED @require_internal_key, so a naive "handler lacks
     is_valid_internal_key()" grep would false-flag them — the detector resolves
     the whole decorator stack, not one fn name.
  3. UNAUTH OUTBOUND ACTION — a handler reaching an outbound sink
     (post_to_linkedin / create_*_post / post_to_twitter / _send_email /
     indexnow) with no gate DOMINATING the sink. The offender
     (linkedin_autopost) and the control (linkedin_poster) call the IDENTICAL
     sink; only the control's `if not _check_admin(request): return 401` before
     it separates them, so the detector verifies a gate guards the handler, not
     merely that the sink is called somewhere.
  4. FAIL-OPEN / GET-BYPASS / UA-AS-AUTH — `if not ADMIN_KEY: return True`
     (auth skipped when the env var is unset), a `request.method == 'POST' and
     provided != key` gate a GET slips past, or a User-Agent substring
     ("dchub-", "brain") used as a credential. is_valid_internal_key fails
     CLOSED (no env → False), the control.
  5. TRACEBACK DISCLOSURE — traceback.format_exc() / repr(e) returned to the
     client inside an except block, plus the ONE central
     @app.errorhandler(Exception) that ships str(e) app-wide. The false-positive
     trap (100+ benign `str(e)[:200]` into an internal health dict) is
     deliberately NOT flagged.
  6. VERDICT — reads the counts the five lanes stashed (never re-scans) and
     states one honest field: are the audited gates clean?

★ READ-ONLY. This module performs NO outbound request, NO email/social send, NO
PR open, and NO INSERT/UPDATE/DELETE of any business/production table. It writes
exactly two diagnostic sinks, the same two every brain scanner uses:
  (a) one trend row into route_auth_snapshots via _write_conn (PRIMARY only —
      the replica rejects writes and the swallowed error would leave the table
      empty forever while ticks report success), and
  (b) triage findings into brain_findings via the canonical
      routes.brain_findings_writer.upsert_brain_finding, status="open" (never
      resolved/wont_fix/dismissed — a human triages). The mutation SQL for
      brain_findings lives in that writer, never here.
Every lane names an actuator; NONE is fired.

★ STATIC shell: lanes 1-5 introspect our own SOURCE (AST + a little regex),
computed ONCE per process and cached — the tree cannot change under a running
deploy, so the full parse+scan of root + routes/*.py (~1,100 files, ~8s, most of
it in main.py) is paid once, never per tick. This module excludes ITSELF from
the scan (a detector auditing its own pattern definitions is noise) and never
recurses below routes/ (so .claude/worktrees, .pg_migration_backups and .git
stale duplicates are out).

★ Admin-only, cached 180s. The first uncached tick blocks ~8s — under the CF
15s route timeout, but do NOT wire this into a health check, a page a user waits
on, or hammer it with ?fresh=1.

Endpoints:
  GET/POST /api/v1/admin/route-auth/master-tick   JSON scoreboard (6 lanes)
  GET      /admin/route-auth-shell                 HTML dashboard
  GET      /api/v1/admin/route-auth-shell          CF zone-worker bypass alias

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY) — same gate as integrity/graph-spine/agreement shells.
Kill: ROUTE_AUTH_SHELL_DISABLE=1
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

logger = logging.getLogger(__name__)

route_auth_master_shell_bp = Blueprint("route_auth_master_shell", __name__)

_ROUTES_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_ROUTES_DIR)
_SELF_BASENAME = "route_auth_master_shell.py"


# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("ROUTE_AUTH_SHELL_DISABLE") or "").strip() == "1"


# ── db helpers (same contract as graph_spine / agreement shells) ──────

def _connect(url):
    if not url:
        return None
    try:
        import psycopg2 as _pg
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[route-auth] db connect failed: %s", e)
        return None


def _conn():
    """READ connection — replica preferred. Lanes 1-5 are pure source scans and
    need no DB; the verdict lane reads a single brain_findings count off this
    connection as a trend gauge."""
    return _connect(os.environ.get("NEON_REPLICA_URL")
                    or os.environ.get("DATABASE_URL")
                    or os.environ.get("NEON_DATABASE_URL"))


def _write_conn():
    """WRITE connection for the snapshot row and the findings upserts ONLY, and
    NEVER the replica. The replica rejects writes; reusing the read connection
    means the write raises, gets swallowed fail-soft, and the trend/findings
    silently never land while every tick reports success — the exact silent
    no-op write this shell exists to audit."""
    return _connect(os.environ.get("DATABASE_URL")
                    or os.environ.get("NEON_DATABASE_URL"))


def _row(c, sql: str):
    """Fail-soft single row; None on error (NOT a zero-filled tuple) so a probe
    tells "query broke" from "count is zero". Literal SQL only, no params tuple:
    a literal % would %-substitute against an empty tuple and 500, so every
    pattern match is a POSIX regex (~)."""
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()
    except Exception as e:
        logger.debug("[route-auth] row failed: %s -- %s", sql[:90], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _scalar(c, sql: str):
    r = _row(c, sql)
    return r[0] if r else None


def _pct(part, whole):
    try:
        p, w = float(part), float(whole)
    except (TypeError, ValueError):
        return None
    return round(p * 100.0 / w, 1) if w else None


# ── check / verdict primitives (mirror integrity_master_shell) ────────

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


# ── AST helpers ───────────────────────────────────────────────────────

def _safe_unparse(node) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _name_of(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _dotted(node) -> str:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id + "." + node.attr
    return ""


def _static_sql(node):
    """Reconstruct a statement from a literal / concat / f-string; None when the
    SQL is assembled dynamically (never judged — guessing at a query we cannot
    see is how a lane starts lying)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant)
                       and isinstance(v.value, str) else " ? "
                       for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _static_sql(node.left), _static_sql(node.right)
        if left is None and right is None:
            return None
        return (left or "") + (right or "")
    return None


def _is_route(dec) -> bool:
    """True when a decorator is a Flask route registration (@bp.route /
    @app.route, incl. app.route nested inside a register_*_routes(app) closure).
    Structural, not unparse — a route decorator is `<x>.route(...)`."""
    node = dec.func if isinstance(dec, ast.Call) else dec
    return isinstance(node, ast.Attribute) and node.attr == "route" \
        and isinstance(dec, ast.Call)


_STMT_CONTAINERS = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                    ast.ClassDef, ast.If, ast.Try, ast.With, ast.AsyncWith,
                    ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler)


def _all_funcdefs(tree):
    """All FunctionDefs, recursing ONLY through statement containers (never
    expression subtrees). Route handlers are module-level or nested one level
    inside a register_*(app) closure — both reached — while skipping the
    expression nodes that make a full ast.walk of main.py (100k nodes) slow."""
    stack = [tree]
    while stack:
        node = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield child
                stack.append(child)
            elif isinstance(child, _STMT_CONTAINERS):
                stack.append(child)


def _route_handlers(tree):
    return [n for n in _all_funcdefs(tree)
            if any(_is_route(d) for d in n.decorator_list)]


def _fn_src(rec, fn) -> str:
    """Cheap source slice for a def (no ast.unparse — that dominated the scan)."""
    end = getattr(fn, "end_lineno", None) or fn.lineno
    return "\n".join(rec["src_lines"][fn.lineno - 1:end])


def _route_info(fn):
    """(paths, methods) declared on a handler's route decorator stack."""
    paths, methods = [], set()
    for d in fn.decorator_list:
        if not (isinstance(d, ast.Call) and _is_route(d)):
            continue
        if d.args and isinstance(d.args[0], ast.Constant) \
                and isinstance(d.args[0].value, str):
            paths.append(d.args[0].value)
        for kw in d.keywords:
            if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                for e in kw.value.elts:
                    if isinstance(e, ast.Constant) and isinstance(e.value, str):
                        methods.add(e.value.upper())
    if not methods:
        methods = {"GET"}  # Flask default
    return paths, methods


def _dec_names(fn):
    out = set()
    for d in fn.decorator_list:
        node = d.func if isinstance(d, ast.Call) else d
        n = getattr(node, "id", None) or getattr(node, "attr", None)
        if n:
            out.add(n)
    return out


def _call_name_set(node):
    names = set()
    for c in ast.walk(node):
        if isinstance(c, ast.Call):
            f = c.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
    return names


# The gate model. A route is authenticated iff a gate is applied IN its
# decorator stack or called IN its body — the central before_request chain
# only rate-limits and never counts. A gate need not be the canonical helper:
# network_ix_ingestion.py defines its own @require_internal_key.
_GATE_DECORATORS = {
    "require_internal_key", "require_admin", "require_internal_or_admin",
    "require_auth", "admin_required", "require_admin_key", "internal_only",
    "require_internal", "requires_admin", "require_key", "requires_auth",
    "login_required", "admin_only", "require_api_key", "requires_api_key",
    "require_plan", "require_pro", "require_tier", "require_paid",
    "require_subscription", "require_login",
}
_GATE_CALLS = {
    "is_valid_internal_key", "_check_auth", "_check_admin", "_admin_ok",
    "_admin_gate", "_check_admin_auth", "_require_admin", "_require_internal",
    "_is_admin", "verify_admin", "check_admin", "check_api_key",
    "_check_api_key", "verify_api_key", "_admin_request_ok", "require_admin",
    "_require_plan", "_check_key",
}


def _handler_is_gated(fn) -> bool:
    if _dec_names(fn) & _GATE_DECORATORS:
        return True
    if _call_name_set(fn) & _GATE_CALLS:
        return True
    # A handler that returns 401/403 is enforcing auth — via ANY gate fn,
    # including ones this module does not know by name (e.g. _authorized()).
    # This is the general form of the audit's "inline compare returns 401", and
    # it is what separates competitor_recon (gated: `if not _authorized(): 401`)
    # from gas /feeds/ingest (ungated: returns only 200/207). Preferring this
    # over a fixed name list keeps the CRITICAL lanes from crying wolf on a
    # handler that is in fact gated.
    for n in ast.walk(fn):
        if isinstance(n, ast.Constant) and n.value in (401, 403):
            return True
    return False


# ── LANE 1 · secret-leak / hardcoded-credential ───────────────────────

# TIGHT auth-fn names — deliberately NOT bare `is_valid` (that swept in
# is_valid_datacenter) and NOT bare `_auth`.
_AUTH_FN = re.compile(
    r"(_check_auth|_check_admin|_admin_ok|_admin_gate|_admin_request_ok"
    r"|_require_admin|_require_internal|is_valid_internal|is_valid_admin"
    r"|is_valid_key|admin_auth|check_admin|verify_admin)")
_CRED_WORD = re.compile(r"(?i)(secret|admin|key|token|passw|cred)")


def _reads_key(fsrc: str) -> bool:
    return bool(re.search(
        r"X-Admin-Key|X-Internal-Key|X-Admin-Token|admin_key|LMP_INGEST_TOKEN"
        r"|ADMIN_KEY|DCHUB_INTERNAL_KEY|compare_digest", fsrc))


def _is_auth_fn(fn, fsrc: str) -> bool:
    """A genuine auth check: a tight-named gate, or a fn that runs a credential
    compare (compare_digest) — never a background job that bails on a feature
    flag."""
    return bool(_AUTH_FN.search(fn.name)) or "compare_digest" in fsrc


def _safe_get_arg_strings(fn):
    """String constants that are the NAME argument to os.environ.get / .getenv /
    request.headers.get / request.args.get — those are lookups (env var names,
    header names), never the compared value, so they are not credential leaks."""
    safe = set()
    for c in ast.walk(fn):
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) \
                and c.func.attr in ("get", "getenv"):
            for a in c.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    safe.add(a.value)
    return safe


_CRED_ARG_NAMES = {"admin_key", "api_key", "apikey", "token", "admin",
                   "key", "admin_token", "secret", "access_token", "auth"}


# A credential VALUE literal: a dashed token ending in a year (the
# `dchub-admin-secret-2026` shape) or a dashed cred-word token. Long bare hex
# (facility ids, sha) is deliberately NOT treated as a credential — it produced
# only false positives.
def _looks_like_credential(v: str) -> bool:
    if re.search(r"-20\d\d$", v) and re.match(r"^[a-z][a-z0-9-]+$", v):
        return True
    return (v.count("-") >= 2 and bool(_CRED_WORD.search(v))
            and bool(re.match(r"^[a-z][a-z0-9-]+$", v)) and len(v) >= 12)


def _detect_l1_hardcoded(records):
    """Sub-(a): a string literal that looks like a credential VALUE, sitting
    inside a genuine auth fn and not read from os.environ/headers. The env-only
    control (is_valid_internal_key) has no such literal and never matches."""
    out = []
    for rec in records:
        if "compare_digest" not in rec["src"] and not _AUTH_FN.search(rec["src"]):
            continue
        for fn in _all_funcdefs(rec["tree"]):
            fsrc = _fn_src(rec, fn)
            if not _is_auth_fn(fn, fsrc):
                continue
            safe = _safe_get_arg_strings(fn)
            for n in ast.walk(fn):
                if not (isinstance(n, ast.Constant) and isinstance(n.value, str)):
                    continue
                v = n.value.strip()
                if v in safe or len(v) < 12:
                    continue
                if not _looks_like_credential(v):
                    continue
                out.append({"file": rec["rel"], "line": n.lineno,
                            "handler": fn.name, "severity": "high",
                            "detail": f"hardcoded credential literal "
                            f"'{v[:32]}' in auth fn {fn.name}() — compare "
                            "against os.environ, never a baked-in string"})
    return out


_PREVIEW_SLICE = re.compile(r"\w+\[\s*:\s*\d+\s*\]\s*\+.{0,50}?\w+\[\s*-\s*\d+\s*:")
_PREVIEW_LEN = re.compile(r"\(\s*\{?\s*len\([a-z_][a-z0-9_]*\)\}?\s*chars", re.I)


_ENV_ITER = re.compile(
    r"for\s+\w+.{0,30}?\bin\b.{0,20}?environ|environ\.(items|keys|values)\(", re.I)


def _detect_l1_preview(records):
    """Sub-(b): a handler that DUMPS per-env-var previews (the /env-snoop class:
    iterate os.environ AND preview each value[:4]+"..."+value[-2:] or "({len}
    chars)"). Correlated PER FUNCTION so a masked single-key fingerprint (a
    Stripe key shown to an admin) in a file that iterates environ elsewhere is
    NOT flagged."""
    out = []
    for rec in records:
        if not _ENV_ITER.search(rec["src"]):
            continue
        for fn in ast.walk(rec["tree"]):
            if not isinstance(fn, ast.FunctionDef):
                continue
            fsrc = _fn_src(rec, fn)
            if not _ENV_ITER.search(fsrc):
                continue
            m = _PREVIEW_SLICE.search(fsrc) or _PREVIEW_LEN.search(fsrc)
            if not m:
                continue
            line = fn.lineno + fsrc.count("\n", 0, m.start())
            out.append({"file": rec["rel"], "line": line, "handler": fn.name,
                        "severity": "high",
                        "detail": f"env-value preview leak in {fn.name}() — "
                        "iterates os.environ and previews each value "
                        "(value[:n]+value[-n:] or '(len chars)'); the /env-snoop "
                        "class"})
    return out


def _detect_l1_querystring(records):
    """Sub-(c): a credential read from the query string into an auth compare.
    Reported as a GAUGE, and the admin-shell convention (X-Admin-Key OR
    ?admin_key=) is EXCLUDED — every master shell reads ?admin_key= on purpose,
    so flagging them would be self-referential noise."""
    out = []
    _WRAPPER = re.compile(r"^(decorated|decorated_function|wrapper|_wrap|require_|register_)")
    _AUTH_CMP = re.compile(
        r"compare_digest|in valid_keys|in valid|[=!]= *[A-Z_]*"
        r"(ADMIN|INTERNAL|KEY|TOKEN|SECRET)[A-Z_]*")
    for rec in records:
        if rec["base"].endswith("_master_shell.py"):
            continue
        if "args.get" not in rec["src"]:
            continue
        for fn in ast.walk(rec["tree"]):
            if not isinstance(fn, ast.FunctionDef):
                continue
            if _WRAPPER.match(fn.name):
                continue
            fnsrc = _fn_src(rec, fn)
            # the value must flow into an auth comparison, not a data lookup.
            if not _AUTH_CMP.search(fnsrc):
                continue
            hit = None
            for c in ast.walk(fn):
                if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                        and c.func.attr == "get"
                        and _dotted(getattr(c.func, "value", None)) in
                        ("request.args", "req.args")
                        and c.args and isinstance(c.args[0], ast.Constant)
                        and c.args[0].value in _CRED_ARG_NAMES):
                    hit = (c.lineno, c.args[0].value)
                    break
            if hit:
                out.append({"file": rec["rel"], "line": hit[0], "handler": fn.name,
                            "severity": "low",
                            "detail": f"credential '?{hit[1]}=' read from the "
                            f"query string into an auth compare in {fn.name}() "
                            "— URLs get logged; move to the X-Internal-Key header"})
    return out


def _lane_secret_leak(c, ctx) -> list[dict]:
    out = []
    s = _scan()
    if s.get("error"):
        out.append(_check("l1_hardcoded", "no hardcoded credential in an auth check",
                          None, f"source scan failed: {s['error']}", critical=True))
        return out

    hard = s["l1"]["hardcoded"]
    prev = s["l1"]["preview"]
    qs = s["l1"]["querystring"]
    ctx["l1_crit"] = len(hard) + len(prev)
    # File the genuine leaks (hardcoded literal + env preview). Querystring is a
    # convention-review GAUGE, not filed — the tree legitimately uses ?admin_key=.
    ctx.setdefault("_findings", {})["l1"] = hard + prev

    # 1a — CRITICAL: a credential compared against the request instead of read
    # from os.environ. #2049 removed the two headline offenders; this reads the
    # honest current state.
    sample = " · ".join(f"{o['file']}:{o['line']}" for o in hard[:5]) or "none"
    out.append(_check(
        "l1_hardcoded", "no hardcoded credential literal in an auth check",
        len(hard) == 0,
        f"{len(hard)} hardcoded credential literal(s) inside an auth fn "
        f"[{sample}] — control internal_auth.is_valid_internal_key reads only "
        "os.environ and is correctly not flagged", critical=True))

    # 1b — env value-preview leak handlers (the /env-snoop class, deleted #2049).
    psample = " · ".join(f"{o['file']}:{o['line']}" for o in prev[:5]) or "none"
    out.append(_check(
        "l1_preview", "no endpoint previews environment-variable values",
        len(prev) == 0,
        f"{len(prev)} env-value preview site(s) [{psample}] — a handler that "
        "returns value[:4]+…+value[-2:] leaks secret shape to any caller"))

    # 1c — GAUGE: credential-in-querystring (admin-shell convention excluded).
    qsample = " · ".join(f"{o['file']}:{o['line']}" for o in qs[:6]) or "none"
    out.append(_check(
        "l1_querystring", "credential read from query string (gauge)", None,
        f"{len(qs)} auth check(s) accept a credential via ?param= [{qsample}] "
        "— excludes the *_master_shell.py ?admin_key= convention; URLs get "
        "logged, so these belong in the X-Internal-Key header"))
    return out


# ── LANE 2 · unauthenticated state-changing job trigger ───────────────

_L2_VERBS = re.compile(
    r"\b(run|sync|ingest|extract|refresh|prune|seed|crawl|discover|backfill"
    r"|reindex|rebuild)\b", re.I)
_MUTATE_SQL = re.compile(r"\b(INSERT\s+INTO|UPDATE\s+|DELETE\s+FROM|TRUNCATE)\b", re.I)
_INGEST_CALL = re.compile(
    r"^(ingest|sync|extract|run_|trigger_|discover|crawl|refresh|backfill"
    r"|reindex|rebuild|scrape)", re.I)
# A route PATH SEGMENT that unambiguously names a mutation action, so a handler
# that dispatches through a registry of ingestors (gas /feeds/ingest calls fn()
# out of a dict, never a direct mutate call) still counts. Path-segment only —
# the bare-word form over-flagged handlers that merely carry "run"/"refresh".
_STRONG_VERB_PATH = re.compile(
    r"/(ingest|sync|run|refresh|crawl|extract|backfill|rebuild|reindex|prune"
    r"|seed|discover|scrape)\b", re.I)
# Read-only markers — a dry-run/status/preview/list handler is NOT a mutation
# trigger even when its name carries a verb (deal_scraper's /refresh/dry-run).
_READONLY_MARK = re.compile(
    r"(?i)(status|preview|dry.?run|/list\b|health|_list\b|/view|/show|/get\b)")


def _is_state_changing(fn) -> bool:
    for c in ast.walk(fn):
        if not isinstance(c, ast.Call):
            continue
        f = c.func
        if isinstance(f, ast.Attribute) and f.attr == "execute" and c.args:
            sql = _static_sql(c.args[0])
            if sql and _MUTATE_SQL.search(sql):
                return True
        # thread spawn
        if isinstance(f, ast.Attribute) and f.attr == "Thread":
            return True
        if isinstance(f, ast.Name) and f.id == "Thread":
            return True
        name = getattr(f, "id", None) or getattr(f, "attr", None) or ""
        if _INGEST_CALL.match(name):
            return True
    return False


def _detect_l2(records):
    out = []
    for rec in records:
        for fn in rec["handlers"]:
            paths, methods = _route_info(fn)
            hay = " ".join(paths) + " " + fn.name
            if not _L2_VERBS.search(hay):
                continue
            if not (methods & {"POST", "GET"}):
                continue
            if _READONLY_MARK.search(hay):
                continue  # dry-run / status / preview / list — not a mutation
            # actionable = a real mutate/thread/ingest call, OR the path/name
            # unambiguously names a mutation action (dict-dispatched ingestors).
            if not (_is_state_changing(fn) or _STRONG_VERB_PATH.search(hay)):
                continue
            if _handler_is_gated(fn):
                continue
            out.append({"file": rec["rel"], "line": fn.lineno, "handler": fn.name,
                        "path": paths[0] if paths else "", "severity": "high",
                        "detail": f"state-changing trigger {paths[0] if paths else fn.name} "
                        f"({'/'.join(sorted(methods))}) reaches an ingest/mutate "
                        "with NO gate — the before_request chain only rate-limits"})
    return out


def _lane_unauth_trigger(c, ctx) -> list[dict]:
    out = []
    s = _scan()
    if s.get("error"):
        out.append(_check("l2_unauth", "no unauthenticated state-changing trigger",
                          None, f"source scan failed: {s['error']}", critical=True))
        return out
    off = s["l2"]
    ctx["l2_crit"] = len(off)
    ctx.setdefault("_findings", {})["l2"] = off
    sample = " · ".join(f"{o['file']}:{o['line']}({o['handler']})" for o in off[:6]) or "none"
    out.append(_check(
        "l2_unauth", "no unauthenticated state-changing job trigger",
        len(off) == 0,
        f"{len(off)} run/sync/ingest/extract handler(s) mutate or spawn with no "
        f"gate [{sample}] — control network_ix_ingestion (@require_internal_key, "
        "a locally-defined decorator) is correctly cleared", critical=True))
    out.append(_check(
        "l2_scan", "route surface scanned", None,
        f"{s['handlers']} route handlers across {s['files']} files "
        f"({s['scan_ms']}ms, cached once per process)"))
    return out


# ── LANE 3 · unauthenticated outbound action ──────────────────────────

_OUTBOUND_SINKS = {
    "post_to_linkedin", "create_text_post", "create_article_post",
    "post_to_twitter", "tweet", "post_to_moltbook", "post_to_x",
    "_send_email", "_p99_send_email", "send_email", "submit_indexnow",
}
_OUTBOUND_HOST = re.compile(
    r"api\.resend\.com|sendgrid|api\.twitter\.com|api\.linkedin\.com"
    r"|graph\.facebook|indexnow", re.I)


def _reaches_sink(fn):
    for c in ast.walk(fn):
        if not isinstance(c, ast.Call):
            continue
        f = c.func
        name = getattr(f, "id", None) or getattr(f, "attr", None) or ""
        if name in _OUTBOUND_SINKS:
            return name
        if isinstance(f, ast.Attribute) and f.attr in ("post", "request", "put"):
            for a in list(c.args) + [k.value for k in c.keywords]:
                s = _static_sql(a)
                if s and _OUTBOUND_HOST.search(s):
                    return "requests." + f.attr
    return None


def _detect_l3(records):
    out = []
    for rec in records:
        for fn in rec["handlers"]:
            sink = _reaches_sink(fn)
            if not sink:
                continue
            if _handler_is_gated(fn):
                continue
            paths, methods = _route_info(fn)
            out.append({"file": rec["rel"], "line": fn.lineno, "handler": fn.name,
                        "sink": sink, "path": paths[0] if paths else "",
                        "severity": "critical",
                        "detail": f"handler {fn.name} reaches outbound sink "
                        f"{sink}() with NO gate — same sink the gated control "
                        "linkedin_poster guards behind _check_admin+401"})
    return out


def _lane_outbound_sink(c, ctx) -> list[dict]:
    out = []
    s = _scan()
    if s.get("error"):
        out.append(_check("l3_outbound", "no unauthenticated outbound action",
                          None, f"source scan failed: {s['error']}", critical=True))
        return out
    off = s["l3"]
    ctx["l3_crit"] = len(off)
    ctx.setdefault("_findings", {})["l3"] = off
    sample = " · ".join(f"{o['file']}:{o['line']}→{o['sink']}" for o in off[:6]) or "none"
    out.append(_check(
        "l3_outbound", "no unauthenticated outbound action (LinkedIn/X/email)",
        len(off) == 0,
        f"{len(off)} handler(s) reach an outbound sink using server-held "
        f"credentials with no gate [{sample}] — reputational/quota blast radius "
        "on the official accounts; control linkedin_poster calls the same sink "
        "but gates it, so gate-dominance (not sink presence) separates them",
        critical=True))
    return out


# ── LANE 4 · fail-open / GET-bypass / UA-as-auth ──────────────────────

_L4_CRED_VAR = re.compile(r"(?i)(key|token|tok|secret|admin|cred|passw|signing)")
# A comparison against an admin/internal/secret credential — the mark of a
# genuine auth gate (vs a background helper that bails on an unset feature var).
_CMP_KEY = re.compile(
    r"compare_digest|[=!]= *[A-Za-z_]*(ADMIN|INTERNAL|SECRET|SIGNING)[A-Za-z_]*")


def _detect_l4_failopen(records):
    """Sub-(1): `if not <credvar>: return True|None` — auth SKIPPED when the env
    var is unset. Confined to genuine auth gates (tight auth name OR a
    credential compare), so a background runner or an outbound helper that bails
    on an unset ANTHROPIC_KEY / DATABASE_URL is NOT flagged. Fail-CLOSED (`return
    False`) is the control and never matches."""
    out = []
    for rec in records:
        src = rec["src"]
        if "compare_digest" not in src and not _AUTH_FN.search(src):
            continue
        for fn in _all_funcdefs(rec["tree"]):
            fsrc = _fn_src(rec, fn)
            if not (_AUTH_FN.search(fn.name) or _CMP_KEY.search(fsrc)):
                continue
            if "is_valid_internal_key" in _call_name_set(fn):
                continue
            for node in ast.walk(fn):
                if not (isinstance(node, ast.If)
                        and isinstance(node.test, ast.UnaryOp)
                        and isinstance(node.test.op, ast.Not)):
                    continue
                var = _name_of(node.test.operand)
                if not var:
                    continue
                if not _L4_CRED_VAR.search(var):
                    continue
                for st in node.body:
                    if isinstance(st, ast.Return):
                        v = st.value
                        permissive = (v is None) or (
                            isinstance(v, ast.Constant) and v.value in (True, None))
                        if permissive:
                            out.append({"file": rec["rel"], "line": node.lineno,
                                        "handler": fn.name, "severity": "high",
                                        "detail": f"fail-open: `if not {var}: "
                                        f"return {'True' if isinstance(v, ast.Constant) and v.value is True else 'None'}` "
                                        f"in {fn.name}() — auth skipped when the "
                                        "env var is unset; route through "
                                        "is_valid_internal_key (fail-closed)"})
                        break
    return out


def _is_method_post_eq(node) -> bool:
    return (isinstance(node, ast.Compare) and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Eq)
            and _dotted(node.left) == "request.method"
            and isinstance(node.comparators[0], ast.Constant)
            and node.comparators[0].value == "POST")


def _detect_l4_bypass(records):
    """Sub-(2): a method-scoped gate `if request.method == 'POST' and provided
    != key: 401` a GET slips past. AST-based so an unconditional `if provided !=
    key: 401` (no method qualifier) — the control — never matches, and prose
    mentions can't false-trigger."""
    out = []
    for rec in records:
        if "request.method" not in rec["src"]:
            continue
        for node in ast.walk(rec["tree"]):
            if not isinstance(node, ast.If):
                continue
            comps = [n for n in ast.walk(node.test) if isinstance(n, ast.Compare)]
            has_post = any(_is_method_post_eq(cmp) for cmp in comps)
            has_neq = any(cmp.ops and isinstance(cmp.ops[0], ast.NotEq) for cmp in comps)
            if not (has_post and has_neq):
                continue
            codes = [k.value for k in ast.walk(node)
                     if isinstance(k, ast.Constant) and k.value in (401, 403)]
            if not codes:
                continue
            out.append({"file": rec["rel"], "line": node.lineno, "handler": "",
                        "severity": "high",
                        "detail": "GET-bypass: auth gate scoped to "
                        "`request.method == 'POST' and provided != key` — a GET "
                        "reaches the same side-effect ungated; gate the effect, "
                        "not the method"})
    return out


_UA_BYPASS_NAME = re.compile(r"(?i)(internal_ua|bypass_ua|ua_bypass|ua_allow|ua_whitelist)")


def _detect_l4_ua(records):
    """Sub-(3): the 'dchub-' internal-traffic User-Agent used as a credential —
    a startswith('dchub-'), a 'dchub-' in <x> membership, or an INTERNAL_UA /
    bypass_ua substring table. AST/name based so comments and pattern strings
    elsewhere do not false-trigger. The bare 'brain' literal is NOT matched (it
    is a data-class token — `"brain" in classes` — all over the tree), and
    generic `_UA_PATTERNS` analytics-fingerprint tables are excluded; only the
    unambiguous 'dchub-' internal-traffic prefix and explicit *bypass* tables
    count. Fires on the UA branch, never the whole file (media_outreach's hmac
    _admin_ok stays clean; its :335 UA branch does not)."""
    out = []
    seen = set()

    def _add(rec, line, sev, detail):
        k = (rec["rel"], line)
        if k in seen:
            return
        seen.add(k)
        out.append({"file": rec["rel"], "line": line, "handler": "",
                    "severity": sev, "detail": detail})

    for rec in records:
        src = rec["src"]
        if "dchub-" not in src and not _UA_BYPASS_NAME.search(src):
            continue
        for node in ast.walk(rec["tree"]):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "startswith" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and node.args[0].value.lower().startswith("dchub")):
                _add(rec, node.lineno, "high",
                     f"UA-as-auth: User-Agent.startswith('{node.args[0].value[:16]}')"
                     " marks internal traffic — a User-Agent is caller-controlled "
                     "and spoofable")
            if (isinstance(node, ast.Compare) and node.ops
                    and isinstance(node.ops[0], ast.In)
                    and isinstance(node.left, ast.Constant)
                    and isinstance(node.left.value, str)
                    and node.left.value.lower().startswith("dchub-")):
                _add(rec, node.lineno, "high",
                     f"UA-as-auth: '{node.left.value}' in <user-agent> as an "
                     "internal-traffic branch — spoofable signal")
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    nm = getattr(t, "id", None)
                    if nm and _UA_BYPASS_NAME.search(nm):
                        _add(rec, node.lineno, "medium",
                             f"UA bypass table {nm} — a User-Agent substring list "
                             "used to grant a bypass is spoofable-signal-as-auth")
    return out


def _lane_failopen(c, ctx) -> list[dict]:
    out = []
    s = _scan()
    if s.get("error"):
        out.append(_check("l4_failopen", "no fail-open auth gate", None,
                          f"source scan failed: {s['error']}", critical=True))
        out.append(_check("l4_bypass", "no method-scoped GET bypass", None,
                          f"source scan failed: {s['error']}", critical=True))
        return out
    fo, byp, ua = s["l4"]["failopen"], s["l4"]["bypass"], s["l4"]["ua"]
    # Load-bearing = fail-open + GET-bypass (both precise & unambiguous). UA is a
    # gauge (below) and is NOT counted here or filed.
    ctx["l4_crit"] = len(fo) + len(byp)
    ctx.setdefault("_findings", {})["l4"] = fo + byp

    fsample = " · ".join(f"{o['file']}:{o['line']}" for o in fo[:5]) or "none"
    out.append(_check(
        "l4_failopen", "no fail-open auth gate (`if not KEY: return True`)",
        len(fo) == 0,
        f"{len(fo)} gate(s) allow-all when the env key is unset [{fsample}] — "
        "control is_valid_internal_key fails CLOSED and is not flagged",
        critical=True))

    bsample = " · ".join(f"{o['file']}:{o['line']}" for o in byp[:5]) or "none"
    out.append(_check(
        "l4_bypass", "no method-scoped GET bypass",
        len(byp) == 0,
        f"{len(byp)} gate(s) only challenge POST, letting a GET reach the same "
        f"side-effect [{bsample}] — an unconditional `if provided != key: 401` "
        "is correct and is not flagged", critical=True))

    # ★ UA-as-auth is a GAUGE, not an assertion. Identifying internal traffic by
    # a 'dchub-' User-Agent is a PERVASIVE, mostly-benign pattern (rate-limit
    # exemption, self-traffic peace, analytics). The genuinely dangerous cases
    # (a UA that gates ACCESS to protected data) cannot be reliably separated
    # from the benign ones statically, so this reports the count for human
    # review rather than crying wolf on an accepted design — and is not filed.
    usample = " · ".join(f"{o['file']}:{o['line']}" for o in ua[:6]) or "none"
    out.append(_check(
        "l4_ua", "User-Agent-as-signal sites (gauge, not asserted)", None,
        f"{len(ua)} 'dchub-' internal-traffic UA check(s) [{usample}] — a "
        "spoofable UA prefix; review any that gate ACCESS (not just rate "
        "limits). The canonical example is sentinel_ratelimit_bypass"))
    return out


# ── LANE 5 · traceback / exception disclosure ─────────────────────────

def _detect_l5_leak(records):
    """traceback.format_exc() or repr(e) that reaches a jsonify/Response/return
    inside an except block — a client-visible traceback. A str(e) logged
    server-side, or written into an internal health dict, is deliberately NOT
    flagged (the 100+ benign str(e)[:200] false-positive trap)."""
    out = []
    for rec in records:
        if "format_exc" not in rec["src"] and "repr(" not in rec["src"]:
            continue
        for h in ast.walk(rec["tree"]):
            if not isinstance(h, ast.ExceptHandler):
                continue
            exc_name = h.name
            for n in ast.walk(h):
                target = None
                if isinstance(n, ast.Return) and n.value is not None:
                    target = n.value
                elif isinstance(n, ast.Call) and (
                        getattr(n.func, "id", None) in ("jsonify", "Response")
                        or getattr(n.func, "attr", None) == "jsonify"):
                    target = n
                if target is None:
                    continue
                for m in ast.walk(target):
                    if (isinstance(m, ast.Call) and isinstance(m.func, ast.Attribute)
                            and m.func.attr == "format_exc"):
                        out.append({"file": rec["rel"], "line": m.lineno, "handler": "",
                                    "kind": "traceback.format_exc()",
                                    "severity": "high",
                                    "detail": "traceback.format_exc() returned to "
                                    "the client — leaks file paths, SQL, internals"})
                    if (isinstance(m, ast.Call) and getattr(m.func, "id", None) == "repr"
                            and m.args and exc_name and _name_of(m.args[0]) == exc_name):
                        out.append({"file": rec["rel"], "line": m.lineno, "handler": "",
                                    "kind": "repr(e)", "severity": "high",
                                    "detail": "repr(e) returned to the client — "
                                    "leaks exception internals"})
    # de-dup by (file,line,kind)
    seen, uniq = set(), []
    for o in out:
        k = (o["file"], o["line"], o["kind"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(o)
    return uniq


def _detect_l5_central(records):
    """The ONE central @app.errorhandler(Exception) that ships str(e) app-wide —
    a single systemic baseline item, not every str(e)."""
    out = []
    for rec in records:
        if "errorhandler" not in rec["src"]:
            continue
        for fn in _all_funcdefs(rec["tree"]):
            if not any("errorhandler(Exception)" in _safe_unparse(d)
                       for d in fn.decorator_list):
                continue
            body = _fn_src(rec, fn)
            if re.search(r"str\(\s*\w+\s*\)", body) and "jsonify" in body:
                out.append({"file": rec["rel"], "line": fn.lineno,
                            "handler": fn.name, "severity": "medium",
                            "detail": "central @app.errorhandler(Exception) "
                            "returns str(e) app-wide — genericize to "
                            "'internal_error' + server-side log"})
    return out


def _lane_traceback(c, ctx) -> list[dict]:
    out = []
    s = _scan()
    if s.get("error"):
        out.append(_check("l5_leak", "no client-visible traceback", None,
                          f"source scan failed: {s['error']}", critical=True))
        return out
    leak, central = s["l5"]["leak"], s["l5"]["central"]
    ctx["l5_crit"] = len(leak)
    ctx.setdefault("_findings", {})["l5"] = leak + central
    sample = " · ".join(f"{o['file']}:{o['line']}" for o in leak[:6]) or "none"
    out.append(_check(
        "l5_leak", "no traceback.format_exc()/repr(e) returned to a client",
        len(leak) == 0,
        f"{len(leak)} except-block(s) return a traceback/repr to the caller "
        f"[{sample}] — benign str(e) into an internal health dict is NOT "
        "flagged", critical=True))
    csample = " · ".join(f"{o['file']}:{o['line']}" for o in central[:3]) or "none"
    out.append(_check(
        "l5_central", "central error handler is generic (gauge)", None,
        f"{len(central)} central @app.errorhandler(Exception) ship str(e) "
        f"app-wide [{csample}] — the systemic baseline, one edit not many"))
    return out


# ── LANE 6 · verdict ──────────────────────────────────────────────────

def _lane_verdict_gate(c, ctx) -> list[dict]:
    """Reads the counts the five lanes stashed in ctx — never re-scans. A
    verdict that disagreed with its own lanes because it sampled a second time
    is worse than none."""
    out = []
    keys = ("l1_crit", "l2_crit", "l3_crit", "l4_crit", "l5_crit")
    if any(ctx.get(k) is None for k in keys):
        out.append(_check(
            "l6_verdict", "audited route-auth gates are clean", None,
            "one or more lanes did not resolve — no verdict", critical=True))
        ctx["verdict"] = "UNKNOWN"
        return out

    total = sum(int(ctx.get(k) or 0) for k in keys)
    clean = total == 0
    ctx["verdict"] = "GATES_CLEAN" if clean else "GAPS_OPEN"
    out.append(_check(
        "l6_verdict", "audited route-auth gates are clean",
        clean,
        f"L1 secret-leak {ctx.get('l1_crit')} · L2 unauth-trigger "
        f"{ctx.get('l2_crit')} · L3 outbound {ctx.get('l3_crit')} · L4 fail-open"
        f"/bypass/UA {ctx.get('l4_crit')} · L5 traceback {ctx.get('l5_crit')} — "
        f"{total} load-bearing gaps open across the route surface", critical=True))

    out.append(_check(
        "l6_note", "remediation is human-gated (gauge)", None,
        "every lane NAMES an actuator (rotate keys, apply require_internal_or_"
        "admin, wrap the outbound sinks, route auth through is_valid_internal_"
        "key, genericize the central handler) and FIRES none — findings are "
        "filed status='open' for a human to triage"))

    # trend gauge off the read connection — how many of this shell's own
    # findings are currently open (POSIX ~, never LIKE, so no literal %).
    n = _scalar(c, "SELECT count(*) FROM brain_findings"
                   " WHERE detector ~ '^route_auth_shell'"
                   "   AND coalesce(status,'open') = 'open'")
    out.append(_check(
        "l6_findings_open", "route-auth findings currently open (gauge)", None,
        f"{int(n) if n is not None else '?'} open brain_findings filed by this "
        "shell — the triage queue this tick feeds"))
    return out


# ── tick orchestration ────────────────────────────────────────────────
# (key, label, fn, actuator) — actuator is NAMED but never fired.

_LANES = [
    ("l1", "1 · Secret-leak / hardcoded credential", _lane_secret_leak,
     "rotate DCHUB_ADMIN_KEY / INTERNAL_KEY / SYNC_KEY, delete any baked-in "
     "credential fallback and any env-preview endpoint, and move every "
     "credential read out of request.args into the X-Internal-Key header via "
     "is_valid_internal_key()"),
    ("l2", "2 · Unauthenticated job / ingestion trigger", _lane_unauth_trigger,
     "add one fail-closed require_internal_or_admin decorator (is_valid_internal"
     "_key + DCHUB_ADMIN_KEY, fail-closed) across the run/sync/ingest endpoint "
     "class — named here, applied to nothing"),
    ("l3", "3 · Unauthenticated outbound action", _lane_outbound_sink,
     "wrap every outbound sink (post_to_linkedin/create_*_post/post_to_twitter/"
     "_send_email/indexnow) in require_internal_or_admin BEFORE the next "
     "automated posting run — reputational + Resend/SendGrid-quota blast radius"),
    ("l4", "4 · Fail-open / GET-bypass / UA-as-auth", _lane_failopen,
     "route ALL internal auth through internal_auth.is_valid_internal_key() "
     "(fail-closed), require a secret not a header/UA substring, and gate the "
     "SIDE-EFFECT rather than only the POST method"),
    ("l5", "5 · Exception / traceback disclosure", _lane_traceback,
     "genericize the central @app.errorhandler(Exception) to a message + "
     "server-side log, and strip traceback.format_exc()/repr(e) returns from "
     "the offending routes"),
    ("l6", "6 · Verdict: are the audited gates clean?", _lane_verdict_gate,
     "no actuator — this lane keeps the answer honest and dated, and reports "
     "the size of the triage queue the detector lanes feed"),
]

_cache: dict = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()
_TICK_TTL = 180.0  # a full source scan is ~2-3s; do not tighten


# ── source scan (computed ONCE per process) ───────────────────────────
# The source tree cannot change under a running deploy, so the full AST scan of
# root + routes/*.py is paid once and cached. NEVER re-scan per tick.

_SCAN_LOCK = threading.Lock()
_SCAN: dict | None = None


def _iter_route_files():
    """Non-recursive: repo root *.py + routes/*.py only. That naturally excludes
    .git/, .claude/worktrees/, .pg_migration_backups/ (stale duplicates of every
    file) without an explicit blocklist. Skips this module itself."""
    seen = set()
    for pat in (os.path.join(_REPO_ROOT, "*.py"),
                os.path.join(_ROUTES_DIR, "*.py")):
        for path in sorted(glob.glob(pat)):
            base = os.path.basename(path)
            if base.startswith(".") or base.startswith("__"):
                continue
            if base == _SELF_BASENAME:
                continue
            rp = os.path.realpath(path)
            if rp in seen:
                continue
            seen.add(rp)
            yield path, base


def _scan_routes() -> dict:
    records = []
    handlers = 0
    t0 = time.time()
    for path, base in _iter_route_files():
        try:
            src = open(path, encoding="utf-8").read()
            tree = ast.parse(src)
        except Exception:
            continue
        rel = os.path.relpath(path, _REPO_ROOT)
        hnds = _route_handlers(tree)
        records.append({"rel": rel, "base": base, "src": src,
                        "src_lines": src.splitlines(), "tree": tree,
                        "handlers": hnds})
        handlers += len(hnds)

    out = {
        "error": None,
        "files": len(records),
        "handlers": handlers,
        "l1": {"hardcoded": _detect_l1_hardcoded(records),
               "preview": _detect_l1_preview(records),
               "querystring": _detect_l1_querystring(records)},
        "l2": _detect_l2(records),
        "l3": _detect_l3(records),
        "l4": {"failopen": _detect_l4_failopen(records),
               "bypass": _detect_l4_bypass(records),
               "ua": _detect_l4_ua(records)},
        "l5": {"leak": _detect_l5_leak(records),
               "central": _detect_l5_central(records)},
    }
    out["scan_ms"] = int((time.time() - t0) * 1000)
    return out


def _scan() -> dict:
    global _SCAN
    with _SCAN_LOCK:
        if _SCAN is None:
            try:
                _SCAN = _scan_routes()
            except Exception as e:
                logger.warning("[route-auth] source scan failed: %s", e)
                _SCAN = {"error": str(e)[:200]}
        return _SCAN


# ── snapshot + findings (the ONLY writes, both diagnostic ledgers) ────

def _ensure_snapshots(c) -> None:
    try:
        with c.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS route_auth_snapshots ("
                " id BIGSERIAL PRIMARY KEY,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                " lanes_pass INT, lanes_total INT, verdict TEXT, payload JSONB)")
    except Exception as e:
        logger.debug("[route-auth] snapshot ddl skipped: %s", e)


def _file_findings(findings_by_lane: dict) -> dict:
    """File each lane's offenders into brain_findings via the CANONICAL writer
    (introspects live columns; status='open'; never resolves). PRIMARY conn
    only. Returns the real outcome tally, never a blind `filed += 1`."""
    result = {"filed": 0, "inserted": 0, "updated": 0, "skipped": 0,
              "ok": False, "error": None}
    total = sum(len(v) for v in findings_by_lane.values())
    if total == 0:
        result["ok"] = True
        return result
    try:
        from routes.brain_findings_writer import upsert_brain_finding
    except Exception as e:
        result["error"] = f"writer import failed: {str(e)[:120]}"
        return result
    w = _write_conn()
    if w is None:
        result["error"] = "no primary DB connection"
        return result
    try:
        with w.cursor() as cur:
            for lane_id, offenders in findings_by_lane.items():
                for o in offenders:
                    subject = o.get("handler") or o.get("path") or o.get("line")
                    issue = f"route-auth-{lane_id}:{o['file']}:{subject}"
                    url = f"dchub://route-auth/{lane_id}/{o['file']}#L{o.get('line')}"
                    detail = f"[{o.get('severity', 'medium')}] {o.get('detail', '')}"
                    outcome = upsert_brain_finding(
                        cur, issue=issue, url=url, count=1, detail=detail[:2000],
                        detector=f"route_auth_shell.{lane_id}", status="open")
                    if outcome in result:
                        result[outcome] += 1
                    result["filed"] += 1
        w.commit()
        result["ok"] = True
    except Exception as e:
        result["error"] = str(e)[:200]
        try:
            w.rollback()
        except Exception:
            pass
    finally:
        try:
            w.close()
        except Exception:
            pass
    return result


def _run_tick() -> dict:
    c = _conn()
    ctx: dict = {"_findings": {}}
    lanes = []
    for key, label, fn, actuator in _LANES:
        t0 = time.time()
        try:
            checks = fn(c, ctx)
        except Exception as e:  # a lane must never sink the tick
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
        "verdict": ctx.get("verdict", "UNKNOWN"),
        "lanes_pass": sum(1 for lane in lanes if lane["pass"]),
        "lanes_total": len(lanes),
        "lanes": lanes,
        "note": "read-only DIAGNOSTIC; measures 5 route-auth-gap classes, names "
                "an actuator per lane and fires nothing. Static source scan "
                "cached per process; files findings status='open' for a human. "
                "See routes/route_auth_master_shell.py",
    }
    if c is not None:
        try:
            c.close()
        except Exception:
            pass

    # File findings for a human to triage (PRIMARY conn), then the trend row.
    payload["findings"] = _file_findings(ctx.get("_findings", {}))

    payload["persisted"] = False
    w = _write_conn()
    if w is not None:
        try:
            _ensure_snapshots(w)
            with w.cursor() as cur:
                cur.execute(
                    "INSERT INTO route_auth_snapshots"
                    " (lanes_pass, lanes_total, verdict, payload)"
                    " VALUES (%s, %s, %s, %s)",
                    (payload["lanes_pass"], payload["lanes_total"],
                     payload["verdict"], json.dumps(payload)))
            payload["persisted"] = True
        except Exception as e:
            logger.warning("[route-auth] snapshot insert failed: %s", e)
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

@route_auth_master_shell_bp.route(
    "/api/v1/admin/route-auth/master-tick", methods=["GET", "POST"])
def route_auth_master_tick():
    if _disabled():
        # 404, never 5xx: the CF worker's proxyWithRetry reads ANY 5xx from
        # Railway as a dead origin and fails over to the stale Render backend.
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if (request.args.get("fresh") or "") == "1":
        with _cache_lock:
            _cache["payload"] = None
    return jsonify(_tick_cached())


@route_auth_master_shell_bp.route("/admin/route-auth-shell", methods=["GET"])
@route_auth_master_shell_bp.route("/api/v1/admin/route-auth-shell", methods=["GET"])
def route_auth_dashboard():
    if _disabled():
        return Response("route-auth shell disabled", status=404)
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

    verdict = p.get("verdict", "UNKNOWN")
    vcolor = {"GATES_CLEAN": "#22c55e", "GAPS_OPEN": "#ef4444"}.get(verdict, "#eab308")
    fnd = p.get("findings", {})
    green = p["lanes_pass"] == p["lanes_total"]
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='300'>"
        "<title>Route-Auth Hardening Master Shell · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,"
        "Roboto,sans-serif;max-width:920px;margin:24px auto;padding:0 16px'>"
        f"<h2 style='margin:0 0 4px'>Route-Auth Hardening Master Shell "
        f"<span style='color:{'#22c55e' if green else '#eab308'}'>"
        f"{p['lanes_pass']}/{p['lanes_total']} lanes green</span></h2>"
        f"<div style='color:#64748b;font-size:12px'>#41 · 07-31 · read-only "
        f"DIAGNOSTIC (names an actuator per lane, fires nothing) · static source "
        f"scan cached per process · {int(_TICK_TTL)}s tick cache · generated "
        f"{_esc(p['generated_at'])} · JSON: "
        f"/api/v1/admin/route-auth/master-tick</div>"
        f"<div style='background:#0f172a;border:1px solid {vcolor};border-radius:12px;"
        f"padding:14px;margin:14px 0'><b style='color:{vcolor}'>"
        f"Audited route-auth gates: {_esc(verdict)}</b>"
        f"<div style='color:#94a3b8;font-size:13px;margin-top:6px'>"
        "Five auth-gap classes measured statically over the route files. The "
        "app-wide before_request chain only rate-limits — only an in-handler or "
        "in-decorator gate authenticates a route. Every lane NAMES a fix and "
        "FIRES nothing; offenders are filed to brain_findings status='open' for "
        f"a human. This tick: {_esc(str(fnd.get('filed', 0)))} findings filed "
        f"({_esc(str(fnd.get('inserted', 0)))} new / "
        f"{_esc(str(fnd.get('updated', 0)))} recurring), persisted="
        f"{_esc(str(p.get('persisted')))}. <b style='color:#f59e0b'>*</b> marks "
        "the check a lane exists for; an undetermined one renders the lane '?', "
        "never green.</div></div>"
        + "".join(cards) + "</body>")
    return Response(html, mimetype="text/html")
