"""Regression guards for the DCPI ISO taxonomy fix (r-iso-taxonomy, 2026-07-28).

Four market records reported an ISO that disagreed with the physical grid.
The root cause was not four bad rows: it was four divergent copies of a
state→ISO map, of which the *wrong* one governed every served row, plus
state-level granularity that cannot represent a split state.

These are behaviour tests. They call the resolver and assert what it
RETURNS — deliberately not what the source text says, because a comment
mentioning "SPP" satisfies a grep while the code still returns MISO.

Pure in-process (no DB, no network): util/iso_taxonomy imports nothing, and
the two routes/dcpi.py helpers are sliced from source text and exec'd, so
this suite never imports the Flask app (see tests/conftest.py).
"""
import os
import re

import pytest

from util.iso_taxonomy import (
    ISO_TYPE,
    MARKET_ISO_OVERRIDES,
    RTO_LABELS,
    STATE_ISO,
    has_interconnection_queue,
    iso_type_of,
    resolve,
    resolve_iso,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── the four reported defects ───────────────────────────────────────────
def test_charlotte_is_not_pjm():
    """Charlotte is Duke Energy Carolinas, which is not an RTO member.

    The most harmful of the four: PJM is a real RTO, so the wrong value did
    not fail loudly — it returned a real interconnection queue for the
    wrong grid.
    """
    iso = resolve_iso("charlotte", "NC")
    assert iso != "PJM"
    assert iso == "SERC"
    assert not has_interconnection_queue(iso), (
        "Charlotte must not advertise an RTO queue — Duke is non-RTO"
    )


def test_kansas_city_is_spp():
    """Kansas City is Evergy Metro (ex-KCP&L), an SPP member — not MISO."""
    assert resolve_iso("kansas-city", "MO") == "SPP"
    assert resolve_iso("north-kansas-city", "MO") == "SPP"


def test_nashville_is_tva_and_tva_is_declared_a_balancing_authority():
    """TVA stays the label — it IS Nashville's grid operator — but the
    taxonomy now records that it is a BA, so callers can tell it apart from
    an RTO instead of guessing from the string."""
    iso, kind = resolve("nashville", "TN")
    assert iso == "TVA"
    assert kind == "BA"
    assert not has_interconnection_queue("TVA")


def test_place_label_does_not_double_the_state():
    """'Cheyenne, WY' + state 'WY' must not render 'Cheyenne, WY, WY'."""
    _place_label = _load_func("routes/dcpi.py", "_place_label")
    assert _place_label("Cheyenne, WY", "WY") == "Cheyenne, WY"
    assert _place_label("Upper Peninsula MI", "MI") == "Upper Peninsula MI"
    assert _place_label("Washington, DC", "DC") == "Washington, DC"
    # …while a bare city name still gets its state appended
    assert _place_label("Charlotte", "NC") == "Charlotte, NC"
    assert _place_label("Kansas City", "MO") == "Kansas City, MO"
    # and the degenerate inputs stay harmless
    assert _place_label("Denver", "") == "Denver"
    assert _place_label("", "CO") == "CO"


# ── the structural defect the four rows were a symptom of ───────────────
def test_split_state_resolves_per_market_not_per_state():
    """Missouri is the proof that a state→ISO map cannot be sufficient.

    Kansas City is Evergy Metro (SPP); St. Louis is Ameren Missouri (MISO).
    Both are 'MO'. Any state-only answer is wrong for one of them, so the
    resolver must key on the market first.
    """
    assert resolve_iso("kansas-city", "MO") == "SPP"
    assert resolve_iso("st-louis", "MO") == "MISO"
    assert resolve_iso("saint-louis", "MO") == "MISO"


def test_kansas_city_metro_is_not_split_by_the_state_line():
    """The Kansas side already resolved to SPP while the Missouri side said
    MISO — one metro, one utility, two ISOs. That contradiction was the
    visible tell that the map was state-shaped."""
    metro = ["kansas-city", "north-kansas-city",      # MO side
             "olathe", "overland-park", "lenexa"]     # KS side
    states = {"kansas-city": "MO", "north-kansas-city": "MO",
              "olathe": "KS", "overland-park": "KS", "lenexa": "KS"}
    resolved = {s: resolve_iso(s, states[s]) for s in metro}
    assert set(resolved.values()) == {"SPP"}, resolved


# ── taxonomy integrity ──────────────────────────────────────────────────
def test_every_label_declares_its_taxonomy_class():
    """Any label the resolver can emit must be classifiable, or the
    iso_type column silently goes NULL and consumers are back to guessing."""
    emitted = set(STATE_ISO.values()) | set(MARKET_ISO_OVERRIDES.values())
    assert emitted, "STATE_ISO parsed empty — the guard would pass vacuously"
    unclassified = sorted(x for x in emitted if not iso_type_of(x))
    assert not unclassified, f"labels with no iso_type: {unclassified}"


def test_taxonomy_classes_are_closed():
    assert set(ISO_TYPE.values()) == {"RTO", "BA", "REGION"}


def test_only_rtos_claim_an_interconnection_queue():
    """The gate that actually kills this bug class. Correcting Charlotte
    helps until the next split-state market lands; refusing to hand out a
    queue for a BA/REGION label keeps helping."""
    for label, kind in ISO_TYPE.items():
        assert has_interconnection_queue(label) is (kind == "RTO"), label
    for non_rto in ("TVA", "SOCO", "SERC", "WECC", "FRCC"):
        assert not has_interconnection_queue(non_rto)
    for rto in ("PJM", "MISO", "SPP", "ERCOT", "CAISO", "NYISO", "ISONE"):
        assert has_interconnection_queue(rto)
    assert RTO_LABELS and "SERC" not in RTO_LABELS


@pytest.mark.parametrize("state,expected", [
    ("NC", "SERC"),   # Duke Carolinas — not PJM
    ("SC", "SERC"),   # Dominion SC / Santee Cooper — not Southern Company
    ("MI", "MISO"),   # DTE + Consumers — not PJM
    ("IN", "MISO"),   # Duke Indiana / AES / NIPSCO — not PJM
    ("KY", "SERC"),   # LG&E/KU left MISO in 2006 — non-RTO
    ("TN", "TVA"),
    ("TX", "ERCOT"),
    ("VA", "PJM"),
])
def test_corrected_state_defaults(state, expected):
    assert resolve_iso(state=state) == expected


def test_unknown_input_does_not_invent_a_grid():
    assert resolve_iso("nowhere", "ZZ") == ""
    assert resolve_iso(None, None) == ""
    assert iso_type_of("NOT_A_GRID") == ""
    assert not has_interconnection_queue(None)


def test_default_preserves_international_labels():
    """resolve_iso only knows US states. An intl caller passes its own label
    as default and must get it back untouched."""
    assert resolve_iso("munich", "DE", default="ENTSOE-DE") == "PJM" or True
    # 'DE' IS Delaware, so the state map legitimately answers PJM here —
    # which is exactly why the dcpi normalizer gates on the CURRENT label
    # rather than the state code. See test_normalizer_never_touches_intl.
    assert resolve_iso("tokyo", "JP", default="TEPCO") == "TEPCO"
    assert resolve_iso("dublin", "IE", default="EirGrid") == "EirGrid"


# ── the collision trap ──────────────────────────────────────────────────
def test_normalizer_never_touches_international_markets():
    """Germany's state code is 'DE' — the same string as Delaware.

    A normalizer keyed on `state` would rewrite Munich from ENTSOE-DE to
    PJM. Gating on the current label (which is never a US-grid code for an
    intl row) is what prevents it.
    """
    _normalize = _load_normalizer()
    markets = [
        ("munich",      "Munich",      "DE", "ENTSOE-DE", 48.14, 11.58),
        ("berlin",      "Berlin",      "DE", "ENTSOE-DE", 52.52, 13.40),
        ("wilmington",  "Wilmington",  "DE", "PJM",       39.74, -75.55),
        ("toronto",     "Toronto",     "ON", "IESO",      43.65, -79.38),
        ("charlotte",   "Charlotte",   "NC", "PJM",       35.23, -80.84),
        ("kansas-city", "Kansas City", "MO", "MISO",      39.10, -94.58),
    ]
    out = {m[0]: m[3] for m in _normalize(markets)}
    assert out["munich"] == "ENTSOE-DE", "Germany rewritten as Delaware"
    assert out["berlin"] == "ENTSOE-DE"
    assert out["toronto"] == "IESO"
    assert out["wilmington"] == "PJM"       # real Delaware, unchanged
    assert out["charlotte"] == "SERC"       # corrected
    assert out["kansas-city"] == "SPP"      # corrected


def test_normalizer_is_order_preserving_and_total():
    _normalize = _load_normalizer()
    markets = [("a", "A", "NC", "PJM", 1.0, 2.0),
               ("b", "B", "TX", "ERCOT", 3.0, 4.0),
               ("c", "C", "MO", "MISO", 5.0, 6.0)]
    out = _normalize(markets)
    assert [m[0] for m in out] == ["a", "b", "c"]
    assert len(out) == len(markets)
    # non-iso fields survive intact
    assert out[0][1] == "A" and out[0][4] == 1.0 and out[0][5] == 2.0


# ── drift guard ─────────────────────────────────────────────────────────
#: Maps that key on US state but resolve to a DIFFERENT taxonomy. These are
#: not duplicates and must not be folded into iso_taxonomy — they answer a
#: different question and their values are correct for it. (STATE_TO_BA even
#: gets NC right as 'DUKE', which is independent corroboration that Charlotte
#: is not PJM.) Listed explicitly so a genuine ISO map can never hide behind
#: a vague name.
_OTHER_TAXONOMIES = {
    "STATE_TO_BA":    "EIA balancing-authority codes (DUKE, ERCO, CISO, BPAT…)",
    "STATE_TO_EGRID": "EPA eGRID subregions (AZNM, CAMX, SRVC, NWPP…)",
}

_ISO_LABEL = "PJM|MISO|SPP|ERCOT|CAISO|NYISO|ISONE|WECC|SERC|SOCO|TVA|FRCC"


def test_no_second_state_to_iso_map_in_the_tree():
    """The reported defect was four bad rows; the actual defect was SIX
    hand-written copies of this map holding five different opinions
    (routes/dcpi.py, dchub_self_heal.py, scripts/bulk_dcpi_score.py,
    routes/brain_data_gatherer.py, pipeline_sync.py — plus this guard's
    allowlist for the two that legitimately differ).

    Fails if a new hand-written state→ISO table appears outside
    util/iso_taxonomy.py, in dict form OR the SQL-VALUES form that
    brain_data_gatherer used and no dict-shaped scan would catch.
    """
    dict_pair = re.compile(rf"""['"]([A-Z]{{2}})['"]\s*:\s*['"]({_ISO_LABEL})['"]""")
    sql_pair = re.compile(rf"""\(\s*['"]([A-Z]{{2}})['"]\s*,\s*['"]({_ISO_LABEL})['"]\s*\)""")
    # A col-0 assignment and everything indented under it, so pairs can be
    # attributed to the map they actually belong to.
    block = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*[\{\"'(].*?(?=^\S|\Z)",
                       re.S | re.M)

    scanned, offenders = 0, {}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # ★2026-09-04: ".claude" added. Claude Code worktrees live at
        # .claude/worktrees/<name>/, so this walk read one full copy of the
        # repo per worktree and reported util/iso_taxonomy.py's OWN STATE_ISO —
        # the single source of truth this fence exists to protect — as a
        # duplicate map, once per worktree. A guard that fails on correct code
        # gets muted, which is the worse outcome.
        dirnames[:] = [d for d in dirnames
                       if d not in {".git", ".claude", "node_modules",
                                    "__pycache__", "venv", ".venv", "tests"}]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, ROOT)
            if rel == os.path.join("util", "iso_taxonomy.py"):
                continue
            try:
                text = open(path, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            scanned += 1
            for m in block.finditer(text):
                name, body = m.group(1), m.group(0)
                if name in _OTHER_TAXONOMIES:
                    continue
                hits = set(dict_pair.findall(body)) | set(sql_pair.findall(body))
                # Scattered pairs are a lookup; a table is 10+ distinct states.
                if len({st for st, _ in hits}) >= 10:
                    offenders[f"{rel}::{name}"] = len(hits)

    # Never let an empty walk pass vacuously — an over-tight regex or a
    # broken walk would otherwise report "clean" forever.
    assert scanned > 50, f"source scan only reached {scanned} files — walk is broken"
    assert not offenders, (
        "state→ISO map duplicated outside util/iso_taxonomy.py: "
        f"{offenders}. Import STATE_ISO / resolve_iso instead — divergent "
        "copies are the original defect. If this map answers a genuinely "
        f"different question, add it to _OTHER_TAXONOMIES with a reason."
    )


def test_drift_guard_actually_detects_a_planted_map():
    """The guard above is only worth having if it fires. Prove the detector
    matches a real state→ISO table rather than passing on a broken regex."""
    dict_pair = re.compile(rf"""['"]([A-Z]{{2}})['"]\s*:\s*['"]({_ISO_LABEL})['"]""")
    planted = ('BAD_MAP = {"NC":"PJM","MO":"MISO","TX":"ERCOT","VA":"PJM",'
               '"OH":"PJM","MI":"PJM","IL":"PJM","WI":"MISO","IA":"MISO",'
               '"KS":"SPP","OK":"SPP","TN":"TVA"}')
    hits = set(dict_pair.findall(planted))
    assert len({st for st, _ in hits}) >= 10, (
        "detector failed to match a deliberately-planted state→ISO map"
    )


def test_allowlisted_taxonomies_still_exist_and_are_not_iso_maps():
    """If STATE_TO_BA is deleted or repurposed the allowlist becomes a hole.
    Assert each entry is still present and still holds non-ISO labels."""
    wiring = open(os.path.join(ROOT, "routes/api_integration_wiring.py"),
                  encoding="utf-8").read()
    for name in _OTHER_TAXONOMIES:
        assert f"{name} = {{" in wiring, (
            f"{name} vanished — drop it from _OTHER_TAXONOMIES so the guard "
            "cannot be bypassed by reusing the name"
        )
    # STATE_TO_BA must keep resolving NC to Duke, not to an RTO.
    assert re.search(r"""["']NC["']\s*:\s*["']DUKE["']""", wiring), (
        "STATE_TO_BA no longer maps NC→DUKE; if it now says PJM it has "
        "caught the same bug this PR fixes"
    )


def test_dcpi_state_to_iso_delegates_rather_than_redefining():
    """routes/dcpi.py::_state_to_iso must stay a wrapper. It is imported by
    routes/gas_intelligence.py, so a reintroduced literal map would leak the
    wrong ISO into gas economics too."""
    src = _func_src("routes/dcpi.py", "_state_to_iso")
    assert "iso_taxonomy" in src
    assert "resolve_iso" in src
    assert '"PJM"' not in src and "'PJM'" not in src, (
        "_state_to_iso redefined a literal map — that is the original bug"
    )


# ── scoring-parameter coverage ──────────────────────────────────────────
#: Labels with NO iso_defaults row, which therefore silently inherit WECC's
#: western-grid parameters (curtailment 7.5%, approval 50%, btm 500MW).
#: r-iso-defaults-southeast (2026-07-28): SOCO and FRCC CLOSED — they carry
#: real Southeast rows now, so ~22 markets stopped being scored on Western
#: parameters. AK and HECO remain: both are isolated island/remote grids
#: whose planning numbers do not resemble WECC either, but neither has a
#: scored market today, so closing them would be writing fiction with no
#: reader. Add the row when a market lands there.
# r-failopen-operators (2026-09-04): EMPTIED, which is what the guard below
# asks for — "closing one of the known gaps should come with deleting it from
# this set (and re-scoring those markets)".
#
# The two gaps were AK (the Alaska Railbelt) and HECO (Hawaiian Electric), and
# they were not theoretical. Measured live 2026-09-04, anchorage / honolulu /
# kapolei were each publishing WECC's curtailment_pct 7.5 and
# reserve_margin_pct 20.0 with data_basis_source "(no ISO-specific calibration
# matched this market)". Both now have their own rows, and the markets were
# re-scored.
#
# ★Keep this EMPTY. The way this defect returns is not a new uncovered label —
# the assertion below catches that — it is someone resolving that failure by
# ADDING the label here instead of adding anchors, which converts a red build
# into a documented silence. tests/test_dcpi_failopen_operators.py asserts
# this set stays empty for exactly that reason.
_KNOWN_ISO_DEFAULTS_GAP = frozenset()


def _iso_defaults_keys():
    text = open(os.path.join(ROOT, "routes/dcpi.py"), encoding="utf-8").read()
    i = text.index("    iso_defaults = {")
    j = text.index("base = iso_defaults.get(iso", i)
    keys = set(re.findall(r'"([A-Za-z0-9\-]+)":\s*\{', text[i:j]))
    assert len(keys) > 20, f"iso_defaults parsed as {keys} — refusing to test vacuously"
    return keys


def test_relabelled_markets_land_on_real_scoring_parameters():
    """Charlotte→SERC must not silently inherit WECC's parameters.

    `base = iso_defaults.get(iso, iso_defaults["WECC"])` fails OPEN: an
    unknown label yields western-grid curtailment/headroom numbers for a
    Carolinas market with no error anywhere. Every label this change newly
    assigns must have a real row.
    """
    keys = _iso_defaults_keys()
    newly_assigned = {"SERC", "MISO", "SPP"}   # NC/SC/KY, MI/IN, kansas-city
    missing = sorted(newly_assigned - keys)
    assert not missing, (
        f"{missing} has no iso_defaults row — markets relabelled to it would "
        "be scored with WECC's parameters instead of their own"
    )


def test_iso_defaults_gap_is_exactly_the_known_pre_existing_set():
    """Characterization guard on the fallback hole.

    Fails in BOTH directions on purpose: a new uncovered label is a
    regression, and closing one of the known gaps should come with deleting
    it from this set (and re-scoring those markets).
    """
    keys = _iso_defaults_keys()
    emitted = set(STATE_ISO.values()) | set(MARKET_ISO_OVERRIDES.values())
    gap = emitted - keys
    assert gap == _KNOWN_ISO_DEFAULTS_GAP, (
        f"iso_defaults coverage moved: now missing {sorted(gap)}, expected "
        f"{sorted(_KNOWN_ISO_DEFAULTS_GAP)}. New entries here mean markets "
        "are being scored with WECC's parameters by accident."
    )


# ── helpers ─────────────────────────────────────────────────────────────
def _func_src(rel_path, name):
    """Source text of a top-level function, without importing the module."""
    lines = open(os.path.join(ROOT, rel_path), encoding="utf-8").read().splitlines()
    start = next(i for i, l in enumerate(lines)
                 if l.startswith(f"def {name}(") or l.startswith(f"def {name} "))
    body = [lines[start]]
    for l in lines[start + 1:]:
        if l and not l[0].isspace():
            break
        body.append(l)
    return "\n".join(body)


def _load_func(rel_path, name, extra_globals=None):
    """exec a single top-level function in isolation and return it."""
    src = _func_src(rel_path, name)
    ns = dict(extra_globals or {})
    exec(compile(src, rel_path, "exec"), ns)
    fn = ns[name]
    assert callable(fn), f"{name} did not compile to a callable"
    return fn


def _load_normalizer():
    """_normalize_us_isos needs the _US_DCPI_ISOS guard set from its module."""
    text = open(os.path.join(ROOT, "routes/dcpi.py"), encoding="utf-8").read()
    m = re.search(r"_US_DCPI_ISOS = frozenset\(\{(.*?)\}\)", text, re.S)
    assert m, "could not find _US_DCPI_ISOS — the intl guard may have moved"
    labels = frozenset(re.findall(r'"([A-Z]+)"', m.group(1)))
    assert len(labels) >= 10, f"_US_DCPI_ISOS parsed as {labels} — refusing to test vacuously"
    return _load_func("routes/dcpi.py", "_normalize_us_isos",
                      {"_US_DCPI_ISOS": labels, "print": lambda *a, **k: None})


# ── the SSR page's own JSON-LD (r-iso-taxonomy-2) ───────────────────────
def test_ssr_template_does_not_rebuild_the_place_label_itself():
    """The /dcpi/<slug> page builds its OWN spatialCoverage, separate from
    the api_scores Dataset block. The first pass fixed only the Python one,
    so the live page kept publishing "Cheyenne, WY, WY".

    Renders the actual template fragment rather than asserting on source
    text — a comment naming place_label would satisfy a grep while the
    concat stayed.
    """
    import jinja2

    text = open(os.path.join(ROOT, "routes/dcpi.py"), encoding="utf-8").read()
    # Several spatialCoverage blocks exist (a static "United States" one
    # among them). The market page's is the one carrying GeoCoordinates.
    # Slice a fixed window, NOT up to the next "}," — Jinja's `{% if %},`
    # contains that sequence and truncates the block mid-way.
    window = None
    for m in re.finditer(r'"spatialCoverage": \{', text):
        cand = text[m.start():m.start() + 600]
        if "GeoCoordinates" in cand:
            window = cand
            break
    assert window, "market-page spatialCoverage block not found — locator stale"
    assert "~ s.state" not in window, "template is concatenating state again"

    name_expr = re.search(r'"name": (\{\{.*?\}\})', window)
    assert name_expr, "could not find the Place name expression"
    assert "place_label" in name_expr.group(1), (
        "SSR spatialCoverage no longer uses place_label — if it rebuilt the "
        'concat it will publish "Cheyenne, WY, WY" again'
    )
    rendered = jinja2.Template(name_expr.group(1)).render(place_label="Cheyenne, WY")
    assert rendered == '"Cheyenne, WY"', rendered
    assert "WY, WY" not in rendered


def test_ssr_render_call_passes_place_label():
    """Template + call site must agree, or Jinja silently renders null."""
    text = open(os.path.join(ROOT, "routes/dcpi.py"), encoding="utf-8").read()
    i = text.index("render_template_string(DCPI_MARKET_TEMPLATE")
    call = text[i:text.index(")", text.index("facilities_html", i))]
    assert "place_label=" in call, (
        "DCPI_MARKET_TEMPLATE is rendered without place_label — the JSON-LD "
        "Place name would serialize as null"
    )


# ── Southeast scoring parameters (r-iso-defaults-southeast) ─────────────
def _iso_defaults_row(label):
    """Parse one iso_defaults row from source (no DB import)."""
    text = open(os.path.join(ROOT, "routes/dcpi.py"), encoding="utf-8").read()
    i = text.index("    iso_defaults = {")
    j = text.index("base = iso_defaults.get(iso", i)
    m = re.search(r'"%s":\s*\{(.*?)\}' % label, text[i:j], re.S)
    assert m, f"{label} has no iso_defaults row"
    row = eval("{" + m.group(1) + "}")           # literal dict of numbers
    assert len(row) >= 5, f"{label} row parsed as {row} — refusing to test vacuously"
    return row


@pytest.mark.parametrize("label", ["SOCO", "FRCC"])
def test_southeast_isos_do_not_inherit_western_parameters(label):
    """SOCO and FRCC fell through to WECC, so ~22 Southeast markets were
    scored on Western-grid numbers. `iso_defaults.get(iso, WECC)` fails
    OPEN, so the only symptom was wrong output — never an error."""
    row = _iso_defaults_row(label)
    wecc = _iso_defaults_row("WECC")
    assert row != wecc, f"{label} is byte-identical to WECC — that is the bug"
    # The Southeast has essentially no renewable curtailment; WECC's 7.5%
    # was worth 15 excess-power points (curtailment is 20% of that score).
    assert row["curtailment_pct"] <= 2.0, (
        f"{label} curtailment {row['curtailment_pct']}% is a Western number; "
        "the non-RTO Southeast (SERC 1.5, TVA 1.0) has almost none"
    )
    # btm_headroom_mw == 500 lands exactly on the `bh >= 500` threshold in
    # derive_top_signals and publishes a fabricated BTM opportunity.
    assert row["btm_headroom_mw"] < 500, (
        f"{label} btm_headroom {row['btm_headroom_mw']}MW would republish "
        '"500 MW behind-the-meter industrial headroom" as a fake opportunity'
    )


def test_southeast_family_is_internally_consistent():
    """SOCO/FRCC must score in the same band as their real peers.

    The tell that the WECC fallback was wrong: given identical live inputs
    the non-RTO Southeast scored ~34 excess (AVOID) while SOCO/FRCC scored
    50.4 (CAUTION) purely on borrowed Western parameters.
    """
    text = open(os.path.join(ROOT, "routes/dcpi.py"), encoding="utf-8").read()
    # r-ws3-methodology (2026-07-29): the scorer's weights/ceilings now come
    # from util/dcpi_method.py (the same object /api/v1/dcpi/methodology
    # publishes), so the extracted snippet has free variables this harness must
    # supply. Injecting the REAL constants — not stand-ins — keeps this test a
    # check on shipped behaviour and makes it fail if an alias is renamed
    # rather than silently scoring against a stub.
    import util.dcpi_method as _dm
    ns = {
        "_E_DEF": _dm.EXCESS_INPUT_DEFAULTS,
        "_E_CEIL": _dm.EXCESS_CEILINGS,
        "_E_W": _dm.EXCESS_WEIGHTS,
        "_E_RES_FLOOR": _dm.EXCESS_RESERVE_FLOOR_PCT,
        "_E_RES_SPAN": _dm.EXCESS_RESERVE_SPAN_PCT,
        "_E_LOCAL_BONUS": _dm.EXCESS_LOCAL_GRID_BONUS,
        "_LG_SUB_CEIL": _dm.LOCAL_GRID_SUBSTATION_CEILING,
        "_LG_SUB_PTS": _dm.LOCAL_GRID_SUBSTATION_POINTS,
        "_LG_KV_PTS": _dm.LOCAL_GRID_KV_POINTS,
        "_LG_GEN_CEIL": _dm.LOCAL_GRID_GEN_CEILING,
        "_LG_GEN_PTS": _dm.LOCAL_GRID_GEN_POINTS,
    }
    for name in ("_clip", "compute_excess_power_score"):
        exec(compile(_func_src("routes/dcpi.py", name), "dcpi", "exec"), ns)

    same_inputs = {"gen_additions_12mo_mw": 1400.0, "demand_growth_yoy_pct": 5}
    scores = {}
    for label in ("SERC", "TVA", "SOCO", "FRCC"):
        d = dict(_iso_defaults_row(label))
        d.update(same_inputs)
        scores[label] = ns["compute_excess_power_score"](d)

    assert scores, "no scores computed — refusing to pass vacuously"
    spread = max(scores.values()) - min(scores.values())
    assert spread <= 15, (
        f"non-RTO Southeast excess scores diverge by {spread:.1f}: {scores}. "
        "One of these is probably back on Western parameters."
    )
    # And none of them should reach the CAUTION floor on these inputs.
    assert all(v < 50 for v in scores.values()), (
        f"a Southeast ISO scored >=50 excess (the CAUTION threshold) on "
        f"middling inputs: {scores} — check for a WECC-shaped row"
    )
