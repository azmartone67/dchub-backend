"""A client-config block must never name `streamable-http` as its transport.

#4264 fixed /connect, which hand-wrote three MCP client configs and got all
three wrong. #4282 generalised that to the five Flask surfaces this repo
RENDERS. This guard is the second generalisation: to the STATIC files it
SERVES, where 32 more of the same defect were sitting untouched — because
#4282's globs (`routes/*.py`, `static/*.html`, `worker.js`) never reached
`static/integrations/`, `static/.well-known/`, `registries/` or the repo-root
markdown. Found by extending the equivalent frontend guard repo-wide
(dchub-frontend #1433).

★ THE DISTINCTION THIS GUARD ENCODES — and it is the whole point, because a
blind find-and-replace of `streamable-http` would have been wrong on 89 of the
121 occurrences that existed repo-wide when this was written:

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

★★ THE WINDOW IS DIRECTIONAL, AND THAT IS LOAD-BEARING.

`static/.well-known/ai-agents.json` carries BOTH classes, ten lines apart:

    line 18   mcp.transport                      = streamable-http   CORRECT
    line 25   mcp.client_config.mcpServers
    line 28   …mcpServers.dchub.transport        = streamable-http   DEFECT

The marker (`mcpServers`, line 25) sits inside a SYMMETRIC ±10 window of both,
so a symmetric window flags line 18 too — and the obvious way to make CI green
is to "fix" a value that was right, which breaks the registry surfaces. A
config block always appears AFTER the key that opens it, so looking only
BACKWARD from the match separates them: line 28 sees `mcpServers`, line 18 does
not. `test_the_window_is_directional_not_symmetric` pins this by running the
real scanner over a synthetic probe with that same adjacency — mutation-verified
to fail when the window is made symmetric again.

★★ `"dchub"` IS NOT A DISCRIMINATOR, though #4282 used it as one. Over that
guard's three narrow globs it was harmless; repo-wide it is DC Hub's own name
sitting next to every self-description, and it flags 20 correct occurrences
(`registries/anthropic-quickstarts.json:5`, `REGISTRY_SUBMISSIONS.md:109`, the
`PATCHES/` proxies…). Only the keys that OPEN a client-config container —
`mcpServers` and `servers` — actually discriminate.

This repo holds NO copy of that canon to check a block against, which is
precisely why hand-writing one here keeps going wrong. The durable fix is that
these surfaces LINK to the generated per-client pages (`/install/<client>`,
derived from canon) instead of restating a shape; this guard stops the specific
value from coming back.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent

# Surfaces this repo serves to a human who might paste from them.
# ★ The static trees and the root markdown are the #4282 gap: that guard's
# globs stopped at `static/*.html`, so `static/integrations/**` (24 defects),
# `static/.well-known/`, `static/AGENTS.md`, `registries/`, `github-repo/` and
# the root docs were all unscanned. Widen this rather than adding a sibling
# guard: two copies of one predicate drift, and only one of them gets updated.
SCAN_GLOBS = (
    "routes/*.py",
    "worker.js",
    "*.html",
    "*.md",
    "static/**/*.html",
    "static/**/*.json",
    "static/**/*.md",
    "registries/*.json",
    "github-repo/**/*.md",
)

# Vendored/superseded trees are not surfaces we serve. `PATCHES/` holds frozen
# copies of retired worker builds kept for diffing; they are not deployed.
SKIP_SEGMENTS = ("node_modules", ".git", "__pycache__", "PATCHES", ".venv")

# The forbidden value, in a client-config key. Whitespace-flexible; matches
# both `"transport": "streamable-http"` and `"type": "streamable-http"`, in
# JSON and in Python/JS source that emits JSON.
FORBIDDEN = re.compile(
    r"""["']?(?:transport|type)["']?\s*:\s*["']streamable-http["']""")

# The same defect in Claude Code's CLI form, which the JSON regex cannot see.
# Canon is `--transport http`. Two live occurrences were fixed with this guard
# (`github-repo/README.md`, `static/integrations/grok/README.md`).
FORBIDDEN_CLI = re.compile(r"--transport\s+streamable-http")

# What makes a block a CLIENT CONFIG rather than a self-description: the key
# that OPENS the container a client keys on. `mcpServers` (Claude Desktop,
# Cursor, Cline, Windsurf, Gemini CLI, Antigravity) or `servers` (VS Code).
# NOT `"dchub"` — see the module docstring.
CONFIG_MARKERS = ("mcpServers", '"servers"', "'servers'")

