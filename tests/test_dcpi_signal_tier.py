"""Regression guards for the WS3 per-market DCPI signal tier (r-ws3-signal-tier).

The gap this pins: before the tier existed, a market whose every score input
came from the hardcoded ``iso_defaults`` dict was published with exactly the
same confidence as one fed by live interconnect_queue / planned_generators /
grid_telemetry reads. The LOW_SIGNAL verdict could not cover it — it is written
by dchub_self_heal's strict matrix only when a score is exactly 0, which the
defaults guarantee never happens (0 of 310 published markets carried it on
2026-07-28).

Style follows tests/test_dcpi_regressions.py: source-level + executed-snippet
asserts, never importing the Flask app, nothing at module scope. Where a claim
can be checked against a real emitted VALUE rather than a source substring, it
is — a grep-for-a-string assert is satisfied by a comment.
"""
import os
import re
import textwrap


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _dcpi_src():
    with open(os.path.join(_repo_root(), "routes", "dcpi.py"), encoding="utf-8") as fh:
        return fh.read()


def _tier_block_src():
    """The shipped tier-derivation snippet, sliced out of routes/dcpi.py.

    Deliberately executes the REAL source rather than a copy of the rule, so a
    silent edit to the thresholds fails here. Slicing by text (not importing
    and calling gather_metrics_for_market) keeps the test DB-free — that
    function opens connections on three tables.
    """
    src = _dcpi_src()
    start = src.index("    _live_adapters = sorted(k for k, v in _adapters.items() if v)")
    end = src.index('    metrics["signal_tier"] = _tier', start)
    return textwrap.dedent(src[start:end])


def _run_tier(adapters, iso_default_matched, iso="PJM"):
    """Execute the shipped snippet against stubs; return (tier, signal_detail).

    Every free variable the snippet needs is supplied explicitly — if the
    shipped code starts reading a name this harness does not provide, the exec
    raises NameError and the test fails loudly rather than silently skipping.
    """
    import util.dcpi_method as _dm
    ns = {
        "_adapters": dict(adapters),
        "_iso_default_matched": iso_default_matched,
        "iso": iso,
        "_live_used": [],
        "_modeled_used": [],
        "_loc": {"local_substation_count": 0},
        "data_basis": {},
        "metrics": {},
        # r-ws3-methodology (2026-07-29): two new free variables in the shipped
        # snippet. _override_replaced_live records fields where a live adapter
        # answered but a slug_override then replaced the value (the adapter
        # still counts toward the tier; the FIELD is no longer live).
        # _METHOD_SIGNAL_TIER replaces the hand-copied always_modeled /
        # never_populated lists with the published input registry.
        "_override_replaced_live": [],
        "_METHOD_SIGNAL_TIER": _dm.SIGNAL_TIER,
    }
    exec(compile(_tier_block_src(), "<tier_block>", "exec"), ns, ns)
    return ns["data_basis"]["signal_tier"], ns["data_basis"]["signal_detail"]


def _adapters(queue, gen, telemetry):
    return {"interconnect_queue": bool(queue),
            "planned_generators": bool(gen),
            "grid_telemetry": bool(telemetry)}


def test_signal_tier_vocabulary_is_exactly_full_partial_low():
    """Only three values may ever be emitted. A fourth would silently break
    every consumer's mapping (and the HTML badge falls through to
    'unrecorded')."""
    seen = set()
    for q in (0, 1):
        for g in (0, 1):
            for t in (0, 1):
                for matched in (True, False):
                    tier, _ = _run_tier(_adapters(q, g, t), matched)
                    seen.add(tier)
    assert seen == {"full", "partial", "low"}, seen


def test_signal_tier_counts_live_adapters_not_live_fields():
    """3/3 adapters => full, 1-2 => partial, 0 => low. Derived from which
    ADAPTERS ran, not from the data_basis live/mixed/modeled label — 'mixed'
    spans 1..3 live adapters and would collapse full into partial."""
    assert _run_tier(_adapters(1, 1, 1), True)[0] == "full"
    assert _run_tier(_adapters(1, 1, 0), True)[0] == "partial"
    assert _run_tier(_adapters(0, 1, 0), True)[0] == "partial"
    assert _run_tier(_adapters(0, 0, 0), True)[0] == "low"


def test_zero_live_adapters_reports_its_numeric_basis():
    """House rule: never an adjective without its number. 'low' must ship the
    count it was derived from, both readings (n and max), plus the reason."""
    tier, detail = _run_tier(_adapters(0, 0, 0), True)
    assert tier == "low"
    assert detail["live_adapter_count"] == 0
    assert detail["live_adapter_max"] == 3
    assert detail["live_adapters"] == []
    assert sorted(detail["silent_adapters"]) == [
        "grid_telemetry", "interconnect_queue", "planned_generators"]
    assert "modeled constant" in detail["reason"]


