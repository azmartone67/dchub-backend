"""Ops/admin read gate: named, revocable, READ-ONLY viewer keys.

What it gates
-------------
Every request whose path is an ops or admin surface (see ``is_gated_path``):
``/api/v1/admin/*``, ``/api/admin/*`` (the legacy admin namespace),
``/api/v1/ops/*`` except ``/api/v1/ops/brief``, ``/api/v1/ops/claims``,
``/api/v1/ops/origin-freshness`` and ``/api/v1/ops/deadman`` (see
PUBLIC_EXACT for why), ``/api/v1/mcp/funnel/*`` and ``/funnel-stages`` (the
bare ``/api/v1/mcp/funnel`` is public),
``/api/v1/mcp/retention`` (+ ``/retention/*``), and the ``/admin/*`` and
``/ops/*`` HTML shells. It is ONE ``before_request`` hook keyed on the request
PATH, registered ahead of every other hook in main.py, so it covers every
handler that serves those paths, whichever module registered it and whichever
copy wins Werkzeug's match. A gate on one handler while a sibling serves the
same path is the miss this shape rules out.

``/dashboard`` is NOT gated by default: it is the customer account dashboard
(API keys, billing portal), not an ops view. ``DCHUB_OPS_GATE_EXTRA_PATHS``
(comma-separated; each entry gates that path and its subtree) adds paths
without a code change if the owner decides otherwise.

Who passes
----------
1. The existing admin/internal credentials, validated by
   ``internal_auth.is_valid_internal_key`` exactly as the route-level checks
   already do: ``X-Admin-Key``, ``X-Internal-Key``, ``?admin_key=``, ``?key=``,
   ``Authorization: Bearer``, ``X-DC-Internal-Token``, the ``dchub_admin_key``
   cookie; plus ``X-Internal-Cron``
   equal to ``DCHUB_CRON_SECRET``. These pass on every method, and the route's
   own check still decides, as it does today.
2. A true loopback self-call (socket peer 127.0.0.1 / ::1), the same trust
   ``routes/tier_gate.caller_is_privileged`` already extends.
3. A VIEWER key (``dchv_`` prefix), presented in the SAME slots as the admin
   key — ``X-Admin-Key``, ``?admin_key=`` or ``Authorization: Bearer`` — or the
   ``dchub_viewer_key`` cookie this gate sets on an HTML shell opened with
   ``?admin_key=dchv_...``. Reusing the admin slots is deliberate: the
   Cloudflare credential bypass (cache rule 24) already names every one of them,
   so a keyed 200 is never stored at the edge and replayed to a keyless caller,
   and the admin HTML shells already forward ``?admin_key=`` to their API
   fetches.

Viewer keys are READ-ONLY. They pass this gate on GET/HEAD only; a mutating
method carrying only a viewer key is refused (403 in enforce mode). They are
never accepted by ``internal_auth`` or any route-level admin check, which
compare against the admin/internal env secrets, so a viewer key cannot
authorize a write anywhere, whatever the mode.

Keys live in ``ops_viewer_keys`` as SHA-256 hashes, one active row per name,
minted and revoked through ``POST /api/v1/admin/ops-gate/keys`` (admin key
only) — revocable without a deploy, effective within ``_KEY_CACHE_TTL_S``.
``scripts/ops_viewer_keys.py`` wraps those endpoints. The raw key is shown
once, at mint, and never stored.

Modes (``DCHUB_OPS_GATE_MODE``)
-------------------------------
``log`` (DEFAULT) — serve exactly as before, but record every would-be denial.
``enforce``       — keyless / wrong key -> 401 ``{"error": "unauthorized"}``,
                    viewer key on a write -> 403. No data in either body.
``off``           — the gate does nothing at all.
Any other value is read as ``log``: a typo must never silently disable the
record, and must never lock callers out either.

Every would-be denial (both modes) goes to ``ops_gate_denials`` (ip, user
agent, method, path, reason, first/last seen, hits), pruned to 7 days, and to
the log as one ``ops_gate_denial {json}`` line per distinct caller per flush
window. ``GET /api/v1/admin/ops-gate/denials`` reports distinct IPs / UAs.
Each request a named viewer key makes is logged as ``ops_gate_allow {json}``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request

log = logging.getLogger("ops_viewer_gate")

VIEWER_PREFIX = "dchv_"
COOKIE_NAME = "dchub_viewer_key"
MODE_ENV = "DCHUB_OPS_GATE_MODE"
EXTRA_PATHS_ENV = "DCHUB_OPS_GATE_EXTRA_PATHS"
RETENTION_DAYS = 7

# A path equal to one of these, or under it ("<p>/..."), is gated.
GATED_SUBTREES = (
    "/api/v1/admin",
    "/api/admin",
    "/api/v1/ops",
    "/api/v1/mcp/funnel",
    "/api/v1/mcp/retention",
    "/admin",
    "/ops",
)
# Gated as exact paths (siblings that share a prefix string, not a subtree).
GATED_EXACT = frozenset((
    "/api/v1/mcp/funnel-stages",
))
# Public even though they sit under a gated subtree.
#   /api/v1/ops/brief  — public by owner decision.
#   /api/v1/ops/claims — the dchub.cloud HOMEPAGE fetches it from the visitor's
#     browser (dchub-frontend index.html, the claim strip), and the frontend's
#     required `preflight` check loads that homepage against live prod and
#     fails on any 4xx. A key cannot fix a browser call; gating it would break
#     the homepage and deadlock every frontend PR. Remove it here only together
#     with the homepage change.
#   /api/v1/ops/origin-freshness — the failover freshness probe; mirrors and
#     the failover scripts read it keylessly, and it has to answer precisely
#     when the origin is stale (routes/failover_stale_gate.py exempts it for
#     the same reason).
#   /api/v1/mcp/funnel — public by owner decision (2026-09-25): seven public
#     dchub-frontend pages (ai, built-for-ai, cited-by, testimonials,
#     audience/eyeball-card, audit, partner landing) read it from the
#     visitor's browser. Its subpaths (/diagnostics etc.) stay gated.
#   /api/v1/ops/deadman — public by owner decision (2026-09-25): the MCP
#     server's instructions, README and smithery.yaml promise agents a
#     keyless liveness read.
# DCHUB_OPS_GATE_PUBLIC_PATHS (comma-separated exact paths) adds more at
# flip time without a code change.
PUBLIC_EXACT = frozenset((
    "/api/v1/ops/brief",
    "/api/v1/ops/claims",
    "/api/v1/ops/origin-freshness",
    "/api/v1/mcp/funnel",
    "/api/v1/ops/deadman",
))
PUBLIC_PATHS_ENV = "DCHUB_OPS_GATE_PUBLIC_PATHS"

READ_METHODS = frozenset(("GET", "HEAD"))
_LOOPBACK = frozenset(("127.0.0.1", "::1", "::ffff:127.0.0.1"))

_KEY_CACHE_TTL_S = 60
_FLUSH_INTERVAL_S = 15
_PRUNE_INTERVAL_S = 3600
_MAX_PENDING = 5000


# ───────────────────────────────────────────────────────────────── paths ──

def normalize_path(path: str) -> str:
    """Collapse repeated slashes and drop a trailing slash (not the root)."""
    p = path or "/"
    while "//" in p:
        p = p.replace("//", "/")
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/") or "/"
    return p


def _extra_subtrees() -> tuple:
    return _env_paths(EXTRA_PATHS_ENV)


def _env_paths(var: str) -> tuple:
    out = []
    for part in (os.environ.get(var, "") or "").split(","):
        part = part.strip()
        if part:
            part = normalize_path(part)
            if part.startswith("/") and part != "/":
                out.append(part)
    return tuple(out)


def public_exact() -> frozenset:
    return PUBLIC_EXACT | frozenset(_env_paths(PUBLIC_PATHS_ENV))


def is_gated_path(path: str) -> bool:
    p = normalize_path(path)
    if p in public_exact():
        return False
    if p in GATED_EXACT:
        return True
    for root in GATED_SUBTREES + _extra_subtrees():
        if p == root or p.startswith(root + "/"):
            return True
    return False


def gate_mode() -> str:
    v = (os.environ.get(MODE_ENV, "") or "").strip().lower()
    if v in ("enforce", "off"):
        return v
    return "log"


# ─────────────────────────────────────────────────────────── credentials ──

def _first_token(v) -> str:
    parts = str(v or "").split()
    return parts[0] if parts else ""


def _bearer(req) -> str:
    auth = req.headers.get("Authorization", "") or ""
    if auth[:7].lower() == "bearer ":
        return _first_token(auth[7:])
    return ""


def _admin_slot_values(req) -> list:
    """Every slot the repo's admin/internal checks read a key from."""
    vals = [
        req.headers.get("X-Admin-Key"),
        req.headers.get("X-Internal-Key"),
        req.headers.get("X-DC-Internal-Token"),
        _bearer(req),
        req.args.get("admin_key"),
        req.args.get("key"),
        # Browser admin pages (paywall-test, devrel-targets, visitor-
        # intelligence) authenticate with this existing admin cookie.
        req.cookies.get("dchub_admin_key"),
    ]
    return [_first_token(v) for v in vals if v]


