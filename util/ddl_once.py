"""Run idempotent schema DDL at most once per process.

WHY — a measured outage, 2026-08-31 ~09:5x UTC on the primary:

    pid 29441  COPY public.fiber_kmz_routes_old_0822 ...   549s   (the nightly dump)
    pid 30160  ALTER TABLE auto_trial_keys ADD COLUMN ...  268s   waiting
    pid 30760  CREATE TABLE IF NOT EXISTS auto_trial_keys    7s   waiting on 30160
    -> 17 of 20 active backends blocked

`ADD COLUMN IF NOT EXISTS` reads as free. It is not. Once the column exists the
statement does nothing — but it still REQUESTS ACCESS EXCLUSIVE, and in
PostgreSQL a *pending* exclusive request blocks every lock request that arrives
after it. A pg_dump holds ACCESS SHARE on every table for its whole run, so:

    dump (ACCESS SHARE, minutes)
      -> our no-op ALTER queues for ACCESS EXCLUSIVE
        -> every ordinary SELECT/UPDATE queues behind the ALTER

Reads that would have coexisted with the dump perfectly happily stall instead,
and the cause is our own DDL, not the backup.

The pattern that produces this is a "defensive" ALTER inline in a hot function —
`_remember_share_urn` ran one immediately before a routine UPDATE, and its own
docstring called it defensive. Defensive against a schema that has been correct
for months, at the cost of an exclusive lock request per call.

USE

    from util.ddl_once import ensure_once

    def _remember_share_urn(...):
        c = _conn()
        ensure_once("social_media_posts.share_urn", c, (
            "ALTER TABLE social_media_posts ADD COLUMN IF NOT EXISTS share_urn TEXT",
        ))
        ...                       # the real work

CONTRACT

  * At most one execution per (process, key). A fresh worker still runs it, so
    the schema guarantee is unchanged — what disappears is the per-CALL lock
    request.
  * Only a CLEAN pass latches. A failure leaves the key unset and the next call
    retries: a half-applied schema recorded as done would be worse than the
    contention this exists to remove.
  * Best-effort. Any exception is swallowed and reported False; callers already
    treated this DDL as optional, and a schema probe must never break the
    request it was guarding.
  * Not for DDL whose result varies at runtime (per-tenant tables, dynamic
    names). Those genuinely need to run per call, and should not use this.

`DCHUB_DDL_ONCE_ALWAYS=1` restores per-call execution everywhere, for the case
where a migration must be re-applied without a redeploy.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

# key -> True once a clean pass has completed in THIS process.
_DONE: dict[str, bool] = {}
_LOCK = threading.Lock()


def _always() -> bool:
    return str(os.environ.get("DCHUB_DDL_ONCE_ALWAYS", "")).strip().lower() in (
        "1", "true", "yes", "on")


def already_done(key: str) -> bool:
    """True if a clean pass for `key` has completed in this process."""
    with _LOCK:
        return bool(_DONE.get(key))


def reset(key: str | None = None) -> None:
    """Forget one key, or all of them. For tests and for a deliberate re-run."""
    with _LOCK:
        if key is None:
            _DONE.clear()
        else:
            _DONE.pop(key, None)


def ensure_once(key: str, conn, statements) -> bool:
    """Execute `statements` once per process for `key`. Returns True if the DDL
    ran cleanly on THIS call, False if it was skipped or failed.

    The return value distinguishes "skipped because already done" from "failed"
    only via already_done() — callers that care should check it. Most callers do
    not: this is a schema guarantee, not a result."""
    if not key or conn is None or not statements:
        return False

    if not _always():
        with _LOCK:
            if _DONE.get(key):
                return False

    try:
        with conn.cursor() as cur:
            for sql in statements:
                cur.execute(sql)
        try:
            conn.commit()
        except Exception:
            pass
        # Latch ONLY after every statement succeeded and the commit was
        # attempted. Setting it earlier would record a partial schema as done.
        with _LOCK:
            _DONE[key] = True
        return True
    except Exception as e:  # noqa: BLE001 — a schema probe must not break a request
        logger.debug("ddl_once(%s) failed, will retry next call: %s", key, e)
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def ensure_once_call(key: str, fn) -> bool:
    """Run an arbitrary zero-arg callable once per process for `key`.

    For call sites that already own their connection handling — the
    `_execute("ALTER TABLE ...")` shape, where there is no conn to hand in.
    Same contract as ensure_once: latch only on a clean pass, swallow failures
    so the next call retries, honour DCHUB_DDL_ONCE_ALWAYS."""
    if not key or fn is None:
        return False
    if not _always():
        with _LOCK:
            if _DONE.get(key):
                return False
    try:
        fn()
        with _LOCK:
            _DONE[key] = True
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("ddl_once_call(%s) failed, will retry next call: %s", key, e)
        return False
