"""The DB circuit breaker must be able to SEE Neon refusing connections.

Incident 2026-08-21 02:00-02:16Z. Neon's primary pooler hit its connection-
attempt permit ceiling and the read replica's compute went unavailable. The
circuit breaker in main.py exists precisely to fail fast in that situation --
it never opened. /api/health/db, read from a worker booted 00:53:33Z (i.e. one
that lived through the entire outage with no restart), reported circuit_trips:0
while requests were stacking to 965s.

Root cause: _is_connectivity_error() is the ONLY gate on _record_circuit_failure()
-- all three call sites (db_utils execute/executemany, main.get_pg_connection) do
nothing else with a True. Its pattern list was written in generic-libpq phrasing
and matched none of the three strings Neon actually emits. With the breaker
closed, every retry loop kept opening fresh connects into a limiter whose
complaint was, verbatim, that too many connection attempts were ongoing.

This test pins the REAL shipped function (imports db_utils; no stub, no AST copy)
against the LITERAL strings captured from the Railway deploy logs.
"""
import db_utils


# Verbatim from Railway deploy logs, dchub-backend, 2026-08-21 02:00:26Z-02:01:22Z.
# Do not paraphrase these -- the whole defect was a near-miss on exact wording.
NEON_REFUSALS = [
    (
        "permit limiter (primary pooler)",
        'connection to server at "ep-polished-breeze-af22mhng-pooler.c-2.us-west-2.'
        'aws.neon.tech" (2600:1f14:25cf:a024:1538:10d4:c257:dde3), port 5432 failed: '
        'ERROR:  Failed to acquire permit to connect to the database. Too many '
        'database connection attempts are currently ongoing.',
    ),
    (
        "compute node unavailable (read replica)",
        'connection to server at "ep-dark-glade-af2837o8-pooler.c-2.us-west-2.'
        'aws.neon.tech" (34.217.228.110), port 5432 failed: '
        "ERROR:  Couldn't connect to compute node",
    ),
    (
        "libpq connect_timeout expiry",
        'connection to server at "ep-polished-breeze-af22mhng-pooler.c-2.us-west-2.'
        'aws.neon.tech" (52.43.156.152), port 5432 failed: timeout expired',
    ),
]

# Errors that are NOT connectivity failures. These must stay False: tripping the
# breaker on a bad query would fail-fast the whole tier over one broken caller.
# They also make a `return True` mutation of the function fail this test.
NOT_CONNECTIVITY = [
    ("statement timeout", "canceling statement due to statement timeout"),
    ("bad column", 'column "name" of relation "eia_generators" does not exist'),
    ("syntax error", 'syntax error at or near "SELCT"'),
    ("integrity", 'duplicate key value violates unique constraint "facilities_pkey"'),
]

# Pre-existing patterns. These must keep matching -- the fix ADDS vocabulary,
# it must not narrow what the breaker already caught.
STILL_CONNECTIVITY = [
    ("refused", "connection refused"),
    ("slot exhaustion", 'FATAL:  too many connections for role "dchub"'),
    ("vanilla libpq", "could not connect to server: Connection refused"),
    ("restarting", "the database system is starting up"),
]


def test_pinning_the_real_shipped_function():
    """Guard the guard: prove we are testing repo code, not an empty stub."""
    assert db_utils.__file__.endswith("db_utils.py"), db_utils.__file__
    src = open(db_utils.__file__, encoding="utf-8").read()
    assert "def _is_connectivity_error" in src, "function vanished from db_utils.py"
    # The function must still be the sole gate on the breaker; if a future
    # refactor adds another branch, this test's premise needs rechecking.
    assert src.count("_is_connectivity_error(e)") >= 2, (
        "expected the classifier to still gate the db_utils call sites"
    )


def test_neon_refusals_trip_the_breaker():
    """The three strings that were invisible during the 02:00 outage."""
    missed = [
        label for label, err in NEON_REFUSALS
        if not db_utils._is_connectivity_error(Exception(err))
    ]
    assert not missed, (
        "circuit breaker BLIND to Neon refusal(s): " + ", ".join(missed)
        + " -- these were measured live on 2026-08-21 02:00Z and caused a "
          "16-minute stall with circuit_trips:0"
    )


def test_query_errors_do_not_trip_the_breaker():
    """A broken query must not fail-fast the entire database layer."""
    false_trips = [
        label for label, err in NOT_CONNECTIVITY
        if db_utils._is_connectivity_error(Exception(err))
    ]
    assert not false_trips, (
        "these are NOT connectivity failures but would open the breaker: "
        + ", ".join(false_trips)
    )


def test_preexisting_patterns_still_match():
    """The fix widens the classifier; it must not narrow it."""
    lost = [
        label for label, err in STILL_CONNECTIVITY
        if not db_utils._is_connectivity_error(Exception(err))
    ]
    assert not lost, "regression -- classifier stopped matching: " + ", ".join(lost)


def test_classifier_actually_discriminates():
    """Substance guard.

    A function stubbed to `return True` passes the Neon test; one stubbed to
    `return False` passes the query-error test. Requiring both directions in a
    single assertion means neither mutation survives, and an emptied input list
    can't pass either.
    """
    trues = [e for _, e in NEON_REFUSALS if db_utils._is_connectivity_error(Exception(e))]
    falses = [e for _, e in NOT_CONNECTIVITY if not db_utils._is_connectivity_error(Exception(e))]
    assert len(trues) == len(NEON_REFUSALS) == 3, f"expected 3 trips, got {len(trues)}"
    assert len(falses) == len(NOT_CONNECTIVITY) == 4, f"expected 4 non-trips, got {len(falses)}"