def _viewer_candidates(req) -> list:
    """Viewer keys ride in the admin slots (and the shell cookie)."""
    vals = [
        req.headers.get("X-Admin-Key"),
        _bearer(req),
        req.args.get("admin_key"),
        req.cookies.get("dchub_viewer_key"),  # == COOKIE_NAME; literal so
        # scripts/check_credential_channel_coverage.py can see the channel
    ]
    out = []
    for v in vals:
        t = _first_token(v)
        if t.startswith(VIEWER_PREFIX):
            out.append(t)
    return out


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_admin_or_internal(req) -> bool:
    try:
        from internal_auth import is_valid_internal_key
    except Exception:  # pragma: no cover - internal_auth is in-repo
        return False
    for v in _admin_slot_values(req):
        if v.startswith(VIEWER_PREFIX):
            continue
        try:
            if is_valid_internal_key(v):
                return True
        except Exception:
            continue
    cron = _first_token(req.headers.get("X-Internal-Cron"))
    secret = _first_token(os.environ.get("DCHUB_CRON_SECRET"))
    if cron and secret and hmac.compare_digest(cron, secret):
        return True
    return False


def _is_loopback(req) -> bool:
    try:
        return (req.remote_addr or "") in _LOOPBACK
    except Exception:
        return False


class Caller:
    __slots__ = ("kind", "name")

    def __init__(self, kind: str, name: str = ""):
        self.kind = kind  # admin | loopback | viewer | bad_viewer | anonymous
        self.name = name

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Caller(%s, %s)" % (self.kind, self.name)


