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
    # r-repro (2026-08-08): this used to read
    #     assert "311" in blob and "317" in blob
    # which PINNED the two stale literals in place. Live they were 315 and
    # 322 — both had drifted — and this assertion is part of why that
    # survived: it demanded the presence of specific numbers rather than the
    # presence of the DISCLOSURE. The counts are now injected, so the module
    # constant honestly reads UNMEASURED and the numbers appear only when a
    # caller supplies them. Assert the topic is covered, never the digits.
    assert "retired alias twin" in blob, \
        "the published-count vs row-count gap must be stated"
    assert "311" not in blob and "317" not in blob, \
        "the stale hardcoded index/row counts are back"
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
    # This endpoint must be incapable of 500ing on a DB outage.
    #
    # r-repro (2026-08-08): this used to ban the strings "psycopg2" /
    # "DATABASE_URL" / "_conn(" outright. That was a proxy for the real
    # property, and it became untenable: three published figures ("311
    # markets", "317 rows", an 89.1-month queue-wait ceiling) had drifted
    # because a module with no DB access cannot describe the live index, so it
    # described a remembered one instead. A stale number in a methodology
    # document is a worse failure than the one this ban prevented.
    #
    # So the ban is replaced by the property it stood for, asserted directly
    # and more strictly: DB access is CONFINED to _live_counts, that helper is
    # exception-total (returns a dict on every path, raises on none), and the
    # request handler itself contains no DB code.
    handler = _func_source("routes/dcpi_methodology.py", "dcpi_methodology")
    for banned in ("psycopg2", "DATABASE_URL", "_conn(", "connect("):
        assert banned not in handler, \
            f"{banned} in the request handler — a DB fault can now 500 the doc"
    counts_fn = _func_source("routes/dcpi_methodology.py", "_live_counts")
    assert "except Exception" in counts_fn, \
        "_live_counts is not exception-total — it can raise into the handler"
    # Every `return` in the helper must hand back a dict, so the handler's
    # `.get(...)` can never explode on a None or a bare value.
    import ast as _ast
    returns = [n for n in _ast.walk(_ast.parse(counts_fn.strip()))
               if isinstance(n, _ast.Return)]
    assert len(returns) >= 4, "_live_counts lost its failure branches"
    for r in returns:
        assert isinstance(r.value, _ast.Dict), \
            "_live_counts has a return that is not a dict literal"
    # A bounded connection: an unbounded connect would hang the doc, which is
    # the same outage in slower clothes.
    assert "connect_timeout" in counts_fn and "statement_timeout" in counts_fn, \
        "the counting query is unbounded — a stuck DB would hang the document"


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


# ── 4. r-repro (2026-08-08): the published reproducibility claim must be TRUE ──
#
# The endpoint published, verbatim:
#
#   "constraint_score and composite_score are reproducible from the fields
#    published on /api/v1/dcpi/scores/<slug> using the weights above."
#
# Half true. Measured across all 315 published markets: composite_score and
# verdict reproduce 315/315 exactly; constraint_score reproduces 0/315,
# because demand_growth_yoy_pct (weight 0.15) and local_dc_count (bonus 0.06)
# are not columns on market_power_scores and so are emitted for NO market.
# Johor's residual is 21.0 of 41.0 — the theoretical maximum.
#
# These tests recompute constraint_score per market from ONLY the published
# fields and bind the RESULT to the CLAIM, in both directions, so neither a
# false "reproducible" nor a gratuitous "not reproducible" can be published.

