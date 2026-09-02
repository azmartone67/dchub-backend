"""Robots.txt Content Signals + discovery-surface Content-Type fences.

Two guards, both added 2026-09-02 for defects measured live:

1. CONTENT SIGNALS PER GROUP. Per RFC 9309 a crawler obeys ONLY its single
   most specific matching group and inherits nothing from `User-agent: *`, so
   a `Content-Signal` line must be repeated in every group or it is void for
   those bots -- the same rule the file's own comments record learning the hard
   way about the hygiene Disallows.

   ★ This fence exists because writing the fix broke it. Placing the directive
   after `User-agent: GrokBot` -- mid-way through a 22-UA run -- TERMINATED the
   user-agent list and split one group in two, orphaning Googlebot, GoogleOther,
   CCBot, Bytespider and the whole xAI alias set from every rule in the group.
   Nothing about the rendered file looks wrong; only parsing it as a crawler
   does reveals it. Hence: parse, don't grep.

2. NO DOUBLED CHARSET. `Response(..., mimetype='text/plain; charset=utf-8')`
   double-appends, because Werkzeug adds the charset to a text/* mimetype
   itself -- shipping `text/plain; charset=utf-8; charset=utf-8`, a malformed
   header served alongside `nosniff` on the most agent-facing files on the site.
   `content_type=` sets the header verbatim and is the correct parameter.
"""
import ast
import re
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SIGNAL_RE = re.compile(r"search=(?:yes|no), ai-input=(?:yes|no), ai-train=(?:yes|no)")

# UAs that must stay inside the named AI-crawler group. Regression anchors: each
# one silently lost its rules when the directive was inserted mid-list.
MUST_BE_GROUPED = (
    "GPTBot", "ClaudeBot", "PerplexityBot", "GrokBot",
    "xAI-Grok", "Bytespider", "CCBot", "Googlebot", "GoogleOther",
)


def _robots_body() -> str:
    """Extract the robots.txt literal from its route without importing Flask."""
    src = (REPO / "ai_discovery_routes.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "serve_robots_txt":
            for const in ast.walk(node):
                if (
                    isinstance(const, ast.Constant)
                    and isinstance(const.value, str)
                    and const.value.startswith("User-agent: *")
                ):
                    return const.value
    raise AssertionError("robots.txt body not found in serve_robots_txt()")


def _parse_groups(body: str):
    """Group the file the way RFC 9309 says a crawler does.

    A run of consecutive `User-agent:` lines shares one rule set; the first
    non-user-agent directive ends the run and begins that group's rules.
    """
    groups, uas, rules = [], [], []
    for line in body.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        key, _, value = text.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if rules:                     # rules seen => previous group closed
                groups.append((uas, rules))
                uas, rules = [], []
            uas.append(value)
        else:
            rules.append((key, value))
    if uas or rules:
        groups.append((uas, rules))
    return [(u, r) for u, r in groups if u]


def test_every_group_carries_content_signal():
    groups = _parse_groups(_robots_body())
    assert groups, "no user-agent groups parsed"
    missing = [uas[0] for uas, rules in groups
               if not any(k == "content-signal" for k, _ in rules)]
    assert not missing, (
        f"groups without Content-Signal (void for those crawlers): {missing}"
    )


def test_content_signal_values_well_formed():
    for uas, rules in _parse_groups(_robots_body()):
        for key, value in rules:
            if key == "content-signal":
                assert SIGNAL_RE.fullmatch(value), f"{uas[0]}: malformed {value!r}"


def test_named_crawler_group_not_split_by_the_directive():
    """The directive must sit BELOW the last User-agent line, not inside the run."""
    groups = _parse_groups(_robots_body())
    named = [(u, r) for u, r in groups if "GPTBot" in u]
    assert len(named) == 1, "the named AI-crawler group was split into several groups"
    uas, rules = named[0]
    for agent in MUST_BE_GROUPED:
        assert agent in uas, (
            f"{agent} fell out of the named group -- it now inherits no rules at all"
        )
    assert sum(1 for k, _ in rules if k == "disallow") >= 3, (
        "named group lost its hygiene Disallows"
    )


def test_ai_input_stays_enabled():
    """Guard the business decision, not just the syntax.

    Assistant citation is the acquisition channel; a future edit flipping this
    to `ai-input=no` would disclaim it. Change this test deliberately or not
    at all.
    """
    for uas, rules in _parse_groups(_robots_body()):
        for key, value in rules:
            if key == "content-signal":
                assert "ai-input=yes" in value, f"{uas[0]} disclaims ai-input"


@pytest.mark.parametrize(
    "path", ["ai_discovery_routes.py", "routes/agents_md_fallback.py"]
)
def test_no_mimetype_with_embedded_charset(path):
    """`mimetype=` must not carry parameters -- Werkzeug appends its own."""
    src = (REPO / path).read_text(encoding="utf-8")
    offenders = re.findall(r"mimetype\s*=\s*['\"]text/[^'\"]*charset[^'\"]*['\"]", src)
    assert not offenders, (
        f"{path}: mimetype= with an embedded charset double-appends "
        f"(serves 'charset=utf-8; charset=utf-8'); use content_type= instead: {offenders}"
    )
