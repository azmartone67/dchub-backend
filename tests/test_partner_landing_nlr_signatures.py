"""Guards for the 2026-08-01 signature repair on the NLR partner surfaces.

`routes/partner_landing.py` renders copy-pasteable curl samples for each
outreach target, and `docs/NLR_REVEAL_INTEGRATION_GUIDE.md` is the document NLR
codes against. Both carried signatures that did not match the live handlers:

    reveal-cell-bulk?bbox=<...>                    -> 400  (needs 4 bounds)
    POST reveal-grid-export                        -> 405  (route is GET-only)
    social-acceptance-index?state=<S>&county=<C>   -> 400  (needs lat/lon)
    carbon-intensity?region=<R>                    -> 400  (needs lat/lon)
    reveal-validation-feed?region=<R>              -> 200, region SILENTLY IGNORED

The last one is the dangerous shape: a partner scoping the feed by region got
unfiltered global rows and no error to tell them.

These guards read the SAMPLE STRINGS and compare them against the parameter
names the handlers actually pull from request.args — so the sample and the code
cannot drift apart again without a test failing. No DB, no network, no
`import main`. Nothing here runs at module scope.
"""
import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_LANDING = "routes/partner_landing.py"
_REVEAL = "reveal_endpoints.py"
_GUIDE = "docs/NLR_REVEAL_INTEGRATION_GUIDE.md"


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _tree(rel):
    tree = ast.parse(_read(rel))
    assert isinstance(tree, ast.Module) and len(tree.body) > 5, (
        f"{rel} parsed to a degenerate module — this harness is not looking at "
        "the real file")
    return tree


def _handler_args(func_name):
    """The parameter names `func_name` reads out of request.args."""
    tree = _tree(_REVEAL)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            fn = node
            break
    assert fn is not None, f"{_REVEAL} no longer defines {func_name}()"
    names = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "args"
                and node.args and isinstance(node.args[0], ast.Constant)):
            names.add(node.args[0].value)
    assert names, f"{func_name} reads no request.args — re-point this guard"
    return names


def _nlr_entry():
    """The `nlr` dict literal out of partner_landing.py."""
    for node in ast.walk(_tree(_LANDING)):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "nlr"
                        and isinstance(v, ast.Dict)):
                    return ast.literal_eval(v)
    raise AssertionError(f"{_LANDING} no longer defines an 'nlr' target entry")


def _query_params(url):
    """Param names in a URL's query string."""
    if "?" not in url:
        return set()
    return {kv.split("=")[0] for kv in url.split("?", 1)[1].split("&") if kv}


def _sample_urls(text, path_fragment):
    """Every dchub.cloud URL in `text` whose path contains `path_fragment`."""
    urls = re.findall(r'https://[^\s"\'\\]+', text)
    return [u for u in urls if path_fragment in u]


# ── 0. must-fail control ─────────────────────────────────────────────────────

def test_harness_reads_real_files_and_can_fail():
    """A collection abort exits SILENT GREEN, so all-passing is not by itself
    evidence these ran (2026-07-28). Pin facts true only if the files loaded."""
    assert "partner_landing" in _read(_LANDING)
    assert _handler_args("social_acceptance_index") >= {"lat", "lon"}
    assert _query_params("https://x/a?lat=1&lon=2") == {"lat", "lon"}
    assert _query_params("https://x/a") == set()


# ── 1. the landing-page curl samples must match the handlers ─────────────────

def test_cell_bulk_sample_uses_the_four_bounds_not_bbox():
    """`?bbox=` returns 400. The handler takes min_lat/max_lat/min_lon/max_lon."""
    sample = _nlr_entry()["code_sample"]
    urls = _sample_urls(sample, "/reveal-cell-bulk")
    assert urls, "the NLR code sample no longer demonstrates reveal-cell-bulk"
    handler = _handler_args("reveal_cell_bulk")
    for u in urls:
        params = _query_params(u)
        assert "bbox" not in params, (
            f"reveal-cell-bulk sample uses bbox= again: {u} — that is a 400")
        required = {"min_lat", "max_lat", "min_lon", "max_lon"}
        assert required <= params, (
            f"reveal-cell-bulk sample is missing {sorted(required - params)}: {u}")
        assert params <= handler, (
            f"sample passes {sorted(params - handler)}, which "
            f"reveal_cell_bulk() never reads: {u}")