# Real payloads captured 2026-08-08 from /api/v1/dcpi/scores/<slug> on the
# Railway origin. Deliberately spans the residual range (upper-michigan 1.22,
# johor 21.00 = the theoretical max), both verdicts, and the four markets whose
# queue_wait_months exceeds the old "true ceiling" of 89.1.
#
# Every field here is one the endpoint ACTUALLY emits. The absence of
# demand_growth_yoy_pct and local_dc_count is not an omission in the fixture —
# it is the defect under test, and _fixture_rows asserts they stay absent.
def _fixture_rows():
    return [
        {"market_slug": "johor",
         "queue_wait_months": 18.0, "reserve_margin_pct": 26.0, "emergency_count_30d": 0,
         "constraint_score": 41.0, "excess_power_score": 43.8, "time_to_power_months": 10.8,
         "verdict": "AVOID", "composite_score": 31.3},
        {"market_slug": "upper-michigan",
         "queue_wait_months": 16.0, "reserve_margin_pct": 24.2, "emergency_count_30d": 0,
         "constraint_score": 19.8, "excess_power_score": 73.2, "time_to_power_months": 9.6,
         "verdict": "BUILD", "composite_score": 76.4},
        {"market_slug": "tokyo",
         "queue_wait_months": 48.0, "reserve_margin_pct": 12.0, "emergency_count_30d": 0,
         "constraint_score": 66.5, "excess_power_score": 19.8, "time_to_power_months": 48.0,
         "verdict": "AVOID", "composite_score": 14.4},
        {"market_slug": "london",
         "queue_wait_months": 144.0, "reserve_margin_pct": 7.0, "emergency_count_30d": 0,
         "constraint_score": 77.8, "excess_power_score": 16.9, "time_to_power_months": 201.6,
         "verdict": "AVOID", "composite_score": 10.1},
        {"market_slug": "rotterdam",
         "queue_wait_months": 107.1, "reserve_margin_pct": 11.0, "emergency_count_30d": 0,
         "constraint_score": 67.4, "excess_power_score": 22.5, "time_to_power_months": 107.1,
         "verdict": "AVOID", "composite_score": 14.0},
        {"market_slug": "dallas",
         "queue_wait_months": 84.5, "reserve_margin_pct": 19.5, "emergency_count_30d": 0,
         "constraint_score": 60.8, "excess_power_score": 65.8, "time_to_power_months": 67.6,
         "verdict": "CAUTION", "composite_score": 43.6},
        {"market_slug": "ashburn",
         "queue_wait_months": 40.9, "reserve_margin_pct": 20.5, "emergency_count_30d": 0,
         "constraint_score": 60.2, "excess_power_score": 45.5, "time_to_power_months": 24.5,
         "verdict": "AVOID", "composite_score": 27.1},
        {"market_slug": "chicago",
         "queue_wait_months": 36.0, "reserve_margin_pct": 20.0, "emergency_count_30d": 0,
         "constraint_score": 56.0, "excess_power_score": 44.2, "time_to_power_months": 21.6,
         "verdict": "AVOID", "composite_score": 27.7},
        {"market_slug": "singapore",
         "queue_wait_months": 36.0, "reserve_margin_pct": 12.0, "emergency_count_30d": 0,
         "constraint_score": 65.2, "excess_power_score": 13.5, "time_to_power_months": 36.0,
         "verdict": "AVOID", "composite_score": 13.5},
        {"market_slug": "midland-tx",
         "queue_wait_months": 16.0, "reserve_margin_pct": 28.0, "emergency_count_30d": 0,
         "constraint_score": 22.8, "excess_power_score": 85.7, "time_to_power_months": 9.6,
         "verdict": "BUILD", "composite_score": 83.0},
    ]


def _constraint_residuals():
    """Per-market (slug, published, derivable, residual), recomputed from the
    published fields alone."""
    from util import dcpi_method as dm
    out = []
    for row in _fixture_rows():
        derivable = dm.constraint_derivable_from_published_fields(
            queue_wait_months=row["queue_wait_months"],
            reserve_margin_pct=row["reserve_margin_pct"],
            emergency_count_30d=row["emergency_count_30d"])
        out.append((row["market_slug"], row["constraint_score"], derivable,
                    round(row["constraint_score"] - derivable, 2)))
    return out


def test_fixtures_really_lack_the_two_unpublished_constraint_inputs():
    """Anchors every assertion below. If these keys were present the residual
    arithmetic would be measuring nothing and the whole section would be
    vacuously green."""
    from util import dcpi_method as dm
    rows = _fixture_rows()
    assert len(rows) >= 8, "fixture sample collapsed — asserts would be weak"
    for row in rows:
        for field in dm.CONSTRAINT_INPUTS_NOT_PUBLISHED:
            assert field not in row, (
                f"{row['market_slug']} fixture carries {field}. If the scores "
                "endpoint now publishes it, that is the GOOD fix (option a) — "
                "recapture the fixtures and flip the reproducibility claim.")
        for field in dm.CONSTRAINT_INPUTS_PUBLISHED:
            assert field in row, f"{row['market_slug']} fixture lost {field}"


