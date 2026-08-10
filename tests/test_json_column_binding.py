"""★ r-json-truncation — the class that caused the 2026-08-04..08-10 press outage.

`json.dumps(payload)[:8000]` slices the SERIALISED STRING. Handed to a json or
jsonb column, the truncated blob fails to parse and takes the WHOLE statement
with it:

    psycopg2.errors.InvalidTextRepresentation: invalid input syntax for type json
    DETAIL: Token ""Midland\\u2...

In marketing_engine._write_release that INSERT was the last statement of a
single transaction, so its failure discarded the press_releases row and the
press_integrity review too — five days of releases that simply never existed,
with nothing a dashboard reads showing a trace.

    ★ CRITICAL DISTINCTION
        json.dumps(xs[:8])      slices the DATA — always SAFE
        json.dumps(xs)[:8000]   slices the TEXT — the bug
    A grep cannot separate them. This guard walks the AST for a Subscript whose
    slice is applied to the RESULT of a json.dumps CALL.

Two guards live here:
  1. the helper behaves (valid at any size, and — non-vacuously — passes small
     payloads through WHOLE);
  2. no sliced json.dumps binds to a database parameter anywhere in the repo,
     except an allow-list of sites PROVEN against the live schema to target
     TEXT columns. The allow-list is asserted to be fully USED, so an entry
     that goes stale fails the build instead of quietly widening the fence.
"""

import ast
import json
import os

import pytest

from util.json_column import json_for_column

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 1. the helper ────────────────────────────────────────────────────────────

def test_oversize_payload_still_serialises_to_VALID_json():
    """★ THE ROOT CAUSE. An em-dash forces \\uXXXX escapes near the cut, as in
    the live payload that broke press."""
    payload = {"as_of": "2026-08-10",
               "markets": [{"name": "Midland–Odessa " + "x" * 40, "i": i}
                           for i in range(200)]}
    assert len(json.dumps(payload)) > 8000, "fixture must exceed the cap"
    parsed = json.loads(json_for_column(payload, 8000))   # the assertion that matters
    assert parsed["_truncated"] is True
    assert parsed["_original_chars"] > 8000


def test_small_payload_is_passed_through_WHOLE():
    """Non-vacuity. A helper that always returned the stub would satisfy every
    "the output parses" test above while destroying every audit row it wrote."""
    payload = {"as_of": "2026-08-10", "markets": [1, 2, 3], "n": 7}
    assert json.loads(json_for_column(payload, 8000)) == payload


def test_keep_keys_survive_truncation():
    payload = {"as_of": "2026-08-10", "daily_topic": "dcpi_leader",
               "blob": ["Midland–Odessa" * 40 for _ in range(200)]}
    parsed = json.loads(json_for_column(payload, 8000,
                                        keep_keys=("as_of", "daily_topic")))
    assert parsed["as_of"] == "2026-08-10"
    assert parsed["daily_topic"] == "dcpi_leader"


def test_unserialisable_payload_never_raises():
    """These calls sit inside live transactions. Raising here would recreate
    the outage through a different door."""
    json.loads(json_for_column({"conn": object()}, 8000))


@pytest.mark.parametrize("cap", [10, 50, 200, 2000, 8000, 400000])
def test_output_is_valid_json_and_within_cap_at_every_cap(cap):
    """★ The stub must not become the thing it replaced. A cap smaller than the
    bookkeeping keys must still yield VALID json, never a slice of one."""
    payload = {"as_of": "2026-08-10",
               "rows": [{"m": "Midland–Odessa", "i": i} for i in range(500)]}
    out = json_for_column(payload, cap, keep_keys=("as_of",))
    json.loads(out)                       # must parse at every cap
    assert len(out) <= max(cap, 60), f"cap={cap} produced {len(out)} chars"


@pytest.mark.parametrize("payload", [None, [], [1, 2, 3], "text", 42, {"a": 1}])
def test_non_dict_payloads_round_trip(payload):
    """keep_keys is a dict affordance; a list or scalar payload must not make
    the helper raise or mangle the value."""
    assert json.loads(json_for_column(payload, 8000, keep_keys=("as_of",))) == payload


def test_any_cut_into_a_serialised_object_is_invalid():
    """Why the fix is 'never slice', not 'slice more carefully'. The outage
    report blamed a cut inside a \\uXXXX escape, which reads as a rare
    coincidence — it is not. Truncation leaves the brackets unbalanced, so
    essentially EVERY cut is invalid. Verified against a live jsonb column,
    where a cut at an ordinary comma failed identically."""
    payload = {"rows": [{"m": "Midland–Odessa", "i": i} for i in range(200)]}
    blob = json.dumps(payload)
    bad = 0
    for cut in range(100, len(blob), 97):
        try:
            json.loads(blob[:cut])
        except ValueError:
            bad += 1
    assert bad == len(range(100, len(blob), 97)), "some cut parsed — model is wrong"


# ── 2. the repo-wide fence ───────────────────────────────────────────────────