def test_iso_fail_open_forces_low_even_with_every_adapter_live():
    """iso_defaults.get(iso, iso_defaults['WECC']) fails OPEN — an unknown or
    NULL ISO is scored with Western-grid parameters and looks identical to a
    matched one. That already shipped once (SOCO/FRCC, ~22 Southeast markets).
    Such a market must never be labelled partial/full, however many adapters
    fired, and the reason must name the fail-open."""
    for iso in (None, "SOCO-TYPO", ""):
        tier, detail = _run_tier(_adapters(1, 1, 1), False, iso=iso)
        assert tier == "low", (iso, tier)
        assert detail["iso_default_matched"] is False
        assert "iso_default_fail_open" in detail["reason"]
        # Both readings stay visible: the adapter count is NOT zeroed just
        # because the ISO anchor was wrong.
        assert detail["live_adapter_count"] == 3


def test_full_does_not_claim_every_input_is_measured():
    """'full' means every adapter that CAN be live was live. Five score inputs
    have no live source at all and emergency_count_30d is never assigned
    anywhere in routes/dcpi.py (20% of constraint_score is a permanent 0), so
    the payload must say so or a consumer will read 'full' as 'fully
    measured'."""
    tier, detail = _run_tier(_adapters(1, 1, 1), True)
    assert tier == "full"
    for k in ("curtailment_pct", "queue_approval_rate_pct", "btm_headroom_mw",
              "stranded_capacity_mw", "demand_growth_yoy_pct"):
        assert k in detail["always_modeled_inputs"], k
    assert detail["never_populated_inputs"] == ["emergency_count_30d"]
    assert "NOT that every score input is measured" in detail["scope_note"]


def test_emergency_count_30d_is_still_never_assigned():
    """Pins the claim above. If someone wires this input up, the honesty note
    in signal_detail becomes wrong and must be updated with it."""
    src = _dcpi_src()
    assigns = re.findall(r'^\s*metrics\["emergency_count_30d"\]\s*=', src, re.M)
    assert assigns == [], assigns


def test_adapter_absence_is_not_reported_as_an_error():
    """_state_queue_depth and _state_gen_additions both return None for a DB
    error AND for a legitimately empty result. The tier may say 'no data'; it
    must not claim to distinguish the two."""
    _, detail = _run_tier(_adapters(0, 0, 0), True)
    assert "cannot distinguish" in detail["adapter_null_semantics"]


def test_tier_key_cannot_reach_the_scorers():
    """signal_tier / _iso_default_matched are non-numeric keys, so
    compute_constraint_score and compute_excess_power_score (which .get() only
    the documented numeric keys) must produce byte-identical output with and
    without them. This is what makes the whole feature score-neutral."""
    from routes.dcpi import compute_constraint_score, compute_excess_power_score
    base = {"queue_wait_months": 42.0, "reserve_margin_pct": 13.5,
            "demand_growth_yoy_pct": 8.0, "curtailment_pct": 4.0,
            "queue_approval_rate_pct": 55.0, "btm_headroom_mw": 800.0,
            "gen_additions_12mo_mw": 1200.0, "stranded_capacity_mw": 0.0}
    tiered = dict(base, signal_tier="low", _iso_default_matched=False,
                  _adapters={"interconnect_queue": True})
    assert compute_constraint_score(base) == compute_constraint_score(tiered)
    assert compute_excess_power_score(base) == compute_excess_power_score(tiered)


def test_signal_tier_is_never_masked_from_non_paid_callers():
    """The tier is an honesty label, not a paid metric — it must stay visible
    exactly like data_basis does. There are TWO mask lists (module-level and a
    duplicate inside api_scores); both are checked, by VALUE not by source."""
    from routes.dcpi import _DCPI_MASK_FIELDS, _DCPI_MASK_EXTRA
    assert "signal_tier" not in _DCPI_MASK_FIELDS
    assert "signal_tier" not in _DCPI_MASK_EXTRA
    src = _dcpi_src()
    local = re.search(r"^    _MASK_FIELDS = \((.*?)\)\n", src, re.S | re.M).group(1)
    assert "signal_tier" not in local
    # quality_score is NOT a signal-quality field (self-heal clamps it to >=80
    # for every published row) — the tier must not be aliased onto it.
    assert "quality_score" in _DCPI_MASK_EXTRA


def test_recompute_insert_placeholder_count_matches_vals():
    """★ The recompute INSERT uses a hand-written positional VALUES string that
    shares its tuple with the UPDATE. A miscounted %s is swallowed by the
    per-market try/except (the run bumps an error counter and the endpoint
    still returns 200), so the arithmetic is asserted here instead: both
    statements bind len(_vals)+1 values (the tuple plus the trailing slug)."""
    import ast
    src = _dcpi_src()
    vals_src = re.search(r"_vals = \((.*?)\n                \)", src, re.S).group(1)
    n_vals = len(ast.parse("(" + vals_src + ")", mode="eval").body.elts)
    upd = re.search(r"UPDATE market_power_scores SET(.*?)WHERE market_slug=%s",
                    src, re.S).group(0)
    assert upd.count("%s") == n_vals + 1, (upd.count("%s"), n_vals)
    ins = re.search(r"INSERT INTO market_power_scores \((.*?)\)\n\s*VALUES \((.*?)\)\n",
                    src, re.S)
    cols = [c.strip() for c in ins.group(1).replace("\n", " ").split(",") if c.strip()]
    n_ph = ins.group(2).count("%s")
    assert n_ph == n_vals + 1, (n_ph, n_vals)
    # TRUE and NOW() are the two literal columns without a placeholder.
    assert len(cols) == n_ph + 2, (len(cols), n_ph)
    assert "signal_tier" in cols