def test_every_weighted_constraint_input_is_declared_published_or_not():
    """The structural guard. Every input carrying weight must be classified,
    so a new weighted term cannot be added without someone deciding — and
    publishing — whether a reader can see it."""
    from util import dcpi_method as dm
    declared = set(dm.CONSTRAINT_INPUTS_PUBLISHED) | set(
        dm.CONSTRAINT_INPUTS_NOT_PUBLISHED)
    assert not (set(dm.CONSTRAINT_INPUTS_PUBLISHED)
                & set(dm.CONSTRAINT_INPUTS_NOT_PUBLISHED)), \
        "an input cannot be both published and unpublished"
    # Every scoring-time input, plus the local bonus term, must be accounted for.
    for name in list(dm.CONSTRAINT_INPUT_DEFAULTS) + ["local_dc_count"]:
        assert name in declared, (
            f"{name} carries constraint weight but is in neither "
            "CONSTRAINT_INPUTS_PUBLISHED nor CONSTRAINT_INPUTS_NOT_PUBLISHED")
    # The advertised worst-case gap must be DERIVED from the live weights, not
    # retyped: recompute it and compare.
    expected = round(100.0 * sum(
        dm.CONSTRAINT_UNPUBLISHED_WEIGHTS.values()), 1)
    assert dm.MAX_UNDERIVABLE_CONSTRAINT_POINTS == expected
    assert dm.CONSTRAINT_UNPUBLISHED_WEIGHTS["demand_growth_yoy_pct"] == \
        dm.CONSTRAINT_WEIGHTS["demand_growth"], "weight was hand-copied"
    assert dm.CONSTRAINT_UNPUBLISHED_WEIGHTS["local_dc_count"] == \
        dm.CONSTRAINT_LOCAL_COMPETITION_BONUS, "bonus weight was hand-copied"


def test_constraint_score_does_not_reproduce_from_published_fields():
    """THE measurement. Recompute constraint_score for every fixture market
    from the published fields and show the gap is real and bounded."""
    from util import dcpi_method as dm
    residuals = _constraint_residuals()
    unexplained = [r for r in residuals if abs(r[3]) > 0.05]
    assert unexplained, (
        "every fixture market now reproduces exactly. If the endpoint began "
        "publishing demand_growth_yoy_pct and local_dc_count, flip "
        "reproducibility_detail.scores.constraint_score.reproducible to True "
        "and update the claim — do not delete this test.")
    worst = max(abs(r[3]) for r in residuals)
    # Nothing may exceed the gap the methodology advertises.
    assert worst <= dm.MAX_UNDERIVABLE_CONSTRAINT_POINTS + 0.05, (
        f"a market is off by {worst}, more than the published "
        f"{dm.MAX_UNDERIVABLE_CONSTRAINT_POINTS}-point maximum — the "
        "disclosed gap understates the real one")
    # Johor sits on the theoretical maximum; that is why it is in the sample.
    johor = [r for r in residuals if r[0] == "johor"][0]
    assert abs(johor[3] - dm.MAX_UNDERIVABLE_CONSTRAINT_POINTS) < 0.05, \
        f"johor residual moved: {johor}"
    # The residual is one-directional: published >= derivable, because both
    # missing terms are ADDITIVE. A negative residual would mean a term this
    # module does not know about.
    for slug, pub, der, resid in residuals:
        assert resid >= -0.05, \
            f"{slug} published {pub} BELOW the derivable {der} — unknown term"


