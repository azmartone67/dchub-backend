"""_swallowed_writes.py — visibility for INTENTIONALLY-swallowed DB writes.

Motivated by the 2026-07-11 fix-verifier postmortem: an INSERT into
nonexistent columns sat inside a bare `except: pass` for WEEKS
(routes/brain_fix_outcome_verify.record_fix_outcome) and the verifier was
silently dead the whole time. The swallow itself was DELIBERATE (a write
failure must never break a tick / block a merge) — the bug was that it was
INVISIBLE. This module keeps the swallow and kills the invisibility.

Usage, from inside an except block that intentionally eats a DB-write error:

    except Exception:
        note_swallowed_write("brain_automerge_log", where="brain_automerge._log_row")
        return False

note_swallowed_write():
  · logs ONE WARNING for the first failure at a (where, table) site and then
    every _LOG_EVERY-th after that (a schema mismatch fails on EVERY call —
    without the cap it would storm the logs);
  · bumps an in-process counter readable via swallowed_write_counts() so the
    metric ground-truth checker / admin surfaces can report chronic sites;
  · pulls the active exception off sys.exc_info() itself, so call sites do
    not need to bind `as e`;
  · NEVER raises — it is called from paths whose contract is "never fatal".

stdlib-only (no flask / psycopg2) so any route module can import it at the
top level and unit tests can import it with no environment at all.
"""
from __future__ import annotations

import logging
import sys
import threading

_log = logging.getLogger("dchub.swallowed_write")

_LOG_EVERY = 25  # after the first, log every Nth repeat per (where, table)

_lock = threading.Lock()
_counts: dict[tuple[str, str], int] = {}


def note_swallowed_write(table: str, where: str = "") -> None:
    """Record that a DB write to `table` failed and was intentionally
    swallowed at `where` (conventionally "module.function"). Logs a
    rate-limited WARNING with the active exception and counts the event.
    Never raises."""
    try:
        exc = sys.exc_info()[1]
        key = ((where or "?"), (table or "?"))
        with _lock:
            n = _counts.get(key, 0) + 1
            _counts[key] = n
        if n == 1 or (n % _LOG_EVERY) == 0:
            _log.warning(
                "swallowed DB write #%d table=%s at=%s err=%s: %s",
                n, key[1], key[0],
                (type(exc).__name__ if exc is not None else "no-active-exception"),
                (str(exc)[:200] if exc is not None else ""),
            )
    except Exception:
        pass


def swallowed_write_counts() -> dict:
    """Snapshot of swallow counters for this process:
    {"module.function:table": count}. Per-process and reset on restart —
    a health signal, not an accounting ledger."""
    try:
        with _lock:
            return {f"{w}:{t}": n for (w, t), n in _counts.items()}
    except Exception:
        return {}


def _reset_for_tests() -> None:
    """Test hook: clear counters (unit tests only)."""
    with _lock:
        _counts.clear()
