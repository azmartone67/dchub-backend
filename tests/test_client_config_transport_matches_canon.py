"""A client-config block must never name `streamable-http` as its transport.

#4264 fixed /connect, which hand-wrote three MCP client configs and got all
three wrong. This guard is the generalisation of that fix to every surface this
repo SERVES, because the same defect was live on four more of them.

★ THE DISTINCTION THIS GUARD ENCODES — and it is the whole point, because a
blind find-and-replace of `streamable-http` would have been wrong on five of
the ten occurrences that existed when this was written:

  `streamable-http` IS a real MCP transport name, and it is the CORRECT value
  when DC Hub describes ITSELF — a registry submission, a capability card, a
  `.well-known` descriptor. Our own published manifests use exactly that
  spelling (dchub-mcp-server `server.json` remotes[0].type, `mcp-server.json`
  transport, `smithery.yaml` type, `glama.json` transport). Those occurrences
  are correct and this guard leaves them alone.

  It is NEVER the value in a CLIENT CONFIG — the block a human pastes into
  Claude Desktop, Cursor, Cline, VS Code, Windsurf, Gemini CLI or Antigravity.
  Canon is dchub-mcp-server `persist_config.clients`, the same snippet set
  `claim_free_key` hands agents, and measured against it:

      claude_desktop   "transport": "http"
      claude_code      --transport http
      cursor           (no transport key at all)
      vscode           "type": "http"       (and nests under `servers`)
      cline            "type": "streamableHttp"   ← camelCase, a DIFFERENT token
      windsurf         (no transport key; `serverUrl`)
      gemini_cli       (no transport key; `httpUrl`)
      antigravity      (no transport key; `serverUrl`)

  `streamable-http` appears ZERO times in that set. A wrong transport does not
  fail loudly — it is a connector that silently does not load.

This repo holds NO copy of that canon to check a block against, which is
precisely why hand-writing one here keeps going wrong. The durable fix is that
these surfaces LINK to the generated per-client pages instead of restating a
shape; this guard stops the specific value from coming back.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# Surfaces this repo serves to a human who might paste from them.
SCAN_GLOBS = ("routes/*.py", "static/*.html", "worker.js")

# The forbidden value, in a client-config key. Whitespace-flexible; matches
# both `"transport": "streamable-http"` and `"type": "streamable-http"`, in
# JSON and in Python/JS source that emits JSON.
FORBIDDEN = re.compile(
    r"""["']?(?:transport|type)["']?\s*:\s*["']streamable-http["']""")

# What makes a block a CLIENT CONFIG rather than a self-description: it names
# the server entry a client would key on. `mcpServers` (Claude Desktop, Cursor,
# Cline, Windsurf, Gemini CLI, Antigravity), `servers` (VS Code), or the
# `dchub` server-entry key itself.
CONFIG_MARKERS = ("mcpServers", '"servers"', "'servers'", '"dchub"', "'dchub'")

# How far from the forbidden value we look for a config marker. The blocks that
# were live when this was written put the marker 1-3 lines away; 10 is slack.
WINDOW = 10


def _files():
    out = []
    for g in SCAN_GLOBS:
        out.extend(sorted(REPO.glob(g)))
    return [p for p in out if p.is_file()]


def _violations():
    bad = []
    for path in _files():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            if not FORBIDDEN.search(line):
                continue
            lo, hi = max(0, i - WINDOW), min(len(lines), i + WINDOW + 1)
            window = "\n".join(lines[lo:hi])
            if any(m in window for m in CONFIG_MARKERS):
                bad.append(
                    f"{path.relative_to(REPO)}:{i + 1}: {line.strip()[:120]}")
    return bad


# ── FLOOR ────────────────────────────────────────────────────────────────────
# Every assertion below is "X is absent", and absence is also what an emptied
# repo, a renamed directory or a broken glob looks like. These pin that the
# scan actually reached content before its silence means anything.

def test_scan_actually_reads_the_surfaces():
    files = _files()
    assert len(files) >= 200, (
        f"expected the served-surface globs to match a repo-sized set, got "
        f"{len(files)} — the glob is broken, so 'no violations' means nothing")
    assert any(p.name == "worker.js" for p in files), "worker.js not scanned"
    assert sum(1 for p in files if p.suffix == ".py") >= 150, "routes/ not scanned"


def test_the_forbidden_pattern_still_matches_the_shape_it_is_meant_to_catch():
    """The regex is the guard. If it stops matching, the guard is vacuous."""
    for probe in (
        '"transport": "streamable-http",',
        '"transport":    "streamable-http",',
        "'transport': 'streamable-http',",
        '      "type": "streamable-http",',
        'transport: "streamable-http"',
    ):
        assert FORBIDDEN.search(probe), f"regex no longer matches: {probe!r}"
    # And it must NOT match canon's legitimate client values, or fixing a
    # surface to canon would trip the guard that demanded the fix.
    for ok in (
        '"transport": "http",',
        '"type": "streamableHttp",',
        '"type": "http",',
    ):
        assert not FORBIDDEN.search(ok), f"regex over-matches canon: {ok!r}"


def test_self_description_occurrences_are_still_present_and_allowed():
    """The discriminator must be doing real work, not passing everything.

    `streamable-http` SHOULD still appear as DC Hub's own transport in the
    capability card and the .well-known descriptors. If these vanish, someone
    ran a blind find-and-replace and broke the registry surfaces — which is the
    other half of this defect, and the half a naive fix causes.
    """
    seen = 0
    for rel in ("routes/agent_capabilities_feed.py",
                "routes/mcp_tool_catalog.py",
                "routes/mcp_registry_outreach.py"):
        text = (REPO / rel).read_text(encoding="utf-8", errors="replace")
        if FORBIDDEN.search(text):
            seen += 1
    assert seen == 3, (
        f"expected all 3 self-description surfaces to still name "
        f"streamable-http as DC Hub's own transport, found {seen} — a blind "
        f"replace stripped the correct occurrences along with the wrong ones")


# ── THE GUARD ────────────────────────────────────────────────────────────────

def test_no_client_config_block_names_streamable_http_as_its_transport():
    bad = _violations()
    assert not bad, (
        "A client config block names `streamable-http` as its transport. "
        "That value appears ZERO times in canon "
        "(dchub-mcp-server persist_config.clients) and a wrong transport is a "
        "connector that silently does not load.\n\n"
        + "\n".join(bad)
        + "\n\nThis repo holds no copy of that canon. Do not hand-write a "
          "corrected block here — link the generated per-client page "
          "(/install/<client>), which IS derived from canon."
    )