def test_the_published_claim_matches_the_measurement_in_both_directions():
    """Binds the prose to the arithmetic. A claim of reproducibility is only
    allowed when every fixture actually reproduces, and a claim of
    NON-reproducibility is only allowed when at least one does not."""
    from util import dcpi_method as dm
    block = dm.method_block()
    detail = block.get("reproducibility_detail") or {}
    scores = detail.get("scores") or {}
    assert scores, "reproducibility_detail.scores is missing"

    residuals = _constraint_residuals()
    reproduces = all(abs(r[3]) <= 0.05 for r in residuals)
    claimed = scores["constraint_score"]["reproducible"]
    assert claimed == reproduces, (
        f"published claim says constraint_score reproducible={claimed}, but "
        f"recomputing the fixtures says {reproduces}. Residuals: {residuals}")

    # The specific sentence that was false must not come back in any form.
    prose = block["reproducibility"]
    assert "constraint_score and composite_score are reproducible" not in prose, \
        "the false claim has been reintroduced verbatim"
    # Whatever the wording, constraint_score must not be listed as reproducible
    # while the measurement disagrees.
    if not reproduces:
        head = prose.split("constraint_score is NOT")[0]
        assert "constraint_score" not in head, (
            "the claim names constraint_score as reproducible before "
            "disclosing that it is not: " + head)
        assert str(dm.MAX_UNDERIVABLE_CONSTRAINT_POINTS) in prose, \
            "the claim does not quantify the gap"
        for field in dm.CONSTRAINT_INPUTS_NOT_PUBLISHED:
            assert field in prose, f"the claim does not name {field}"


def test_the_true_half_of_the_claim_stays_true():
    """composite_score and verdict DO reproduce exactly — that half was
    correct and a correction must not throw it away."""
    from util import dcpi_method as dm
    block = dm.method_block()
    scores = block["reproducibility_detail"]["scores"]
    for row in _fixture_rows():
        got = dm.composite_from_published_fields(
            row["excess_power_score"], row["constraint_score"],
            row["time_to_power_months"], row["verdict"])
        assert got == row["composite_score"], (
            f"{row['market_slug']}: composite recomputed {got}, published "
            f"{row['composite_score']}")
        v = dm.verdict_from_scores(row["constraint_score"],
                                   row["excess_power_score"])
        assert v == row["verdict"], \
            f"{row['market_slug']}: verdict recomputed {v}, published {row['verdict']}"
    assert scores["composite_score"]["reproducible"] is True
    assert scores["verdict"]["reproducible"] is True
    assert "composite_score" in block["reproducibility"]


def test_queue_wait_ceiling_is_scoped_to_the_path_it_actually_bounds():
    """89.1 was published as `queue_wait_true_ceiling_months`, i.e. as the
    ceiling of the PUBLISHED FIELD. 6 of 315 markets exceed it. The transform
    is correct — the highest proxy-path market measured 87.6 — so the CEILING
    was the wrong thing, and it is now scoped to the proxy path."""
    from util import dcpi_method as dm
    sat = dm.method_block()["local_saturation"]
    assert "queue_wait_true_ceiling_months" not in sat, \
        "the field-wide 'true ceiling' claim is back; 6 of 315 markets break it"
    assert sat["queue_wait_proxy_path_ceiling_months"] == 89.1
    assert "PROXY" in sat["queue_wait_ceiling_note"]

    paths = sat["queue_wait_fill_paths"]
    assert len(paths) == 3, "queue_wait_months has three fill paths"
    # Exactly one path is clipped to 66 first — that is the only one 89.1 bounds.
    clipped = [p for p in paths if p["clipped_to_66_first"]]
    assert len(clipped) == 1 and clipped[0]["ceiling_months"] == 89.1
    # The unclipped constant paths must NOT advertise a ceiling: their maxima
    # live in routes/dcpi.py and retyping them here is the hand-copy bug.
    for p in paths:
        if not p["clipped_to_66_first"]:
            assert p["ceiling_months"] is None, (
                f"{p['path']} restates a bound that lives in routes/dcpi.py")

    # Real markets that exceed the proxy ceiling must not be treated as
    # impossible by anything reading this document.
    over = [r for r in _fixture_rows()
            if r["queue_wait_months"] > dm.QUEUE_WAIT_PROXY_PATH_CEILING_MONTHS]
    assert len(over) >= 2, "fixtures lost the ceiling-breaching markets"


