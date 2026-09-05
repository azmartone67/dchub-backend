"""r-restatement-marker (2026-08-08) — a restatement is not a verdict shift.

WHAT THIS PINS
--------------
routes/agent_broadcast.py::_fetch_dcpi_verdict_shifts publishes "DCPI verdict
shifts" to AI agents on /api/v1/agent-broadcast, /today, /dcpi-shifts and the
RSS variant. Pass 1's premise is that a market whose verdict differs from its
verdict `days` ago has MOVED.

THE DEFECT THIS EXISTS TO CATCH
-------------------------------
For most of dcpi_daily_snapshots' history that premise was false, because
market_power_scores.verdict had three writers using three DIFFERENT band
tables — routes/dcpi.py's derive_verdict (the published bands) and two armed
healers in dchub_self_heal.py — and the ~06:00 UTC snapshot froze whichever
fired last. Replaying all 71 snapshot days against each candidate table, every
day reproduces ONE table at 99-100%, and the churn spikes are exactly the days
the winning table changes:

    2026-07-15  154 flips, 118 with byte-identical scores
    2026-07-18  189 flips, 144 identical
    2026-07-24  180 flips, 119 identical
    2026-07-27  107 flips,  58 identical
    2026-07-30  123 flips,  92 identical
    2026-08-06  204 flips, 127 identical
    2026-08-08  104 flips,  52 identical

against 0-9 flips on ordinary days. A flip whose two scores are byte-identical
cannot be a market move under any single rule. The healers are retired
(#2436), but a methodology bump produces the same shape — 2.3.0 alone moves
220 of 324 published verdicts back onto the published bands — so the feed has
to test the DATA, not a list of dates.

TWO MARKERS, and the test proves NEITHER is sufficient alone:

  VINTAGE   the pair's method_version differs. Catches a weight / ceiling /
            input-source change, where both labels are legal under the
            current bands and only the scores moved. Measured across the
            whole table it fires on exactly ONE day (07-30, the NULL->2.0.1
            transition), which is why it cannot be the only test.
  OFF-BAND  either row's stored verdict is not what VERDICT_BANDS produces
            from that row's OWN stored scores. Works retroactively across the
            healer era, where method_version is uninformative by construction:
            a healer rewrote `verdict` and left `method_version` alone.

WHY THE FUNCTION IS EXEC'd RATHER THAN IMPORTED
-----------------------------------------------
Same reason as tests/test_agent_broadcast_verdict_shift_source.py, whose
_load_function this reuses in spirit: tests never import main.py, and
routes/agent_broadcast.py pulls in flask and the URL registry. The function is
pulled out of the source with `ast` and executed against stubs, so what runs
here is the SHIPPED body — a structural assertion about its AST would pass
just as happily on a body that classified every row as genuine.
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "routes", "agent_broadcast.py")

FUNC = "_fetch_dcpi_verdict_shifts"
HELPER = "_restatement_item"


# ─────────────────────────────────────────────────────────────────────────
# Harness
# ─────────────────────────────────────────────────────────────────────────

class _Cursor:
    """Hands Pass 1 the rows under test, then Pass 2 whatever is left.

    Records the SQL it was given so the tests can assert the statement
    actually asked for the two markers rather than defaulting them.
    """

    def __init__(self, pass1_rows, pass2_rows):
        self._queued = [pass1_rows, pass2_rows]
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def fetchall(self):
        return self._queued.pop(0) if self._queued else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def rollback(self):
        pass

    def close(self):
        pass


def _shipped(names):
    """exec the named top-level defs from the shipped source against stubs."""
    with open(SRC, "r", encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src, filename=SRC)
    wanted = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            wanted[node.name] = node
    missing = set(names) - set(wanted)
    assert not missing, (
        f"{sorted(missing)} not found as a top-level def in {SRC}. If renamed, "
        "move this guard with it rather than deleting it — it is the only "
        "thing standing between a methodology restatement and the agent feed."
    )

    import datetime as _dt

    from util.dcpi_method import verdict_case_sql
    # ★2026-09-05 (utcnow batch 2): this namespace is hand-built, so ANY new
    # import in the shipped module arrives here as an unbound free variable —
    # the function raises, _run() returns [], and every assertion below fails
    # with an empty-result message that says nothing about the real cause.
    # Mirror the module's imports here when they change.
    from utc_clock import utc_iso_z

    ns = {
        "datetime": _dt,
        "utc_iso_z": utc_iso_z,
        "build_public_url": lambda kind, slug: f"https://dchub.cloud/{kind}/{slug}",
        "_verdict_case_sql": verdict_case_sql,
        "_METHODOLOGY_URL": "https://dchub.cloud/api/v1/dcpi/methodology",
    }
    mod = ast.Module(body=[wanted[n] for n in names], type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), SRC, "exec"), ns)  # noqa: S102
    return ns


def _row(slug, was, now, *, vintage=False, off_band=False, excess=70.0):
    """One Pass 1 result row, in the shipped SELECT's column order."""
    import datetime as _dt
    return (slug, slug.title(), "PJM", was, now, excess,
            _dt.datetime(2026, 8, 8, 6, 0), _dt.date(2026, 8, 1),
            vintage, off_band)


