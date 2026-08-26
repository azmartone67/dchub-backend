"""Guards for the public activation-signal feed (2026-08-26).

This surface exists because a flat ZERO was being read as a finding: paid, MRR
and conversions have all read 0 for the whole 30-day window, so "is the funnel
turning?" got answered by re-reading a zero that could not move.

The failure mode it must not reproduce is the same one in miniature — a probe
that fails returning 0 instead of null, or a signal whose direction is reported
as 'flat' when in truth no window could be read. Both would publish a
confident-looking answer built on nothing.

Pure functions: no DB, no network, never imports main.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from routes.ops_activation import (  # noqa: E402
    SHAPE, _ratio, _verdict, direction_of, signal,
)


# ── direction: movement, never a verdict ─────────────────────────────────
def test_direction_is_unknown_when_a_window_is_unreadable():
    """The whole point: a missing read must NOT look like 'no change'."""
    assert direction_of(None, 5, "up") == "unknown"
    assert direction_of(5, None, "up") == "unknown"
    assert direction_of(None, None, "up") == "unknown"
    # ...and a real equal pair IS flat, so 'unknown' is not just a catch-all
    assert direction_of(5, 5, "up") == "flat"


def test_direction_is_raw_movement_not_goodness():
    assert direction_of(9, 4, "up") == "up"
    assert direction_of(4, 9, "up") == "down"
    # same movement, opposite goodness — direction must not flip with `better`
    assert direction_of(4, 9, "down") == "down"


# ── two of the five improve by going DOWN ────────────────────────────────
def test_a_falling_remint_ratio_reads_as_improving():
    s = signal("remint_ratio", "l", 12.0, 22.0, "down", "b")
    assert s["direction"] == "down"
    assert s["improving"] is True, "remint_ratio falling is GOOD — 22x re-mints is the defect"


def test_a_rising_remint_ratio_reads_as_worsening():
    s = signal("remint_ratio", "l", 30.0, 22.0, "down", "b")
    assert s["improving"] is False


def test_a_rising_click_count_reads_as_improving():
    s = signal("anon_checkout_clicks", "l", 3, 0, "up", "b")
    assert s["improving"] is True


def test_improving_is_null_when_direction_is_not_a_movement():
    for value, prior in ((5, 5), (None, 3), (3, None)):
        assert signal("x", "l", value, prior, "up", "b")["improving"] is None


# ── never a fabricated zero ──────────────────────────────────────────────
def test_ratio_returns_none_not_zero_on_a_failed_probe():
    assert _ratio(None, 10) is None
    assert _ratio(10, None) is None
    assert _ratio(10, 0) is None, "division by zero must be null, not 0"
    # a genuine zero numerator IS a finding and must survive as 0.0
    assert _ratio(0, 10) == 0.0


def test_the_shape_tells_a_reader_null_and_zero_differ():
    """An agent reading this feed must not treat a failed probe as 'nothing
    happened'. That distinction only exists if the shape states it."""
    v = SHAPE["value"].lower()
    assert "null" in v and "fail" in v
    assert "0 means" in v or "0 means it ran" in v
    assert "never" in SHAPE["direction"].lower()


def test_the_shape_warns_that_direction_is_not_a_verdict():
    assert "better" in SHAPE
    assert "down" in SHAPE["better"].lower()


# ── verdict counts ───────────────────────────────────────────────────────
def _sig(direction_pair, better):
    return signal("s", "l", direction_pair[0], direction_pair[1], better, "b")


def test_verdict_counts_each_state_once():
    sigs = [
        _sig((5, 1), "up"),      # improving
        _sig((1, 5), "up"),      # worsening
        _sig((4, 4), "up"),      # flat
        _sig((None, 2), "up"),   # unknown
        _sig((1, 9), "down"),    # improving (lower is better)
    ]
    v = _verdict(sigs)
    assert v == {"improving": 2, "worsening": 1, "flat": 1, "unknown": 1}
    assert sum(v.values()) == len(sigs), "every signal must land in exactly one bucket"


def test_an_unreadable_signal_is_never_counted_as_flat():
    """The defect this feed exists to prevent, asserted directly."""
    v = _verdict([_sig((None, None), "up")])
    assert v["unknown"] == 1
    assert v["flat"] == 0


