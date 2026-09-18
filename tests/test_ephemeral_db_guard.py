"""Prove util/ephemeral_db_guard.py can actually REFUSE.

A guard is only worth its line count if it fails on bad input. The blocklist
this replaces was never tested, and it could not have refused the case that
mattered (a production host whose name lacked the five vendor substrings) while
it DID refuse the case we now depend on (a legitimate Neon branch).

Classes here:

  1. NO DATABASE NEEDED — the fail-closed paths. These run on every PR, in the
     ordinary unit-tests job, and they are the ones that catch a future edit
     that turns a verification failure into a silent pass.

  2. AGAINST THE STAMPED BRANCH — the real mutation. Drops the sentinel,
     CONFIRMS THE DROP LANDED, asserts the guard refuses, and restores in a
     `finally`. Without step 3 a no-op "mutation" leaves the test asserting
     nothing while reporting green.
"""
from __future__ import annotations

import os

import pytest

from util import ephemeral_db_guard
from util.ephemeral_db_guard import (SENTINEL_TABLE, NotAnEphemeralDatabase,
                                     assert_ephemeral)


@pytest.fixture()
def fast_connect(monkeypatch):
    """Shrink the verification timeout for the unreachable-host cases.

    A blackholed private IP (no RST) otherwise burns the full production
    timeout on every PR. Patched rather than baked in: the 15s default is
    correct for a cold Neon endpoint resuming from suspend, and a test that
    quietly lowered it for everyone would be tuning prod to suit itself.
    """
    monkeypatch.setattr(ephemeral_db_guard, "CONNECT_TIMEOUT_SECONDS", 1)

# ---------------------------------------------------------------------------
# CLASS 1 — fail-closed. No database.
# ---------------------------------------------------------------------------


def test_an_empty_dsn_is_refused():
    with pytest.raises(NotAnEphemeralDatabase):
        assert_ephemeral("")


def test_an_unreachable_database_is_refused_not_allowed(fast_connect):
    """The guard must not read "I could not check" as "it is safe".

    Port 1 on localhost refuses instantly, so this is a fast, network-free
    stand-in for every unreachable-target case.
    """
    with pytest.raises(NotAnEphemeralDatabase):
        assert_ephemeral("postgresql://u:p@127.0.0.1:1/nope?connect_timeout=2")


def test_a_production_shaped_dsn_is_refused_on_its_contents_not_its_name(fast_connect):
    """The regression the blocklist could never catch.

    This host contains none of ("neon", "azure", "amazonaws", "railway",
    "prod") — the old guard waved it straight through. The new one refuses it
    because it cannot find a sentinel, which is a fact about the database and
    not about how somebody spelled its hostname.
    """
    with pytest.raises(NotAnEphemeralDatabase):
        assert_ephemeral("postgresql://u:p@10.0.0.4:1/dchub?connect_timeout=2")


# ---------------------------------------------------------------------------
# CLASS 2 — the mutation, against the stamped ephemeral branch.
# ---------------------------------------------------------------------------
DSN = os.environ.get("DCHUB_PG_TEST_DSN", "")
needs_branch = pytest.mark.skipif(
    not DSN, reason="set DCHUB_PG_TEST_DSN to a stamped ephemeral Neon branch")


def _conn():
    psycopg2 = pytest.importorskip("psycopg2")
    c = psycopg2.connect(DSN, connect_timeout=15)
    c.autocommit = True
    return c


@needs_branch
def test_the_guard_accepts_the_stamped_branch():
    assert_ephemeral(DSN)          # must not raise


@needs_branch
def test_the_guard_refuses_the_same_branch_once_the_sentinel_is_gone():
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (SENTINEL_TABLE,))
            assert cur.fetchone()[0] is not None, (
                "precondition: the branch should be stamped before we un-stamp it")

            cur.execute(f"DROP TABLE {SENTINEL_TABLE}")

            # ★ CONFIRM THE MUTATION APPLIED. A DROP that silently did nothing
            #   would leave the assertion below passing for the wrong reason —
            #   the guard would be refusing nothing, and this test would be the
            #   green that hides it.
            cur.execute("SELECT to_regclass(%s)", (SENTINEL_TABLE,))
            assert cur.fetchone()[0] is None, "the DROP did not land; test is vacuous"

        with pytest.raises(NotAnEphemeralDatabase):
            assert_ephemeral(DSN)
    finally:
        # Restore unconditionally: later files in the same run gate on this
        # table, and a failed assertion above must not disarm them.
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {SENTINEL_TABLE} ("
                "  branch_id text,"
                "  stamped_at timestamptz NOT NULL DEFAULT now())")
            cur.execute(
                f"INSERT INTO {SENTINEL_TABLE} (branch_id) VALUES ('restored-by-test')")
        conn.close()

    assert_ephemeral(DSN)          # and the restore actually worked
