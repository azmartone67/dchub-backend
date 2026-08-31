"""/robots.txt crawl-hygiene guard (2026-07-28).

This file has now silently broken TWICE in ways a "does robots.txt exist" check
could never catch:

  1. 2026-07-16 — the whole file served a blank 204 (edge cache-poisoning via the
     internal-UA short-circuit). The directives were correct and reached nobody.
  2. 2026-07-28 — the directives reached crawlers and were still void: the named
     UA groups (GPTBot, Googlebot, Bingbot, …) each carried a bare "Allow: /",
     and per RFC 9309 a crawler obeys ONLY its single most specific matching
     group and ignores "User-agent: *" entirely. Measured that day: bingbot
     spent 20% of its crawl budget on /sites/* and 2% on /cdn-cgi/*, while only
     24% reached /facilities/*.

So the property worth pinning is NOT "the file contains the string
'Disallow: /sites/'" — a comment satisfies that grep. It is "for every crawler
this file names, is the never-rankable surface actually unreachable, and does
the content surface actually stay reachable?" Those are can_fetch verdicts, so
that is what this file asserts.

The recurrence guard is test_every_named_group_carries_hygiene_rules: it
enumerates the UA tokens declared in the file itself rather than a hardcoded
list, so adding a new crawler group with a bare "Allow: /" fails the build.

CI-SAFETY: the unit-tests job installs ONLY pytest. The robots payload is read
straight out of the source literal via ast (it is returned verbatim, so that IS
the served bytes) and the matcher below is a self-contained RFC 9309 evaluator
— no flask, no protego, nothing to skip. When flask IS importable an extra test
proves the served response body equals that literal, closing the gap between
"what the literal says" and "what the route returns".
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTES = os.path.join(ROOT, "ai_discovery_routes.py")

# Surfaces that must stay crawlable for every crawler the file names.
MUST_ALLOW = [
    "/",
    "/pricing",
    "/facilities/raxio-raxio-kinshasa-6c5dec12",
    "/markets/ashburn-va",
    "/dcpi/dallas",
    "/sites",            # the real "Site Capacity Report" landing page
    "/sites/",           # ditto, trailing slash — kept alive by "Allow: /sites/$"
]

# Surfaces that can never rank and must not consume crawl budget.
MUST_BLOCK = [
    "/sites/advania-sland-ehf-ice01-bfb7adf7",
    "/sites/raxio-raxio-kinshasa-6c5dec12",
    "/cdn-cgi/l/email-protection",
    "/cdn-cgi/trace",
    # 2026-08-08 — the named groups used to omit these two hygiene Disallows that
    # the "*" group has always carried, so per RFC 9309 they were void for every
    # crawler this file names (Googlebot could crawl ?cb= duplicates and the
    # /admin ops shells). Both must now be blocked for EVERY group.
    "/facilities/edgecore-mesa-ph01-54e3fad5?cb=99123",   # parameterized dup — /*? sink
    "/markets/ashburn-va?utm_source=x&utm_medium=y",       # tracking-param dup
    "/admin",             # bare ops shell ("Disallow: /admin/" alone misses it)
    "/admin-qa",          # internal bug inventory (sibling of /admin, not a child)
    "/admin-outreach",    # outreach templates/strategy
]


# ── RFC 9309 evaluator (self-contained; no third-party parser in CI) ─────────

def _parse_groups(text):
    """-> [(set_of_ua_tokens_lowercased, [(is_allow, pattern), ...]), ...]"""
    groups, uas, rules, collecting_uas = [], set(), [], False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if not collecting_uas and uas:      # rules ended -> previous group closes
                groups.append((uas, rules))
                uas, rules = set(), []
            uas.add(value.lower())
            collecting_uas = True
        elif field in ("allow", "disallow"):
            collecting_uas = False
            if uas:
                rules.append((field == "allow", value))
    if uas:
        groups.append((uas, rules))
    return groups


def _pattern_to_regex(pattern):
    out, anchor = [], False
    if pattern.endswith("$"):
        pattern, anchor = pattern[:-1], True
    for ch in pattern:
        out.append(".*" if ch == "*" else re.escape(ch))
    return re.compile("".join(out) + (r"\Z" if anchor else ""))


def can_fetch(robots_text, user_agent, path):
    """RFC 9309 / Google semantics: most-specific UA group wins; within it the
    longest matching pattern wins; on an equal-length tie Allow wins."""
    groups = _parse_groups(robots_text)
    ua = user_agent.lower()
    chosen = None
    for uas, rules in groups:
        for token in uas:
            if token != "*" and token in ua:
                if chosen is None or len(token) > chosen[0]:
                    chosen = (len(token), rules)
    if chosen is None:
        for uas, rules in groups:
            if "*" in uas:
                chosen = (0, rules)
                break
    if chosen is None:
        return True

    best_len, best_allow = -1, True
    for is_allow, pattern in chosen[1]:
        if not pattern:
            continue
        if _pattern_to_regex(pattern).match(path):
            weight = len(pattern.rstrip("$"))
            if weight > best_len or (weight == best_len and is_allow):
                best_len, best_allow = weight, is_allow
    return best_allow if best_len >= 0 else True


# ── payload ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def robots():
    """The exact string serve_robots_txt() returns, lifted via ast."""
    with open(ROUTES, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "serve_robots_txt":
            for stmt in ast.walk(node):
                if (isinstance(stmt, ast.Assign)
                        and getattr(stmt.targets[0], "id", None) == "content"
                        and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)):
                    return stmt.value.value
    pytest.fail("serve_robots_txt() no longer assigns a literal `content` string")


@pytest.fixture(scope="module")
def named_agents(robots):
    """Every UA token the file names, minus the wildcard."""
    tokens = sorted({
        ua for uas, _ in _parse_groups(robots) for ua in uas if ua != "*"
    })
    assert tokens, "robots.txt names no crawlers — did the groups get dropped?"
    return tokens


# ── 1 · the evaluator itself is honest ──────────────────────────────────────

def test_evaluator_matches_spec_on_known_cases():
    sample = "User-agent: *\nDisallow: /x/\nAllow: /x/$\nUser-agent: bot\nAllow: /\n"
    assert can_fetch(sample, "other", "/x/") is True        # Allow wins the tie
    assert can_fetch(sample, "other", "/x/deep") is False   # longest match blocks
    assert can_fetch(sample, "other", "/y") is True         # unmatched -> allowed
    assert can_fetch(sample, "bot", "/x/deep") is True      # named group ignores *


# ── 2 · content surfaces stay reachable ─────────────────────────────────────

def test_content_surfaces_crawlable_by_every_named_agent(robots, named_agents):
    for ua in named_agents + ["SomeUnknownCrawler"]:
        for path in MUST_ALLOW:
            assert can_fetch(robots, ua, path) is True, (
                f"{ua} can no longer crawl {path} — this deindexes real pages"
            )


# ── 3 · the recurrence guard ────────────────────────────────────────────────

def test_every_named_group_carries_hygiene_rules(robots, named_agents):
    """Enumerated from the file, not hardcoded: a new crawler group added with a
    bare "Allow: /" inherits nothing from "User-agent: *" and fails here."""
    for ua in named_agents:
        for path in MUST_BLOCK:
            assert can_fetch(robots, ua, path) is False, (
                f"{ua} may crawl {path}: its group is missing the hygiene "
                f"Disallow. Named groups do NOT inherit from 'User-agent: *' "
                f"(RFC 9309) — repeat the rule inside the group."
            )


def test_wildcard_group_also_blocks_the_sinks(robots):
    for path in MUST_BLOCK:
        assert can_fetch(robots, "SomeUnknownCrawler", path) is False


def test_parameterized_hygiene_does_not_block_the_clean_canonical(robots, named_agents):
    """/*? must sink the ?cb=/utm= duplicate WITHOUT taking its clean twin down —
    the clean canonical path (no query string) has to stay crawlable for every
    crawler, or the hygiene rule would deindex the real page it protects."""
    clean_twins = ["/facilities/edgecore-mesa-ph01-54e3fad5", "/markets/ashburn-va"]
    for ua in named_agents + ["SomeUnknownCrawler"]:
        for path in clean_twins:
            assert can_fetch(robots, ua, path) is True, (
                f"{ua} lost the clean canonical {path} — /*? over-reached onto "
                f"the very page it exists to keep rankable"
            )


# ── 4 · /api/* is deliberately still open to the assistant crawlers ─────────

def test_api_open_to_bingbot_and_the_other_assistant_crawlers(robots):
    """Pins the 2026-08-31 REOPEN so it changes on purpose, not by accident.

    This test previously pinned the opposite. /api/* was closed to Bingbot on
    2026-07-28 because it took 36.4% of the crawl budget (raw JSON, 1 in 3
    sampled paths 404) against 24.3% reaching /facilities/*, during a Bing
    "limited crawl capacity" period. The accepted cost was Copilot, which
    crawls as Bingbot and has no other surface.

    Reopened by owner decision. Copilot is the point, and in five weeks nothing
    measured whether Bing organic improved in exchange — the trade was made on a
    prediction and left unchecked.

    The budget protection that actually mattered stays: `Disallow: /*?` closes
    every parameterized URL, which is where the 404s lived. See
    test_bingbot_still_refuses_the_query_string_long_tail below.

    Still asymmetric in the other direction: an unknown crawler gets nothing.
    """
    assert can_fetch(robots, "bingbot", "/api/v1/facilities/foo") is True
    assert can_fetch(robots, "Googlebot", "/api/v1/facilities/foo") is True
    assert can_fetch(robots, "GoogleOther", "/api/v1/facilities/foo") is True
    assert can_fetch(robots, "GPTBot", "/api/v1/facilities/foo") is True
    assert can_fetch(robots, "SomeUnknownCrawler", "/api/v1/facilities/foo") is False


def test_bingbot_still_refuses_the_query_string_long_tail(robots):
    """Reopening /api/ must NOT hand back the junk that made it a sink.

    The 2026-07-28 measurement found 1 in 3 sampled /api/ paths 404ing. Those
    live behind query strings, and `Disallow: /*?` closes them for this group
    independently of the /api/ rule. If that line is ever dropped, the crawl
    budget problem returns with the reopen and this fails."""
    for path in ("/api/v1/facilities?page=99",
                 "/api/v1/search?q=junk&offset=5000",
                 "/facilities?sort=power&dir=desc"):
        assert can_fetch(robots, "bingbot", path) is False, (
            f"bingbot regained {path} — the query-string guard that made "
            f"reopening /api/ safe has been removed")


def test_bingbot_keeps_every_content_surface(robots):
    """Whatever the /api/ rule is, Bing must never lose a rankable page. Written
    when /api/ was closed; still the right assertion now that it is open."""
    for path in MUST_ALLOW + ["/facilities/edgecore-mesa-ph01-54e3fad5", "/grid/ercot"]:
        assert can_fetch(robots, "bingbot", path) is True, (
            f"bingbot lost {path} — the /api/ split over-reached"
        )


# ── 5 · the literal is what the route actually returns ──────────────────────

def test_served_body_equals_the_literal(robots):
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from flask import Flask
    from ai_discovery_routes import register_discovery_routes

    app = Flask(__name__)
    register_discovery_routes(app)
    resp = app.test_client().get("/robots.txt")
    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == robots
    assert resp.mimetype == "text/plain"