def test_index_size_limitation_is_generated_not_hardcoded():
    """known_limitations' index-size entry was the literal '311 markets /
    317 rows'. Live on 2026-08-08 it was 315 / 322 — both numbers drifted."""
    from util import dcpi_method as dm
    src = _read("util/dcpi_method.py")
    # The stale pair must not survive anywhere in the module, including in a
    # replacement literal.
    assert "publishes 311 markets" not in src
    assert "carries 317 rows" not in src

    # Injected counts must actually drive the sentence.
    lim = dm.known_limitations(
        {"index_size": 999, "table_rows": 1000})[-1]
    assert "999 markets" in lim and "1000 rows" in lim, lim
    assert "1 retired alias twin" in lim, \
        f"the difference is not derived from the counts: {lim}"
    # A different pair must produce a different sentence — proves it is a
    # function of its input, not a constant that happens to contain digits.
    other = dm.known_limitations({"index_size": 42, "table_rows": 50})[-1]
    assert other != lim and "42 markets" in other and "8 retired" in other

    # Absent/garbage counts degrade to "unmeasured", never to a stale number
    # and never to a fabricated zero.
    for bad in (None, {}, {"index_size": 0, "table_rows": 0},
                {"index_size": 315}, {"index_size": 400, "table_rows": 10}):
        degraded = dm.known_limitations(bad)[-1]
        assert "UNMEASURED" in degraded, f"{bad} -> {degraded}"
        assert "311" not in degraded and "317" not in degraded

    # method_block must thread the counts through rather than dropping them.
    threaded = dm.method_block(
        {"index_size": 777, "table_rows": 800})["known_limitations"][-1]
    assert "777 markets" in threaded and "800 rows" in threaded


def test_queue_wait_breach_count_is_never_a_frozen_literal():
    """r-repro-2 (2026-08-08). The first cut of this change shipped the breach
    count as the literal "6 of 315 markets on 2026-08-08, up to 144.0". The
    06:53 recompute moved edinburgh 90.9 -> 87.3 and the true count became 5
    — stale within hours, and disagreeing with the live figure carried
    elsewhere in the SAME document.

    Only the three slug_override markets are structurally above the ceiling;
    the per-ISO-default markets straddle it and cross on every recompute. A
    count over a moving set cannot be a literal."""
    from util import dcpi_method as dm
    src = _read("util/dcpi_method.py")
    assert "6 of 315" not in src, "the frozen breach count is back"

    lim = [x for x in dm.known_limitations(
        {"queue_wait_max": 144.0, "queue_wait_over_proxy_ceiling": 6,
         "index_size": 315}) if "queue-depth PROXY" in x]
    assert len(lim) == 1, "the queue-wait ceiling limitation went missing"
    assert "6 markets of 315" in lim[0] and "144.0" in lim[0], lim[0]

    # A different live reading must produce a different sentence — proving it
    # is a function of its input, which the literal was not.
    other = [x for x in dm.known_limitations(
        {"queue_wait_max": 96.0, "queue_wait_over_proxy_ceiling": 5,
         "index_size": 315}) if "queue-depth PROXY" in x][0]
    assert other != lim[0] and "5 markets" in other and "96.0" in other
    # Singular reads as singular, not "1 markets".
    one = [x for x in dm.known_limitations(
        {"queue_wait_max": 144.0, "queue_wait_over_proxy_ceiling": 1,
         "index_size": 315}) if "queue-depth PROXY" in x][0]
    assert "1 market of 315" in one, one

    # Unmeasured degrades to a pointer at the per-request field, never a count.
    for bad in (None, {}, {"queue_wait_over_proxy_ceiling": 6},
                {"queue_wait_max": 144.0}):
        deg = [x for x in dm.known_limitations(bad)
               if "queue-depth PROXY" in x][0]
        assert "UNMEASURED" in deg, f"{bad} -> {deg}"
        assert "queue_wait_markets_over_proxy_ceiling" in deg

    # The forensic fallback entry may name the STRUCTURAL markets, but must
    # not freeze a count over the markets that move.
    fb = [f for f in dm.FALLBACKS
          if f["id"] == "queue_wait_constant_paths_are_unclipped"][0]
    assert "queue_wait_markets_over_proxy_ceiling" in fb["effect"], \
        "the fallback does not point at the per-request measurement"
    import re
    assert not re.search(r"\b\d+ of \d+ published markets", fb["effect"]), \
        "the fallback froze a count over a set that moves every recompute"


