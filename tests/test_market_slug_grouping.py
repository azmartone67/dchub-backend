"""market_intel_preview groups a city slug into ONE market, not four.

r-market-slug-groups (2026-07-31), follow-on to #2057.

`LOWER(REPLACE(city,' ','-')) = 'ashburn'` matches four raw (city, state)
groups on the live table — ('Ashburn','VA'), ('ASHBURN','VA'), ('Ashburn','')
and ('Ashburn',NULL). #2058 found the route picked one ARBITRARILY; #2057 made
the pick deterministic. Deterministic was not the same as right: it still threw
the siblings away, which on the replica left 26 markets publishing 0.0 MW while
a sibling group held their capacity.

The SHAPE of the query is fenced in tests/test_ai_capacity_depth_basis.py
::test_preview_picks_its_market_deterministically, which asserts against the
SQL the function actually executes. A shape fence cannot prove the shape
changes an answer, so this file supplies the other half: the REAL shipped
handler driven against the live table, carrying its own MUST-FAIL control —
"aggregate >= largest single group" is trivially true of the code this test
exists to reject, so it asserts a STRICT gain somewhere.
"""
import os

import pytest

REL = "routes/market_intel_preview.py"


def test_the_fold_helpers_resolve():
    """The SQL-shape fence lives in tests/test_ai_capacity_depth_basis.py
    ::test_preview_picks_its_market_deterministically, which asserts against
    the SQL the function actually EXECUTES rather than against source text.

    These are the free variables that SQL interpolates. A renamed constant
    would leave the shape fence passing and the route raising NameError at
    request time, so resolve them explicitly."""
    import importlib
    mod = importlib.import_module("routes.market_intel_preview")
    for name in ("_NSTATE", "_NSTATE_S", "_FLEET", "_FACILITIES", "_OPERATORS",
                 "_OPERATIONAL"):
        assert getattr(mod, name, None), f"{name} missing from {REL}"


# ── behavioural proof (needs a DB; skipped without one) ────────────────────

_DB = (os.environ.get("NEON_REPLICA_URL") or os.environ.get("DATABASE_URL")
       or os.environ.get("NEON_DATABASE_URL"))


@pytest.mark.skipif(not _DB, reason="no database URL in the environment")
def test_normalised_grouping_recovers_facilities_the_raw_pick_discarded(monkeypatch):
    """Drive the shipped handler, then compare it against what the OLD raw
    grouping would have returned for the same slug.

    The control is the point: `aggregated >= largest raw group` is true of the
    superseded code too, so this asserts a STRICT gain on a slug known to
    split. If the fix is reverted, that assertion fails.
    """
    import psycopg2
    from flask import Flask

    import routes.market_intel_preview as mip
    # Point the route at the replica WITHOUT mutating os.environ — a global
    # DATABASE_URL set here would leak into every later test in the session.
    monkeypatch.setattr(mip, "_dsn", lambda: _DB)

    app = Flask(__name__)
    app.register_blueprint(mip.market_intel_preview_bp)

    conn = psycopg2.connect(_DB, connect_timeout=20)
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor()

    def raw_pick(slug):
        """What `GROUP BY city, state ... ORDER BY ... LIMIT 1` returned."""
        cur.execute(f"""
            SELECT {mip._FACILITIES} FILTER (WHERE {mip._OPERATIONAL})::int,
                   COALESCE(SUM(power_mw) FILTER (WHERE {mip._OPERATIONAL}),0)
                       ::numeric(10,1)::float
              FROM discovered_facilities
             WHERE LOWER(REPLACE(city, ' ', '-')) = %s
               AND {mip._FLEET}
          GROUP BY city, state
          ORDER BY 1 DESC, 2 DESC, state ASC
             LIMIT 1
        """, (slug,))
        return cur.fetchone()

    gained = []
    with app.test_client() as cl:
        for slug in ("ashburn", "sterling", "sao-paulo", "markham", "dallas",
                     "chicago", "santa-clara"):
            body = cl.get(f"/api/v1/market-intel-preview?market={slug}").get_json()
            assert not body.get("error"), f"{slug}: {body['error']}"
            data = body["data"]
            assert not data.get("error"), f"{slug}: {data['error']}"

            old = raw_pick(slug)
            assert old, f"{slug} matched no rows — the comparison is vacuous"
            old_fac, old_mw = old

            assert data["facility_count"] >= old_fac, (
                f"{slug}: normalised grouping LOST facilities "
                f"({old_fac} -> {data['facility_count']})")
            assert data["total_mw"] >= old_mw, (
                f"{slug}: normalised grouping LOST capacity "
                f"({old_mw} -> {data['total_mw']})")
            if (data["facility_count"], data["total_mw"]) != (old_fac, old_mw):
                gained.append(slug)

        # CONTROL: without a strict gain somewhere, the assertions above pass
        # unchanged against the code this test rejects.
        assert gained, (
            "CONTROL FAILED: aggregating changed nothing on any split slug, so "
            "this test cannot detect a revert to raw (city, state) grouping")

        # Ashburn is the flagship the upsell CTA sits next to: it must read as
        # the real market, not a case-variant shell.
        ash = cl.get("/api/v1/market-intel-preview?market=ashburn").get_json()["data"]
        assert (ash["city"], ash["state"]) == ("Ashburn", "VA"), (
            f"flagship market reported as {ash['city']!r}/{ash['state']!r}")
        assert ash["total_mw"] > 1000, (
            f"flagship market published as {ash['total_mw']} MW — the "
            "'empty flagship' failure is back")

        # A slug that matches nothing must still say so rather than 500.
        missing = cl.get("/api/v1/market-intel-preview?market=not-a-real-market")
        assert missing.status_code == 200
        assert missing.get_json()["data"]["error"] == "market_not_found"

    conn.close()
