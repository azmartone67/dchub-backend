"""Glama listing health must be MEASURED, and the probe must not cry wolf.

Measured 2026-09-05: a second, Glama-native DC Hub connector had been showing
a public red Unhealthy badge since 2026-09-01 advertising "33 tools", while
the real card was tested Healthy at 14:41 the same day with 83. Four days, and
nothing in this codebase could see it — `check_mcp_presence_stale` watches the
presence CRAWLER (a substring search for "dchub" on a registry index), which
cannot see a health badge or a second card existing at all.

These tests use fixtures shaped like the real markup. No network: `probe()`
takes its fetcher as an argument precisely so this file never leaves the box.

The load-bearing cases are the ones that keep the probe QUIET:

  * Glama's pages carry their own AI review — "With 82 tools, this server is
    extremely heavy compared to typical MCP servers (3-15 tools)" — so a loose
    "N tools" scan reads 15, 82 and 83 off a page whose real answer is 83.
    The count comes from the Available Tools badge or nowhere.
  * The /mcp/servers/… repo listing has NO health badge. Treating its missing
    Status block as a parse failure is a false alarm on a working page.
  * Retired counts on Glama live in GLAMA-authored copy we cannot commit
    against. They are reported inside a finding a human can close, and never
    fire one of their own — a daily red that no commit can clear is how a
    monitor gets ignored.
"""
import ast
import os
import re

import pytest

glp = pytest.importorskip("routes.glama_listing_probe")


def _connector_page(status="Healthy", tested="2026-09-05T14:41:28.260Z",
                    badge=83, extra="", before=""):
    """Shaped like Glama's real connector markup, hashed class names included.

    The badge deliberately carries the React HTML comments Glama emits inside
    it — `83<!-- --> tool<!-- -->s` — because a regex that only matches clean
    text passes here and finds nothing in production.
    """
    badge_html = (
        f'<h2 class="jsgMqa jRXFuD">Available Tools</h2>'
        f'<span class="bZBozA kuKlME">{badge}<!-- --> tool<!-- -->s</span>'
    ) if badge is not None else ""
    return (
        '<div class="dAotlZ"><dt class="czikZZ jrPWok">Status</dt>'
        '<dd class="jrPWok"><div class="bYPztT fyplMZ">'
        '<div style="background:#12b981"></div>'
        f'<span>{status}</span></div></dd>'
        '<dt class="czikZZ jrPWok">Last Tested</dt>'
        f'<dd class="jrPWok"><time dateTime="{tested}">x</time></dd></div>'
        + badge_html + extra
    )


# Glama's own AI review — present on the real pages, and the reason a loose
# "N tools" scan is wrong.
AI_REVIEW = ('<p class="jrPWok jGqtWe">With 82 tools, this server is extremely '
             'heavy compared to typical MCP servers (3-15 tools).</p>')


# ── parsing ─────────────────────────────────────────────────────────────────

def test_reads_status_and_last_tested_without_touching_class_names():
    got = glp.parse_listing(_connector_page())
    assert got["status"] == "Healthy"
    assert got["last_tested"].startswith("2026-09-05T14:41")
    assert got["status_found"] is True


def test_reads_unhealthy():
    assert glp.parse_listing(_connector_page(status="Unhealthy"))["status"] == "Unhealthy"


def test_tool_count_comes_from_the_badge_not_from_glamas_ai_review():
    """The whole point. 82 and 15 appear on the page; 83 is the answer.

    The review is placed BEFORE the badge on purpose. On the live page it
    happens to sit after it, so a loose first-match scan would read 83 there
    by luck of ordering and this test would prove nothing about the anchor.
    Putting it first tests what we actually rely on — that the count is taken
    from the Available Tools badge and not from wherever "N tools" appears.
    """
    got = glp.parse_listing(_connector_page(badge=83, before=AI_REVIEW))
    assert got["tools_badge"] == 83


def test_no_badge_when_glama_never_introspected():
    """The unhealthy duplicate renders no Available Tools block at all — its
    '33 tools' is a stale Glama-side description, not an introspection."""
    page = _connector_page(status="Unhealthy", badge=None,
                           extra="Description: … 33 tools covering 21,000+ …")
    got = glp.parse_listing(page)
    assert got["tools_badge"] is None
    assert "33 tools" in got["retired_phrases"]


def test_retired_phrases_are_observed_not_inferred():
    got = glp.parse_listing(_connector_page(extra="21,000+ facilities, 232 markets"))
    assert "21,000+" in got["retired_phrases"]
    assert "232 markets" in got["retired_phrases"]


# ── findings ────────────────────────────────────────────────────────────────