def _run(pass1_rows, pass2_rows=(), days=7):
    ns = _shipped([HELPER, FUNC])
    cur = _Cursor(list(pass1_rows), list(pass2_rows))
    ns["_db_conn"] = lambda: _Conn(cur)
    return ns[FUNC](days), cur


def _kinds(items):
    out = {}
    for i in items:
        out[i["kind"]] = out.get(i["kind"], 0) + 1
    return out


# ─────────────────────────────────────────────────────────────────────────
# The harness must be able to fail
# ─────────────────────────────────────────────────────────────────────────

def test_harness_publishes_a_genuine_shift():
    """Without this every assertion below could pass on a function that
    returns nothing at all — the vacuous-guard failure mode."""
    items, _ = _run([_row("little-rock", "AVOID", "CAUTION")])
    assert _kinds(items) == {"dcpi_verdict_shift": 1}, items
    assert "verdict shifted AVOID → CAUTION" in items[0]["title"]


# ─────────────────────────────────────────────────────────────────────────
# The two markers, each on its own
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("marker", ["vintage", "off_band"])
def test_either_marker_alone_suppresses_the_shift(marker):
    """NEITHER marker may be load-bearing on the other.

    off_band alone is what covers the healer era (method_version was left
    untouched by the rewrites, so vintage is silent there). vintage alone is
    what covers a weights/ceilings bump, where both labels are legal under
    the current bands so off_band is silent. Drop either and one whole class
    of restatement is republished as market news.
    """
    items, _ = _run([_row("akron", "CAUTION", "AVOID", **{marker: True})])
    assert _kinds(items) == {"dcpi_restatement": 1}, (
        f"a shift flagged {marker}=True was published as a verdict shift: "
        f"{items}"
    )
    assert "restated, not moved" in items[0]["title"]


def test_genuine_shifts_survive_alongside_restatements():
    """The point is not to go quiet on a spike day — it is to publish the
    real moves and label the rest. A filter that dropped everything would
    satisfy the two tests above."""
    items, _ = _run([
        _row("akron", "CAUTION", "AVOID", off_band=True),
        _row("little-rock", "AVOID", "CAUTION"),
        _row("gilbert", "CAUTION", "BUILD", vintage=True),
    ])
    assert _kinds(items) == {"dcpi_verdict_shift": 1, "dcpi_restatement": 1}
    shift = next(i for i in items if i["kind"] == "dcpi_verdict_shift")
    assert "Little-Rock" in shift["title"]
    notice = next(i for i in items if i["kind"] == "dcpi_restatement")
    assert notice["title"].startswith("2 DCPI verdicts restated")


def test_restatement_never_outranks_a_genuine_shift():
    """Consumers sort on weight and cut at 50 items. A restatement notice
    that outranked a real move would bury the signal it exists to protect."""
    items, _ = _run([
        _row("akron", "CAUTION", "AVOID", off_band=True),
        _row("little-rock", "CAUTION", "BUILD"),
    ])
    notice = next(i for i in items if i["kind"] == "dcpi_restatement")
    shift = next(i for i in items if i["kind"] == "dcpi_verdict_shift")
    assert notice["weight"] < shift["weight"], (notice, shift)


def test_restatement_is_one_item_not_one_per_market():
    """A spike day is ~200 markets. Emitting one item each would flood the
    50-item payload and drown every other kind in the feed."""
    rows = [_row(f"m{i}", "CAUTION", "AVOID", off_band=True) for i in range(200)]
    items, _ = _run(rows)
    assert _kinds(items) == {"dcpi_restatement": 1}, _kinds(items)
    assert items[0]["title"].startswith("200 DCPI verdicts restated")


# ─────────────────────────────────────────────────────────────────────────
# The statement has to actually ask for the markers
# ─────────────────────────────────────────────────────────────────────────

