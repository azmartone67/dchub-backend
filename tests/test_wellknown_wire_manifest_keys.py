"""The wire-level /.well-known/mcp.json responders must publish the canon keys.

Regression guarded (2026-07-31): `anchor_intents` (07-28) and
`problem_taxonomy` (07-31, PR #2072) were added to `_canonical_mcp_manifest()`
— the `@app.route('/.well-known/mcp.json')` builder — and every check passed.
But that route is SHADOWED: the before_request hook intercepts the path first
(documented in its own r68.1 comment) and serves a different, older dict.
Neither key ever reached the wire, on the Railway origin or through the CF
zone worker (which builds its own copy of the surface). Verified live
2026-07-31: both dchub.cloud and the origin served a manifest without either
key, while every helper-level test was green.

So this file pins the RESPONDERS, not the helpers:

  1. the before_request wire dict (the one with "tiers") carries both keys,
     valued by calls to the fail-open helpers — not literal transcriptions;
  2. the @app.route builder dict (the one with "protocol_version") still
     carries them, same discipline;
  3. worker.js merges the same two keys from the origin manifest into its
     /.well-known/mcp.json response (whitelist + fail-open spread).

Pure functions: no DB, no network, and never imports main (tests/ must not).
main.py is read with `ast` — an empty/failed parse must FAIL here, never pass
vacuously.
"""
import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

WIRE_KEYS = ("anchor_intents", "problem_taxonomy")
HELPERS = {
    "anchor_intents": "_canonical_anchor_intents",
    "problem_taxonomy": "_canonical_problem_taxonomy",
}


def _main_tree():
    src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # An empty parse passes every for-loop assertion — refuse it loudly.
    assert len(tree.body) > 100, "main.py parsed suspiciously small"
    return tree


def _dict_key_names(d):
    return {k.value for k in d.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _manifest_dicts(tree):
    """Both /.well-known/mcp.json manifest dicts, identified structurally: the
    wire (before_request) dict is the only anchor_intents-bearing dict with a
    "tiers" key; the @app.route builder's is the only one with
    "protocol_version"."""
    carriers = [d for d in ast.walk(tree)
                if isinstance(d, ast.Dict) and "anchor_intents" in _dict_key_names(d)]
    wire = [d for d in carriers if "tiers" in _dict_key_names(d)]
    builder = [d for d in carriers if "protocol_version" in _dict_key_names(d)]
    assert len(wire) == 1, (
        f"expected exactly one WIRE manifest dict (before_request branch, the "
        f"one with 'tiers') carrying anchor_intents; found {len(wire)}. The "
        f"before_request hook is the ACTUAL responder for "
        f"/.well-known/mcp.json — losing the canon keys there is the "
        f"responder-shadow regression this file exists to catch."
    )
    assert len(builder) == 1, (
        f"expected exactly one @app.route builder dict (the one with "
        f"'protocol_version') carrying anchor_intents; found {len(builder)}."
    )
    return wire[0], builder[0]


def test_both_manifest_responders_publish_both_canon_keys():
    wire, builder = _manifest_dicts(_main_tree())
    for which, d in (("wire/before_request", wire), ("@app.route builder", builder)):
        names = _dict_key_names(d)
        for key in WIRE_KEYS:
            assert key in names, f"{which} manifest dict lost {key!r}"


def test_canon_keys_are_derived_not_transcribed():
    """Each key's value must be a CALL to its fail-open helper. A literal dict
    here would be one more independent transcription — the disease both canon
    modules exist to kill."""
    wire, builder = _manifest_dicts(_main_tree())
    for which, d in (("wire/before_request", wire), ("@app.route builder", builder)):
        by_name = {k.value: v for k, v in zip(d.keys, d.values)
                   if isinstance(k, ast.Constant)}
        for key, helper in HELPERS.items():
            v = by_name[key]
            assert (isinstance(v, ast.Call)
                    and isinstance(v.func, ast.Name)
                    and v.func.id == helper), (
                f"{which}: {key!r} must be valued by {helper}() — found "
                f"{ast.dump(v)[:80]}"
            )


def test_helpers_exist_and_fail_open():
    """The helpers must exist at module level and swallow their import errors —
    the manifest must never break because a canon module failed to import."""
    tree = _main_tree()
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for helper in HELPERS.values():
        assert helper in fns, f"{helper} missing from main.py module level"
        assert any(isinstance(n, ast.Try) for n in ast.walk(fns[helper])), (
            f"{helper} lost its try/except — it must fail open (return None), "
            f"never take the manifest down"
        )


def test_worker_merges_the_same_two_keys_from_origin():
    """worker.js (the CF zone worker's repo deploy base) must whitelist-merge
    exactly these two keys from the origin manifest into its own
    /.well-known/mcp.json response. Guarded at source level like
    test_worker_mcp_card_is_canonical — worker.js is pasted, not imported."""
    text = (REPO_ROOT / "worker.js").read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"MANIFEST_EXTRA_KEYS\s*=\s*\[([^\]]*)\]", text)
    assert m, "worker.js lost MANIFEST_EXTRA_KEYS (the origin-manifest merge whitelist)"
    listed = set(re.findall(r"'([a-z_]+)'", m.group(1)))
    assert listed == set(WIRE_KEYS), (
        f"worker.js MANIFEST_EXTRA_KEYS {sorted(listed)} != {sorted(WIRE_KEYS)}"
    )
    assert "resolveManifestExtras" in text, "worker.js lost resolveManifestExtras()"
    assert re.search(r"\.\.\.mcpExtras,", text), (
        "worker.js /.well-known/mcp.json response no longer spreads mcpExtras — "
        "the merged canon keys would silently vanish from the served manifest"
    )