def test_social_acceptance_sample_is_keyed_on_latlon():
    """`?state=&county=` returns 400 — the handler is lat/lon."""
    sample = _nlr_entry()["code_sample"]
    urls = _sample_urls(sample, "/social-acceptance-index")
    assert urls, "the NLR code sample no longer demonstrates social-acceptance-index"
    handler = _handler_args("social_acceptance_index")
    for u in urls:
        params = _query_params(u)
        assert {"lat", "lon"} <= params, (
            f"social-acceptance-index sample lacks lat/lon: {u}")
        assert "county" not in params and "state" not in params, (
            f"social-acceptance-index sample uses state/county again: {u} — 400")
        assert params <= handler, (
            f"sample passes {sorted(params - handler)}, which "
            f"social_acceptance_index() never reads: {u}")


def test_cell_bulk_sample_extent_stays_small():
    """A 1.0deg box (414 cells) took 85s at the origin and 503'd at the edge
    after 25s. The sample is copy-pasted by partners, so it must stay inside
    what dchub.cloud can actually serve."""
    for u in _sample_urls(_nlr_entry()["code_sample"], "/reveal-cell-bulk"):
        q = dict(kv.split("=", 1) for kv in u.split("?", 1)[1].split("&") if "=" in kv)
        span_lat = abs(float(q["max_lat"]) - float(q["min_lat"]))
        span_lon = abs(float(q["max_lon"]) - float(q["min_lon"]))
        assert span_lat <= 0.35 and span_lon <= 0.35, (
            f"cell-bulk sample box is {span_lat}x{span_lon} deg — measured "
            "2026-08-01, 0.5deg ran 23s cold and 1.0deg returned 503 at the "
            f"edge. Keep the published example small: {u}")


# ── 2. the bullet must not re-inflate to "already shipped" ───────────────────

def test_no_already_shipped_claim():
    """Audited 2026-08-01: all 10 routes answer 200, but reveal-grid-export
    hands back download URLs on a host that does not serve, and
    reveal-validation-feed was dead until that morning. "Shipped" overstated
    both. See docs/NLR_LEGAL_REDLINE_NOTES.md A3."""
    bullets = " ".join(_nlr_entry()["value_bullets"]).lower()
    assert "already shipped" not in bullets, (
        "the NLR landing page claims endpoints are 'already shipped' again — "
        "re-verify grid-export's download path before restoring that wording")


def test_geothermal_named_by_its_real_route():
    """The route is /api/v1/geothermal-potential (nlr_intelligence.py); bare
    /api/v1/geothermal is a 404."""
    bullets = " ".join(_nlr_entry()["value_bullets"])
    assert "geothermal-potential" in bullets, (
        "bullet no longer names geothermal-potential — bare 'geothermal' 404s")


# ── 3. the integration guide must not re-publish the broken signatures ───────

def _guide_code_fences():
    """Only the ``` fenced blocks — what a reader actually copies.

    The surrounding prose MUST be free to name the broken signatures, because
    that is how the correction explains itself. Scanning the whole file made
    this guard fire on its own explanatory comment — the same
    prose-satisfies-grep trap as the reveal schema guards.
    """
    fences = re.findall(r"```.*?\n(.*?)```", _read(_GUIDE), re.S)
    assert fences, f"{_GUIDE} has no fenced code blocks — re-point this guard"
    return "\n".join(fences)


def test_guide_does_not_document_the_400ing_signatures():
    fenced = _guide_code_fences()
    for bad, why in (
        ("reveal-cell-bulk?bbox=", "400 — needs four discrete bounds"),
        ("social-acceptance-index?state=", "400 — keyed on lat/lon"),
        ("carbon-intensity?region=", "400 — keyed on lat/lon"),
        ("POST /api/v1/reveal-grid-export", "405 — the route is GET-only"),
    ):
        assert bad not in fenced, (
            f"{_GUIDE} publishes `{bad}` in a copyable code block again: {why}")


def test_guide_warns_that_region_is_silently_ignored():
    """The worst of the set: 200 OK while dropping the caller's region scope."""
    guide = _read(_GUIDE)
    assert "reveal-validation-feed?region=" not in _guide_code_fences(), (
        f"{_GUIDE} publishes a region param the feed does not implement")
    assert "no `region` parameter" in guide, (
        "the guide no longer warns that region= is accepted and ignored — a "
        "region-scoped integration silently receives global rows")
    assert _query_params("https://x/reveal-validation-feed?region=PJM") == {"region"}
    assert "region" not in _handler_args("reveal_validation_feed"), (
        "reveal_validation_feed now READS region — if that is real, update the "
        "guide and delete this guard rather than leaving the warning stale")