# Sites where a sliced json.dumps DOES reach a query parameter but the target
# column is TEXT, verified against the live schema on 2026-08-10. Truncating
# text is ugly, not fatal — a jsonb column here would be an outage.
# Keyed on (file, table.column): line numbers drift, these do not.
TEXT_COLUMN_ALLOWLIST = {
    ("ai_tracking.py", "mcp_connections.params"),
    # local SQLite spill buffer (ai_tracking.py:419) — no type parsing on
    # insert, and it drains into mcp_connections.params, which is text.
    ("ai_tracking.py", "buffered_mcp.params"),
    ("routes/brain_feature_proposer.py", "brain_feature_proposal_log.spec"),
    ("routes/lead_enrichment.py", "identified_checkout_signals.notes"),
    ("routes/media_comment_engagement.py", "media_comment_engagement_log.decision_reason"),
    ("routes/media_dm_follow_up.py", "media_dm_log.decision_reason"),
    ("routes/press_integrity.py", "press_integrity_reviews.issues"),
    ("testimonials_auto_capture.py", "ai_testimonials.query"),
}

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".claude",
             "tmp", "static"}


def _is_json_dumps(node):
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr == "dumps":
        return isinstance(f.value, ast.Name) and f.value.id in (
            "json", "simplejson", "ujson", "orjson")
    return isinstance(f, ast.Name) and f.id == "dumps"


def _py_files():
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                p = os.path.join(dirpath, fn)
                yield os.path.relpath(p, REPO), p


def _sliced_dumps_reaching_a_query():
    """Every sliced json.dumps that lands in an execute() parameter, directly
    or through one assignment hop."""
    out = []
    for rel, path in _py_files():
        if rel.startswith("tests/"):
            continue
        try:
            src = open(path, encoding="utf-8", errors="replace").read()
            tree = ast.parse(src)
        except Exception:
            continue
        parents = {}
        for n in ast.walk(tree):
            for ch in ast.iter_child_nodes(n):
                parents[ch] = n

        sliced = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Subscript)
                  and isinstance(n.slice, ast.Slice)
                  and _is_json_dumps(n.value)]
        if not sliced:
            continue

        # names bound to a sliced dumps, anywhere in the file
        tainted = set()
        for n in sliced:
            par = parents.get(n)
            if isinstance(par, ast.Assign):
                for t in par.targets:
                    if isinstance(t, ast.Name):
                        tainted.add(t.id)
                    elif isinstance(t, ast.Tuple):        # a, b = ...
                        for e in t.elts:
                            if isinstance(e, ast.Name):
                                tainted.add(e.id)
            elif isinstance(par, ast.Tuple):              # base_vals = (..., x, ...)
                gp = parents.get(par)
                if isinstance(gp, ast.Assign):
                    for t in gp.targets:
                        if isinstance(t, ast.Name):
                            tainted.add(t.id)

        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            f = call.func
            name = f.attr if isinstance(f, ast.Attribute) else (
                f.id if isinstance(f, ast.Name) else "")
            if name not in ("execute", "executemany"):
                continue
            for arg in call.args[1:]:
                for sub in ast.walk(arg):
                    direct = (isinstance(sub, ast.Subscript)
                              and isinstance(sub.slice, ast.Slice)
                              and _is_json_dumps(sub.value))
                    named = isinstance(sub, ast.Name) and sub.id in tainted
                    if direct or named:
                        out.append((rel, call.lineno,
                                    ast.get_source_segment(src, call.args[0]) or ""))
                        break
                else:
                    continue
                break
    return out


def _target(sql):
    """table.column for the allow-list, best effort — only used to name a
    finding, never to decide whether it is one."""
    import re
    m = re.search(r"(?:INSERT\s+INTO|UPDATE)\s+([A-Za-z_]\w*)", sql or "", re.I)
    return (m.group(1) if m else "?")


def test_no_sliced_json_dumps_reaches_a_query_parameter():
    """★ THE FENCE. Serialise with util.json_column.json_for_column instead —
    it stores a VALID object recording the truncation rather than a cut string.

    This fires on TEXT columns too. That is deliberate: proving a column is
    text costs a live-schema lookup, and a column that changes type later must
    not silently re-arm the outage. Add proven-text sites to
    TEXT_COLUMN_ALLOWLIST with the table.column you verified.
    """
    found = _sliced_dumps_reaching_a_query()
    unexpected = [(rel, line, _target(sql)) for rel, line, sql in found
                  if not any(rel == a_rel and a_col.split(".")[0] == _target(sql)
                             for a_rel, a_col in TEXT_COLUMN_ALLOWLIST)]
    assert not unexpected, (
        "sliced json.dumps() bound to a query parameter — use "
        "util.json_column.json_for_column:\n  " +
        "\n  ".join(f"{r}:{l} -> {t}" for r, l, t in unexpected))


def test_allowlist_is_fully_used():
    """★ Count the allow-list too. A stale entry silently widens the fence —
    exactly how a census guard stops guarding."""
    found = _sliced_dumps_reaching_a_query()
    files_with_findings = {rel for rel, _, _ in found}
    stale = sorted(rel for rel, _col in TEXT_COLUMN_ALLOWLIST
                   if rel not in files_with_findings)
    assert not stale, (
        "allow-list entries no longer match any site (fixed? moved?) — "
        f"remove them: {stale}")


def test_the_press_outage_site_stays_fixed():
    """The specific regression, pinned where it happened."""
    src = open(os.path.join(REPO, "routes", "marketing_engine.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    i = src.index("def _write_release")
    j = src.index("def _queue_distribution_posts")
    bad = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Slice) \
           and _is_json_dumps(n.value):
            off = sum(len(l) + 1 for l in src.splitlines()[:n.lineno - 1])
            if i <= off <= j:
                bad.append(n.lineno)
    assert not bad, f"sliced json.dumps() back in _write_release at {bad}"