def test_pass_one_selects_both_markers_from_sql():
    """The stub hands the flags in, so a body that ignored the SQL entirely
    would still pass the behavioural tests above. This pins the other end:
    Pass 1 must compute both markers in the statement."""
    _items, cur = _run([_row("little-rock", "AVOID", "CAUTION")])
    pass1 = next((s for s in cur.sql if "prior_verdict" in s.lower()), "")
    assert pass1, "Pass 1 statement not found"
    low = " ".join(pass1.lower().split())
    assert "method_version is distinct from" in low, (
        "Pass 1 no longer derives the VINTAGE marker; a method bump would be "
        "republished as a market move.")
    assert "as off_band" in low, (
        "Pass 1 no longer derives the OFF-BAND marker; the healer-era "
        "relabels and any future band change would be republished as moves.")


def test_pass_one_bands_come_from_the_published_table():
    """The band thresholds in Pass 1 must be GENERATED from VERDICT_BANDS.

    A fifth hand-typed band table is the defect #2436 closed and
    tests/test_dcpi_verdict_bands.py bans on the write side; this is the same
    ban on the read side. Proved by mutating the published bands and
    requiring the emitted SQL to follow.

    ★ The ABSENCE assertion is the load-bearing half, and it was added after
    this guard survived a mutation. Pass 1 splices the bands TWICE — once for
    the latest row, once for the prior one. Checking only that the mutated
    thresholds appear passes when just ONE splice is still generated, because
    the other one supplies them: a hand-typed copy on the `latest` side
    survived undetected. Requiring the ORIGINAL thresholds to be gone catches
    a stale copy on either side, because a generated splice cannot leave them
    behind and a typed one cannot help it.
    """
    import util.dcpi_method as dm

    _items, cur = _run([_row("little-rock", "AVOID", "CAUTION")])
    pass1 = next(s for s in cur.sql if "prior_verdict" in s.lower())
    for _label, band in dm.VERDICT_BANDS:
        assert str(band["excess_min"]) in pass1, (
            f"excess_min {band['excess_min']} from VERDICT_BANDS is absent "
            "from Pass 1 — the SQL is not generated from the published bands.")

    stale = sorted({str(b["excess_min"]) for _l, b in dm.VERDICT_BANDS}
                   | {str(b["constraint_max"]) for _l, b in dm.VERDICT_BANDS})
    original = dm.VERDICT_BANDS
    try:
        dm.VERDICT_BANDS = (("BUILD", {"excess_min": 12.5,
                                       "constraint_max": 87.5}),)
        _items2, cur2 = _run([_row("little-rock", "AVOID", "CAUTION")])
        mutated = next(s for s in cur2.sql if "prior_verdict" in s.lower())
    finally:
        dm.VERDICT_BANDS = original

    assert "12.5" in mutated and "87.5" in mutated, (
        "moving a published band did not move Pass 1's SQL, so Pass 1 is "
        "carrying its own copy of the thresholds.")
    left_behind = [t for t in stale if t in mutated]
    assert not left_behind, (
        f"Pass 1 still carries the pre-mutation thresholds {left_behind} "
        "after VERDICT_BANDS moved. At least one of its two band splices is "
        "hand-typed rather than generated by verdict_case_sql, so it will "
        "silently disagree with the published rule the next time a band "
        "changes — and disagreeing band tables are the whole defect here.")


# ─────────────────────────────────────────────────────────────────────────
# The fallback
# ─────────────────────────────────────────────────────────────────────────

def test_restatement_notice_does_not_retire_the_pass_two_fallback():
    """★ The regression this ordering invites.

    Pass 2 used to be gated on `if not out`. A restatement notice makes
    `out` truthy while being no DCPI signal at all, so keying on `out` would
    silently kill the fallback on precisely the days it matters most — a mass
    relabel, where every candidate is suppressed and the feed would carry one
    notice and nothing else.
    """
    pass2 = [("gilbert", "Gilbert", "WECC", "BUILD", 72.0, 30.0, None)]
    items, _ = _run([_row("akron", "CAUTION", "AVOID", off_band=True)],
                    pass2_rows=pass2)
    kinds = _kinds(items)
    assert kinds.get("dcpi_restatement") == 1, kinds
    assert kinds.get("dcpi_verdict_shift") == 1, (
        "Pass 2 did not fire on an all-restatement day. It is gated on the "
        "count of GENUINE shifts, not on whether `out` is empty.")


def test_pass_two_still_suppressed_when_a_genuine_shift_exists():
    """The other half of that gate: a real shift must still pre-empt the
    current-state fallback, or every day's feed carries both."""
    pass2 = [("gilbert", "Gilbert", "WECC", "BUILD", 72.0, 30.0, None)]
    items, _ = _run([_row("little-rock", "AVOID", "CAUTION")],
                    pass2_rows=pass2)
    assert _kinds(items) == {"dcpi_verdict_shift": 1}, _kinds(items)
    assert "shifted" in items[0]["title"]
