"""Mint->first-call cliff (2026-08-12) — the breakdown must never flatter us.

41.3% of minted keys (309/748 in 30d) never make one call. That was visible
only as a COUNT, which cannot distinguish a re-mint ARTIFACT (one agent minting
19 keys and using the last) from a delivery bug from an agent that left.
routes/mcp_mint_cliff.py splits it into mutually exclusive causes.

These tests pin the three properties that make the breakdown trustworthy:

  1. UNMEASURED != 0. If the live schema lacks a column, the block must say so
     and return None — never zeros. A zero here reads as "no keys died", the
     exact inverse of "we could not look". This repo has shipped that bug at
     least three times (funnel leakage stage 4, /health funnel, key counts).
  2. The cohorts SUM to never_called, and the code notices out loud when they
     do not, rather than publishing percentages over a silently short total.
  3. The classifier order is load-bearing: the artifact bucket
     (superseded_by_remint) must be evaluated BEFORE the two buckets that
     would otherwise absorb it, or the artifact hides inside "the agent left"
     and 41.3% gets read as a loss rate it may not be.

CI-SAFETY: no network, no DB — build_mint_cliff runs against a stub cursor.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class StubCursor:
    """Replays queued results in call order. Records the SQL it was given."""

    def __init__(self, columns, fetchone_q=None, fetchall_q=None):
        self._columns = columns          # {table: [col, ...]}
        self._one = list(fetchone_q or [])
        self._all = list(fetchall_q or [])
        self.sql = []
        self._last_is_schema_probe = False

    def execute(self, sql, args=None):
        self.sql.append(sql)
        self._last_is_schema_probe = "information_schema.columns" in sql
        if self._last_is_schema_probe:
            self._pending_table = (args or ("",))[0]

    def fetchone(self):
        return self._one.pop(0) if self._one else None

    def fetchall(self):
        if self._last_is_schema_probe:
            return [(c,) for c in self._columns.get(self._pending_table, [])]
        return self._all.pop(0) if self._all else []


FULL_SCHEMA = {
    "mcp_dev_keys": ["api_key", "created_at", "last_used_at", "metadata", "email"],
    "mcp_call_log": ["api_key", "session_id", "timestamp", "status", "tool"],
}


def _build(**kw):
    from routes.mcp_mint_cliff import build_mint_cliff
    return build_mint_cliff(StubCursor(**kw), days=30)


# ── 1. UNMEASURED is never zero ──────────────────────────────────────

@pytest.mark.parametrize("missing", ["mcp_dev_keys", "mcp_call_log"])
def test_absent_live_column_reports_unmeasured_not_zero(missing):
    """A schema miss must not render as 'nobody hit that case'."""
    schema = {k: list(v) for k, v in FULL_SCHEMA.items()}
    schema[missing] = ["api_key"]          # column set incomplete
    out = _build(columns=schema)

    assert out["ok"] is False
    # The load-bearing assertion: None, NOT 0.
    assert out["population"] is None
    assert out["cohorts"] is None
    assert out["unmeasured"], "a schema miss must name itself in unmeasured"
    assert missing in " ".join(out["unmeasured"])
    assert "UNMEASURED" in out["note"]


def test_funnel_health_failure_branch_publishes_unmeasured_not_zeros():
    """The card beside the waterfall must fail-soft WITHOUT fabricating zeros."""
    src = _read(os.path.join("routes", "funnel_health.py"))
    i = src.index('out["mint_cliff"] = {')
    block = src[i:i + 700]
    assert '"population": None' in block
    assert '"cohorts": None' in block
    assert "UNMEASURED" in block


# ── 2. the breakdown must add up, and admit when it does not ─────────

def test_cohorts_sum_to_never_called():
    out = _build(
        columns=FULL_SCHEMA,
        fetchone_q=[(748, 439), (3, 41)],       # population, immaturity
        fetchall_q=[
            [("superseded_by_remint", 200), ("silent_no_return", 60),
             ("session_continued_keyless", 30), ("born_gated", 19)],
            [],                                  # last_seen
        ],
    )
    assert out["population"]["never_called"] == 309
    assert out["population"]["never_called_pct"] == 41.31
    assert out["sums_ok"] is True
    assert "sums_note" not in out
    assert sum(c["n"] for c in out["cohorts"]) == 309


def test_short_breakdown_is_flagged_instead_of_silently_publishing_pcts():
    """If a row escapes every bucket the endpoint must SAY so."""
    out = _build(
        columns=FULL_SCHEMA,
        fetchone_q=[(748, 439), (0, 0)],
        fetchall_q=[[("silent_no_return", 100)], []],   # 100 != 309
    )
    assert out["sums_ok"] is False
    assert "BREAKDOWN INCOMPLETE" in out["sums_note"]


def test_zero_never_called_does_not_divide_by_zero():
    out = _build(
        columns=FULL_SCHEMA,
        fetchone_q=[(50, 50), (0, 0)],
        fetchall_q=[[], []],
    )
    assert out["population"]["never_called"] == 0
    assert out["sums_ok"] is True


def test_empty_population_reports_none_pct_not_zero_pct():
    """No keys minted is not a 0% cliff — it is nothing to divide."""
    out = _build(
        columns=FULL_SCHEMA,
        fetchone_q=[(0, 0), (0, 0)],
        fetchall_q=[[], []],
    )
    assert out["population"]["never_called_pct"] is None


# ── 3. classifier ORDER is the whole diagnosis ───────────────────────

def _case_sql():
    from routes.mcp_mint_cliff import _COHORT_CASE
    return _COHORT_CASE


def test_artifact_bucket_is_evaluated_before_the_buckets_that_would_absorb_it():
    """superseded_by_remint must outrank silent_no_return and the
    no-session bucket, or one agent's 19 re-mints get counted as 19 lost
    agents and 41.3% is read as a loss rate it may not be."""
    sql = _case_sql()
    assert sql.index("superseded_by_remint") < sql.index("silent_no_return")
    assert sql.index("superseded_by_remint") < sql.index("unattributable_no_session")


def test_mechanical_non_start_outranks_every_behavioural_bucket():
    """born_gated is a refusal WE cause. Counted first or it hides inside
    'agents just leave'."""
    sql = _case_sql()
    for later in ("presented_never_logged", "superseded_by_remint",
                  "session_continued_keyless", "silent_no_return"):
        assert sql.index("born_gated") < sql.index(later)


def test_unknown_is_its_own_bucket_never_folded_into_agent_left():
    """Rows we cannot see must not inflate the only bucket that supports
    'the agent left'."""
    from routes.mcp_mint_cliff import _COHORT_META
    assert "unattributable_no_session" in _COHORT_META
    assert "UNKNOWN" in _COHORT_META["unattributable_no_session"][1]
    assert "left" in _COHORT_META["silent_no_return"][1]


def test_every_cohort_carries_a_meaning_and_a_next_question():
    """A bucket with no interpretation invites the confident story with no
    data that this whole task exists to prevent."""
    from routes.mcp_mint_cliff import _COHORT_META
    sql = _case_sql()
    for code, (label, means, nxt) in _COHORT_META.items():
        assert code in sql, f"{code} is documented but never assigned"
        assert label and means and nxt, f"{code} is missing interpretation"


def test_remint_artifact_warning_is_published_with_the_numbers():
    """The reader must not be able to quote the rate without meeting the
    caveat that it may be mostly duplicate keys."""
    src = _read(os.path.join("routes", "mcp_mint_cliff.py"))
    i = src.index('"read_this_first"')
    assert "ARTIFACT" in src[i:i + 500]
    assert "Deduct" in src[i:i + 500]


# ── 4. the timeout guard must actually bind, and must not leak ───────

def test_every_probe_is_bounded_inside_an_explicit_transaction():
    """The ONLY form that bounds anything here.

    `SET LOCAL` alone is discarded under autocommit (each statement is its own
    implicit transaction). A session-level `SET` does not stick on Neon's
    POOLED endpoint — under pgbouncer transaction mode it lands on a different
    backend connection than the queries (verified live 2026-07-01, see
    funnel_health._bounded). Only BEGIN / SET LOCAL / query / COMMIT holds.
    """
    cur = StubCursor(columns=FULL_SCHEMA,
                     fetchone_q=[(10, 5), (0, 0)], fetchall_q=[[], []])
    from routes.mcp_mint_cliff import build_mint_cliff
    build_mint_cliff(cur, days=30)

    # every real probe carries its own BEGIN / SET LOCAL / COMMIT
    assert cur.sql.count("BEGIN") >= 4
    assert cur.sql.count("COMMIT") >= 4
    assert sum("SET LOCAL statement_timeout" in s for s in cur.sql) >= 4
    # and the cap is never left dangling as a session setting
    assert not any(s.strip().startswith("SET statement_timeout") for s in cur.sql)

    # ordering: SET LOCAL must fall INSIDE a transaction, never before BEGIN
    first_begin = cur.sql.index("BEGIN")
    first_local = next(i for i, s in enumerate(cur.sql)
                       if "SET LOCAL statement_timeout" in s)
    assert first_begin < first_local


def test_a_failed_probe_rolls_back_so_it_cannot_poison_the_next_one():
    """A timed-out probe that is not rolled back leaves the connection in
    'current transaction is aborted' and every later query on it fails —
    which on the funnel-health page means the whole board goes dark."""
    class Boom(StubCursor):
        def execute(self, sql, args=None):
            super().execute(sql, args)
            if "mcp_dev_keys k" in sql and "COUNT" in sql:
                raise RuntimeError("statement timeout")

    cur = Boom(columns=FULL_SCHEMA)
    from routes.mcp_mint_cliff import build_mint_cliff
    with pytest.raises(RuntimeError):
        build_mint_cliff(cur, days=30)
    assert "ROLLBACK" in cur.sql, "a failed probe must roll back its transaction"


def test_immaturity_is_measured_so_a_fresh_cohort_cannot_inflate_the_cliff():
    out = _build(
        columns=FULL_SCHEMA,
        fetchone_q=[(748, 439), (3, 41)],
        fetchall_q=[[("silent_no_return", 309)], []],
    )
    assert out["immaturity"]["never_called_minted_last_1h"] == 3
    assert out["immaturity"]["never_called_minted_last_24h"] == 41