# How far BACK from the forbidden value we look for a config marker. Directional
# on purpose — see the module docstring. The blocks that were live when this was
# written put the marker 1-6 lines above; 10 is slack.
WINDOW = 10


def _files():
    seen, out = set(), []
    for g in SCAN_GLOBS:
        for p in sorted(REPO.glob(g)):
            rel = p.relative_to(REPO).as_posix()
            if any(seg in rel.split("/") for seg in SKIP_SEGMENTS):
                continue
            if p.is_file() and rel not in seen:
                seen.add(rel)
                out.append(p)
    return out


def _violations():
    bad = []
    for path in _files():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            if FORBIDDEN_CLI.search(line):
                bad.append(
                    f"{path.relative_to(REPO)}:{i + 1}: {line.strip()[:120]}")
                continue
            if not FORBIDDEN.search(line):
                continue
            # ★ BACKWARD only. A symmetric window flags the correct
            # self-description in static/.well-known/ai-agents.json:18.
            window = "\n".join(lines[max(0, i - WINDOW):i + 1])
            if any(m in window for m in CONFIG_MARKERS):
                bad.append(
                    f"{path.relative_to(REPO)}:{i + 1}: {line.strip()[:120]}")
    return bad


# ── FLOOR ────────────────────────────────────────────────────────────────────
# Every assertion below is "X is absent", and absence is also what an emptied
# repo, a renamed directory or a broken glob looks like. These pin that the
# scan actually reached content before its silence means anything.
#
# ★ THE scan_floors.json PIN DOES NOT COVER THE NEW GLOBS, and that is not an
# oversight to "fix" by raising it. That mechanism records the MAX single scan
# per test file; here that is `routes/*.py` at 838, which swamps every static
# glob (the largest is `static/**/*.json` at 48). So `static/integrations/`
# could stop being scanned entirely and the pinned floor would not move — the
# precise shape of the #4282 gap this guard exists to close.
#
# The explicit assertions below are therefore the real coverage floor for the
# static surfaces. Mutation-verified: reverting SCAN_GLOBS to #4282's narrow set
# fails `test_scan_actually_reads_the_surfaces`, NOT the scan_floors pin.

def test_scan_actually_reads_the_surfaces():
    files = _files()
    rels = {p.relative_to(REPO).as_posix() for p in files}
    assert len(files) >= 300, (
        f"expected the served-surface globs to match a repo-sized set, got "
        f"{len(files)} — the glob is broken, so 'no violations' means nothing")
    assert "worker.js" in rels, "worker.js not scanned"
    assert sum(1 for p in files if p.suffix == ".py") >= 150, "routes/ not scanned"
    # ★ The #4282 gap. If these stop being scanned, the 32 defects this guard
    # was written for can come back green.
    assert sum(1 for r in rels if r.startswith("static/integrations/")) >= 20, (
        "static/integrations/ not scanned — this is the tree #4282's globs "
        "missed and where 24 of the 32 defects lived")
    for must in ("static/.well-known/ai-agents.json", "static/AGENTS.md",
                 "registries/anthropic-quickstarts.json", "github-repo/README.md",
                 "REGISTRY_SUBMISSIONS.md", "ai-hub-standalone.html"):
        assert must in rels, f"{must} not scanned — it carried a live defect"


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
    assert FORBIDDEN_CLI.search(
        "claude mcp add dchub --transport streamable-http https://dchub.cloud/mcp")
    # And it must NOT match canon's legitimate client values, or fixing a
    # surface to canon would trip the guard that demanded the fix.
    for ok in (
        '"transport": "http",',
        '"type": "streamableHttp",',
        '"type": "http",',
        '"httpUrl": "https://dchub.cloud/mcp"',
        '"serverUrl": "https://dchub.cloud/mcp"',
    ):
        assert not FORBIDDEN.search(ok), f"regex over-matches canon: {ok!r}"
    assert not FORBIDDEN_CLI.search(
        "claude mcp add dchub --transport http https://dchub.cloud/mcp")


