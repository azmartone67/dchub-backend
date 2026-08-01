"""Guards for the 2026-08-01 honesty repair on three reVeal endpoints.

All three asserted things they had no evidence for. Same family as the
validation-feed bug (#2073): a confident-looking 200 covering an absence.

1. reveal-grid-export — returned `status:"ready"` plus a download_url on
   cdn.dchub.com from a hard-coded 15-state table stamped 2026-04-20 and
   annotated "assumed to have nightly pre-renders". cdn.dchub.com has DNS
   records but does not serve (TLS "unrecognized name"), and no pre-render job
   has ever existed. /status/<job_id> returned "ready" for ANY job id — an
   invented one was confirmed ready on the first try. Readiness is now derived
   from a HEAD against R2 and never asserted.

2. climate-risk — _zone_score() returned 0 for a point outside every modelled
   circle, and 0 rendered as "Minimal". Loudoun County VA, a core reVeal siting
   market and outside all 20 zones, reported composite 0 / "Minimal" on all
   three hazards: indistinguishable from a measured finding of no risk.
   Uncovered is now None -> "Unknown".

3. social-acceptance-index — the worst of the three. news_sentiment_score,
   litigation_count_12mo and community_opposition_signals were computed as

       seed = abs(math.sin(lat * 12.9898 + lon * 78.233)) % 1

   a hash of the coordinates, shaped around the composite. A partner
   regressing against a "count of lawsuits in the last 12 months" was
   consuming a trig function. Nulled, with the keys kept for wire
   compatibility exactly as announcement_date is on the validation feed.
   The composite also defaulted to 70 for uncovered points; now None.

Pure source/AST + executed-fragment asserts. No DB, no network, no flask
import, no `import main`. Nothing here runs at module scope.
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REVEAL = "reveal_endpoints.py"

# The host the old download links pointed at. It resolves but does not serve.
DEAD_CDN = "cdn.dchub.com"


def _read(rel=_REVEAL):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _tree():
    tree = ast.parse(_read())
    assert isinstance(tree, ast.Module) and len(tree.body) > 5, (
        f"{_REVEAL} parsed to a degenerate module — this harness is not looking "
        "at the real file")
    return tree


def _fn(name):
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{_REVEAL} no longer defines {name}()")


def _code_strings(node):
    """String constants in `node`, EXCLUDING its docstring.

    The docstrings and comments here deliberately quote the old broken
    behaviour in order to explain it. Scanning prose would let that
    explanation satisfy — or trip — these guards. Same lesson as the fenced-
    block scoping in tests/test_partner_landing_nlr_signatures.py.
    """
    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    assert body, f"{node.name} has no body beyond its docstring"
    out = []
    for stmt in body:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                out.append(n.value)
            elif isinstance(n, ast.JoinedStr):
                out.extend(v.value for v in n.values
                           if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return out


def _code_src(name):
    """A function's CODE as text — no comments, no docstring.

    ast.get_source_segment() keeps both, and every guard below documents the
    old broken behaviour verbatim (the coordinate-hash seed, the literal
    "ready"). Scanning raw source made two guards fire on their own
    explanation. ast.unparse() drops comments; the docstring is stripped
    explicitly. Third time this trap has come up in this file's tests — scan
    code, never prose.
    """
    fn = _fn(name)
    node = ast.FunctionDef(
        name=fn.name, args=fn.args, decorator_list=[], returns=None,
        type_comment=None, type_params=[],
        body=list(fn.body),
    )
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        node.body = node.body[1:]
    assert node.body, f"{name} has no body beyond its docstring"
    return ast.unparse(ast.fix_missing_locations(node))


def _exec_fragments(names, extra_nodes=()):
    """Execute selected top-level defs/assigns in isolation and return the ns.

    Keeps to the house rule that this suite never imports the web stack —
    reveal_endpoints pulls in flask at module scope, so fragments are lifted
    out and run against a bare namespace instead.
    """
    tree = _tree()
    wanted, consts = set(names), []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in wanted:
            consts.append(n)
        elif (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id in wanted):
            consts.append(n)
        elif isinstance(n, ast.Import) and any(a.name == "math" for a in n.names):
            consts.append(n)
    consts.extend(extra_nodes)
    got = {getattr(n, "name", None) or n.targets[0].id
           for n in consts if not isinstance(n, ast.Import)}
    missing = wanted - got
    assert not missing, f"fragments moved or renamed: {sorted(missing)}"
    ns = {}
    exec(compile(ast.Module(body=consts, type_ignores=[]), "<reveal:frag>", "exec"), ns)
    # Free variables must actually resolve, or every assert below is vacuous.
    for name in names:
        assert name in ns, f"{name} did not survive exec — free var unresolved"
    return ns


# ── 0. must-fail control ─────────────────────────────────────────────────────

def test_harness_reads_the_real_file():
    """A collection abort exits SILENT GREEN, so all-passing is not by itself
    evidence these ran (2026-07-28). Pin facts true only if the file loaded."""
    src = _read()
    assert "reveal-grid-export" in src and "climate-risk" in src
    ns = _exec_fragments(["_haversine_km"])
    # Known distance: Loudoun (39.04,-77.48) to Phoenix (33.45,-112.07) ~3100km
    d = ns["_haversine_km"](39.04, -77.48, 33.45, -112.07)
    assert 2900 < d < 3300, f"haversine returned {d} — fragment exec is broken"


# ── 1. grid-export may never claim readiness it has not verified ─────────────

def test_no_dead_cdn_host_in_any_payload():
    """Every response string across the module. The old download_url pointed
    at a host with DNS records and no service behind it."""
    for fname in ("reveal_grid_export", "reveal_grid_export_status"):
        for s in _code_strings(_fn(fname)):
            assert DEAD_CDN not in s or "does not serve" in s, (
                f"{fname} references {DEAD_CDN} in a payload string: {s[:120]!r}")


def test_readiness_comes_from_storage_not_a_literal():
    """`status:"ready"` must only ever be reachable behind _grid_artifact()."""
    for fname in ("reveal_grid_export", "reveal_grid_export_status"):
        fn_src = _code_src(fname)
        assert "_grid_artifact(" in fn_src, (
            f"{fname} no longer probes storage — readiness would be asserted again")
        # ast.unparse normalises string literals to single quotes.
        ready_idx = fn_src.find("'ready'")
        probe_idx = fn_src.find("_grid_artifact(")
        assert ready_idx != -1, f"{fname} no longer emits a ready status at all"
        assert ready_idx > probe_idx, (
            f'{fname} emits "ready" before probing storage')


def test_no_hardcoded_precomputed_state_table():
    """The old GRID_EXPORT_PRECOMPUTED_STATES mapped 15 states to a single
    hard-coded 2026-04-20 timestamp and WAS the availability answer."""
    src = _read()
    assert "GRID_EXPORT_PRECOMPUTED_STATES" not in src, (
        "the hard-coded availability table is back — availability must come "
        "from the storage probe, not a literal")
    assert "2026-04-20T06:00:00Z" not in src, (
        "the fabricated last_refresh stamp is back")


def test_grid_artifact_fails_closed():
    """Any doubt -> exists=False. It must never report an artifact it did not
    verify, including when storage is unconfigured or errors."""
    fn_src = _code_src("_grid_artifact")
    assert "except Exception" in fn_src, "_grid_artifact no longer catches storage errors"
    # Every return in the function must be a 2-tuple whose first element is a
    # literal True/False — never a bare truthy default.
    firsts = []
    for node in ast.walk(_fn("_grid_artifact")):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            head = node.value.elts[0]
            assert isinstance(head, ast.Constant) and isinstance(head.value, bool), (
                f"_grid_artifact returns a non-literal existence flag: "
                f"{ast.unparse(head)}")
            firsts.append(head.value)
    assert firsts.count(True) == 1, (
        f"_grid_artifact has {firsts.count(True)} success returns; exactly one "
        "path may report an artifact present")
    assert firsts.count(False) >= 3, (
        "_grid_artifact lost a fail-closed branch (unconfigured / missing / presign)")


def test_status_route_rejects_arbitrary_job_ids():
    """It used to confirm "ready" for any string. An invented id must 4xx."""
    fn_src = _code_src("reveal_grid_export_status")
    assert "isalpha()" in fn_src and "len(state) != 2" in fn_src, (
        "the job_id shape check is gone — arbitrary ids would resolve again")
    assert "400" in fn_src and "404" in fn_src, (
        "status route no longer distinguishes a malformed id from a missing artifact")


# ── 2. climate-risk must not report absence of coverage as zero risk ─────────

def test_zone_score_returns_none_when_uncovered():
    """The whole bug in one assert. 0 meant "measured, no risk"."""
    ns = _exec_fragments(
        ["_haversine_km", "_zone_score", "FLOOD_RISK_ZONES",
         "WILDFIRE_RISK_ZONES", "EXTREME_HEAT_ZONES"])
    zs = ns["_zone_score"]

    # Loudoun County VA — the case that motivated this. Outside all 20 zones.
    for table in ("FLOOD_RISK_ZONES", "WILDFIRE_RISK_ZONES", "EXTREME_HEAT_ZONES"):
        score, contrib = zs(39.04, -77.48, ns[table])
        assert score is None, (
            f"{table} scored Loudoun County VA {score} instead of None — an "
            "uncovered location must never read as a measured zero")
        assert contrib is None

    # Inside a zone it must still score, or the endpoint is merely broken.
    heat, contrib = zs(33.45, -112.07, ns["EXTREME_HEAT_ZONES"])  # Phoenix
    assert heat is not None and heat > 50, f"Phoenix heat score {heat} — model is dead"
    assert contrib and contrib["zone"] == "Phoenix heat island"


def test_climate_composite_is_none_when_nothing_is_covered():
    fn_src = _code_src("climate_risk")
    assert "composite = None" in fn_src, (
        "climate_risk no longer yields a null composite for uncovered points")
    assert "coverage" in fn_src and "components_covered" in fn_src, (
        "climate_risk stopped declaring how much of the model covered the point")
    # A bare `int(round(...))` with no covered-guard would reintroduce the zero.
    assert "if covered:" in fn_src, "the covered/uncovered branch is gone"


def test_climate_risk_does_not_claim_live_agency_feeds():
    """It cited FEMA + NIFC + NOAA while calling none of them."""
    strings = " ".join(_code_strings(_fn("climate_risk")))
    src = _read()
    assert "FEMA flood + NIFC wildfire + NOAA extreme heat proxies" not in src, (
        "the live-agency-feed attribution is back")
    assert "live_feeds" in strings, (
        "climate_risk no longer discloses that it makes no live agency calls")


# ── 3. social-acceptance must not fabricate measurements ────────────────────

def test_no_coordinate_hash_components():
    """news_sentiment_score / litigation_count_12mo / community_opposition_signals
    were derived from sin(lat*12.9898 + lon*78.233)."""
    fn_src = _code_src("social_acceptance_index")
    for marker in ("12.9898", "78.233"):
        assert marker not in fn_src, (
            f"the coordinate-hash seed ({marker}) is back in "
            "social_acceptance_index — those fields are presented as measured "
            "counts and must not be synthesised")
    # Not a substring check on the KEY names — news_sentiment_score is a
    # legitimate (now-null) key and would false-positive on "news_sent".
    # What must not come back is arithmetic synthesising a component.
    assert "math.sin" not in fn_src, (
        "social_acceptance_index is computing a trig-hashed component again")
    assert "litigation =" not in fn_src, (
        "a synthetic litigation count is being computed again")


def test_fabricated_components_are_null_not_invented():
    fn = _fn("social_acceptance_index")
    comp = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if {"news_sentiment_score", "litigation_count_12mo",
                "community_opposition_signals"} <= keys:
                comp = node
                break
    assert comp is not None, "the components block is gone entirely"
    for k, v in zip(comp.keys, comp.values):
        assert isinstance(v, ast.Constant) and v.value is None, (
            f"components[{k.value!r}] is {ast.unparse(v)} — DC Hub does not "
            "measure this; it must stay null, not be re-invented")


def test_social_index_is_none_when_no_jurisdiction_is_in_range():
    """It defaulted to 70 ('most of the country is moderately accepting'),
    publishing a specific-looking score for every point on earth."""
    fn_src = _code_src("social_acceptance_index")
    assert "composite = 70" not in fn_src, (
        "the blanket default score is back — an uncovered point must read null")
    assert "if covered else None" in fn_src, "the uncovered branch is gone"


def test_no_endpoint_claims_a_live_feed_it_does_not_call():
    """Every `source` string on the three modelled endpoints must disclose."""
    src = _read()
    for fname, must_say in (
        ("social_acceptance_index", "not a live feed"),
        ("climate_risk", "not a live agency feed"),
    ):
        joined = " ".join(_code_strings(_fn(fname)))
        assert must_say in joined, (
            f"{fname}'s source string no longer discloses that it is a model")
    assert "EIA + eGRID/EPA 2024 reference\"," not in src, (
        "carbon-intensity's source reads as a live feed again")
