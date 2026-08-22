"""Leader-lock hygiene: find the session holding the singleton advisory lock and
terminate it ONLY when it is provably dead.

★ WHY (2026-08-22): dchub-worker holds leadership as a SESSION-level advisory
lock on a direct (non-pooler) Neon connection (main.py `_acquire_leader_lock`).
When a container is killed hard on a redeploy, the old backend keeps the lock
until the server notices the dead peer — and every replica's keepalive loop
just sees `pg_try_advisory_lock() = false` and waits. Measured: after the
01:25Z redeploy BOTH worker processes logged "not current leader" for 48+
minutes (the previous deployment took 5 min to win the lock back; the one
before that, ~90 s). While nobody leads, the ENTIRE singleton fleet — the 71
crawler slots, the publishers, the brain's autonomous cycle, self-heal — idles,
and nothing outside the container says so. With ~21 worker redeploys a day
this is not an incident, it is a duty cycle.

The discriminator is the holder's `pg_stat_activity.state_change`: a LIVE
leader runs `SELECT 1` on the lock connection every 30 s, so its idle time
never exceeds ~30 s. A holder idle for minutes is a corpse. We never terminate
a holder younger than `stale_seconds` (default 300 — ten ping intervals), never
our own backend, and any error here means "no steal" (the prior behaviour).
`pg_terminate_backend` needs the same role as the target, which the lock
connections share.

Pure functions over a DB cursor so tests can drive them with a fake; nothing
here imports main.py or opens a connection.
"""
from __future__ import annotations

LOCK_ID = 911714323           # main._LEADER_LOCK_ID — keep in sync (pinned by tests)
STALE_SECONDS_DEFAULT = 300.0  # 10x the leader's 30 s keepalive ping
STALE_SECONDS_FLOOR = 60.0     # never steal from a holder idle < 1 min, whatever env says

_HOLDER_SQL = """
    SELECT a.pid, a.backend_start, a.state, a.state_change, a.application_name,
           EXTRACT(EPOCH FROM (now() - a.state_change)) AS idle_s,
           EXTRACT(EPOCH FROM (now() - a.backend_start)) AS age_s
      FROM pg_locks l
      JOIN pg_stat_activity a ON a.pid = l.pid
     WHERE l.locktype = 'advisory'
       AND l.classid = 0 AND l.objid = %s AND l.objsubid = 1
       AND l.granted
     LIMIT 1
"""


def lock_holder(cur, lock_id=LOCK_ID):
    """The session holding the advisory lock, as a dict, or None if unheld."""
    cur.execute(_HOLDER_SQL, (int(lock_id),))
    row = cur.fetchone()
    if not row:
        return None
    pid, backend_start, state, state_change, app, idle_s, age_s = row[:7]
    return {
        "pid": int(pid),
        "backend_start": backend_start.isoformat() if hasattr(backend_start, "isoformat") else backend_start,
        "state": state,
        "state_change": state_change.isoformat() if hasattr(state_change, "isoformat") else state_change,
        "application_name": app,
        "idle_s": float(idle_s) if idle_s is not None else None,
        "age_s": float(age_s) if age_s is not None else None,
    }


def stale_holder(cur, lock_id=LOCK_ID, stale_seconds=STALE_SECONDS_DEFAULT, my_pid=None):
    """The holder row if it is provably dead (idle >= stale_seconds), else None.

    None also for: no holder, our own backend, an unknown idle time (never
    guess a corpse), or a threshold below the floor.
    """
    threshold = max(float(STALE_SECONDS_FLOOR), float(stale_seconds))
    h = lock_holder(cur, lock_id)
    if not h:
        return None
    if my_pid is not None and h["pid"] == int(my_pid):
        return None
    if h["idle_s"] is None or h["idle_s"] < threshold:
        return None
    return h


def terminate_stale_holder(cur, lock_id=LOCK_ID, stale_seconds=STALE_SECONDS_DEFAULT, my_pid=None):
    """Terminate a provably-dead holder. Returns the holder dict (+ `terminated`)
    when a termination was ATTEMPTED, else None. Never raises on a fresh
    holder; DB errors propagate so the caller can log them and stay follower."""
    h = stale_holder(cur, lock_id, stale_seconds, my_pid)
    if not h:
        return None
    cur.execute("SELECT pg_terminate_backend(%s)", (h["pid"],))
    row = cur.fetchone()
    h["terminated"] = bool(row and row[0])
    return h