def _row(key, disposition, badge_page=True, **kw):
    row = {"key": key, "url": f"https://glama.ai/{key}",
           "disposition": disposition, "has_health_badge": badge_page,
           "reachable": True, "status": "Healthy", "last_tested": "t",
           "tools_badge": 83, "retired_phrases": [], "status_found": True}
    row.update(kw)
    return row


def test_the_duplicate_connector_fires_while_it_exists():
    out = glp.findings([_row("dupe", glp.DEPRECATE, status="Unhealthy",
                             tools_badge=None, retired_phrases=["33 tools"])],
                       canon_tools=83)
    assert [f["issue"] for f in out] == ["glama_duplicate_connector_listed"]
    detail = out[0]["detail"]
    # The finding has to carry BOTH halves of the fix. Deleting the card
    # without replacing the email-form ownership file lets it come back.
    assert "DEPRECATE this connector" in detail
    assert "glama_claim_" in detail
    assert "33 tools" in detail  # the stale copy rides along, informationally


def test_a_healthy_keeper_at_canon_is_silent():
    assert glp.findings([_row("keeper", glp.KEEP)], canon_tools=83) == []


def test_unhealthy_keeper_fires_with_the_sync_then_deploy_order():
    out = glp.findings([_row("keeper", glp.KEEP, status="Unhealthy")], canon_tools=83)
    assert [f["issue"] for f in out] == ["glama_listing_unhealthy"]
    # Deploy alone rebuilds the mirror's frozen SHA forever — the order is the
    # whole remediation, so it must survive in the text.
    assert "Sync Server THEN Deploy" in out[0]["detail"]


def test_tool_count_drift_fires_only_off_the_badge():
    out = glp.findings([_row("keeper", glp.KEEP, tools_badge=73)], canon_tools=83)
    assert [f["issue"] for f in out] == ["glama_listing_tool_count_drift"]
    assert "73 tools" in out[0]["detail"] or "advertises 73" in out[0]["detail"]


def test_no_badge_never_reads_as_drift():
    """A page Glama could not introspect has no count to disagree with. Making
    that a drift finding would double-report the duplicate for one cause."""
    out = glp.findings([_row("keeper", glp.KEEP, tools_badge=None)], canon_tools=83)
    assert [f["issue"] for f in out] == []


# ── the quiet cases, which are the ones that decide whether anyone reads it ──

def test_unreachable_is_silent_not_a_finding():
    """Glama being down, or rate-limiting us, is not DC Hub being broken.
    FAIL-SAFE on transport."""
    rows = [{"key": "keeper", "url": "u", "disposition": glp.KEEP,
             "has_health_badge": True, "reachable": False}]
    assert glp.findings(rows, canon_tools=83) == []


def test_a_connector_page_we_could_not_parse_fires_rather_than_passing():
    """FAIL-CLOSED on parse. A probe that cannot read the badge must not
    report the listing healthy — could-not-run is not ran-and-passed."""
    out = glp.findings([_row("keeper", glp.KEEP, status=None, status_found=False)],
                       canon_tools=83)
    assert [f["issue"] for f in out] == ["glama_listing_unparseable"]


def test_the_repo_listing_has_no_health_badge_and_stays_quiet():
    """/mcp/servers/… renders no Status block by design. Reporting that as a
    layout break would put a permanent red on a page that is working."""
    out = glp.findings([_row("server_repo_listing", glp.KEEP, badge_page=False,
                             status=None, status_found=False)], canon_tools=83)
    assert out == []


def test_retired_copy_alone_never_fires():
    """Glama's cached summary and AI review are not ours to fix. Firing daily
    on text no commit can change is how a monitor gets muted."""
    out = glp.findings(
        [_row("keeper", glp.KEEP,
              retired_phrases=["21,000+", "232 markets", "82 tools"])],
        canon_tools=83)
    assert out == []


# ── wiring ──────────────────────────────────────────────────────────────────

def test_probe_takes_an_injectable_fetcher_and_covers_all_three_listings():
    seen = []

    def fake_fetch(url):
        seen.append(url)
        return _connector_page()

    rows = glp.probe(fetch=fake_fetch)
    assert len(rows) == len(glp.GLAMA_LISTINGS) == 3
    assert len(seen) == 3
    assert all(r["reachable"] for r in rows)


def test_probe_survives_a_fetcher_that_returns_nothing():
    rows = glp.probe(fetch=lambda url: None)
    assert all(r["reachable"] is False for r in rows)
    assert glp.findings(rows, canon_tools=83) == []


def test_exactly_one_listing_is_marked_for_deprecation():
    dispositions = [d for _, _, d, _ in glp.GLAMA_LISTINGS]
    assert dispositions.count(glp.DEPRECATE) == 1
    assert dispositions.count(glp.KEEP) == 2


