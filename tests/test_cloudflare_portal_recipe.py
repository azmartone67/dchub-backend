"""Pins /integrations/cloudflare — the Cloudflare MCP Server Portal recipe.

Three things this page can silently lose, each of which has bitten a sibling:

1. THE HONESTY BLOCK. The page's whole reason to exist is that it documents
   Cloudflare's own limits on portal-based access — a blocked user can still
   reach an upstream by its direct URL, and independent MFA / purpose
   justification / temporary authentication are NOT enforced for MCP servers
   reached through a portal — plus the fact that OAuth-backed upstreams are
   excluded from service-token (headless agent) sessions. A guide that quietly
   drops those is worse than no guide, and nothing else in CI would notice.
   Equally load-bearing in the other direction: the page must NEVER claim a
   Cloudflare partnership or affiliation. We are compatible, not affiliated.

2. CANON BINDING. The tool count and the server version render from
   ai_surface_canon, never a fresh literal — routes/integrations_landing.py is
   NOT in tests/test_canonical_counts_drift.AGENT_CODE_SURFACES, so a hardcoded
   "83 tools" / "2.12.3" ships green and then rots. Same rule the Copilot Studio
   page follows (tests/test_copilot_studio_recipe.py).

3. THE SHARED CHROME. _recipe_page() fills 14 slots by literal __UPPERCASE__
   replacement with no escaping and no error on a missing slot, so a typo'd or
   omitted slot ships the literal token "__EXTRA_HTML__" to a crawler. The slug
   slot alone drives og:url, rel=canonical and the JSON-LD url, so a wrong slug
   mis-canonicalises the page with no other symptom.

Pure functions: no DB, no network, and never imports main (tests/ must not).
"""
import ast
import json
import pathlib
import re

import pytest

il = pytest.importorskip("routes.integrations_landing")


def _html():
    return il.CLOUDFLARE_PORTAL_RECIPE_HTML


def test_the_cloudflare_form_is_walked_field_by_field():
    html = _html()
    # The live "Add a server" form, as measured 2026-09-02.
    assert "Enter the full URL of the remote server" in html
    assert "Route traffic through Cloudflare Gateway" in html
    assert "https://dchub.cloud/mcp" in html
    assert "Streamable HTTP" in html
    assert "2025-06-18" in html
    # Dashboard labels vs API values — the form says OAuth / Custom headers /
    # None where the API takes oauth / bearer / unauthenticated.
    for label in ("OAuth", "Custom headers", "None"):
        assert label in html, label
    for api_value in ("<code>oauth</code>",
                      "<code>bearer</code>",
                      "<code>unauthenticated</code>"):
        assert api_value in html, api_value


def test_oauth_automatic_is_recommended_with_the_dcr_reason():
    html = _html()
    assert "Automatic (recommended)" in html
    assert "Manual credentials" in html
    assert "Dynamic Client Registration" in html
    assert "registration_endpoint" in html
    assert "S256" in html
    # The verified result, not a claim: a dch_oauth_ key rather than key=none.
    assert "dch_oauth_" in html
    assert "key=none" in html


def test_the_documented_limits_are_not_quietly_dropped():
    """The three limitations the honesty rules make non-negotiable."""
    html = _html()
    low = html.lower()
    # (a) the direct-URL bypass
    assert "directly by its\n    own URL" in html or "directly by its own URL" in html
    # (b) MFA / purpose justification / temporary authentication not enforced
    assert "purpose justification" in low
    assert "temporary authentication" in low
    assert "not enforced" in low
    # (c) OAuth upstreams are excluded from service-token sessions
    assert "service-token" in low
    assert "mutually exclusive" in low
    # (d) an invalid key is indistinguishable from no key
    assert "invalid key looks exactly like no key" in low


def test_context_optimization_modes_are_named_and_discouraged():
    html = _html()
    assert "minimize_tools" in html
    assert "search_and_execute" in html
    # The reason, not just the verdict: the contracts live in the schemas.
    assert "constraint_coverage" in html
    assert "coverage_ratio" in html


def test_the_anonymous_baseline_is_the_measured_one():
    html = _html()
    assert "search_facilities" in html
    assert "_data_total_in_pro" in html
    assert "upgrade_url" in html


def test_claim_free_key_caveat_is_present():
    """The shared template's free-tier pane tells the reader to call
    claim_free_key. Behind a portal the credential is admin-set, so that advice
    is wrong there — the page must say so, on the same page, above the pane."""
    html = _html()
    assert "claim_free_key" in html
    assert "administrator" in html
    idx_caveat = html.find("cannot rewrite portal configuration")
    idx_pane = html.find("Free tier — works with no key at all")
    assert idx_caveat != -1, "the claim_free_key caveat is gone"
    assert idx_pane != -1, "the shared free-tier pane is gone"
    assert idx_caveat < idx_pane, (
        "the caveat must appear before the pane it contradicts")


