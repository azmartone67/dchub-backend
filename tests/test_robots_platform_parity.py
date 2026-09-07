"""Every platform we RECOGNISE must be NAMED in robots.txt.

★ THE FAILURE IS SILENT BY CONSTRUCTION. Per RFC 9309 a crawler obeys only its
single most specific matching group and inherits nothing, so a platform absent
from the named AI group falls through to "User-agent: *" — which carries
`Disallow: /api/`, the surface the assistant crawlers actually fetch. The
traffic keeps arriving, so nothing looks broken; the platform is simply served
a stricter policy than every named peer.

That happened to You.com (1.34K reach/7d while unnamed) and was caught by hand.
Measured again 2026-09-07, 11 of 21 recognised platforms were still unnamed,
including deepseek (3,107 requests all-time) and cursor (601).

These tests read the SHIPPED sources — the robots.txt body out of
ai_discovery_routes.py and AI_PLATFORMS out of ai_tracking.py — with `ast`,
because importing either pulls in flask and a DB pool.
"""
import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
ROUTES = ROOT / "ai_discovery_routes.py"
TRACKING = ROOT / "ai_tracking.py"


def _robots_body() -> str:
    """The robots.txt string literal actually served by serve_robots_txt()."""
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "serve_robots_txt":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                        and "User-agent:" in sub.value:
                    return sub.value
    raise AssertionError("serve_robots_txt's robots.txt body not found")


def _named_groups(body: str):
    return [ln.split(":", 1)[1].strip()
            for ln in body.splitlines()
            if ln.startswith("User-agent:") and ln.split(":", 1)[1].strip() != "*"]


def _platforms() -> dict:
    src = TRACKING.read_text(encoding="utf-8")
    m = re.search(r"^AI_PLATFORMS\s*=\s*\{", src, re.M)
    assert m, "AI_PLATFORMS not found in ai_tracking.py"
    start = m.start()
    end = src.index("\n}", start) + 2
    ns: dict = {}
    exec(compile(src[start:end], str(TRACKING), "exec"), ns)
    return ns["AI_PLATFORMS"]


def _covers(named: str, agent: str) -> bool:
    """robots.txt matches when the named product token appears in the crawler's
    UA. Compared both ways so 'mistralai' and 'MistralAI-User' count as a match.
    Case-insensitive: RFC 9309 says UA matching ignores case."""
    a, b = named.lower(), agent.lower()
    return a in b or b in a


def test_every_recognised_platform_is_named():
    named = _named_groups(_robots_body())
    unnamed = {}
    for key, cfg in _platforms().items():
        agents = cfg.get("agents") or []
        if not agents:
            continue
        if not any(_covers(n, a) for n in named for a in agents):
            unnamed[key] = agents
    assert not unnamed, (
        "these platforms are counted by AI_PLATFORMS but have no robots.txt "
        "group, so they fall to 'User-agent: *' and are served Disallow: /api/ "
        f"— a stricter policy than every named peer: {unnamed}")


def test_the_wildcard_group_really_is_stricter():
    """The whole risk depends on '*' restricting something the named group
    allows. If that ever stops being true this guard is theatre, so assert the
    asymmetry rather than assuming it."""
    body = _robots_body()
    star = body.split("User-agent:")[1]
    assert star.lstrip().startswith("*"), "expected '*' to be the first group"
    assert "Disallow: /api/" in star, (
        "the '*' group no longer restricts /api/ — re-check whether an unnamed "
        "platform is still disadvantaged before relaxing this suite")


def _group_directives(body: str, ua: str):
    """The directives belonging to the group `ua` sits in — from the last
    User-agent line of that run to the NEXT User-agent line, exclusive.

    ★ The first version of this helper read to END OF FILE, which swept through
    the Bingbot and PetalBot groups that follow. PetalBot legitimately carries
    `Disallow: /api/`, so the test reported the AI group as blocking /api/ when
    it does not. A group boundary is a real boundary — read to the next
    User-agent line, not to EOF."""
    idx = body.index(f"User-agent: {ua}")
    after = body.index("\n", idx) + 1
    nxt = body.find("\nUser-agent:", after)
    block = body[after:] if nxt == -1 else body[after:nxt]
    return [ln.strip() for ln in block.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def test_named_group_keeps_api_open():
    """Parity only means something if the named group is actually permissive.
    The AI group must not disallow /api/ — otherwise naming a platform would
    restrict it rather than free it, and this whole suite argues backwards."""
    body = _robots_body()
    directives = _group_directives(body, "webmcp")
    assert directives, "no directives found for the named AI group"
    for d in directives:
        if d.startswith("Disallow:") and d.split(":", 1)[1].strip() in ("/api/", "/api"):
            raise AssertionError(
                f"the named AI group disallows /api/: {d!r} — naming a "
                "platform would restrict it rather than free it")
    assert "Allow: /" in directives, (
        "the named AI group has no blanket Allow: / — check the policy really "
        "is more permissive than the wildcard before trusting this suite")


def test_the_asymmetry_is_real_not_assumed():
    """The premise in one assertion: '*' restricts /api/, the AI group does
    not. If this ever flips, being unnamed stops being a disadvantage and the
    parity test above is arguing for nothing."""
    body = _robots_body()
    star = _group_directives(body, "*")
    ai = _group_directives(body, "webmcp")
    star_blocks = any(d.startswith("Disallow:")
                      and d.split(":", 1)[1].strip() in ("/api/", "/api")
                      for d in star)
    ai_blocks = any(d.startswith("Disallow:")
                    and d.split(":", 1)[1].strip() in ("/api/", "/api")
                    for d in ai)
    assert star_blocks and not ai_blocks, (
        f"expected '*' to block /api/ and the AI group not to; "
        f"got star_blocks={star_blocks} ai_blocks={ai_blocks}")


def test_content_signal_is_repeated_for_the_named_group():
    """RFC 9309: a named group inherits nothing. The Content-Signal line must
    appear again below the named UA run or it is void for every platform here —
    and it must sit AFTER the last User-agent line, since a non-UA directive
    terminates the run and would orphan the UAs below it."""
    body = _robots_body()
    # Check the AI group's OWN directives. An earlier version asserted
    # `"Content-Signal:" in body[last_ua_line:]` and
    # `body.count("Content-Signal:") >= 2`, both of which were satisfied by the
    # Bingbot and PetalBot groups further down — deleting the AI group's copy
    # left the test green. Scope the assertion to the group it is about.
    for group in ("*", "webmcp"):
        directives = _group_directives(body, group)
        assert any(d.startswith("Content-Signal:") for d in directives), (
            f"the '{group}' group has no Content-Signal of its own; per RFC "
            "9309 it inherits nothing, so the signal is void for it")


def test_no_platform_is_fully_blocked():
    body = _robots_body()
    for line in body.splitlines():
        assert line.strip() != "Disallow: /", (
            "a group blocks the whole site; the door must stay open")
