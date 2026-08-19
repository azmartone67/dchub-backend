"""Guard for the stored-slug 404 gap — 2026-08-19.

★ THE DEFECT

`backfill_id_scheme_aliases` aliases the slug it RECOMPUTES from
(provider, name, id) — the pre-2026-06-16 MD5(id) form. It never looks at the
`slug` value actually stored on the row. After the scheme swap that column is
stale nearly everywhere.

Measured live:

    discovered_facilities rows with a slug        26,239
    stored slug != canonical_slug                 26,112
    of those, pre-swap non-hash8 form              9,822
    ...that had an alias row                           0
    live probe of 30 such slugs                       17 x 404
    GSC "Not found (404)" on the property           3,576

The serving path was never broken — render_facility_profile calls
resolve_alias() before it 404s. The alias table simply had no rows for this
population, and /api/v1/admin/slug/status published `frozen: 26,112 / 26,239`,
which reads finished. A completion metric that cannot express its own gap is
how this stayed invisible for two months.

★ THE BACKFILL WAS MEASURED BEFORE IT SHIPPED. 40 (old, canonical) pairs were
probed live: 40/40 canonical targets returned 200 while 33/40 old slugs
returned 404. Pointing 301s at 404s would be worse than the 404s.

Static and pure: the DB functions are checked by AST, the detector contract by
its shape. No DB, no network — routes/facility_slug_freeze.py opens a
connection at import time.
"""
import ast
import os

import pytest

_FREEZE = os.path.join("routes", "facility_slug_freeze.py")
_RADAR = os.path.join("routes", "brain_consistency_radar.py")


def _tree(path):
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read()), fh


def _func(path, name):
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _src(path, node):
    return ast.get_source_segment(open(path, encoding="utf-8").read(), node) or ""


# ── the backfill exists and is non-destructive ───────────────────────────────

def test_the_backfill_exists():
    assert _func(_FREEZE, "backfill_stored_slug_aliases"), (
        "the stored `slug` column has no backfill — id-scheme aliases do NOT "
        "cover it")


def test_the_backfill_never_repoints_an_existing_alias():
    """ON CONFLICT DO NOTHING, not DO UPDATE.

    An explicit load (GSC-export capture) or an id-scheme alias must win. This
    pass ADDS rescue paths; if it could repoint one it could break a redirect
    that is already correct and already indexed.
    """
    src = _src(_FREEZE, _func(_FREEZE, "backfill_stored_slug_aliases"))
    assert "ON CONFLICT (old_slug) DO NOTHING" in src
    assert "DO UPDATE" not in src


def test_the_backfill_only_aliases_slugs_that_differ_from_canonical():
    """Aliasing slug -> itself is a redirect loop, and a 308 loop passes a
    naive status-code check."""
    src = _src(_FREEZE, _func(_FREEZE, "backfill_stored_slug_aliases"))
    assert "slug IS DISTINCT FROM canonical_slug" in src
    assert "canonical_slug IS NOT NULL AND canonical_slug <> ''" in src


def test_the_backfill_is_batched_and_bounded():
    """Admin POSTs through the edge time out at 15s; an unbounded loop over
    26k rows would 503 while the origin kept writing."""
    fn = _func(_FREEZE, "backfill_stored_slug_aliases")
    args = {a.arg for a in fn.args.args}
    assert {"batch", "max_batches"} <= args
    assert "LIMIT %s OFFSET %s" in _src(_FREEZE, fn)


# ── the gap is measurable, and measurable separately from completion ─────────

def test_the_gap_function_exists_and_returns_a_pair():
    fn = _func(_FREEZE, "stored_slug_alias_gap")
    assert fn, "no way to measure the gap = no way to detect its return"
    src = _src(_FREEZE, fn)
    assert "LEFT JOIN facility_slug_aliases" in src, (
        "the gap must be rows WITHOUT an alias — an inner join measures the "
        "opposite population")
    assert "a.old_slug IS NULL" in src


def test_status_publishes_the_gap_beside_the_completion_number():
    """`frozen: 26,112` read finished while 9,822 slugs 404'd. The gap has to
    sit next to it or the same misread recurs."""
    src = _src(_FREEZE, _func(_FREEZE, "slug_freeze_status"))
    assert "stored_slug_no_alias_gap" in src
    assert "stored_slug_stale" in src


