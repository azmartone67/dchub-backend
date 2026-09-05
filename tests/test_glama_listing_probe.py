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
    out = glp.findings([_row("dupe", glp.DELETE, status="Unhealthy",
                             tools_badge=None, retired_phrases=["33 tools"])],
                       canon_tools=83)
    assert [f["issue"] for f in out] == ["glama_duplicate_connector_listed"]
    detail = out[0]["detail"]
    # The finding has to carry BOTH halves of the fix. Deleting the card
    # without replacing the email-form ownership file lets it come back.
    assert "delete this connector" in detail
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


def test_exactly_one_listing_is_marked_for_deletion():
    dispositions = [d for _, _, d, _ in glp.GLAMA_LISTINGS]
    assert dispositions.count(glp.DELETE) == 1
    assert dispositions.count(glp.KEEP) == 2