def test_no_frozen_market_count_anywhere_in_the_published_document():
    """r-repro-3 (2026-08-08). The index size was hardcoded in FOUR places in
    this module and had drifted in every one: the index-size limitation, the
    queue-wait breach count, the reproducibility prose ("0 of 315 markets"),
    and the per-score markets_exact/markets_tested pairs. r-universe-dedup
    rescored the index the same day and moved the residual mean 10.74 -> 10.55,
    which is what a frozen aggregate over a recomputed set always does.

    Structural statements ("no market", "every published market") are safe
    because they follow from the schema. A COUNT is not, so the document must
    not contain one that was typed rather than measured."""
    import json
    import re
    from util import dcpi_method as dm
    block = dm.method_block()          # NO live counts injected
    blob = json.dumps(block)  # noqa: F841 (kept for the mean check below)

    # revisions[] is EXEMPT, deliberately and by the module's own stated
    # policy: a revision is a dated record of what was measured on a date, and
    # freezing that number is the whole point — "the measured values as of
    # that date are recorded once, in REVISIONS, where a dated observation
    # belongs". Every OTHER key describes the CURRENT document, so a count
    # there is a standing claim and must be measured or absent.
    current = {k: v for k, v in block.items() if k != "revisions"}
    blob_current = json.dumps(current)
    assert '"revisions"' in json.dumps(block), "revisions key renamed"
    for pat in (r"\b315\b", r"\b311\b", r"\b317\b", r"\b322\b"):
        hits = re.findall(pat, blob_current)
        assert not hits, (
            f"{pat} appears outside revisions[] with no live counts injected — "
            "a frozen index/row count is back")
    # The floating aggregate must not be published at all.
    assert "10.74" not in blob and "10.55" not in blob, \
        "a residual MEAN is published again; it moves on every recompute"
    detail = block["reproducibility_detail"]
    for name, sc in detail["scores"].items():
        assert "markets_exact" not in sc and "markets_tested" not in sc, \
            f"{name} republishes a frozen market count"
    # Structural wording survives, so the claim still says something.
    assert "no market" in block["reproducibility"]
    assert detail["scores"]["constraint_score"]["exact_on"].startswith("no market")
    assert detail["index_size_at_this_response"] is None, \
        "an uninjected index size must be null, never a remembered number"

    # Injected, the count appears and tracks the input.
    live = dm.method_block({"index_size": 315, "table_rows": 322,
                            "queue_wait_max": 144.0,
                            "queue_wait_over_proxy_ceiling": 5})
    assert "315 as of this response" in live["reproducibility"]
    assert live["reproducibility_detail"]["index_size_at_this_response"] == 315
    other = dm.method_block({"index_size": 400, "table_rows": 410,
                             "queue_wait_max": 90.0,
                             "queue_wait_over_proxy_ceiling": 1})
    assert "400 as of this response" in other["reproducibility"]
    assert "315" not in other["reproducibility"]

    # The residual snapshot must be dated and must say it moves.
    snap = detail["scores"]["constraint_score"]["observed_residual_points"]
    assert snap.get("as_of"), "the residual snapshot is undated"
    assert "move" in snap.get("note", ""), \
        "the snapshot does not disclose that residuals move on recompute"
    # The CAP stays a standing property, derived from the weights.
    assert detail["scores"]["constraint_score"]["max_underivable_points"] == \
        dm.MAX_UNDERIVABLE_CONSTRAINT_POINTS == snap["max"]


def test_methodology_endpoint_measures_counts_and_never_fabricates_them():
    """The live figures are measured in the route, because a module with no DB
    access cannot describe the live index — which is how the literals went
    stale in the first place."""
    src = _read("routes/dcpi_methodology.py")
    assert "def _live_counts" in src
    assert "FROM market_power_scores" in src
    # The breach count must be parameterised off the constant, not inlined —
    # an inlined 89.1 is a second copy that can drift from the first.
    assert "89.1" not in src, \
        "the ceiling is hand-copied into the SQL instead of bound as a param"
    assert "QUEUE_WAIT_PROXY_PATH_CEILING_MONTHS" in src
    # Failure must degrade the document, not the response.
    assert '"available": False' in src
    fn = _func_source("routes/dcpi_methodology.py", "_live_counts")
    assert "except Exception" in fn, "_live_counts can raise into the route"
    assert "counts.get(\"available\")" in src, \
        "unmeasured counts are passed to method_block as if measured"