def test_an_unmeasurable_gap_is_none_not_zero():
    """★ An empty/failed read must not certify a clean window.

    Same rule as the CI-origin lane: 0 and UNMEASURED are different findings,
    and only one of them means 'nothing to do'.

    ★ ASSERTED ON THE EXCEPT HANDLER, NOT ON THE FILE TEXT. The first draft
    checked `"gap = stale = None" in src` and SURVIVED a mutation that changed
    the handler to `gap, stale = 0, 0` — because the identical string also
    appears on the initialiser a few lines above. The substring was true of the
    file while being false of the branch under test. Walk to the handler.
    """
    fn = _func(_FREEZE, "slug_freeze_status")
    handlers = [h for node in ast.walk(fn) if isinstance(node, ast.Try)
                for h in node.handlers
                if any(isinstance(n, ast.Name) and n.id in ("gap", "stale")
                       for stmt in h.body for n in ast.walk(stmt))]
    assert handlers, "no except handler assigns gap/stale — nothing guards it"
    for h in handlers:
        for stmt in h.body:
            if not isinstance(stmt, ast.Assign):
                continue
            names = {n.id for t in stmt.targets for n in ast.walk(t)
                     if isinstance(n, ast.Name)}
            if not names & {"gap", "stale"}:
                continue
            vals = ([stmt.value] if not isinstance(stmt.value, ast.Tuple)
                    else list(stmt.value.elts))
            for v in vals:
                assert isinstance(v, ast.Constant) and v.value is None, (
                    "a failed gap read must publish null, never a reassuring "
                    f"0 — handler assigns {ast.dump(v)[:60]}")


def test_the_freeze_run_actually_calls_the_backfill():
    """A backfill nothing invokes is dead code that reads as a fix."""
    src = _src(_FREEZE, _func(_FREEZE, "slug_freeze_run"))
    assert "backfill_stored_slug_aliases(" in src


# ── the detector, so the hole cannot silently reopen ─────────────────────────

def test_the_detector_exists_and_is_registered():
    """★ THE ARMING TEST. A detector that is never in the sweep list never
    runs, and the finding it would have raised is indistinguishable from
    'nothing is wrong'."""
    assert _func(_RADAR, "check_stale_stored_slug_404s"), "detector missing"
    body = open(_RADAR, encoding="utf-8").read()
    fn_start = body.index("def check_stale_stored_slug_404s")
    registered = body.count("check_stale_stored_slug_404s")
    assert registered >= 2, (
        "the detector is defined but never registered in the sweep tuple — "
        f"only {registered} mention(s) in the file")
    # ...and the registration is not merely inside its own definition
    assert body.index("check_stale_stored_slug_404s,") != fn_start


def test_the_detector_has_a_nonzero_floor_and_reports_the_count():
    src = _src(_RADAR, _func(_RADAR, "check_stale_stored_slug_404s"))
    assert "_SLUG_GAP_FLOOR" in src, "no threshold — fires on any churn"
    assert "gap <= _SLUG_GAP_FLOOR" in src
    assert '"issue": "stale_stored_slug_no_alias"' in src
    assert '"count": gap' in src


def test_the_detector_names_the_remedy():
    """A finding a human cannot act on is a log line."""
    src = _src(_RADAR, _func(_RADAR, "check_stale_stored_slug_404s"))
    assert "/api/v1/admin/slug/freeze" in src


def test_a_db_failure_yields_no_finding_rather_than_a_false_clean():
    """The detector must not invent a 0-gap finding when it could not read.

    Absence of a finding is the correct failure here (the sweep reports it as
    unmeasured); a finding claiming gap=0 would be a lie.
    """
    src = _src(_RADAR, _func(_RADAR, "check_stale_stored_slug_404s"))
    assert "continue          # UNMEASURED for this table, not clean" in src
    assert "if conn is None:" in src


@pytest.mark.parametrize("bad", ["DO UPDATE", "slug = canonical_slug"])
def test_backfill_does_not_contain_known_footguns(bad):
    assert bad not in _src(
        _FREEZE, _func(_FREEZE, "backfill_stored_slug_aliases"))