def test_self_description_occurrences_are_still_present_and_allowed():
    """The discriminator must be doing real work, not passing everything.

    `streamable-http` SHOULD still appear as DC Hub's own transport in the
    capability card, the .well-known descriptors and every registry submission.
    If these vanish, someone ran a blind find-and-replace and broke the registry
    surfaces — which is the other half of this defect, and the half a naive fix
    causes. 25 such occurrences sit inside the scanned set (89 repo-wide,
    counting `PATCHES/` and the Python modules outside SCAN_GLOBS — do not
    conflate the two populations, the floor below is the scanned-set one). The
    floor is ~20% under so retiring one file does not manufacture a red build.
    """
    seen = 0
    for rel in ("routes/agent_capabilities_feed.py",
                "routes/mcp_tool_catalog.py",
                "routes/mcp_registry_outreach.py",
                "static/.well-known/ai-agents.json",
                "registries/anthropic-quickstarts.json",
                "REGISTRY_SUBMISSIONS.md"):
        text = (REPO / rel).read_text(encoding="utf-8", errors="replace")
        if FORBIDDEN.search(text):
            seen += 1
    assert seen == 6, (
        f"expected all 6 self-description surfaces to still name "
        f"streamable-http as DC Hub's own transport, found {seen} — a blind "
        f"replace stripped the correct occurrences along with the wrong ones")

    total = 0
    for path in _files():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines):
            if not FORBIDDEN.search(line):
                continue
            window = "\n".join(lines[max(0, i - WINDOW):i + 1])
            if not any(m in window for m in CONFIG_MARKERS):
                total += 1
    assert total >= 20, (
        f"only {total} self-description occurrences left in the scanned set "
        f"(25 when written) — a blind find-and-replace stripped the correct "
        f"spelling from our own published manifests")


def test_the_window_is_directional_not_symmetric():
    """★REGRESSION. The two classes coexist 10 lines apart in one file.

    static/.well-known/ai-agents.json holds `mcp.transport` (self-description,
    CORRECT) at line 18 and `mcp.client_config.mcpServers…transport` (a real
    defect, now fixed) at line 28, with the `mcpServers` marker between them at
    line 25. A SYMMETRIC ±10 window sees the marker from BOTH and flags the
    correct one — inviting someone to "fix" it and break the registry surfaces.

    ★ This asserts against `_violations()` ITSELF, on a synthetic file, not
    against the shape of ai-agents.json. An earlier draft only checked that the
    real file's line 18 had no marker behind it — which stayed GREEN when the
    implementation was mutated back to a symmetric window (mutation D). It was
    describing the data, not testing the code. Binding it to the real scanner
    means a symmetric window fails HERE, by name, instead of surfacing as a
    confusing false positive somewhere else in the repo.
    """
    probe = REPO / "static" / "_directional_window_probe.json"
    # A self-description FOLLOWED by a config container 4 lines later: the exact
    # adjacency from ai-agents.json, which a symmetric window cannot separate.
    probe.write_text(
        '{\n'
        '  "name": "DC Hub MCP Server",\n'
        '  "type": "remote-mcp-server",\n'
        '  "transport": "streamable-http",\n'      # line 4 — CORRECT
        '  "url": "https://dchub.cloud/mcp",\n'
        '  "client_config": {\n'
        '    "mcpServers": {\n'                    # line 7 — marker, AFTER
        '      "dchub": {"url": "https://dchub.cloud/mcp", "transport": "http"}\n'
        '    }\n'
        '  }\n'
        '}\n', encoding="utf-8")
    try:
        flagged = [v for v in _violations() if probe.name in v]
        assert not flagged, (
            "the scanner flagged a self-description whose only config marker "
            "sits AFTER it — the window has become symmetric, and the next "
            "person to see this will 'fix' a value that was correct and break "
            "our registry surfaces:\n  " + "\n  ".join(flagged))

        # ...and the marker really is inside a SYMMETRIC window, so this probe
        # is exercising the difference rather than passing for a trivial reason.
        lines = probe.read_text(encoding="utf-8").splitlines()
        hit = next(i for i, l in enumerate(lines) if FORBIDDEN.search(l))
        sym = "\n".join(lines[max(0, hit - WINDOW):min(len(lines), hit + WINDOW + 1)])
        assert any(m in sym for m in CONFIG_MARKERS), (
            "the probe no longer places a marker inside a symmetric window, so "
            "it would pass even with the bug present — rebuild the probe")
    finally:
        probe.unlink(missing_ok=True)


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
