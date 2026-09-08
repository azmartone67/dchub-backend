"""_ap_resolve_state — the state a US coordinate is actually in.

The air-permitting score uses `state` to pick the regulatory context (which
agency, which NNSR/PSD thresholds) and to weight 5% of the composite, and the
Land & Power map renders that context verbatim. Resolving it wrongly does not
look like an error — it looks like a confident answer about the wrong
jurisdiction.

It resolved through _AP_STATE_BOXES, which cannot be right for an irregular
state: Ashburn VA is inside BOTH Virginia's and Maryland's box, Maryland's is
smaller, so smallest-bbox-wins returned MD for the densest data-centre market
on earth. util/state_polygons.py already existed for exactly this — its
docstring names this coordinate — and _ap_in_us_coverage already used it, so
coverage and context disagreed about the same point.

The polygons are offline and committed (data/geo/us_state_boundaries.json.gz),
so these are REAL end-to-end resolutions, not stubs.
"""
import ast
import pathlib

_MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"


def _load(names, extra=None):
    """Execute the named top-level functions from main.py in a bare namespace.

    main.py is never imported — it opens pools and registers ~200 blueprints.
    """
    tree = ast.parse(_MAIN.read_text(encoding="utf-8"))
    want = dict.fromkeys(names)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want:
            want[node.name] = node
    missing = [n for n, v in want.items() if v is None]
    assert not missing, f"not found in main.py: {missing} — renamed or moved"
    ns = dict(extra or {})
    for n in names:
        exec(compile(ast.Module(body=[want[n]], type_ignores=[]), "<main>", "exec"), ns)
    return ns


def _resolver(**extra):
    return _load(["_ap_resolve_state"], extra)["_ap_resolve_state"]


# real boxes: Ashburn is inside BOTH, and MD's is the smaller one
_BOXES = {"VA": ((36.5, -83.7), (39.5, -75.2)), "MD": ((37.9, -79.5), (39.7, -75.0))}
_IN_BOUNDS = lambda la, lo, b: b[0][0] <= la <= b[1][0] and b[0][1] <= lo <= b[1][1]


# ── the real path: Census polygons ──────────────────────────────────────────
def test_ashburn_resolves_to_virginia():
    # The whole point. Before this, the score said MD and the map printed
    # "MD context: MDE — Baltimore-DC ozone …" over a Virginia parcel.
    assert _resolver()(39.0438, -77.4874) == "VA"


def test_the_documented_border_and_lake_cases():
    r = _resolver()
    assert r(39.10, -77.55) == "VA"      # Leesburg — was MD
    assert r(43.04, -87.91) == "WI"      # Milwaukee — the MI/Lake Michigan case
    assert r(39.29, -76.61) == "MD"      # Baltimore is genuinely MD
    assert r(38.90, -77.04) == "DC"


def test_outside_the_us_is_none_not_a_guessed_state():
    # ★ '' from the polygons is a REAL answer (offshore, Great Lakes, abroad),
    # not a miss. Falling through to the boxes here is how Toronto became 'NY'.
    r = _resolver(_AP_STATE_BOXES=_BOXES, _ap_in_bounds=_IN_BOUNDS)
    assert r(43.6532, -79.3832) is None, "Toronto must not be given a US state"
    assert r(0.0, 0.0) is None


# ── the fallback, and why it is only a fallback ─────────────────────────────
def _broken_polygons():
    """Namespace whose state_polygons import raises — geometry unavailable."""
    import builtins
    real = builtins.__import__

    def _imp(name, *a, **k):
        if name == "util.state_polygons":
            raise ImportError("simulated geometry failure")
        return real(name, *a, **k)
    return {"_AP_STATE_BOXES": _BOXES, "_ap_in_bounds": _IN_BOUNDS,
            "__builtins__": {**vars(builtins), "__import__": _imp}}


def test_geometry_failure_falls_back_to_boxes_rather_than_blanking_the_country():
    # Wrong-but-present beats reporting the entire US as unplaceable — that is
    # the failure direction _ap_in_us_coverage's comment calls the worse one.
    assert _resolver(**_broken_polygons())(39.0438, -77.4874) == "MD"


def test_the_fallback_answer_is_pinned_as_wrong_on_purpose():
    # Locking in WHY the boxes are only a fallback. If this ever returns VA the
    # boxes changed and this file needs re-reading rather than quietly passing.
    r = _resolver(**_broken_polygons())
    assert r(39.0438, -77.4874) != "VA"


def test_fallback_still_returns_none_off_the_map():
    assert _resolver(**_broken_polygons())(0.0, 0.0) is None


# ── the published basis ─────────────────────────────────────────────────────
def test_basis_says_polygon_when_the_geometry_loaded():
    assert _load(["_ap_state_basis"])["_ap_state_basis"](39.04, -77.48) == "census_polygon"


def test_basis_says_fallback_when_it_did_not():
    ns = _load(["_ap_state_basis"], _broken_polygons())
    assert ns["_ap_state_basis"](39.04, -77.48) == "state_bbox_fallback"


def test_resolver_returns_a_bare_state_not_a_tuple():
    # _ap_state_supported() and _ap_in_us_coverage() both do `... is not None`.
    got = _resolver()(39.0438, -77.4874)
    assert isinstance(got, str)