def resolve_caller(req) -> Caller:
    if _is_admin_or_internal(req):
        return Caller("admin")
    if _is_loopback(req):
        return Caller("loopback")
    cands = _viewer_candidates(req)
    for c in cands:
        name = KEYSTORE.lookup(hash_key(c))
        if name:
            return Caller("viewer", name)
    if cands:
        return Caller("bad_viewer")
    return Caller("anonymous")


def client_ip(req) -> str:
    return (req.headers.get("CF-Connecting-IP")
            or (req.headers.get("X-Forwarded-For", "") or "").split(",")[0].strip()
            or req.remote_addr or "")


# ───────────────────────────────────────────────────────────── key store ──

_KEYS_DDL = (
    "CREATE TABLE IF NOT EXISTS ops_viewer_keys ("
    " id BIGSERIAL PRIMARY KEY,"
    " name TEXT NOT NULL,"
    " key_sha256 TEXT NOT NULL UNIQUE,"
    " note TEXT,"
    " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
    " revoked_at TIMESTAMPTZ,"
    " last_used_at TIMESTAMPTZ)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ops_viewer_keys_active_name"
    " ON ops_viewer_keys (name) WHERE revoked_at IS NULL",
)
_DENIALS_DDL = (
    "CREATE TABLE IF NOT EXISTS ops_gate_denials ("
    " id BIGSERIAL PRIMARY KEY,"
    " first_at TIMESTAMPTZ NOT NULL,"
    " last_at TIMESTAMPTZ NOT NULL,"
    " hits INTEGER NOT NULL DEFAULT 1,"
    " ip TEXT,"
    " user_agent TEXT,"
    " method TEXT,"
    " path TEXT,"
    " reason TEXT,"
    " mode TEXT,"
    " status INTEGER)",
    "CREATE INDEX IF NOT EXISTS ops_gate_denials_last_at"
    " ON ops_gate_denials (last_at)",
)

