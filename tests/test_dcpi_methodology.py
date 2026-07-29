"""Guards for r-ws3-methodology (2026-07-29): the published method must BE
the scoring method.

Until this change the only DCPI methodology DC Hub published
(https://dchub.cloud/dcpi/methodology/, a Cloudflare Pages static file) was
fiction: a five-term excess formula whose terms exist nowhere in this repo, a
NEUTRAL verdict band derive_verdict cannot emit, and a /data/dcpi-history.csv
download that 404s. Measured 2026-07-29: 209 of 311 published markets (67%)
carried a verdict those published bands could not produce.

The root cause is HAND-COPYING a weight into a second place. So these tests
pin the coupling itself, not just today's numbers:

  * util/dcpi_method.py is the only place a weight is written down, and
  * routes/dcpi.py IMPORTS it rather than restating it, and
  * real published rows reproduce from the constants alone.

Pure source/AST + pure-function asserts. No DB, no network, and — per
tests/conftest.py — no `import main`. Nothing runs at module scope.
"""
import ast
import os


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel_path):
    with open(os.path.join(ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


def _parsed(rel_path):
    """AST of a file, with the parse ITSELF asserted.

    An empty/failed parse would make every downstream `not in` assertion pass
    against zero characters — the silent-green trap. Assert the module parsed
    and has a body before trusting anything extracted from it.
    """
    src = _read(rel_path)
    tree = ast.parse(src)
    assert isinstance(tree, ast.Module) and tree.body, \
        f"{rel_path} produced an empty parse — assertions would be vacuous"
    return src, tree


def _func_source(rel_path, func_name):
    """Source of a function, located via a real AST parse and asserted found."""
    src, tree = _parsed(rel_path)
    found = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == func_name:
            found = node
            break
    assert found is not None, f"{func_name} not found in {rel_path} (renamed?)"
    segment = ast.get_source_segment(src, found)
    assert segment and len(segment) > 120, \
        f"{func_name} source segment came back empty/tiny — extraction broken"
    return segment


def _numeric_literals(rel_path, func_name):
    """Every bare int/float literal inside a function body.

    Used to prove the scorer no longer carries its own copies of the weights.
    """
    src, tree = _parsed(rel_path)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            target = node
            break
    assert target is not None, f"{func_name} not found in {rel_path}"
    out = []
    for node in ast.walk(target):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            out.append(float(node.value))
    assert out or func_name == "derive_verdict", \
        f"{func_name} yielded zero literals — extraction is broken, not clean"
    return out


# ── 1. The method module exists and says nothing fabricated ────────────────

# Terms the fabricated static page published. None of them has ever existed in
# this codebase. If one shows up in the method module, someone transcribed the
# old page instead of reading the scorer.
_FABRICATED_TERMS = (
    "utility_subscription_ratio",
    "tier1_market_overlap_score",
    "generator_queue_velocity",
    "local_renewable_surplus_mw",
    "transmission_congestion_lmp_spread",
    "regulatory_friction_index",
    "water_stress_index_weight",
    "substation_distance_weight",
)


def test_method_module_publishes_the_real_inputs_not_the_fabricated_ones():
    """FAILS before this change: util/dcpi_method.py did not exist.

    Asserted against the EMITTED payload, not the file text: the module
    docstring deliberately quotes the fabricated formula as a record of what
    went wrong, and that history is worth keeping. What must never carry a
    fabricated term is the document served to readers."""
    import json
    from util import dcpi_method as dm
    published = json.dumps(dm.method_block())
    for term in _FABRICATED_TERMS:
        assert term not in published, \
            f"{term!r} is from the FABRICATED static page — it scores nothing"
    names = {i["name"] for i in dm.INPUTS}
    # The nine real score inputs, exactly.
    for real in ("queue_wait_months", "reserve_margin_pct", "emergency_count_30d",
                 "demand_growth_yoy_pct", "curtailment_pct",
                 "queue_approval_rate_pct", "btm_headroom_mw",
                 "stranded_capacity_mw", "gen_additions_12mo_mw"):
        assert real in names, f"{real} missing from the published input registry"
    # Every input must state units and a missing-behaviour. A published input
    # with no stated fallback is how iso_defaults' WECC fail-open shipped
    # "500 MW behind-the-meter headroom" to ~22 Southeast markets.
    for i in dm.INPUTS:
        assert i.get("units"), f"{i['name']} has no units"
        assert i.get("missing_behaviour"), f"{i['name']} has no missing-behaviour"
        assert "live_capable" in i, f"{i['name']} does not say if it can be live"


def test_never_populated_and_always_modeled_inputs_are_declared():
    """emergency_count_30d is never assigned anywhere in routes/dcpi.py, so
    20% of every constraint score is a structural zero. That must be published,
    not merely true."""
    from util import dcpi_method as dm
    assert dm.SIGNAL_TIER["never_populated_inputs"] == ["emergency_count_30d"]
    always = set(dm.SIGNAL_TIER["always_modeled_inputs"])
    assert {"curtailment_pct", "queue_approval_rate_pct", "btm_headroom_mw",
            "stranded_capacity_mw", "demand_growth_yoy_pct"} <= always
    # And it must be in the plainly-worded limitations, not only in a schema.
    blob = " ".join(dm.KNOWN_LIMITATIONS)
    assert "emergency_count_30d" in blob
    assert "queue DEPTH" in blob, "the wait proxy must be published as a proxy"
    assert "311" in blob and "317" in blob, \
        "the published-count vs row-count gap must be stated"
    # An UNMEASURED figure is never published as a number.
    src = _read("util/dcpi_method.py")
    assert "operational-MW figure from DCPI is citeable" in src


def test_there_is_no_neutral_verdict_band():
    """The fabricated page published a NEUTRAL band. derive_verdict has three
    outcomes, and only three."""
    import json
    from util import dcpi_method as dm
    labels = {v for v, _ in dm.VERDICT_BANDS} | {dm.VERDICT_FALLBACK}
    assert labels == {"BUILD", "CAUTION", "AVOID"}, labels
    # No verdict token named NEUTRAL may appear anywhere in the emitted doc.
    # (The prose key `has_neutral_band: false` is the explicit denial, so the
    # check is on the TOKEN, not the substring.)
    published = json.dumps(dm.method_block())
    assert '"NEUTRAL"' not in published and "'NEUTRAL'" not in published
    assert dm.method_block()["verdict"]["has_neutral_band"] is False
    published_labels = {b["verdict"] for b in dm.method_block()["verdict"]["bands"]}
    assert "NEUTRAL" not in published_labels
    # The exact live bands, pinned. north-kansas-city (e=73.6, c=36.8) is BUILD
    # in the live index and UNDEFINED under the fabricated page's table.
    assert dm.verdict_from_scores(36.8, 73.6) == "BUILD"
    assert dm.verdict_from_scores(70.0, 50.0) == "CAUTION"
    assert dm.verdict_from_scores(90.0, 10.0) == "AVOID"


# ── 2. The scorer imports the constants instead of restating them ──────────

def test_scorers_import_the_published_constants():
    """FAILS before this change: compute_constraint_score carried
    `0.4*s_wait + 0.25*s_reserve + 0.20*s_emerg + 0.15*s_demand` inline, so the
    published doc and the scorer were two independent strings."""
    src = _read("routes/dcpi.py")
    assert "from util.dcpi_method import" in src, \
        "routes/dcpi.py no longer sources its weights from the method module"
    for name in ("CONSTRAINT_WEIGHTS", "EXCESS_WEIGHTS", "VERDICT_BANDS",
                 "COMPOSITE_VERDICT_MULTIPLIERS", "DCPI_METHOD_VERSION"):
        assert name in src, f"{name} not imported into routes/dcpi.py"


def test_scorer_bodies_carry_no_hand_written_weights():
    """The weights must be gone from the function bodies, not merely also
    present in the module. Only the 0/100 clip bounds and the *100 scaling may
    remain as literals."""
    allowed = {0.0, 1.0, 100.0}
    for func in ("compute_constraint_score", "compute_excess_power_score"):
        stray = sorted(set(_numeric_literals("routes/dcpi.py", func)) - allowed)
        assert not stray, \
            f"{func} still hard-codes {stray} — that is the drift bug returning"
    # derive_verdict must not carry its thresholds either.
    vsrc = _func_source("routes/dcpi.py", "derive_verdict")
    for banned in ("65", "50", "70"):
        assert banned not in vsrc, \
            f"derive_verdict still hard-codes the {banned} threshold"
    assert "_V_BANDS" in vsrc and "_V_FALLBACK" in vsrc
    # And the composite multiplier table must be imported, not retyped.
    csrc = _func_source("routes/dcpi.py", "derive_composite_score")
    assert "'LOW_SIGNAL': 0.35" not in csrc and '"LOW_SIGNAL": 0.35' not in csrc, \
        "the composite multiplier table is hand-copied again"
    assert "_CO_MULT" in csrc


def test_published_weights_still_sum_to_one():
    """A weights table that stops summing to 1.0 changes the scale silently.
    The bounded local terms sit ON TOP, so the pre-clip maximum is 106/108 —
    which is itself published rather than rounded away."""
    from util import dcpi_method as dm
    assert abs(sum(dm.CONSTRAINT_WEIGHTS.values()) - 1.0) < 1e-9
    assert abs(sum(dm.EXCESS_WEIGHTS.values()) - 1.0) < 1e-9
    assert abs(sum(dm.COMPOSITE_WEIGHTS.values()) - 1.0) < 1e-9
    assert abs(sum(dm.SATURATION_WEIGHTS.values()) - 1.0) < 1e-9
    block = dm.method_block()
    assert block["constraint_score"]["max_before_outer_clip"] == 106.0
    assert block["excess_power_score"]["max_before_outer_clip"] == 108.0


def test_constants_reproduce_real_published_rows():
    """The reproducibility promise, checked against LIVE values captured
    2026-07-29 from /api/v1/dcpi/scores/midland-tx. If a weight moves without
    a REVISIONS entry, this is what catches it."""
    from util import dcpi_method as dm
    # constraint: queue_wait 16.0mo, reserve 30% (>25 ceiling -> 0), no
    # emergencies (never populated), demand growth 4.0%, no local DC rows.
    assert dm.constraint_from_published_fields(
        queue_wait_months=16.0, reserve_margin_pct=30.0,
        emergency_count_30d=0, demand_growth_yoy_pct=4.0,
        local_dc_count=0) == 22.8
    # composite from the published excess/constraint/ttp/verdict triple.
    assert dm.composite_from_published_fields(85.7, 22.8, 9.6, "BUILD") == 83.0
    # The verdict multiplier is not cosmetic: an unrecognised verdict must not
    # silently earn the BUILD discount-free rate by accident of dict ordering.
    assert dm.composite_from_published_fields(85.7, 22.8, 9.6, "AVOID") < 83.0


def test_method_block_is_json_serialisable_and_complete():
    """The endpoint jsonify()s this verbatim — a non-serialisable value would
    500 a public route."""
    import json
    from util import dcpi_method as dm
    blob = json.dumps(dm.method_block())
    assert len(blob) > 5000, "method_block collapsed — the doc would be a stub"
    for key in ("fallbacks", "revisions", "cadence", "revision_policy",
                "known_limitations", "inputs", "signal_tier"):
        assert f'"{key}"' in blob, f"method_block dropped {key}"
    # Cadence must NOT be published as a clean 6-hour promise: observed run
    # starts drift 30-45 minutes past nominal.
    assert "as-of" in json.dumps(dm.CADENCE) or \
           "authoritative_timestamp" in dm.CADENCE
    # Every revision must say whether it moved scores AND whether it restated
    # the back series. That is the whole point of the list.
    for rev in dm.REVISIONS:
        assert "scores_changed" in rev and "restated_back_series" in rev, rev
        assert rev.get("date") and rev.get("what")
    # The two measured index-wide restatements must be enumerated.
    dates = {r["date"] for r in dm.REVISIONS}
    assert {"2026-07-17", "2026-07-25"} <= dates


# ── 3. The endpoint ────────────────────────────────────────────────────────

def test_methodology_endpoint_is_under_api_v1_not_dcpi():
    """FAILS before this change: routes/dcpi_methodology.py did not exist.

    Cloudflare Pages intercepts /dcpi/* before Flask, so a backend route at
    /dcpi/methodology is dead code — exactly how the fabricated static page
    went unvalidated by this repo for months."""
    src, tree = _parsed("routes/dcpi_methodology.py")
    assert '"/api/v1/dcpi/methodology"' in src or \
           "'/api/v1/dcpi/methodology'" in src, \
           "the endpoint is not at /api/v1/dcpi/methodology"
    assert '"/dcpi/methodology"' not in src, \
        "a /dcpi/* backend route is dead code — CF Pages serves that prefix"
    # Fail-soft + cached, like routes/canon_phrases.py.
    assert "public, max-age=3600" in src, "public doc must be cacheable"
    assert "PINNED" in src, "no fallback — a consumer could get nothing"
    # The fallback must NOT restate any weight; a hand-copied fallback is
    # precisely how the previous published methodology came to describe a
    # formula that does not exist. It may say "unavailable" — never a number.
    import re
    fallback = src[src.find("_PINNED"):src.find("@dcpi_methodology_bp")]
    assert len(fallback) > 100, "fallback block not found — marker moved"
    numerics = re.findall(r"\b\d+\.\d+\b", fallback)
    assert not numerics, \
        f"the PINNED fallback hand-copies numeric constants {numerics}"
    # No DB import: this endpoint must be incapable of 500ing on a DB outage.
    for banned in ("psycopg2", "DATABASE_URL", "_conn("):
        assert banned not in src, f"{banned} in a doc endpoint — it can now fail"


def test_methodology_blueprint_registered_in_the_safe_zone():
    """Late-line blueprint registration silently 404s in prod — the pattern
    that bit press_loop, market_deep_dive and competitor_recon. main.py is read
    as TEXT; this suite must never import it."""
    src = _read("main.py")
    marker = "from routes.dcpi_methodology import dcpi_methodology_bp"
    idx = src.find(marker)
    assert idx != -1, "dcpi_methodology blueprint is never registered"
    assert src.count(marker) == 1, "registered twice — one of them is dead"
    line_no = src[:idx].count("\n") + 1
    assert line_no < 3000, \
        (f"registered at line {line_no} — outside the safe zone (~1820). "
         "Late registration silently 404s on Railway.")
    # Registration must be wrapped so an import error degrades, never 500s boot.
    window = src[idx - 400:idx + 400]
    assert "try:" in window and "except Exception" in window, \
        "unwrapped registration — an import error would take the app down"


# ── 4. The provenance correction ───────────────────────────────────────────

def test_slug_override_revokes_the_live_label():
    """FAILS before this change.

    slug_overrides ran an unconditional metrics.update() AFTER the live
    interconnect_queue read, while _live_fields was populated at that call site
    and never revoked. Measured live 2026-07-29: phoenix's real queue depth
    (9,920 MW -> 18.0 months) was replaced by the hardcoded 42.0 and the
    published data_basis_note STILL listed queue_wait_months as live. Same for
    chicago. That is a provenance lie on a published field."""
    src = _func_source("routes/dcpi.py", "gather_metrics_for_market")
    assert "_live_fields.discard" in src, \
        "an override still overwrites a live value while keeping its live label"
    assert "_override_replaced_live" in src, \
        "the fields whose live label was revoked are not reported"
    # The revocation must happen where the override is applied, before the
    # data_basis label is derived from _live_fields.
    i_disc = src.find("_live_fields.discard")
    i_label = src.find('_live_used = sorted(')
    assert 0 < i_disc < i_label, \
        "the live label is computed BEFORE the override revokes it"
    # And the fact must be surfaced, not merely corrected silently.
    assert '"override_replaced_live_fields"' in src


def test_signal_tier_basis_does_not_blame_a_writer_that_wrote_nothing():
    """All four copies of the basis string claimed an unrecorded tier might
    mean the row 'was written by the lite recompute path'. That path iterates
    MARKETS (tuples) and raises AttributeError on every market inside its own
    swallow-all — it has written zero rows. A confidently wrong reason is worse
    than none."""
    src = _read("routes/dcpi.py")
    assert "lite recompute path — tier unknown" not in src, \
        "the false lite-recompute attribution is still published"
    assert "def _signal_tier_basis(" in src, \
        "the basis string is hand-copied again instead of shared"
    # One definition, and every reader uses it.
    assert src.count("_SIGNAL_TIER_BASIS_UNRECORDED = (") == 1
    assert src.count("_signal_tier_basis(") >= 5, \
        "some reader still builds the basis string itself"
    # NULL must never be coerced to a measurement.
    assert "NOT low" in src


def test_method_version_is_persisted_and_never_backfilled():
    """FAILS before this change: there was no method_version column anywhere,
    so a methodology restatement was indistinguishable from a market move in
    the published series."""
    src = _read("routes/dcpi.py")
    assert ("ALTER TABLE market_power_scores \"\n                    \"ADD COLUMN "
            "IF NOT EXISTS method_version TEXT" in src) or \
           ("ADD COLUMN IF NOT EXISTS method_version TEXT" in src), \
           "method_version column is never created"
    assert src.count("ADD COLUMN IF NOT EXISTS method_version TEXT") == 2, \
        "method_version must exist on BOTH market_power_scores and " \
        "dcpi_daily_snapshots — the snapshot table is the official series"
    # Written by the real recompute...
    writer = _func_source("routes/dcpi.py", "recompute_all_scores")
    assert "method_version=%s" in writer, "the UPDATE never sets method_version"
    assert "_method_version" in writer
    # ...and carried into the daily series.
    snap = _func_source("routes/dcpi.py", "write_dcpi_snapshot")
    assert "method_version" in snap, \
        "the official daily series does not record which method produced it"
    # Never invented: readers emit what the row holds, or nothing.
    assert "def _attach_method_version(" in src
    assert 'out["method_version"] = row.get("method_version") or None' in src


def test_forecast_reads_the_only_table_that_has_history():
    """FAILS before this change.

    market_power_scores is UPDATE-in-place with computed_at=NOW() — exactly one
    row per slug, forever — so the 30-day forecast query could only ever return
    1 sample and every market returned insufficient_history, permanently.
    Verified live 2026-07-29: midland-tx forecast.samples_in_30d = 1 while
    /api/v1/dcpi/history returned 53 real points for the same market."""
    src = _func_source("routes/dcpi.py", "api_score_market")
    assert "FROM dcpi_daily_snapshots" in src, \
        "the forecast still reads the one-row-per-market table"
    assert "FROM market_power_scores\n                 WHERE market_slug = %s\n" \
           "                   AND computed_at >= NOW() - INTERVAL '30 days'" \
           not in src, "the dead history query is back"
    # _compute_forecast reads .tzinfo off "computed_at", so a bare DATE would
    # raise inside the swallow and silently re-break the block.
    assert "snapshot_date::timestamptz AS computed_at" in src, \
        "a bare DATE would raise in _compute_forecast's _trend()"
    # UNMEASURED emits None, never 0: TTP is not snapshotted daily.
    assert "NULL::real AS time_to_power_months" in src
    assert "time_to_power_projection" in src, \
        "the missing TTP projection is not disclosed to the reader"


def test_forecast_implied_verdict_uses_the_real_verdict_function():
    """_verdict_for hand-copied a THIRD set of bands (excess>=60 &
    constraint<40 -> BUILD) that disagreed with derive_verdict's 65/50, so
    'verdict_change_from_now' could report a change the scorer would never
    make."""
    src = _func_source("routes/dcpi.py", "_compute_forecast")
    assert "return derive_verdict(constraint, excess)" in src, \
        "the forecast still uses its own verdict bands"
    assert "excess >= 60 and constraint < 40" not in src, \
        "the hand-copied forecast verdict bands are back"
