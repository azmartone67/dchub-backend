"""Every published reach row states which vendor it collapses to.

`distinct_platforms_basis` instructs the reader to "recompute either from
per_platform[] to check". Until now they could not: the collapse lives in
ai_platform_canon and the payload shipped only raw client ids. Measured
2026-09-07 the live payload carried 16 client ids and 577 requests beside
distinct_platforms=3, with nothing connecting them.

The helper is executed against the REAL live shape, not asserted about.
"""
import ast
import json
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "routes" / "ai_reach.py"
CANON = pathlib.Path(__file__).resolve().parents[1] / "ai_platform_canon.py"

# The exact rows /api/v1/ai/reach published on 2026-09-07.
LIVE_ROWS = [
    {"platform_id": "mcp-generic-client", "agents": 5, "requests": 211},
    {"platform_id": "anthropic/api", "agents": 16, "requests": 165},
    {"platform_id": "actionist-apps-verification", "agents": 2, "requests": 83},
    {"platform_id": "claude", "agents": 19, "requests": 47},
    {"platform_id": "connectors-manager", "agents": 11, "requests": 41},
    {"platform_id": "claude-ai", "agents": 1, "requests": 8},
    {"platform_id": "claude-code", "agents": 2, "requests": 5},
    {"platform_id": "codex", "agents": 1, "requests": 5},
    {"platform_id": "mcp-spec-study", "agents": 1, "requests": 3},
    {"platform_id": "chain-hire", "agents": 1, "requests": 2},
    {"platform_id": "robinsaige-verifier", "agents": 1, "requests": 2},
    {"platform_id": "grok", "agents": 1, "requests": 1},
    {"platform_id": "northwind-agent", "agents": 1, "requests": 1},
    {"platform_id": "glama-user-sim", "agents": 1, "requests": 1},
    {"platform_id": "anthropic/claudeai", "agents": 1, "requests": 1},
    {"platform_id": "smithery-cli", "agents": 1, "requests": 1},
]


def _load():
    """Execute _stamp_vendor with the real canon bound, nothing else."""
    canon_ns: dict = {}
    exec(compile(CANON.read_text(encoding="utf-8"), str(CANON), "exec"), canon_ns)
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "_stamp_vendor"), None)
    assert fn is not None, "_stamp_vendor not found in routes/ai_reach.py"
    ns = {"canonical_platform": canon_ns["canonical_platform"]}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)
    return ns["_stamp_vendor"], canon_ns


def test_distinct_platforms_is_reproducible_from_the_payload_alone():
    """The load-bearing one: the published instruction must actually work."""
    stamp, canon = _load()
    rows = json.loads(json.dumps(LIVE_ROWS))
    stamp(rows)
    from_payload = len({r["canonical_vendor"] for r in rows
                        if r.get("canonical_vendor")})
    from_canon = canon["count_platforms"](r["platform_id"] for r in LIVE_ROWS)
    assert from_payload == from_canon == 3, (
        f"a reader recomputing from per_platform[] gets {from_payload}, the "
        f"server publishes {from_canon}")


def test_the_claude_family_collapses_and_is_visible_as_such():
    stamp, _ = _load()
    rows = json.loads(json.dumps(LIVE_ROWS))
    stamp(rows)
    claude = {r["platform_id"] for r in rows if r.get("canonical_vendor") == "claude"}
    assert {"claude", "claude-ai", "claude-code",
            "anthropic/api", "anthropic/claudeai"} <= claude, claude


def test_codex_is_visible_as_openai_not_dropped():
    """codex was invisible until 2026-09-05 and only looked counted because
    chatgpt happened to be calling in the same window."""
    stamp, _ = _load()
    rows = json.loads(json.dumps(LIVE_ROWS))
    stamp(rows)
    codex = next(r for r in rows if r["platform_id"] == "codex")
    assert codex["canonical_vendor"] == "openai", codex


def test_unrecognised_rows_are_counted_not_silently_absent():
    stamp, _ = _load()
    rows = json.loads(json.dumps(LIVE_ROWS))
    summary = stamp(rows)
    assert summary["unrecognised_client_ids"] == 9, summary
    assert summary["unrecognised_requests"] == 345, summary
    assert summary["unrecognised_basis"], "no basis published for the excluded set"


def test_every_row_carries_the_key_even_when_it_is_null():
    """A missing key and a null vendor are different claims; a consumer must
    not have to guess which one an absent field means."""
    stamp, _ = _load()
    rows = json.loads(json.dumps(LIVE_ROWS))
    stamp(rows)
    assert all("canonical_vendor" in r for r in rows)
    assert any(r["canonical_vendor"] is None for r in rows)


def test_it_never_raises_when_the_canon_is_unavailable():
    """reach is fail-soft by contract — it must never 5xx on canon import."""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_stamp_vendor")
    ns = {"canonical_platform": None}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)
    rows = json.loads(json.dumps(LIVE_ROWS))
    summary = ns["_stamp_vendor"](rows)
    assert all(r["canonical_vendor"] is None for r in rows)
    assert summary["unrecognised_client_ids"] == len(rows)