# ── SQL safety: the % trap, and the complete-week basis ──────────────────
def test_no_percent_literal_in_the_module_sql():
    """Sibling executors bind params inconsistently and both fail soft to 0, so
    a LIKE here would be silently wrong in one direction or the other."""
    src = open(os.path.join(REPO_ROOT, "routes", "ops_activation.py"),
               encoding="utf-8").read()
    body = src[src.index("def read_signals("):src.index("SHAPE = {")]
    for frag in ("LIKE '", "like '"):
        assert frag not in body, f"{frag!r} in the SQL — use LEFT(col, n) instead"
    assert "LEFT(cc.ref, 2) = 'a-'" in body, "the anon-ref predicate went missing"


def test_agents_use_the_complete_week_basis_not_a_rolling_window():
    """Comparing windows of unequal composition is how the same population read
    -65% rolling and +37% on complete weeks. Pin the basis."""
    src = open(os.path.join(REPO_ROOT, "routes", "ops_activation.py"),
               encoding="utf-8").read()
    assert "canonical_external_complete_week_sql" in src
    assert "canonical_external_activity_sql" not in src, (
        "that is the ROLLING helper — it reintroduces the partial-week artifact")


def test_the_self_traffic_filter_fails_closed():
    """If the canonical predicate cannot be imported, the fallback must still
    exclude our own probes — counting them is how QA becomes a customer."""
    src = open(os.path.join(REPO_ROOT, "routes", "ops_activation.py"),
               encoding="utf-8").read()
    fallback = src[src.index("except Exception:"):src.index("conn = None")]
    for token in ("dchub", "curl/", "probe"):
        assert token in fallback, f"fallback UA filter does not exclude {token!r}"


def test_kill_switch_answers_404_never_5xx():
    """A 5xx from Railway makes the CF worker fail the whole site over to the
    stale Render backend."""
    src = open(os.path.join(REPO_ROOT, "routes", "ops_activation.py"),
               encoding="utf-8").read()
    assert "), 404" in src
    assert "OPS_ACTIVATION_DISABLE" in src


def test_every_probed_column_exists_in_the_committed_ddl():
    """A guessed column name fails the probe and publishes `null / unknown` —
    honest, but useless. That happened on the first live read: the feed asked
    mcp_session_upgrades for `created_at`; the column is `upgraded_at`.

    Cross-checks every table.column this module probes against the CREATE TABLE
    statements committed in the repo, so a wrong guess fails here rather than
    silently becoming an unreadable signal in production.
    """
    import re
    src = open(os.path.join(REPO_ROOT, "routes", "ops_activation.py"),
               encoding="utf-8").read()
    ddl_sources = "\n".join(
        open(os.path.join(REPO_ROOT, f), encoding="utf-8", errors="replace").read()
        for f in ("main.py", "routes/schema_repair.py")
    )

    def columns_of(table):
        m = re.search(r"CREATE TABLE IF NOT EXISTS " + table + r"\s*\((.*?)\)\s*\"",
                      ddl_sources, re.S)
        if not m:
            m = re.search(r"CREATE TABLE IF NOT EXISTS " + table + r"\s*\((.*?)\n\s*\)",
                          ddl_sources, re.S)
        assert m, f"no committed DDL found for {table} — cannot verify its columns"
        return {c.group(1) for c in re.finditer(r"^\s*([a-z_]+)\s+[A-Z]", m.group(1), re.M)}

    su_cols = columns_of("mcp_session_upgrades")
    assert "upgraded_at" in su_cols, "DDL changed — this guard is stale"
    # the module must not probe a column the table does not have
    probed = set(re.findall(r"FROM mcp_session_upgrades WHERE ([a-z_]+)", src))
    probed |= set(re.findall(r"WHERE ([a-z_]+) >= \{W", src))
    unknown = {c for c in probed if c not in su_cols}
    assert not unknown, (
        f"ops_activation probes mcp_session_upgrades.{sorted(unknown)} which is not "
        f"in the committed DDL {sorted(su_cols)} — the probe will fail and the "
        "signal will publish as null")


def test_the_brain_digest_reads_the_same_source():
    """One computation, two consumers — otherwise the brain's report and the
    public feed can drift and both look authoritative."""
    dig = open(os.path.join(REPO_ROOT, "routes", "growth_ops_digest.py"),
               encoding="utf-8").read()
    assert "from routes.ops_activation import read_signals" in dig
    assert "LEADING SIGNALS" in dig
    # a failed read must not print five zeros
    assert "not zero, unread" in dig
