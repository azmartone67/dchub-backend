"""_conn_provenance.py — name the ENDPOINT that a failed write was attempted on.

WHY THIS EXISTS. dchub-backend talks to two Neon computes: a writable primary
(DATABASE_URL) and a read-only replica (NEON_REPLICA_URL), each with its own
pool. When a write fails with SQLSTATE 25006 the log records WHICH TABLE but
never WHICH ENDPOINT, so every explanation is equally consistent with the
evidence and none can be refuted.

MEASURED 2026-09-09: the failures are NOT an outage. While `surface_telemetry`,
`agent_requests`, `discovered_platforms` and `discovery_hits` were all failing
25006, `ai_requests` took 2,256 rows in 30 minutes on the same database. So a
subset of write attempts land on something read-only while most do not, and the
only fact that separates them — the endpoint host — is the one nobody logs.

This module adds that one fact. It is LOG-ONLY: it never raises, never changes
a connection, and never alters control flow. Read it back with

    railway logs | grep conn-provenance

★ It reports the host it OBSERVED, not the host it expected. A probe that
  assumed the answer would just restate the assumption.
"""
import logging
import os
from urllib.parse import urlparse

log = logging.getLogger("conn_provenance")

_UNKNOWN = "unknown"


def _host_of_url(url):
    """Host inside a libpq URL, or '' when unset/unparseable."""
    if not url:
        return ""
    try:
        return (urlparse(url).hostname or "")
    except Exception:  # noqa: BLE001 — a malformed DSN must not break logging
        return ""


def expected_hosts():
    """(primary_host, replica_host) as THIS process resolves them.

    Read at call time, not import time: the point of the probe is to report what
    the running process actually has, and an import-time snapshot would hide a
    later divergence.
    """
    primary = _host_of_url(os.environ.get("DATABASE_URL")
                           or os.environ.get("NEON_DATABASE_URL"))
    replica = _host_of_url(os.environ.get("NEON_REPLICA_URL")
                           or os.environ.get("DATABASE_READ_URL"))
    return primary, replica


def conn_host(conn):
    """Host this connection is actually attached to, or 'unknown'.

    Tolerates the wrapper objects used around pooled connections.

    ★ A str is passed straight through, and callers should PREFER that.
      psycopg2 raises InterfaceError("connection already closed") from
      get_dsn_parameters() once the connection is closed — VERIFIED against
      the production DSN on 2026-09-09 — and the writers instrumented here all
      close in a `finally` that runs BEFORE their except block. Reading the
      host off the corpse always yields 'unknown', which is a probe that
      cannot fail and therefore cannot inform. Capture the host when the
      connection is opened and hand the string in.
    """
    if isinstance(conn, str):
        return conn or _UNKNOWN
    # Wrappers NEST: db_utils.PGConnectionWrapper._conn holds a
    # main._PoolConnWrapper, whose raw psycopg2 connection is ._raw. Peeking a
    # single level finds a wrapper, not a connection, and reports 'unknown'.
    seen = 0
    candidate = conn
    while candidate is not None and seen < 6:
        seen += 1
        try:
            params = candidate.get_dsn_parameters()
        except Exception:  # noqa: BLE001 — closed conn, or a wrapper without it
            params = None
        if params:
            host = params.get("host") or ""
            if host:
                return host
        nxt = None
        for attr in ("_raw", "_conn", "conn", "connection"):
            nxt = getattr(candidate, attr, None)
            if nxt is not None and nxt is not candidate:
                break
            nxt = None
        candidate = nxt
    return _UNKNOWN


def classify(host):
    """'primary' | 'replica' | 'unknown' | 'other:<host>'."""
    if not host or host == _UNKNOWN:
        return _UNKNOWN
    primary, replica = expected_hosts()
    if primary and host == primary:
        return "primary"
    if replica and host == replica:
        return "replica"
    return "other:" + host


def session_state(conn):
    """What the SERVER says about this very connection, or None.

    ★ Round 1 answered "which endpoint" — every failing write was on the
      PRIMARY (endpoint=primary, sqlstate=25006), which exonerated the read
      replica entirely. That leaves the harder question: a session on a
      WRITABLE primary refusing writes. The distinguishing facts all live on
      the server side, so ask the server rather than infer.

    The connection has an ABORTED transaction at this point — the failed write
    is what brought us here — so roll back first or every query returns 25P02.
    """
    if isinstance(conn, str) or conn is None:
        return None
    target = conn
    for _ in range(6):
        if hasattr(target, "cursor"):
            break
        nxt = None
        for attr in ("_raw", "_conn", "conn", "connection"):
            nxt = getattr(target, attr, None)
            if nxt is not None and nxt is not target:
                break
            nxt = None
        if nxt is None:
            return None
        target = nxt
    try:
        try:
            target.rollback()   # 25P02 otherwise: the tx is already aborted
        except Exception:  # noqa: BLE001
            pass
        with target.cursor() as cur:
            cur.execute(
                "SELECT current_setting('transaction_read_only'),"
                "       current_setting('default_transaction_read_only'),"
                "       current_user, current_database(), pg_backend_pid(),"
                "       pg_is_in_recovery()")
            row = cur.fetchone()
        params = {}
        try:
            params = target.get_dsn_parameters() or {}
        except Exception:  # noqa: BLE001
            pass
        return {
            "tx_read_only": row[0], "default_read_only": row[1],
            "user": row[2], "db": row[3], "pid": row[4], "in_recovery": row[5],
            "dsn_options": params.get("options") or "",
            "dsn_user": params.get("user") or "",
            "dsn_db": params.get("dbname") or "",
        }
    except Exception:  # noqa: BLE001
        return None


_ENDPOINTS_LOGGED = False


def note_failed_write(conn, table, where, exc=None):
    """Log the provenance of a write that just failed. Never raises."""
    global _ENDPOINTS_LOGGED
    try:
        if not _ENDPOINTS_LOGGED:
            _ENDPOINTS_LOGGED = True
            log_startup_endpoints()   # once per process, so reports read alone
        host = conn_host(conn)
        verdict = classify(host)
        code = getattr(exc, "pgcode", None)
        log.warning(
            "conn-provenance: FAILED WRITE table=%s at=%s endpoint=%s host=%s "
            "sqlstate=%s err=%s",
            table, where, verdict, host or _UNKNOWN, code,
            (str(exc)[:120] if exc is not None else ""),
        )
        st = session_state(conn)
        if st:
            log.warning("conn-provenance: SESSION table=%s tx_read_only=%s "
                        "default_read_only=%s user=%s db=%s pid=%s "
                        "in_recovery=%s dsn_options=%r dsn_user=%s dsn_db=%s",
                        table, st["tx_read_only"], st["default_read_only"],
                        st["user"], st["db"], st["pid"], st["in_recovery"],
                        st["dsn_options"], st["dsn_user"], st["dsn_db"])
    except Exception:  # noqa: BLE001 — instrumentation must never break a caller
        pass


def log_startup_endpoints():
    """One line per process so every later report is self-describing."""
    try:
        primary, replica = expected_hosts()
        log.warning("conn-provenance: endpoints primary=%s replica=%s",
                    primary or "(unset)", replica or "(unset)")
    except Exception:  # noqa: BLE001
        pass
