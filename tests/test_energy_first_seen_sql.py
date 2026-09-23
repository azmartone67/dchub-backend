"""util/first_seen.py against a REAL Postgres: the registry that decides what
/whats-new may call "new" for interconnection requests and EIA generating units.

tests/test_energy_first_seen.py pins the pure decision and the board's use of
it. This file runs the SHIPPED SQL (ensure / record / added_counts, the same
functions the ingest routes and routes/infra_growth.py call) and checks the
claims the board makes publicly:

  * day one publishes 0 new, not +N (every key present at the start is baseline);
  * a project that had dropped out of the feed and comes back is not new;
  * an ingest that carries the same keys again adds nothing (ON CONFLICT);
  * a re-keyed feed is baseline, not a burst of news;
  * the 7-day window excludes keys first seen 8 days ago, and a young registry
    never reports a 7-day window;
  * a registry failure inside the caller's ingest transaction rolls back only
    to its savepoint, so the ingest's own write still commits.

Set ENERGY_FIRST_SEEN_SQL_DSN to run it. CI passes the db-parity service DSN
and then asserts this file did not skip. This test owns and recreates only
energy_first_seen and fs_probe_ingest.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("ENERGY_FIRST_SEEN_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="ENERGY_FIRST_SEEN_SQL_DSN not set — no Postgres to run against")


@pytest.fixture
def conn():
    from util import ddl_once, first_seen
    c = psycopg2.connect(DSN)
    with c.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS energy_first_seen")
        cur.execute("DROP TABLE IF EXISTS fs_probe_ingest")
    c.commit()
    ddl_once.reset(first_seen.TABLE)
    assert first_seen.ensure(c), "ensure() did not create the registry"
    yield c
    c.rollback()
    c.close()


def _record(c, layer, scope, keys, seed=None):
    from util import first_seen
    with c.cursor() as cur:
        out = first_seen.record(cur, layer, scope, keys, seed_keys=seed)
    c.commit()
    assert out.get("ok"), out
    return out


def _added(c, layer):
    from util import first_seen
    with c.cursor() as cur:
        return first_seen.added_counts(cur, layer)


def _age(c, layer, days, keys=None):
    """Move first_seen_at back in time, as if those rows were written earlier."""
    with c.cursor() as cur:
        if keys is None:
            cur.execute("UPDATE energy_first_seen SET first_seen_at = NOW() - "
                        "make_interval(days => %s) WHERE layer = %s", (days, layer))
        else:
            cur.execute("UPDATE energy_first_seen SET first_seen_at = NOW() - "
                        "make_interval(days => %s) WHERE layer = %s AND "
                        "item_key = ANY(%s)", (days, layer, list(keys)))
    c.commit()


def test_day_one_is_all_baseline_and_publishes_zero_new(conn):
    feed = [f"ERCOT-{i}" for i in range(1810)]
    stale = [f"ERCOT-OLD-{i}" for i in range(97)]
    out = _record(conn, "interconnect_queue", "ERCOT", feed,
                  seed=lambda: feed + stale)
    assert out["reason"] == "seed" and out["new"] == 0 and out["baselined"] == 1907
    out = _record(conn, "interconnect_queue", "MISO", [f"MISO-{i}" for i in range(1048)])
    assert out["reason"] == "first_run_for_scope" and out["new"] == 0
    a = _added(conn, "interconnect_queue")
    assert (a["added_window"], a["added_1d"]) == (0, 0), (
        f"day one published {a['added_window']} new — the baseline leaked into the count")
    assert a["baseline"] == 1907 + 1048
    assert a["window_days"] == 1


def test_only_genuinely_new_keys_count_and_repeats_do_not(conn):
    feed = [f"PJM-{i}" for i in range(958)]
    stale = ["PJM-GONE-1", "PJM-GONE-2"]
    _record(conn, "interconnect_queue", "PJM", feed, seed=lambda: feed + stale)
    _age(conn, "interconnect_queue", 30)
    # next day: two new filings, one project that had left the feed comes back
    out = _record(conn, "interconnect_queue", "PJM",
                  feed + ["PJM-NEW-1", "PJM-NEW-2", "PJM-GONE-1"])
    assert (out["reason"], out["new"]) == ("new", 2), out
    # the same feed again adds nothing
    out = _record(conn, "interconnect_queue", "PJM",
                  feed + ["PJM-NEW-1", "PJM-NEW-2", "PJM-GONE-1"])
    assert out["new"] == 0, out
    a = _added(conn, "interconnect_queue")
    assert (a["added_window"], a["added_1d"], a["window_days"]) == (2, 2, 7), a
    assert a["baseline"] == 960


def test_a_rekeyed_feed_is_baseline_not_news(conn):
    old = [f"SPP-{i}" for i in range(998)]
    _record(conn, "interconnect_queue", "SPP", old)
    _age(conn, "interconnect_queue", 30)
    out = _record(conn, "interconnect_queue", "SPP", [f"SPP-GI-{i}" for i in range(998)])
    assert (out["reason"], out["new"], out["baselined"]) == ("rekey_suspected", 0, 998), out
    assert _added(conn, "interconnect_queue")["added_window"] == 0


def test_the_seven_day_window_is_seven_days(conn):
    base = [f"{p}:{g}" for p in range(100, 140) for g in ("1", "2")]
    _record(conn, "generator_inventory", "", base)
    _age(conn, "generator_inventory", 40)
    _record(conn, "generator_inventory", "", base + ["900:1"])
    _age(conn, "generator_inventory", 8, keys=["900:1"])      # first seen 8 days ago
    _record(conn, "generator_inventory", "", base + ["900:1", "901:1", "902:1"])
    a = _added(conn, "generator_inventory")
    assert (a["added_window"], a["added_1d"], a["window_days"]) == (2, 2, 7), a
    # the other layers are unaffected
    assert _added(conn, "planned_generators") is None


def test_no_registry_reads_as_unmeasured(conn):
    with conn.cursor() as cur:
        cur.execute("DROP TABLE energy_first_seen")
    conn.commit()
    assert _added(conn, "interconnect_queue") is None


def test_a_registry_failure_does_not_abort_the_callers_ingest(conn):
    """The generator routes call record() inside their own DELETE+INSERT
    transaction. A failure there must roll back to its savepoint only."""
    from util import first_seen
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE fs_probe_ingest (v INT)")
        cur.execute("DROP TABLE energy_first_seen")   # force record() to fail
        cur.execute("INSERT INTO fs_probe_ingest VALUES (1)")
        out = first_seen.record(cur, "planned_generators", "", ["1:1"])
        assert out["ok"] is False and "error" in out
        cur.execute("INSERT INTO fs_probe_ingest VALUES (2)")   # txn still usable
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM fs_probe_ingest")
        assert cur.fetchone()[0] == 2
