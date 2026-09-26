"""The news re-resolve must be able to lower the number that fires it.

Live 2026-09-25: brain-autonomy fired news_entity_reresolve daily (5 fires
in 7d), healed 0 rows each time, and blindspot sat at 12. The trigger
counts EVERY unresolved row; the fire only scanned the 300 most recently
seen, so blind-spot rows older than that window were never re-checked.
This runs both against a real Postgres (C
collation, which the count requires) and asserts the fire drives the
trigger to 0 — and that the squasher rollback pre-image covers every row
the fire flipped, including ones outside its recency window.

Needs DCHUB_PG_TEST_DSN pointing at a disposable, C-collated database.
"""
from __future__ import annotations

import os

import pytest

DSN = os.environ.get("DCHUB_PG_TEST_DSN")


@pytest.fixture
def pg(monkeypatch):
    if not DSN:
        pytest.skip("set DCHUB_PG_TEST_DSN to a disposable Postgres")
    if any(h in DSN for h in ("neon.tech", "railway", "rlwy", "amazonaws")):
        pytest.fail("DCHUB_PG_TEST_DSN looks managed — point it at a throwaway")
    import psycopg2
    from routes import news_entity_extraction as m
    monkeypatch.setattr(m, "_COLLATION_BYTE_ORDERED", None)
    c = psycopg2.connect(DSN)
    with c.cursor() as cur:
        cur.execute("SELECT datcollate FROM pg_database"
                    " WHERE datname = current_database()")
        if cur.fetchone()[0] not in m._BYTE_ORDERED_COLLATIONS:
            pytest.skip("test database is not byte-ordered (create it with"
                        " LC_COLLATE 'C' TEMPLATE template0)")
        cur.execute("DROP TABLE IF EXISTS news_discovered_entities, facilities,"
                    " discovered_facilities")
        cur.execute("CREATE TABLE facilities (name TEXT, provider TEXT)")
        cur.execute("CREATE TABLE discovered_facilities (name TEXT, provider TEXT)")
        cur.execute("CREATE TABLE news_discovered_entities (id SERIAL PRIMARY KEY,"
                    " entity_name TEXT, in_facilities BOOLEAN NOT NULL DEFAULT FALSE,"
                    " status TEXT, last_seen_at TIMESTAMPTZ DEFAULT NOW())")
    c.commit()
    yield c
    with c.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS news_discovered_entities, facilities,"
                    " discovered_facilities")
    c.commit()
    c.close()


def _seed(c):
    with c.cursor() as cur:
        cur.executemany("INSERT INTO facilities (name, provider) VALUES (%s, %s) ON CONFLICT DO NOTHING", [
            ("Aligned Data Centers PHX1", "Aligned"),
            ("Stack Infrastructure Ashburn", None),
            ("Vantage Data Centers VA1", None),
            ("Power Grid Campus", None),
        ])
        # prefix blind spots (the live shape), one exact match, and controls
        cur.executemany("INSERT INTO news_discovered_entities (entity_name, status,"
                        " last_seen_at) VALUES (%s, %s, NOW() ON CONFLICT DO NOTHING - %s * INTERVAL '1 day')", [
            # blind spots, both OLDER than the recency window
            ("Stack Infrastructure", "unknown", 400),
            ("Vantage", "rejected", 300),
            # recent rows that fill a cap=3 window
            ("Aligned", "unknown", 1),                  # resolves in the window
            ("Stackpath", "unknown", 1),                # NOT a token-boundary prefix
            ("Power", "unknown", 1),                    # stoplisted
            ("Nobody Corp", "unknown", 2),              # no facility at all
        ])
    c.commit()


def _flags(c):
    with c.cursor() as cur:
        cur.execute("SELECT entity_name, in_facilities, status"
                    " FROM news_discovered_entities ORDER BY id")
        return {n: (f, s) for n, f, s in cur.fetchall()}


def test_the_fire_drives_its_own_trigger_to_zero(pg):
    from routes import news_entity_extraction as m
    _seed(pg)
    with pg.cursor() as cur:
        assert m.entity_blindspot_count(cur) == 3      # Stack Infrastructure, Vantage, Aligned
    # cap=3: the window holds Aligned/Stackpath/Power — never the old rows.
    out = m._reresolve_unmatched(pg, cap=3)
    assert "error" not in out, out
    with pg.cursor() as cur:
        assert m.entity_blindspot_count(cur) == 0
    f = _flags(pg)
    assert f["Stack Infrastructure"] == (True, "known")
    assert f["Vantage"] == (True, "known"), "rejected + resolved is forbidden"
    assert f["Aligned"][0] is True                     # the exact pass still works
    for ctl in ("Stackpath", "Power", "Nobody Corp"):
        assert f[ctl][0] is False, ctl
    assert out["resolved_prefix"] == 2 and out["resolved"] == 3, (
        "the two old blind spots must be reached by the prefix pass")


def test_the_rollback_pre_image_covers_rows_outside_the_window(pg, monkeypatch):
    from routes import squasher_action_classes as sac
    _seed(pg)
    monkeypatch.setattr(sac, "_NEWS_PRE_IMAGE_CAP", 2)   # window misses the old row
    with pg.cursor() as cur:
        pre = sac._pre_image(cur, "news_entity_reresolve")
    from routes import news_entity_extraction as m
    m._reresolve_unmatched(pg, cap=2)
    with pg.cursor() as cur:
        rb = sac._rollback_from_pre_image(cur, pre)
    with pg.cursor() as cur:
        cur.execute("SELECT id FROM news_discovered_entities WHERE in_facilities")
        flipped = {r[0] for r in cur.fetchall()}
    assert flipped and {r["id"] for r in rb} == flipped
