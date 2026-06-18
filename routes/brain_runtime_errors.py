"""r-runtime-capture (2026-06-18) — make the brain SEE runtime errors.

WHY
===
The brain's detectors (consistency radar, self-heal, Inspector) read
HTTP probes + heal findings — NOT the process's runtime logs. So when a
DB/runtime error fires at WARNING+ (the NOW()::TEXT cast, a non-IMMUTABLE
index, the OSM crawler's "FORCED RECLAIM: held 62s" pool reclaims), it
scrolls past in the Railway logs and NEVER becomes a finding. The brain
literally cannot see the bugs that bite production hardest.

WHAT
====
A lightweight, in-memory runtime-error capture:
  · _RuntimeErrorHandler — a logging.Handler that grabs every WARNING+
    record into a CAPPED deque(maxlen=300) plus a dict that counts
    occurrences keyed by a NORMALIZED message (digits / hex / timestamps
    stripped) so "held 62s" and "held 71s" collapse into ONE pattern.
  · It is attached to the ROOT logger at app startup (from main.py).
  · It NEVER writes to the DB per log record (that would add load and
    worsen the very flapping we're diagnosing) — in-memory only.
  · It is TOTALLY exception-safe: a logging handler must never raise, or
    it can wedge the logging subsystem for the whole process.

  · GET /api/v1/brain/runtime-errors (admin-gated, same gate as
    brain_inspector._admin_ok) returns the top recurring runtime errors
    so the brain (and a human) can finally read them.
"""
import os
import re
import time
import logging
import threading
from collections import deque

from flask import Blueprint, jsonify, request

try:
    from internal_auth import accepted_internal_keys
except Exception:  # pragma: no cover — keep import-safe in odd contexts
    def accepted_internal_keys():
        return set()

logger = logging.getLogger(__name__)
brain_runtime_errors_bp = Blueprint("brain_runtime_errors", __name__)


# ── Auth (mirrors routes/brain_inspector._admin_ok exactly) ──────────
_INTERNAL_KEYS = accepted_internal_keys()
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


# ── Message normalization ────────────────────────────────────────────
# Strip the volatile parts of a message so structurally-identical errors
# group into ONE pattern: "held 62s" and "held 71s" → "held <n>s".
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
_LONGHEX_RE = re.compile(r"\b[0-9a-fA-F]{12,}\b")
_ISO_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?")
_NUM_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")


def _normalize_message(msg: str) -> str:
    """Collapse digits / hex / timestamps so recurring runtime errors
    group by their stable shape. Defensive: never raises."""
    try:
        s = str(msg)
    except Exception:
        return "<unprintable>"
    try:
        s = _ISO_TS_RE.sub("<ts>", s)
        s = _HEX_RE.sub("<hex>", s)
        s = _LONGHEX_RE.sub("<hex>", s)
        s = _NUM_RE.sub("<n>", s)
        s = _WS_RE.sub(" ", s).strip()
        return s[:300]
    except Exception:
        return "<normalize-error>"


# ── The capped in-memory store ───────────────────────────────────────
_CAP = int(os.environ.get("BRAIN_RUNTIME_ERR_CAP", "300") or "300")
_lock = threading.Lock()
# Rolling buffer of the most recent raw events (bounded).
_recent: deque = deque(maxlen=_CAP)
# Aggregate per normalized-pattern stats. Bounded by _MAX_PATTERNS so a
# pathological high-cardinality logger can't grow this without limit.
_MAX_PATTERNS = 500
_patterns: dict = {}


def _record(level_no: int, level_name: str, logger_name: str,
            raw_msg: str) -> None:
    """Add one captured record to the in-memory structures. Holds the
    lock only for the dict/deque mutation (microseconds) — never across
    I/O. Caller (the handler) guarantees this can't raise out."""
    pattern = _normalize_message(raw_msg)
    now = time.time()
    with _lock:
        ev = _patterns.get(pattern)
        if ev is None:
            # Cap distinct patterns — once full, fold new ones into a
            # catch-all bucket so memory stays bounded.
            if len(_patterns) >= _MAX_PATTERNS:
                pattern = "<overflow:other>"
                ev = _patterns.get(pattern)
            if ev is None:
                ev = {
                    "pattern": pattern,
                    "count": 0,
                    "level": level_name,
                    "level_no": level_no,
                    "logger": logger_name,
                    "first_seen": now,
                    "last_seen": now,
                    "sample": str(raw_msg)[:400],
                }
                _patterns[pattern] = ev
        ev["count"] += 1
        ev["last_seen"] = now
        # Track the highest-severity level + most recent sample/logger.
        if level_no >= ev.get("level_no", 0):
            ev["level"] = level_name
            ev["level_no"] = level_no
        ev["logger"] = logger_name
        ev["sample"] = str(raw_msg)[:400]
        _recent.append({
            "ts": now,
            "level": level_name,
            "logger": logger_name,
            "pattern": pattern,
        })