_schema_lock = threading.Lock()
_schema_ready = False


def _dsn() -> str:
    return (os.environ.get("DATABASE_URL")
            or os.environ.get("NEON_DATABASE_URL") or "").strip()


def _connect(timeout: int = 5):
    import psycopg2
    dsn = _dsn()
    if not dsn:
        raise RuntimeError("ops_viewer_gate: no DATABASE_URL")
    return psycopg2.connect(dsn, connect_timeout=timeout)


def ensure_schema() -> None:
    """Create both tables (idempotent). DDL on its own autocommit connection."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        from db_utils import ddl_cursor
        with ddl_cursor() as cur:
            for stmt in _KEYS_DDL + _DENIALS_DDL:
                cur.execute(stmt)
        _schema_ready = True


class DbKeyStore:
    """sha256 -> name for ACTIVE keys, cached for _KEY_CACHE_TTL_S.

    A stale cache refreshes in the background (single flight), so a request
    never waits on the DB except for the very first load. If the DB is
    unreachable the last good map is kept: revocation then waits for the DB,
    and the admin key keeps working regardless."""

    def __init__(self):
        self._map: dict = {}
        self._loaded_at = 0.0
        self._loaded_once = False
        self._lock = threading.Lock()
        self._refreshing = False

    def _load(self) -> None:
        try:
            ensure_schema()
            conn = _connect(timeout=3)
            try:
                cur = conn.cursor()
                cur.execute("SELECT key_sha256, name FROM ops_viewer_keys"
                            " WHERE revoked_at IS NULL")
                m = {r[0]: r[1] for r in cur.fetchall()}
            finally:
                conn.close()
            with self._lock:
                self._map = m
                self._loaded_at = time.time()
                self._loaded_once = True
        except Exception as e:
            log.warning("ops_viewer_gate: key cache refresh failed: %s", e)
            with self._lock:
                self._loaded_at = time.time()  # back off a full TTL
                self._loaded_once = True
        finally:
            with self._lock:
                self._refreshing = False

    def _kick(self) -> None:
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        threading.Thread(target=self._load, name="ops-viewer-keys",
                         daemon=True).start()

    def invalidate(self) -> None:
        with self._lock:
            self._loaded_at = 0.0

    def lookup(self, sha: str):
        if not self._loaded_once:
            with self._lock:
                self._refreshing = True
            self._load()
        elif time.time() - self._loaded_at > _KEY_CACHE_TTL_S:
            self._kick()
        return self._map.get(sha)

    def names(self) -> list:
        return sorted(set(self._map.values()))


# ────────────────────────────────────────────────────────────── recorder ──

class DbRecorder:
    """Aggregates would-be denials in memory and flushes them every
    _FLUSH_INTERVAL_S from one daemon thread. Bounded: past _MAX_PENDING
    distinct tuples in a window, new tuples are counted as dropped, not
    stored, so a flood cannot grow memory or the table without limit."""

    def __init__(self):
        self._pending: dict = {}
        self._used: set = set()
        self._dropped = 0
        self._lock = threading.Lock()
        self._thread = None
        self._last_prune = 0.0

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        with self._lock:
            if self._thread is not None:
                return
            t = threading.Thread(target=self._run, name="ops-gate-recorder",
                                 daemon=True)
            self._thread = t
        t.start()

    def record_denial(self, row: dict) -> None:
        key = (row["ip"], row["user_agent"], row["method"], row["path"],
               row["reason"], row["mode"], row["status"])
        now = datetime.now(timezone.utc)
        first = False
        with self._lock:
            slot = self._pending.get(key)
            if slot is None:
                if len(self._pending) >= _MAX_PENDING:
                    self._dropped += 1
                    return
                self._pending[key] = [now, now, 1]
                first = True
            else:
                slot[1] = now
                slot[2] += 1
        if first:
            log.info("ops_gate_denial %s", json.dumps(row, sort_keys=True))
        self._ensure_thread()

    def record_use(self, name: str) -> None:
        with self._lock:
            self._used.add(name)
        self._ensure_thread()

    def _run(self) -> None:
        while True:
            time.sleep(_FLUSH_INTERVAL_S)
            try:
                self.flush()
            except Exception as e:
                log.warning("ops_viewer_gate: flush failed: %s", e)

    def flush(self) -> int:
        with self._lock:
            pending, self._pending = self._pending, {}
            used, self._used = self._used, set()
            dropped, self._dropped = self._dropped, 0
        if dropped:
            log.warning("ops_viewer_gate: dropped %d denial tuples (cap %d)",
                        dropped, _MAX_PENDING)
        if not pending and not used:
            return 0
        if not _dsn():
            return 0
        ensure_schema()
        conn = _connect()
        try:
            cur = conn.cursor()
            for (ip, ua, method, path, reason, mode, status), (f, l, n) in pending.items():
                cur.execute(
                    """INSERT INTO ops_gate_denials (first_at, last_at, hits, ip,
                           user_agent, method, path, reason, mode, status)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    (f, l, n, ip, ua, method, path, reason, mode, status))
            if used:
                cur.execute(
                    "UPDATE ops_viewer_keys SET last_used_at = NOW()"
                    " WHERE revoked_at IS NULL AND name = ANY(%s)",
                    (sorted(used),))
            if time.time() - self._last_prune > _PRUNE_INTERVAL_S:
                cur.execute(
                    "DELETE FROM ops_gate_denials"
                    " WHERE last_at < NOW() - INTERVAL '7 days'")
                self._last_prune = time.time()
            conn.commit()
        finally:
            conn.close()
        return len(pending)