def test_no_affiliation_or_partnership_claim():
    """DC Hub is compatible with Cloudflare, not affiliated with it."""
    html = _html()
    low = html.lower()
    for banned in ("partnership", "partner with", "in partnership",
                   "official cloudflare", "endorsed by cloudflare,",
                   "cloudflare partner"):
        assert banned not in low, f"affiliation claim on the page: {banned!r}"
    # And the disclaimer is stated outright.
    assert "not affiliated with, sponsored by or endorsed" in low
    # The JSON-LD stays a plain DC Hub SoftwareApplication block. Naming the
    # target platform in alternateName/url is what every sibling does ("DC Hub
    # for Amazon Bedrock AgentCore") and is not a claim; asserting a Cloudflare
    # ORGANIZATION, brand or sponsor into structured data would be, because a
    # search engine reads those as a declared relationship.
    ld_raw = html.split('<script type="application/ld+json">')[1].split("</script>")[0]
    ld = json.loads(ld_raw)
    assert ld["@type"] == "SoftwareApplication"
    assert ld["provider"] == {"@type": "Organization",
                             "name": "DC Hub",
                             "url": "https://dchub.cloud"}
    for relationship_key in ("brand", "sponsor", "publisher", "parentOrganization",
                             "memberOf", "affiliation", "isPartOf"):
        assert relationship_key not in ld, (
            f"structured data declares a {relationship_key} relationship")
    # Cloudflare may only appear as a name/URL string, never as an entity.
    for key, value in ld.items():
        if isinstance(value, dict) and "cloudflare" in json.dumps(value).lower():
            raise AssertionError(f"Cloudflare appears as an entity under {key!r}")


def test_tool_count_and_version_are_canon_bound_not_fresh_literals():
    canon = pytest.importorskip("ai_surface_canon")
    html = _html()
    nums = canon.canon_nums()
    tools = nums.get("{canon_tools}")
    version = nums.get("{canon_version}")
    if tools:
        assert f"<b>{tools} tools</b>" in html
    if version:
        assert f"<code>{version}</code>" in html
    # Canon-resolved, so no raw placeholder may survive to the wire. That is
    # the failure ai_surface_canon.canon_text's docstring calls the worst one.
    assert not re.search(r"\{canon_[a-z_]+\}", html), (
        "unresolved canon placeholder served to an agent")


def _cloudflare_block_source():
    """The source text of the CLOUDFLARE_PORTAL_RECIPE_HTML assignment.

    Rendered-output checks cannot tell a canon placeholder from a literal that
    happens to equal canon today — "83 tools" and "{canon_tools} tools" render
    identically while canon says 83. Only the SOURCE distinguishes them, which
    is the whole point of the binding.
    """
    path = pathlib.Path(il.__file__)
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name)
                        and t.id == "CLOUDFLARE_PORTAL_RECIPE_HTML"
                        for t in node.targets)):
            lines = text.splitlines()
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError("CLOUDFLARE_PORTAL_RECIPE_HTML assignment not found")


def test_the_source_uses_canon_placeholders_not_typed_numbers():
    src = _cloudflare_block_source()
    assert "{canon_tools}" in src, (
        "the tool count must be written as {canon_tools}, not typed")
    assert "{canon_version}" in src, (
        "the server version must be written as {canon_version}, not typed")
    # And no typed tool count anywhere in the block, whatever the digits.
    # Same shape as tests/test_canonical_counts_drift.TOOL_COUNT_RE — the
    # lookbehind must NOT require a space, or "<b>83 tools</b>" slips past
    # (measured: it did, on the first draft of this assertion).
    typed = re.findall(r"(?<![\d,{])(\d{1,3})\s+(?:live\s+|MCP\s+)?tools\b", src)
    assert typed == [], f"typed tool counts in the source: {typed}"
    # A typed serverInfo version is the same rot in a different shape: the
    # artifact this page was adapted from said 2.12.3 while canon says 2.12.1.
    typed_versions = re.findall(r"(?<![\w.])2\.\d+\.\d+", src)
    assert typed_versions == [], f"typed version literals: {typed_versions}"


def test_every_recipe_slot_was_filled():
    """_recipe_page does literal __UPPERCASE__ replacement and does not error on
    a missing slot — it just ships the token. Nothing else in CI catches it."""
    html = _html()
    leftovers = sorted(set(re.findall(r"__[A-Z0-9_]+__", html)))
    assert leftovers == [], f"unfilled recipe slots: {leftovers}"


def test_slug_drives_canonical_og_url_and_jsonld_url():
    html = _html()
    url = "https://dchub.cloud/integrations/cloudflare"
    assert f'<link rel="canonical" href="{url}">' in html
    assert f'<meta property="og:url" content="{url}">' in html
    assert f'"url": "{url}"' in html


def test_shared_chrome_is_inherited_not_reimplemented():
    html = _html()
    # brand.css, one <head>, the endpoint urlbox, the injected front door and
    # the footer link row all come from _RECIPE_PAGE_TEMPLATE. A page that
    # ported the standalone artifact's own <style>/<head> would double these.
    assert html.count("<html lang=\"en\">") == 1
    assert html.count("dchub-brand.css") == 1
    assert 'id="front-door"' in html
    assert "execute_plan" in html
    assert 'onclick="copyUrl()"' in html


def test_the_route_is_registered_with_strict_slashes_off():
    """Without strict_slashes=False, /integrations/cloudflare/ is claimed by
    main.py's legacy /integrations/<platform>/ package handler and answers a
    JSON 404. Verified live 2026-09-02 before this change shipped."""
    rules = [r for r in il.integrations_landing_bp.deferred_functions]
    assert rules, "blueprint has no deferred route registrations"
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(il.integrations_landing_bp)
    matched = [r for r in app.url_map.iter_rules()
               if r.rule == "/integrations/cloudflare"]
    assert len(matched) == 1, f"expected exactly one rule, got {matched}"
    assert matched[0].strict_slashes is False
    assert "GET" in matched[0].methods
