"""tests/test_canon_placeholders_resolved.py — a placeholder must never SHIP (2026-08-16).

The canon sweep replaced hand-typed headline counts on the AI-discovery surfaces
(/openapi.json, /.well-known/ai-plugin.json, the MCP server-card, /agents-md-inline,
/llms.txt, /llms-full.txt) with {canon_*} placeholders resolved at render time.

★ THE FAILURE MODE THIS FILE EXISTS FOR is the one the sweep INTRODUCES: adding a
placeholder to a string and forgetting to pass it through canon_text(), which
serves the literal "{canon_facilities}" to an agent. That is strictly WORSE than
the stale number it replaced — a stale count is merely wrong, a raw placeholder
is visibly broken and unparseable.

So the guard is not "are the numbers right" (they move); it is "can an unresolved
placeholder reach a response". It walks the AST and requires every
placeholder-bearing string constant to sit inside a resolver call.

What was swept, and why these numbers were wrong:
  17,000+ facilities -> canon 18,000+   (stale; live 18,073)
  1,700+ deals       -> canon 1,800+    (stale; live 1,849)
  "2.1.22" card version -> canon 2.12.0 (a version on ai_surface_canon's OWN
                                         stale_markers denylist, served anyway)
  facilities_tracked 21000 -> 18,000    (OVER-claim; floors round DOWN)
  isos_covered 10          -> 7         (OVER-claim)
  dcpi_markets 233         -> 300       (stale under-claim)

NOT swept, deliberately: post_announcement.py. It is a DORMANT one-off script
(tests/test_linkedin_token_single_source.py lists it as dormant) holding a DATED
press release. A press release is a point-in-time record; floating its numbers
with the canon would rewrite history, not fix drift.

Run:  python3 -m pytest tests/test_canon_placeholders_resolved.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
# ★2026-08-18: was just ["ai_discovery_routes.py"]. The facility-floor sweep put
# {canon_facilities} into every module below, and a placeholder that never
# reaches canon_text() ships literal braces to an agent — strictly worse than
# the stale number it replaced. Every swept module is listed so that failure is
# caught here rather than in someone's context window.
#
# main.py is deliberately absent: at 42k lines it gets surgical guards instead
# of a whole-file scan (same reason tests/test_canonical_counts_drift.py keeps
# it out of AGENT_CODE_SURFACES).
# NOT listed: ai_interconnection.py, which resolves through its OWN
# _canon_fill() replace-chain rather than a bare canon_text() call, so this
# lexical guard cannot see it. It is covered instead by
# test_every_module_resolver_covers_the_whole_shared_canon below, which probes
# _canon_fill with every shared placeholder.
#
# ★2026-08-25: routes/agent_concierge.py was listed here for the same reason and
# no longer belongs — it now DELEGATES to canon_text() (that opt-out is exactly
# why /agent alone kept serving 18,500+ while every sibling surface healed to
# 18,800+ the hour #3196 deployed). It stays out of _SWEPT because its
# placeholders live in a module-level _LANDING_HTML constant that this lexical
# scan cannot tie to the canon_text() call in agent_landing(). It is covered by
# something stronger: tests/test_agent_landing_derives_canon.py RENDERS the body
# and asserts no placeholder survives, in both the warm and cold branches.
_SWEPT = [
    "agent_hub.py",
    # ★2026-09-04 (b): added when the retired deal floor was drained from this
    # file. Its ground-truth block for the editor LLM is an f-string, so the
    # obvious repair — doubling the braces — parses, imports and passes every
    # other guard while handing the model a literal {canon_*} token as a fact.
    # The value is interpolated instead; this entry is what makes the wrong
    # repair fail rather than ship silently.
    "content_publisher.py",
    "ai_agent_discovery.py",
    "ai_agent_teaching.py",
    "ai_discovery_routes.py",
    "ai_ecosystem_agent.py",
    "ai_outreach_agent.py",
    "api_response_enrichment.py",
    "auto_pilot.py",
    "backend_patch_mcp_routes.py",
    "chatgpt_mcp_compat.py",
    "competitor_intelligence.py",
    "dchub-fix-all.py",
    "dchub_daily_automation.py",
    "email_service.py",
    "enhanced_promotion.py",
    "facilities_hub.py",
    "fix_slug_body_update.py",
    "gdci.py",
    "generate_facility_pages.py",
    "global_intelligence_agent.py",
    "google_integration_routes.py",
    "google_meta_integration.py",
    "inject_meta_tags.py",
    "linkedin_autopost.py",
    "linkedin_image_post.py",
    "linkedin_poster.py",
    "mcp_gateway.py",
    "mcp_server.py",
    "moltbook_integration.py",
    "nav_config.py",
    "populate_press_bodies.py",
    "replit-nav-config-endpoint.py",
    "routes/agent_a2a.py",
    "routes/agent_broadcast.py",
    "routes/agent_self_register.py",
    "routes/ai_platform_tool_tuner.py",
    "routes/autopilot_routes.py",
    "routes/brain_answer_cache.py",
    "routes/competitive_intel.py",
    "routes/competitive_seo.py",
    "routes/competitive_vs.py",
    "routes/comprehensive_report.py",
    "routes/content_enqueue.py",
    "routes/dchub_media_hub.py",
    "routes/demo.py",
    "routes/devrel_targets.py",
    "routes/integrations_landing.py",
    "routes/mcp_citation.py",
    "routes/mcp_connect.py",
    "routes/mcp_outreach_drafts.py",
    "routes/mcp_registry_outreach.py",
    "routes/mcp_tool_catalog.py",
    "routes/media_editorial.py",
    "routes/media_outreach.py",
    "routes/media_showcase.py",
    # ★2026-08-25: monthly_trend was MISSING while already carrying
    # {canon_tools} (added 08-25) — mutation testing found it: deleting the
    # canon_text() wrapper around a placeholder-bearing string left this
    # suite green. Unfenced is exactly the literal-braces failure this file
    # exists to prevent.
    "routes/monthly_trend.py",
    "routes/nav_config_routes.py",
    "routes/og_images.py",
    "routes/onboard_auto_approve.py",
    "routes/onboarding_recover.py",
    "routes/openapi_autogen.py",
    "routes/partner_landing.py",
    "routes/paywall_hint_middleware.py",
    "routes/quick_redirects.py",
    "routes/seo_pages.py",
    "routes/state_of_power.py",
    "routes/surface_brain.py",
    "seo_agents.py",
    "seo_meta_tags.py",
    "seo_promotion_engine.py",
    "welcome_emails.py",
]

# Functions that resolve a placeholder. _canon_int calls canon_text internally.
_RESOLVERS = {"canon_text", "_canon_text", "_canon_int"}


def _known_placeholders() -> set[str]:
    from ai_surface_canon import canon_nums
    return set(canon_nums().keys())


def _str_constants(node) -> list[str]:
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _resolver_name(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


@pytest.mark.parametrize("fname", _SWEPT)
def test_every_placeholder_string_is_resolved(fname):
    """THE PIN: no {canon_*} string may sit outside a resolver call."""
    path = _ROOT / fname
    tree = ast.parse(path.read_text())
    known = _known_placeholders()
    assert known, "canon_nums() returned no placeholders — the guard would be vacuous"

    covered: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _resolver_name(node) in _RESOLVERS:
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                covered.update(id(n) for n in ast.walk(arg)
                               if isinstance(n, ast.Constant))

    unresolved = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if not any(ph in node.value for ph in known):
            continue           # regex patterns / prose mentioning {canon_x} won't match a KNOWN key
        if id(node) not in covered:
            unresolved.append((getattr(node, "lineno", "?"), node.value[:70]))

    assert not unresolved, (
        f"{fname}: {len(unresolved)} placeholder string(s) never reach canon_text() "
        f"and would SHIP the literal braces:\n" +
        "\n".join(f"  line {ln}: {txt!r}" for ln, txt in unresolved)
    )


@pytest.mark.parametrize("fname", _SWEPT)
def test_no_placeholder_is_eaten_by_an_fstring(fname):
    """★ THE BUG THE AST-COVERAGE TEST ABOVE CANNOT SEE.

    /llms.txt is built from an f-STRING. Writing {canon_facilities} there makes
    Python parse it as an EXPRESSION, not literal text:
        NameError: name 'canon_facilities' is not defined
    — a hard 500 on a crawler-facing surface, and the coverage test above passed
    the whole time because the string genuinely was inside canon_text().

    Inside an f-string the braces must be DOUBLED ({{canon_x}}) so the render
    emits literal braces for canon_text() to substitute afterwards.

    Detected structurally: a JoinedStr whose FormattedValue interpolates a bare
    Name beginning with `canon_` is always this mistake.
    """
    tree = ast.parse((_ROOT / fname).read_text())
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        for part in node.values:
            if not isinstance(part, ast.FormattedValue):
                continue
            expr = part.value
            if isinstance(expr, ast.Name) and expr.id.startswith("canon_"):
                bad.append((getattr(part, "lineno", "?"), expr.id))
    assert not bad, (
        "placeholder(s) interpolated by an f-string instead of left literal — "
        "double the braces to {{canon_x}}:\n" +
        "\n".join(f"  line {ln}: {{{name}}}" for ln, name in bad)
    )


@pytest.mark.parametrize("fname", _SWEPT)
def test_swept_literals_do_not_return(fname):
    """Regression pin on the exact values the sweep removed.

    Scoped to non-comment lines: the sweep's own comments quote the old numbers
    to explain what was wrong, and that prose must stay legal.
    """
    path = _ROOT / fname
    offenders = []
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for bad in ("17,000+", "1,700+", "20,000 tracked", "2.1.22"):
            if bad in line:
                offenders.append((i, bad, line.strip()[:70]))
    assert not offenders, "hand-typed canon values are back:\n" + "\n".join(
        f"  line {ln}: {bad} in {txt!r}" for ln, bad, txt in offenders)


# ── the surfaces, actually RENDERED ────────────────────────────────────────
# ★ Both real bugs in this sweep passed every static check above and were only
# caught by rendering:
#   (1) /llms.txt 500'd — an f-string parsed {canon_facilities} as an EXPRESSION
#       (NameError). The AST-coverage test passed: the string WAS inside
#       canon_text().
#   (2) then the over-broad fix emitted "{18,000+}" — braces around the number —
#       because the f-string ends mid-block at `""" + """` and the rest is a
#       PLAIN string where doubling is wrong. No static check saw that either.
# Static structure cannot tell you what a byte-stream looks like. Render it.

_SURFACES = ["/openapi.json", "/.well-known/mcp/server-card.json",
             "/agents-md-inline", "/llms.txt", "/llms-full.txt"]


@pytest.fixture(scope="module")
def rendered():
    flask = pytest.importorskip("flask")
    import ai_discovery_routes as adr
    app = flask.Flask(__name__)
    adr.register_discovery_routes(app)
    client = app.test_client()
    out = {}
    for path in _SURFACES:
        r = client.get(path)
        out[path] = (r.status_code, r.get_data(as_text=True))
    return out


@pytest.mark.parametrize("path", _SURFACES)
def test_surface_renders_200(path, rendered):
    """Bug (1): an f-string-eaten placeholder is a hard 500, not a bad number."""
    status, _ = rendered[path]
    assert status == 200, f"{path} returned {status}"


@pytest.mark.parametrize("path", _SURFACES)
def test_surface_ships_no_raw_placeholder(path, rendered):
    _, body = rendered[path]
    assert "{canon_" not in body, f"{path} served an unresolved placeholder"


@pytest.mark.parametrize("path", _SURFACES)
def test_surface_has_no_braces_around_a_canon_value(path, rendered):
    """Bug (2): "{18,000+} facilities" — resolved, but wrapped in stray braces.

    Checked against the VALUES, not a brace regex, because these documents
    legitimately contain API templates like {score} and {market_slug}.
    """
    from ai_surface_canon import canon_nums
    _, body = rendered[path]
    bad = [v for v in canon_nums().values() if v and ("{%s}" % v) in body]
    assert not bad, f"{path} wrapped canon value(s) in braces: {bad}"


@pytest.mark.parametrize("path", _SURFACES)
def test_surface_carries_the_current_facility_count(path, rendered):
    """Positive control: without this, nulling every count would pass the above."""
    from ai_surface_canon import canon_nums
    _, body = rendered[path]
    assert canon_nums()["{canon_facilities}"] in body, (
        f"{path} lost its facility count entirely — canon_text may be returning ''"
    )


def _extract_canon_fill():
    """Pull _canon_fill out of ai_interconnection with ast and exec it on stubs.

    ★ `import ai_interconnection` transitively imports main — full DB startup,
    ~14s, and the house rule is that pre-merge pytest NEVER imports main. So the
    function is extracted instead. A silently-empty extraction would pass every
    assertion below, so the parse asserts a real FunctionDef body.
    """
    src = (_ROOT / "ai_interconnection.py").read_text()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_canon_fill"), None)
    assert fn is not None and fn.body, "_canon_fill parsed with an EMPTY body"
    from ai_surface_canon import PINNED
    ns = {"_CANON": PINNED, "_canon_tool_lines": lambda: "TOOL_LINES"}
    exec(compile(ast.get_source_segment(src, fn), "<_canon_fill>", "exec"), ns)
    return ns["_canon_fill"]


def test_every_module_resolver_covers_the_whole_shared_canon():
    """A SECOND resolver must not fall behind the shared placeholder set.

    ai_interconnection._canon_fill was a hand-maintained .replace() chain
    covering five of the nine shared placeholders. No live bug — its bodies only
    used those five — but the lists could drift apart silently, and writing
    {canon_version} into a body would have SHIPPED RAW BRACES to an agent.

    Probing with every shared key is what makes this future-proof: add a
    placeholder to canon_nums() and any resolver that cannot handle it fails
    here rather than in an agent's context window.
    """
    from ai_surface_canon import canon_nums
    fill = _extract_canon_fill()
    probe = " ".join(canon_nums().keys())
    out = fill(probe)
    assert "{canon_" not in out, (
        f"_canon_fill left placeholder(s) unresolved: {out!r}"
    )


def test_canon_fill_still_handles_its_local_placeholder():
    """{canon_tool_lines} is module-local, not part of the shared set — the
    delegation must not drop it."""
    out = _extract_canon_fill()("{canon_tool_lines}")
    assert "{canon_tool_lines}" not in out


def test_canon_text_leaves_no_placeholder_behind():
    """Every key canon_nums publishes must actually substitute."""
    from ai_surface_canon import canon_nums, canon_text
    probe = " ".join(canon_nums().keys())
    out = canon_text(probe)
    assert "{canon_" not in out, f"canon_text left a placeholder: {out!r}"


def test_canon_text_is_fail_open_not_fail_loud():
    """Falsy input must pass through rather than raise inside a route."""
    from ai_surface_canon import canon_text
    assert canon_text("") == ""
    assert canon_text(None) is None


def test_canon_int_parses_the_display_floor():
    """The numeric claim block needs ints from strings like '18,000+'."""
    import ai_discovery_routes as adr
    from ai_surface_canon import canon_nums
    got = adr._canon_int("{canon_facilities}", -1)
    assert isinstance(got, int) and got > 0
    assert str(got) == canon_nums()["{canon_facilities}"].replace(",", "").rstrip("+")


def test_canon_int_falls_back_rather_than_raising():
    import ai_discovery_routes as adr
    assert adr._canon_int("{canon_not_a_real_key}", 4242) == 4242


def test_main_delegates_and_matches():
    """main._canon_nums must equal the shared implementation.

    House rule: never import main — the function is pulled out with ast. A
    silently-empty extraction would pass everything, so the body is asserted.
    """
    from ai_surface_canon import canon_nums
    src = (_ROOT / "main.py").read_text()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_canon_nums"), None)
    assert fn is not None and fn.body, "_canon_nums parsed with an EMPTY body"
    ns: dict = {}
    exec(compile(ast.get_source_segment(src, fn), "<_canon_nums>", "exec"), ns)
    assert ns["_canon_nums"]() == canon_nums(), "main drifted from the shared canon"