KEYSTORE = DbKeyStore()
RECORDER = DbRecorder()


# ────────────────────────────────────────────────────────────────── gate ──

def _deny(status: int):
    # Flag read by routes/paywall_hint_middleware: never pitch or A/B-log
    # an ops refusal as if it were a blocked prospect.
    try:
        g.ops_gate_denied = True
    except Exception:
        pass
    body = jsonify(error="unauthorized" if status == 401 else "forbidden")
    body.status_code = status
    body.headers["Cache-Control"] = "no-store, private"
    if status == 401:
        body.headers["WWW-Authenticate"] = 'Bearer realm="dchub-ops"'
    return body


def ops_gate():
    """The before_request hook. Returns None to continue, or a response."""
    mode = gate_mode()
    if mode == "off":
        return None
    path = request.path
    if not is_gated_path(path):
        return None
    method = (request.method or "GET").upper()
    if method == "OPTIONS":
        return None  # CORS preflight carries no credential and returns no data
    who = resolve_caller(request)
    g.ops_gate_caller = who
    g.ops_gate_mode = mode
    if who.kind in ("admin", "loopback"):
        return None
    if who.kind == "viewer" and method in READ_METHODS:
        try:
            RECORDER.record_use(who.name)
        except Exception:
            pass
        log.info("ops_gate_allow %s", json.dumps({
            "key": who.name, "method": method, "path": path,
            "ip": client_ip(request)}, sort_keys=True))
        if request.args.get("admin_key", "").startswith(VIEWER_PREFIX) \
                and not normalize_path(path).startswith("/api/"):
            g.ops_gate_set_cookie = request.args.get("admin_key")
        return None
    if who.kind == "viewer":
        reason, status = "viewer_key_on_write", 403
    elif who.kind == "bad_viewer":
        reason, status = "unknown_or_revoked_viewer_key", 401
    else:
        reason, status = "no_credential", 401
    # A request the web replica relayed to the worker was already recorded
    # there; do not count it twice.
    relayed = (request.headers.get("X-Dchub-Delegated")
               and (os.environ.get("DCHUB_ROLE", "") or "").strip().lower() == "worker")
    if not relayed:
        try:
            RECORDER.record_denial({
                "ip": client_ip(request)[:64],
                "user_agent": (request.headers.get("User-Agent") or "")[:300],
                "method": method,
                "path": normalize_path(path)[:300],
                "reason": reason,
                "mode": mode,
                "status": status,
            })
        except Exception as e:
            log.warning("ops_viewer_gate: record failed: %s", e)
    if mode == "enforce":
        return _deny(status)
    return None