class _RuntimeErrorHandler(logging.Handler):
    """Capture WARNING+ log records into the in-memory store. This is a
    logging handler, so its emit() MUST NEVER raise — any exception is
    swallowed (the base class would route to handleError otherwise, but
    we don't even want the overhead/noise). In-memory only; no DB."""

    def __init__(self, level=logging.WARNING):
        super().__init__(level=level)

    def emit(self, record):  # noqa: D401
        try:
            # Skip our own logger to avoid any feedback loop.
            if record.name == __name__:
                return
            try:
                msg = record.getMessage()
            except Exception:
                msg = str(getattr(record, "msg", ""))
            _record(record.levelno, record.levelname, record.name, msg)
        except Exception:
            # A logging handler must never raise. Swallow everything.
            try:
                pass
            except Exception:
                pass


_handler_singleton = None


def attach_runtime_capture_handler():
    """Attach the capture handler to the ROOT logger. Idempotent + fully
    guarded — called from main.py at startup. Returns True if attached
    (or already attached), False on any failure (never raises)."""
    global _handler_singleton
    try:
        root = logging.getLogger()
        # Idempotency: don't double-attach if already present.
        for h in root.handlers:
            if isinstance(h, _RuntimeErrorHandler):
                _handler_singleton = h
                return True
        h = _RuntimeErrorHandler(level=logging.WARNING)
        root.addHandler(h)
        _handler_singleton = h
        return True
    except Exception as e:
        try:
            logger.warning("[runtime-capture] attach failed: %s", e)
        except Exception:
            pass
        return False


# ── Endpoint ─────────────────────────────────────────────────────────
@brain_runtime_errors_bp.route("/api/v1/brain/runtime-errors",
                               methods=["GET"])
def runtime_errors():
    """Admin: the top recurring runtime errors the process has logged at
    WARNING+ since startup. This is what finally lets the brain (and a
    human) SEE runtime/DB bugs that never reach an HTTP/heal finding —
    the forced-reclaims, cast errors, non-IMMUTABLE index failures, etc.

    Query params:
      · limit       — max patterns to return (default 50, cap 300)
      · min_level   — WARNING|ERROR|CRITICAL filter (default WARNING)
    """
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403

    try:
        limit = int(request.args.get("limit", "50"))
    except Exception:
        limit = 50
    limit = max(1, min(limit, 300))

    min_level_name = (request.args.get("min_level") or "WARNING").upper()
    min_level_no = logging.getLevelName(min_level_name)
    if not isinstance(min_level_no, int):
        min_level_no = logging.WARNING

    with _lock:
        snapshot = list(_patterns.values())
        total_recent = len(_recent)
    attached = _handler_singleton is not None

    rows = [r for r in snapshot if r.get("level_no", 0) >= min_level_no]
    rows.sort(key=lambda r: (r.get("count", 0), r.get("last_seen", 0)),
              reverse=True)
    rows = rows[:limit]

    def _iso(ts):
        try:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
        except Exception:
            return None

    items = [{
        "pattern": r.get("pattern"),
        "count": r.get("count", 0),
        "level": r.get("level"),
        "logger": r.get("logger"),
        "first_seen": _iso(r.get("first_seen")),
        "last_seen": _iso(r.get("last_seen")),
        "sample": r.get("sample"),
    } for r in rows]

    resp = jsonify(
        ok=True,
        capture_attached=attached,
        distinct_patterns=len(snapshot),
        recent_buffer=total_recent,
        recent_buffer_cap=_CAP,
        min_level=min_level_name,
        count=len(items),
        items=items,
        note=("In-memory only (resets on each process restart / replica). "
              "WARNING+ records, grouped by a digit/hex/timestamp-normalized "
              "message so recurring runtime errors collapse into one pattern."),
    )
    resp.headers["Cache-Control"] = "no-store, private"
    return resp


def _smoke():
    logger.info("[brain-runtime-errors] ready · "
                "GET /api/v1/brain/runtime-errors (admin) · "
                "in-memory cap=%d", _CAP)


_smoke()