def test_signal_tier_column_is_added_inside_the_advisory_lock():
    """ADD COLUMN takes an AccessExclusiveLock; concurrent ALTERs from both
    replicas deadlocked before pg_advisory_xact_lock(572341001) was added. The
    new ALTER must sit inside _ensure_tables, after that lock."""
    src = _dcpi_src()
    lock = src.index("pg_advisory_xact_lock(572341001)")
    alter = src.index("ADD COLUMN IF NOT EXISTS signal_tier TEXT")
    end_of_fn = src.index("\ndef ", lock)
    assert lock < alter < end_of_fn


def test_null_tier_is_surfaced_as_unknown_never_as_low():
    """Rows predating the column have no tier. Coercing NULL to 'low' would
    publish a measurement that was never taken, so every reader must emit null
    plus an explicit basis. Checked on the emitted expression, not a comment.

    r-ws3-methodology (2026-07-29): the basis phrase used to be hand-copied
    into four readers, and all four copies carried the same FALSE claim — that
    an unrecorded tier might mean the row "was written by the lite recompute
    path". That path iterates MARKETS (tuples) and raises AttributeError on
    every market inside its own swallow-all; it has written zero rows. There is
    now ONE definition and every reader calls it, so this test pins the shared
    helper rather than counting copies of a string.
    """
    src = _dcpi_src()
    assert "def _signal_tier_basis(" in src, \
        "the basis string is hand-copied again instead of shared"
    assert src.count("_SIGNAL_TIER_BASIS_UNRECORDED = (") == 1, \
        "more than one definition of the unrecorded basis phrase"
    assert src.count("_signal_tier_basis(") >= 5, \
        "some reader still builds the basis string itself"
    assert "tier unknown, NOT low" in src
    # The retracted false attribution must not come back.
    assert "lite recompute path — tier unknown" not in src
    # and none of them defaults the value itself to the string "low".
    assert 'signal_tier") or "low"' not in src
    assert "signal_tier'] or 'low'" not in src


def test_iso_aggregates_expose_both_count_families():
    """low_signal_count (the LOW_SIGNAL verdict) is a permanent 0 in
    production. The signal-tier counts are a different measurement; both are
    exposed and the response says so, so neither can be read as the other."""
    src = _dcpi_src()
    for col in ("signal_tier_full_count", "signal_tier_partial_count",
                "signal_tier_low_count", "signal_tier_unrecorded_count"):
        assert col in src, col
        # *_count suffix is load-bearing: the iso-comparison non-paid mask
        # keeps every key ending in _count and nulls the rest.
        assert col.endswith("_count")
    assert "AS low_signal_count" in src


def _rank_markets_src():
    with open(os.path.join(_repo_root(), "routes", "mcp_tier1_tools.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    fn = src[src.index("def _rank_markets_ai_ready("):]
    return fn[:fn.index("\ndef ", 1)]


def test_rank_markets_emits_the_tier_itself():
    """rank_markets hand-builds its result dicts, so it inherits nothing from
    the REST envelope (unlike get_market_dcpi_rank, which passes the body
    through verbatim). Patching routes/dcpi.py alone leaves it tier-less."""
    fn = _rank_markets_src()
    assert "signal_tier, computed_at " in fn, "tier not selected"
    assert '"signal_tier":' in fn, "tier not emitted in the result dict"
    assert '"as_of":' in fn, "as-of not emitted"


def test_rank_markets_never_goes_dark_over_the_new_column():
    """This is the one DCPI-reading surface that does not run through a
    routes/dcpi.py route, so nothing else guarantees _ensure_tables has added
    signal_tier before the SELECT. It must ensure the column AND keep a
    pre-tier retry, or a cold process 500s the top ranking tool."""
    fn = _rank_markets_src()
    assert "_dcpi_ensure_tables()" in fn, "column not ensured before the read"
    assert "sql_legacy" in fn, "no pre-tier retry"
    assert "c.rollback()" in fn, "retry reuses an aborted transaction"


def test_rank_markets_distinguishes_no_tier_from_no_column():
    """A row that carries no tier is NOT the same fact as a read that could not
    see the column. Both are unknown, neither is 'low', and the response says
    which one happened."""
    fn = _rank_markets_src()
    assert "unrecorded: row predates signal tiering" in fn
    assert "unavailable: signal_tier was not readable" in fn
    assert "not low" in fn
