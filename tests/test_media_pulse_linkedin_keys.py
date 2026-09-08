"""Every reader of components.linkedin must key on something the writer emits.

2026-09-08. The LinkedIn tile of /api/v1/media/pulse read "quiet, score 0.0"
while the channel was publishing 6 posts/7d (live: published_7d=6,
published_24h=1, hours_since_last_publish=16.5). Nothing errored.

The producer's keys were renamed to published_24h / published_7d on 2026-08-15,
when the counter moved off auto_press_releases.linkedin_sent_at (the press
cross-post path) onto linkedin_quad_posts (the publisher's own table). Three
consumers were never updated, and each had drifted to a DIFFERENT wrong
spelling:

    routes/media_organism.py       sent_7d   / sent_24h
    routes/brain_ownership_loop.py sent_7d
    routes/morning_briefing.py     count_7d  / count_24h

`.get()` on an absent key returns None, `int(None or 0)` is 0, and 0 scores as
"quiet". So the organism published weakest_channel="linkedin@0.0", the ownership
loop proposed "strengthen weakest channel: linkedin", and the morning brief
reported "Posts published yesterday: 0" — every day, on a healthy channel.

This guard binds to the writer at RUNTIME (it calls the pure verdict function
and reads the keys back) rather than restating a key list here, so a future
rename moves the expectation with it instead of leaving a stale copy behind.
"""
import ast
import pathlib
import pytest

from routes.dchub_media_revival import linkedin_publisher_verdict

REPO = pathlib.Path(__file__).resolve().parents[1]

# The writer's own output, obtained by calling it — not transcribed.
EMITTED = set(linkedin_publisher_verdict({}).keys())

# file -> the expression whose .get(...) calls read the linkedin component.
CONSUMERS = {
    "routes/media_organism.py": "li",
    "routes/brain_ownership_loop.py": None,       # inline (p.get("linkedin") or {})
    "routes/morning_briefing.py": "linkedin",
}


def _keys_read_from(path: str, varname: str | None) -> set[str]:
    """Collect the literal keys any `<var>.get("k")` reads in the file.

    For the inline case (varname None) we look for .get("k") applied to an
    expression that mentions "linkedin", which is how brain_ownership_loop
    spells it.
    """
    tree = ast.parse((REPO / path).read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            continue
        recv = node.func.value
        if varname is not None:
            if isinstance(recv, ast.Name) and recv.id == varname:
                found.add(node.args[0].value)
        else:
            if "linkedin" in ast.unparse(recv):
                found.add(node.args[0].value)
    return found


def test_writer_emits_the_published_counters():
    """Anchor: if the writer stops emitting these, the guard below goes vacuous."""
    assert "published_7d" in EMITTED
    assert "published_24h" in EMITTED


@pytest.mark.parametrize("path,var", sorted(CONSUMERS.items()))
def test_consumer_keys_exist_on_the_writer(path, var):
    read = _keys_read_from(path, var)
    assert read, f"{path}: found no .get() reads — the extractor lost its target"
    unknown = read - EMITTED
    assert not unknown, (
        f"{path} reads {sorted(unknown)} from components.linkedin, which "
        f"/api/v1/media/pulse never emits. It emits {sorted(EMITTED)}. "
        "An absent key resolves to 0 and scores as 'quiet' — silence, not an error."
    )