def _after(resp):
    try:
        raw = getattr(g, "ops_gate_set_cookie", None)
        if raw and resp.status_code < 400:
            resp.set_cookie(COOKIE_NAME, raw, max_age=12 * 3600, secure=True,
                            httponly=True, samesite="Strict", path="/")
        if getattr(g, "ops_gate_mode", None) == "enforce" \
                and getattr(g, "ops_gate_caller", None) is not None:
            resp.headers["Cache-Control"] = "no-store, private"
    except Exception:
        pass
    return resp


def install(app) -> None:
    """Register the gate AHEAD of every before_request hook already on the
    app, plus the report/mint endpoints. Call once, right after app creation."""
    funcs = app.before_request_funcs.setdefault(None, [])
    if ops_gate not in funcs:
        funcs.insert(0, ops_gate)
    app.after_request(_after)
    app.register_blueprint(ops_gate_bp)


# ──────────────────────────────────────────────────────── admin endpoints ──

ops_gate_bp = Blueprint("ops_viewer_gate", __name__)


def _require_read():
    """Viewer, admin or loopback — enforced here in EVERY mode, because in
    log mode the gate itself lets keyless requests through."""
    who = resolve_caller(request)
    if who.kind in ("admin", "loopback", "viewer"):
        return None
    return _deny(401)


def _require_admin_key():
    """Minting and revocation take the real admin key in the X-Admin-Key
    HEADER only: not an internal key, not a viewer key, not a query arg."""
    got = _first_token(request.headers.get("X-Admin-Key"))
    want = _first_token(os.environ.get("DCHUB_ADMIN_KEY"))
    if got and want and hmac.compare_digest(got, want):
        return None
    return _deny(401)


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store, private"
    return resp


@ops_gate_bp.get("/api/v1/admin/ops-gate/status")
def ops_gate_status():
    denied = _require_read()
    if denied is not None:
        return denied
    rows = []
    err = None
    try:
        ensure_schema()
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT name, created_at, revoked_at, last_used_at, note"
                        " FROM ops_viewer_keys ORDER BY name, created_at")
            for n, c, r, u, note in cur.fetchall():
                rows.append({"name": n,
                             "created_at": c.isoformat() if c else None,
                             "revoked_at": r.isoformat() if r else None,
                             "last_used_at": u.isoformat() if u else None,
                             "note": note})
        finally:
            conn.close()
    except Exception as e:
        err = type(e).__name__
    return _no_store(jsonify(mode=gate_mode(), keys=rows, error=err,
                             gated_subtrees=list(GATED_SUBTREES + _extra_subtrees()),
                             gated_exact=sorted(GATED_EXACT),
                             public_exact=sorted(public_exact())))


