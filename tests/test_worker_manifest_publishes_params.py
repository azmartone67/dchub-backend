#!/usr/bin/env python3
"""tests/test_worker_manifest_publishes_params.py — the edge manifest must
publish the argument NAMES it already has.

`resolveManifestTools()` fetches the live `tools/list` — the real source of
truth, inputSchema and all — and then slimmed it to `{name, description}`,
discarding every schema. So `https://dchub.cloud/.well-known/mcp.json` served
83 tool names and no way to call one. That is the document registries and AI
crawlers scan, and the only tool surface readable WITHOUT opening an MCP
session.

★ WHY IT COSTS CALLS. Measured live 2026-09-05, anonymous free tier, same
session seconds apart:

    get_market_intel(market_slug: "dallas")  -> NO market data. Zod strips the
        undeclared argument, `market` is empty, the tier gate fires and answers
        with an upgrade envelope + for_your_human relay link.
    get_market_intel(market: "dallas")       -> _gated:false, 372 facilities.

The agent then tells its human DC Hub charges for data it returns free.
`params: ["market", ...]` is the one line that would have prevented it. Same
family as the four drifted examples in test_worker_tool_examples_run.py.

★ AND THE PATH HAS TWO BUILDERS. dchub-backend #3959 added a per-tool `example`
to the ORIGIN manifest and verified it live at the Railway origin (83/83). The
public edge URL still served 0/83, because the worker builds its own manifest
here and only merges a whitelist of origin keys. Measured the same minute:

    origin  84,825 b  tools[0] keys {name, tier, description, example}
    edge   131,170 b  tools[0] keys {name, description}

Fixing the origin alone fixed a document the public path does not serve.

★ THE KV KEY IS PART OF THE FIX. The cached value's SHAPE changed, and the
reader accepts any non-empty array — so a surviving v1 entry would keep serving
the param-less shape for a full hour after the paste, and a stale cache that
satisfies the freshness check is indistinguishable from a working one. Shape
change and key bump must land together; this file asserts they did.
"""
import json
import os
import re
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "worker.js")


@pytest.fixture(scope="module")
def src():
    with open(SRC, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def slim_expr(src):
    """The mapping applied to the live tools/list, pulled out of worker.js."""
    m = re.search(r"const slim = (t\.map\(x => \(\{.*?\}\)\));", src, re.S)
    assert m, "could not find the `const slim = t.map(...)` mapping in worker.js"
    return m.group(1)


def _run_mapping(expr, tools):
    """Evaluate the real mapping against a tools/list fixture, in node."""
    js = "const t = %s;\nconsole.log(JSON.stringify(%s));" % (json.dumps(tools), expr)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0, f"node failed: {out.stderr[:400]}"
    return json.loads(out.stdout)


# Shaped exactly like the live tools/list entries this mapping consumes.
FIXTURE = [
    {"name": "get_market_intel",
     "description": "market intel",
     "inputSchema": {"type": "object", "properties": {
         "market": {"type": "string"}, "metric": {"type": "string"},
         "period": {"type": "string"}, "compare_to": {"type": "string"}}}},
    {"name": "search_facilities",
     "description": "facility search",
     "inputSchema": {"type": "object", "properties": {
         "country": {"type": "string"}, "min_capacity_mw": {"type": "number"}}}},
    # A genuinely parameterless tool must survive, not crash the mapping.
    {"name": "get_backup_status", "description": "status", "inputSchema": {}},
]


def test_the_mapping_publishes_declared_parameter_names(slim_expr):
    """The defect: the schema was fetched and thrown away."""
    out = {t["name"]: t for t in _run_mapping(slim_expr, FIXTURE)}
    assert "params" in out["get_market_intel"], (
        "the manifest still publishes no parameter names, so an agent reading "
        "it cannot construct a call")
    assert out["get_market_intel"]["params"] == [
        "market", "metric", "period", "compare_to"]


def test_it_teaches_market_not_market_slug(slim_expr):
    """The measured case. market_slug is the name of the VALUE; passing it as
    the argument is silently stripped and returns no market data."""
    out = {t["name"]: t for t in _run_mapping(slim_expr, FIXTURE)}
    params = out["get_market_intel"]["params"]
    assert "market" in params, f"must publish the real argument: {params}"
    assert "market_slug" not in params


def test_a_parameterless_tool_still_publishes(slim_expr):
    """An empty or absent inputSchema must yield [], not an exception."""
    out = {t["name"]: t for t in _run_mapping(slim_expr, FIXTURE)}
    assert out["get_backup_status"]["params"] == []


def test_name_and_description_are_still_published(slim_expr):
    """Adding a key must not drop the two the manifest already contracted."""
    out = _run_mapping(slim_expr, FIXTURE)
    for t in out:
        assert t.get("name") and "description" in t, f"lost a key: {t}"


def test_the_kv_key_was_bumped_with_the_shape(src):
    """A surviving v1 entry would serve the param-less shape for a full TTL."""
    assert "'mcp:manifest-tools-v2'" in src, (
        "the cached shape changed but MANIFEST_TOOLS_KV_KEY did not — a stale "
        "v1 entry satisfies the reader's non-empty check and silently keeps "
        "serving tools with no params")
    assert "'mcp:manifest-tools'" not in src, (
        "the old KV key is still referenced somewhere; both shapes would be live")
