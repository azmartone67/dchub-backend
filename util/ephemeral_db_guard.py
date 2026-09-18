"""Refuse to run destructive tests against anything but a stamped CI branch.

WHAT THIS REPLACES
------------------
Two test files that DROP tables guarded themselves with a substring blocklist:

    if DSN and any(x in DSN.lower() for x in
                   ("neon", "azure", "amazonaws", "railway", "prod")):
        raise RuntimeError("... looks like a real database")

That guard is wrong in both directions.

  * FALSE POSITIVE, and it is why this module exists: every Neon host contains
    the literal "neon", so a legitimate throwaway Neon branch trips it. Worse,
    the check sits at MODULE level, so it does not skip — it raises during
    collection and fails the run.

  * FALSE NEGATIVE, which is the dangerous half: the list only knows the five
    vendor spellings someone thought of. `postgres://...@10.0.0.4/dchub` is
    production and matches none of them. The guard's safety was never a
    property of the database — only of how its hostname happened to be spelled.

A blocklist of substrings cannot answer "is this database safe to DROP tables
in". So this asks the database instead: an ephemeral CI branch is STAMPED at
creation (scripts/neon_ci_branch.py stamp) with a sentinel table, and nothing
else in the world has one. Prod cannot acquire it by accident or by rename.

FAILS CLOSED
------------
Every failure path — unreachable host, bad credentials, missing driver, an
error nobody predicted — raises `NotAnEphemeralDatabase`. A guard that answers
"safe" when it could not check is worse than no guard, because the suite goes
green while the protection is gone.
"""
from __future__ import annotations

SENTINEL_TABLE = "_ci_ephemeral_branch"

# Applied to the verification connection. A caller cannot express this through
# the DSN, because the kwarg below wins over any `connect_timeout` in the URL —
# so it lives here, as one named knob, rather than being silently unreachable.
# 15s is for a cold Neon endpoint resuming from suspend; tests lower it.
CONNECT_TIMEOUT_SECONDS = 15


class NotAnEphemeralDatabase(RuntimeError):
    """Raised when a DSN is not a verified, stamped, disposable CI database."""


def assert_ephemeral(dsn: str) -> None:
    """Raise unless `dsn` points at a database carrying the CI sentinel.

    Call this at import time in any test module that DROPs or TRUNCATEs.
    """
    if not dsn:
        raise NotAnEphemeralDatabase("no DSN given")

    try:
        import psycopg2
    except Exception as exc:                      # pragma: no cover - env-specific
        raise NotAnEphemeralDatabase(
            f"cannot verify the target database (psycopg2 unavailable): {exc}") from exc

    conn = None
    try:
        conn = psycopg2.connect(dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS)
        with conn.cursor() as cur:
            # to_regclass returns NULL rather than raising for an absent table,
            # so this is one round trip and no exception-as-control-flow.
            cur.execute("SELECT to_regclass(%s)", (SENTINEL_TABLE,))
            row = cur.fetchone()
    except NotAnEphemeralDatabase:
        raise
    except Exception as exc:
        raise NotAnEphemeralDatabase(
            f"could not verify the target database, so refusing to touch it: {exc}"
        ) from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if not row or row[0] is None:
        raise NotAnEphemeralDatabase(
            f"target database has no {SENTINEL_TABLE!r} table, so it is NOT a "
            "disposable CI branch. This test DROPs tables. Point "
            "DCHUB_PG_TEST_DSN at a branch created by "
            "scripts/neon_ci_branch.py, or leave it unset to skip.")