@ops_gate_bp.get("/api/v1/admin/ops-gate/denials")
def ops_gate_denials():
    """Distinct IPs and user agents that hit a gated path without a valid
    credential, over the last N days (max 7 — the retention)."""
    denied = _require_read()
    if denied is not None:
        return denied
    try:
        days = max(1, min(RETENTION_DAYS, int(request.args.get("days", RETENTION_DAYS))))
    except (TypeError, ValueError):
        days = RETENTION_DAYS
    try:
        ensure_schema()
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT ip, SUM(hits), MIN(first_at), MAX(last_at),"
                " ARRAY_AGG(DISTINCT user_agent), ARRAY_AGG(DISTINCT path),"
                " ARRAY_AGG(DISTINCT reason)"
                " FROM ops_gate_denials"
                " WHERE last_at >= NOW() - (%s * INTERVAL '1 day')"
                " GROUP BY ip ORDER BY SUM(hits) DESC LIMIT 500", (days,))
            by_ip = [{"ip": ip, "hits": int(h or 0),
                      "first_at": f.isoformat() if f else None,
                      "last_at": l.isoformat() if l else None,
                      "user_agents": sorted(u for u in (uas or []) if u)[:20],
                      "paths": sorted(p for p in (ps or []) if p)[:50],
                      "reasons": sorted(r for r in (rs or []) if r)}
                     for ip, h, f, l, uas, ps, rs in cur.fetchall()]
            cur.execute(
                "SELECT user_agent, SUM(hits), COUNT(DISTINCT ip)"
                " FROM ops_gate_denials"
                " WHERE last_at >= NOW() - (%s * INTERVAL '1 day')"
                " GROUP BY user_agent ORDER BY SUM(hits) DESC LIMIT 200", (days,))
            by_ua = [{"user_agent": ua, "hits": int(h or 0), "distinct_ips": int(n or 0)}
                     for ua, h, n in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        return _no_store(jsonify(error=type(e).__name__, days=days)), 503
    return _no_store(jsonify(mode=gate_mode(), days=days,
                             distinct_ips=len(by_ip), by_ip=by_ip, by_user_agent=by_ua))


@ops_gate_bp.post("/api/v1/admin/ops-gate/keys")
def ops_gate_mint():
    """Mint a named viewer key. Body: {"name": "...", "note": "..."}.
    Returns the raw key ONCE. A name with an active key must be revoked first."""
    denied = _require_admin_key()
    if denied is not None:
        return denied
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()
    note = str(body.get("note") or "").strip()[:300] or None
    if not name or len(name) > 64 or not all(c.isalnum() or c in "-_." for c in name):
        return jsonify(error="name must be 1-64 chars of [A-Za-z0-9-_.]"), 400
    raw = VIEWER_PREFIX + secrets.token_urlsafe(32)
    try:
        ensure_schema()
        conn = _connect()
        try:
            cur = conn.cursor()
            # Bare DO NOTHING covers both unique indexes: the partial one
            # (one ACTIVE key per name) and key_sha256. rowcount 0 = refused,
            # including when two mints of the same name race.
            cur.execute("""INSERT INTO ops_viewer_keys (name, key_sha256, note)
                           VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                        (name, hash_key(raw), note))
            inserted = cur.rowcount
            conn.commit()
            if inserted != 1:
                return _no_store(jsonify(error="active key exists for this name;"
                                               " revoke it first")), 409
        finally:
            conn.close()
    except Exception as e:
        return _no_store(jsonify(error=type(e).__name__)), 503
    KEYSTORE.invalidate()
    log.info("ops_viewer_gate: minted viewer key name=%s", name)
    return _no_store(jsonify(name=name, key=raw,
                             note="shown once; store it in the caller's secret store")), 201


@ops_gate_bp.post("/api/v1/admin/ops-gate/keys/<name>/revoke")
def ops_gate_revoke(name):
    denied = _require_admin_key()
    if denied is not None:
        return denied
    try:
        ensure_schema()
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE ops_viewer_keys SET revoked_at = NOW()"
                        " WHERE name = %s AND revoked_at IS NULL", (name,))
            n = cur.rowcount
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        return _no_store(jsonify(error=type(e).__name__)), 503
    KEYSTORE.invalidate()
    log.info("ops_viewer_gate: revoked viewer key name=%s rows=%s", name, n)
    return _no_store(jsonify(name=name, revoked=n)), (200 if n else 404)
