#!/usr/bin/env python3
"""tests/test_worker_tool_examples_run.py — every worked example in a tool
description must be callable against that tool's own inputSchema.

worker.js is what serves tools/list, so its `description` string and its
`inputSchema` are the two halves of the contract an agent reads. They sit in
the same object literal and drifted apart on four tools:

    list_transactions  "Try: list_transactions year=2026 …"   schema: date_from/date_to
    search_facilities  "… min_mw=10 status=operational"       schema: min_capacity_mw, no status
    get_news           "Try: get_news topic=AI limit=10"      schema: category
    get_pipeline       "get_pipeline market=northern-virginia" schema: country

An agent cannot recover from this by trying harder — it copies the example and
gets rejected, or worse, gets an unfiltered answer it believes was filtered.
The `list_transactions year=2026` example is also reproduced in DC Hub's MCP
server instructions, so it was being taught to every agent on connect.

This parses the ACTUAL tool objects out of worker.js and asserts that every
`name=value` pair appearing in an example call names a real schema property.

Run standalone:   python3 tests/test_worker_tool_examples_run.py
Run under pytest: pytest tests/test_worker_tool_examples_run.py
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "worker.js")

# `{ name: "x", description: "...", inputSchema: {...} },`
TOOL_RE = re.compile(
    r'\{\s*name:\s*"(?P<name>[a-z_0-9]+)"\s*,\s*'
    r'description:\s*"(?P<desc>(?:[^"\\]|\\.)*)"\s*,\s*'
    r'inputSchema:\s*(?P<schema>\{.*?\})\s*\}\s*,\s*\n',
    re.DOTALL,
)
ARG = re.compile(r"([a-z][a-z0-9_]{2,30})\s*=")


def _tools():
    src = open(WORKER, encoding="utf-8").read()
    out = []
    for m in TOOL_RE.finditer(src):
        try:
            schema = json.loads(m.group("schema"))
        except json.JSONDecodeError:
            continue
        desc = json.loads('"' + m.group("desc") + '"')
        out.append((m.group("name"), desc, set((schema.get("properties") or {}).keys())))
    return out


def _example_args(desc, name):
    """Argument names used in an example invocation of THIS tool."""
    found = set()
    pattern = re.escape(name) + r"((?:\s+[a-z][a-z0-9_]*\s*=\s*[^\s,;.)]+){1,8})"
    for m in re.finditer(pattern, desc):
        found |= {a.group(1) for a in ARG.finditer(m.group(1))}
    for m in re.finditer(re.escape(name) + r"\s*\(([^)]{0,300})\)", desc):
        found |= {a.group(1) for a in ARG.finditer(m.group(1))}
    return found


def test_worker_tool_definitions_are_parseable():
    tools = _tools()
    assert len(tools) >= 60, (
        f"only parsed {len(tools)} tool definitions from worker.js — the regex has "
        f"drifted from the file format, so every assertion below would vacuously pass"
    )


def test_every_example_call_uses_real_parameters():
    broken = []
    for name, desc, keys in _tools():
        if not keys:
            continue
        phantom = sorted(_example_args(desc, name) - keys)
        if phantom:
            broken.append(f"{name}: example passes {phantom} but schema accepts {sorted(keys)}")
    assert not broken, (
        "tool descriptions advertise parameters their schema rejects:\n  "
        + "\n  ".join(broken)
    )


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"✓ {_name}")
            except AssertionError as _e:
                _failed += 1
                print(f"✗ {_name}: {_e}")
    print(f"\n{'FAILED' if _failed else 'PASSED'} — {_failed} failure(s)")
    sys.exit(1 if _failed else 0)