def test_the_remediation_names_the_action_glama_actually_offers():
    """Glama's owner UI offers DEPRECATION for a claimed connector, not
    deletion. A finding that tells a human to click a button that is not there
    reads as a broken monitor, and the real action never gets taken."""
    out = glp.findings([_row("dupe", glp.DEPRECATE, status="Unhealthy")],
                       canon_tools=83)
    detail = out[0]["detail"]
    assert "DEPRECATE" in detail
    assert "delete this connector" not in detail


# ── the origin half: a deprecation only sticks if this is closed ────────────

def test_the_email_ownership_form_fires():
    out = glp.ownership_finding(
        fetch=lambda u: '{"maintainers":[{"email":"x@y.z"}]}')
    assert [f["issue"] for f in out] == ["glama_origin_publishes_email_ownership"]
    assert "GLAMA_CLAIM_TOKEN" in out[0]["detail"]


def test_a_valid_claim_token_is_silent():
    tok = "glama_claim_" + "a" * 32
    assert glp.ownership_finding(fetch=lambda u: '{"claim":"%s"}' % tok) == []


def test_a_malformed_claim_still_fires_and_says_so():
    """A typo'd claim is not a claim — and it is the failure that looks fixed."""
    out = glp.ownership_finding(fetch=lambda u: '{"claim":"glama_claim_short"}')
    assert [f["issue"] for f in out] == ["glama_origin_publishes_email_ownership"]
    assert "does not match Glama's pattern" in out[0]["detail"]


def test_our_endpoint_being_down_is_silent_not_a_finding():
    assert glp.ownership_finding(fetch=lambda u: None) == []


def test_unparseable_ownership_doc_fires_rather_than_passing():
    out = glp.ownership_finding(fetch=lambda u: "<html>not json</html>")
    assert [f["issue"] for f in out] == ["glama_ownership_doc_unparseable"]


# ── the price of the stale-count skip ───────────────────────────────────────
#
# routes/glama_listing_probe.py is in STALE_SCAN_SKIP_FILES
# (tests/test_canonical_counts_drift.py) for the same reason as
# ai_surface_canon.py and canon_floor.py: it IS a denylist, and scanning a
# denylist for the values it lists would demand the ban list stop naming what
# it bans.
#
# ★ A file skip is the fail-open direction, so — following the canon_floor.py
# precedent exactly — the skip does not rest on that argument alone. This test
# is STRICTER than the scan it replaces: the repo scan matches a fixed set of
# retired tokens, while this walks the module and asserts that EVERY
# count-shaped literal in it, retired or not, sits in a docstring or in
# RETIRED_COUNT_PHRASES. Nothing carrying a count can reach an emitted string.

_COUNT_SHAPE = re.compile(
    r"(?<!\{)\b\d{1,3},\d{3}\+?|\b\d{1,4}\s+tools?\b|\b232\b")


def _docstring_line_ranges(tree) -> list[range]:
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None) or []
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            d = body[0]
            out.append(range(d.lineno, (d.end_lineno or d.lineno) + 1))
    return out


def _retired_phrase_line_range(tree) -> range:
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "RETIRED_COUNT_PHRASES":
                return range(node.lineno, (node.end_lineno or node.lineno) + 1)
    raise AssertionError("RETIRED_COUNT_PHRASES assignment not found — the "
                         "stale-count skip for this module is unjustified")


def test_no_count_literal_escapes_the_docstrings_or_the_phrase_list():
    src_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "routes", "glama_listing_probe.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    allowed = _docstring_line_ranges(tree) + [_retired_phrase_line_range(tree)]

    escaped = []
    for i, line in enumerate(src.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue  # comments explain; they are not emitted
        for m in _COUNT_SHAPE.finditer(line):
            if not any(i in r for r in allowed):
                escaped.append(f"  line {i}: {m.group(0)!r} in {line.strip()[:90]!r}")
    assert not escaped, (
        "count-shaped literal(s) outside a docstring and outside "
        "RETIRED_COUNT_PHRASES. This module is skipped by the repo-wide "
        "stale-count scan on the grounds that it only NAMES retired values as "
        "detection data — a count reaching an emitted string breaks that "
        "argument and the skip with it:\n" + "\n".join(escaped))


def test_that_guard_can_actually_see_a_count():
    """A scanner that matches nothing passes vacuously. Assert the shape
    recognises the forms this module traffics in."""
    for sample in ("21,000+", "33 tools", "1 tool", "232"):
        assert _COUNT_SHAPE.search(sample), sample
    # ...and does NOT read a regex quantifier as a count. Without this the
    # guard flagged its own `.{0,400}?` anchors, which is the kind of noise
    # that gets a guard deleted rather than fixed.
    assert not _COUNT_SHAPE.search("re.compile(r\"x.{0,400}?y\")")
