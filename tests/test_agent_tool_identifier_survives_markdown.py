"""2026-09-05 — an `<em>`-wrapped `<code>` publishes a tool name that does not exist.

MEASURED LIVE, through the edge, before this fix. Every `/dcpi/<slug>` page
carries one line telling an agent how to re-query the market. In HTML it read:

    AI agents: <code>get_market_dcpi_rank market_slug="midland-tx"</code>
               via https://dchub.cloud/mcp for the citable record.

Fetched with `Accept: text/markdown` — the representation Cloudflare's
"Markdown for Agents" serves, and the one a token-thrifty agent asks for — the
same line came back as:

    _Source: DC Hub (dchub.cloud), updated 2026-09-05\\. AI agents:
     `getmarketdcpirank marketslug="midland-tx"` via https://dchub.cloud/mcp …_

The caption was NOT dropped. Its underscores were EATEN. The surrounding `<em>`
converts to an underscore-emphasis run (`_…_`), and every `_` inside that run is
consumed as a delimiter — including the ones inside the `<code>` span. The page
therefore instructs the agent to call `getmarketdcpirank(marketslug=…)`, which
is not a tool and never was. That is worse than saying nothing: an agent that
follows the page gets a tool-not-found error and learns DC Hub does not work.

★ THE CONFOUND, RULED OUT BEFORE FIXING. Two hypotheses predicted the data
equally well: (a) the `<em>` wrapper, (b) "identifiers with 3+ underscores get
mangled" — the surviving examples nearby, `get_facility` and `list_transactions`,
both have exactly one underscore, which cannot form an emphasis pair. Measured
`/integrations/mcp`, whose `<code>` spans are NOT inside `<em>`:

    claude_desktop_config.json   3 underscores   intact in markdown
    get_grid_data                2 underscores   intact
    get_market_intel             2 underscores   intact  (/agent)

Three underscores survive fine outside an `<em>`. The wrapper is the cause, so
the fix is to drop it and carry the italics in CSS — identical rendering for a
human, intact identifier for an agent.

★ WHY A RENDER TEST AND NOT A GREP. Grepping the source proves a string changed.
It does not prove the page an agent receives is right, and this defect lives
exactly in the gap between those two. These tests render the shipped Jinja
template (pulled out with `ast`, no main.py import — the house rule) and assert
on the emitted HTML.

Scope measured 2026-09-05: 8 of 8 sampled `/dcpi/` slugs mangled (abilene,
bogota, commack, fremont, kuala-lumpur, mesa, oslo, saint-louis) — the template
line is unconditional, so all ~323 DCPI market pages carried it. A repo-wide
scan found this as the only instance of the class; `test_no_route_wraps_a_code_
identifier_in_em` keeps it that way.
"""
import ast
import pathlib
import re

import pytest

jinja2 = pytest.importorskip("jinja2")

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "routes" / "dcpi.py"

# The tool and argument the page tells an agent to call. Both contain the
# underscores the defect ate.
TOOL = "get_market_dcpi_rank"
ARG = "market_slug"

MARKET = {
    "market_name": "Midland-Odessa", "market_slug": "midland-tx", "state": "TX",
    "iso": "ERCOT", "verdict": "BUILD", "excess_power_score": 85.7,
    "constraint_score": 22.8, "composite_score": 83.0,
    "time_to_power_months": 10.0, "queue_wait_months": 16.0,
    "reserve_margin_pct": 28.0, "gen_additions_12mo_mw": 22572.0,
    "curtailment_pct": 10.0, "stranded_capacity_mw": None,
    "queue_capacity_mw": None, "latitude": 31.99, "longitude": -102.08,
    "computed_at": "2026-09-05T03:30:00Z", "signal_tier": "full",
    "signal_tier_basis": "3 of 3 live grid feeds fed this score",
    "data_basis": "measured", "emergency_count_30d": 0,
    "method_version": "2.0.1", "iso_type": None,
}

# An <em> run that swallows a <code> span containing an underscore. Non-greedy
# and blocked from crossing a closing </em>, so it matches one emphasis run.
EM_WRAPPED_CODE = re.compile(
    r"<em>(?:(?!</em>).)*?<code>[^<]*_[^<]*</code>(?:(?!</em>).)*?</em>", re.S)


@pytest.fixture(scope="module")
def template():
    for node in ast.walk(ast.parse(SRC.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Assign)
                and getattr(node.targets[0], "id", "") == "DCPI_MARKET_TEMPLATE"):
            return jinja2.Environment().from_string(ast.literal_eval(node.value))
    raise AssertionError("DCPI_MARKET_TEMPLATE not found in routes/dcpi.py")


def _render(template, gated=False):
    return template.render(s=MARKET, risks=[], opps=[], gated=gated,
                           narrative="n", place_label="Midland-Odessa, TX",
                           facilities_html="")


def test_the_agent_handoff_names_the_tool_and_its_argument(template):
    """The line must survive at all — deleting it would pass the em test."""
    html = _render(template)
    assert TOOL in html, f"the page no longer names the {TOOL} tool"
    assert ARG in html, f"the page no longer names the {ARG} argument"


@pytest.mark.parametrize("gated", [False, True])
def test_no_code_identifier_is_wrapped_in_em(template, gated):
    """The defect itself: an <em> run eats the underscores inside its <code>."""
    offenders = EM_WRAPPED_CODE.findall(_render(template, gated=gated))
    assert not offenders, (
        "an <em> run wraps a <code> identifier containing '_'. In the markdown "
        "representation the wrapper becomes _…_ and every underscore inside it "
        "is consumed as an emphasis delimiter, so the agent is handed a tool "
        "name that does not exist. Carry the italics in CSS "
        "(font-style:italic) instead. Offending run(s): "
        + " | ".join(re.sub(r"\s+", " ", o)[:200] for o in offenders))


def test_the_handoff_still_reads_as_italic_to_a_human(template):
    """Dropping <em> must not silently drop the styling it carried."""
    html = _render(template)
    i = html.find(TOOL)
    para = html.rfind("<p", 0, i)
    assert para != -1 and "font-style:italic" in html[para:i], (
        "the <em> was removed without moving the italics into CSS — the "
        "caption now renders upright, which is a visible regression")


def test_no_route_wraps_a_code_identifier_in_em():
    """Repo-wide: keep this the only shape it can take, everywhere.

    Scans the modules that emit served HTML. The class recurs whenever someone
    styles a caption with <em> and happens to put a tool name in it, and every
    recurrence silently publishes a nonexistent identifier.
    """
    offenders = []
    for path in sorted((ROOT / "routes").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in EM_WRAPPED_CODE.finditer(text):
            line = text[:m.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(ROOT)}:{line}")
    assert not offenders, (
        "these emit an <em>-wrapped <code> identifier, whose underscores are "
        "eaten in the markdown representation served to agents: "
        + ", ".join(offenders))
