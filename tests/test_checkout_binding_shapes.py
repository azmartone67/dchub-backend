"""checkout_binding — the decomposition that makes a 0 legible.

`session_upgrades` read 0 all-time from 2026-06-06 and was being quoted as
"the funnel's terminal step has never fired." It is not the terminal step.
`server.mjs` chooses ONE binder for the Stripe `client_reference_id` by what
the caller holds — a durable key gets `pk-`/`k-`, a keyless caller with a
session gets the bare sid, one with neither gets `a-` — and every binder is
idempotent, so the first to run wins. main.py's Fix E branch writes
`mcp_session_upgrades` ONLY for the bare-sid shape and explicitly excludes the
rest. A paying customer almost certainly holds a key, which routes them to
`pk-` and never to that table.

So the 0 is consistent with BOTH "nobody converts" and "nobody is routed
here", and the signal alone cannot separate them. `checkout_binding` reports
the shapes side by side so it can.

What this file guards, and why each is not a restatement:

  1. THE CROSS-FILE INVARIANT. Every prefix Fix E treats as reserved must be
     classified by the decomposition's CASE. Add a seventh reserved prefix to
     main.py and forget this surface, and its clicks silently fall into
     'session' — the bucket whose whole job is to be the one that CAN convert.
     Read from main.py's source as TEXT; this file never imports main.
  2. writes_session_upgrades must name only labels the CASE can emit, or the
     field points at a bucket that never appears.
  3. No `%` literal in the added SQL — module docstring rule 5. These strings
     go through cur.execute(sql) with no params, so a stray % is a runtime
     ValueError on a public keyless endpoint.
  4. The null-not-empty contract, which is this module's entire reason to
     exist: a failed probe must be null, never {} and never 0.

Pure source reads: no DB, no network, never imports main.
"""
import ast
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from routes.ops_activation import SHAPE  # noqa: E402

ACTIVATION = os.path.join(REPO_ROOT, "routes", "ops_activation.py")
MAIN = os.path.join(REPO_ROOT, "main.py")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _case_sql() -> str:
    """The _shape_case CASE expression, as shipped."""
    src = _read(ACTIVATION)
    m = re.search(r"_shape_case = \((.*?)\n        \)", src, re.S)
    assert m, "the _shape_case CASE expression is gone from ops_activation.py"
    return m.group(1)


def _fix_e_reserved() -> set:
    """Prefixes main.py's Fix E refuses to bind as a session id.

    Extracted from the `_is_reserved = (...)` block by source text — importing
    main.py is not an option (module-level DB/env work) and this file follows
    tests/test_ops_activation_feed.py's rule of never importing it.
    """
    src = _read(MAIN)
    m = re.search(r"_is_reserved = \((.*?)\n            \)", src, re.S)
    assert m, "main.py's Fix E _is_reserved block not found — did it move?"
    prefixes = set(re.findall(r"startswith\('([^']+)'\)", m.group(1)))
    assert prefixes, "no startswith() prefixes parsed out of _is_reserved"
    return prefixes


# ── 1 · the cross-file invariant ─────────────────────────────────────────

def test_every_reserved_prefix_is_classified_by_the_decomposition():
    case = _case_sql()
    missing = sorted(p for p in _fix_e_reserved() if f"'{p}'" not in case)
    assert not missing, (
        f"main.py's Fix E treats {missing} as reserved — it will NOT write "
        f"mcp_session_upgrades for them — but checkout_binding's CASE does not "
        f"classify them, so their clicks fall into the 'session' bucket. That "
        f"bucket exists to name the one shape that CAN convert; polluting it "
        f"reintroduces exactly the ambiguity this decomposition removes."
    )


def test_the_reserved_list_is_not_silently_empty():
    """A parse that quietly returns nothing would make test 1 vacuous."""
    reserved = _fix_e_reserved()
    assert len(reserved) >= 5, (
        f"only parsed {sorted(reserved)} out of Fix E's reserved block; the "
        f"cross-file check above would pass on almost anything. Fix the "
        f"extraction rather than lowering this bound."
    )


# ── 2 · the pointer field must point somewhere real ──────────────────────

def test_writes_session_upgrades_names_only_emittable_shapes():
    src = _read(ACTIVATION)
    m = re.search(r'"writes_session_upgrades": \[(.*?)\]', src, re.S)
    assert m, "writes_session_upgrades is gone from checkout_binding"
    named = set(re.findall(r'"([^"]+)"', m.group(1)))
    emitted = set(re.findall(r"THEN '([a-z_]+)'", _case_sql())) | {"session"}
    assert named <= emitted, (
        f"writes_session_upgrades names {sorted(named - emitted)}, which the "
        f"CASE can never produce — the field would point at an empty bucket."
    )
    assert named, "writes_session_upgrades is empty; the 0 becomes unreadable again"


# ── 3 · module rule 5: no % literal in SQL ───────────────────────────────

def test_binding_sql_carries_no_percent_literal():
    case = _case_sql()
    assert "%" not in case, (
        "the CASE expression contains a % literal. These strings run through "
        "cur.execute(sql) with NO parameters, so psycopg2 raises on the "
        "placeholder — on a public, keyless endpoint. Use LEFT(), not LIKE."
    )
    src = _read(ACTIVATION)
    m = re.search(r"def _shapes\(since=None\):(.*?)\n        binding_cohort", src, re.S)
    assert m, "_shapes() helper not found"
    assert "%" not in m.group(1), "_shapes() builds SQL containing a % literal"


# ── 4 · null, never {} — the contract this module exists for ─────────────

def _func_named(name):
    """The ast.FunctionDef for `name`, wherever it is nested."""
    tree = ast.parse(_read(ACTIVATION))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in ops_activation.py")


def test_failed_row_probe_returns_none_not_empty():
    """Every return inside qrows()'s except handler must be None.

    ★ A first version of this asserted the handler's SOURCE matched
    `except Exception.*?return None`. Mutation proved it vacuous: inserting
    `return []` ABOVE the existing `return None` left it green, because the
    regex only needed `return None` to appear SOMEWHERE after the except — and
    the dead line below the early return satisfied that forever. Presence is
    not behaviour. Read the returns, not the text around them.
    """
    fn = _func_named("qrows")
    handlers = [h for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler)]
    assert handlers, "qrows() no longer catches anything"
    returns = [n for h in handlers for n in ast.walk(h) if isinstance(n, ast.Return)]
    assert returns, "qrows()'s except handler falls through instead of returning"
    for r in returns:
        assert isinstance(r.value, ast.Constant) and r.value.value is None, (
            f"qrows() returns {ast.unparse(r.value)} from its except handler. "
            f"Anything but None — [] most of all — makes a failed read "
            f"indistinguishable from 'no clicks', which is the precise "
            f"confusion this feed was built to end."
        )


def test_shapes_propagates_none_rather_than_coercing():
    src = _read(ACTIVATION)
    m = re.search(r"def _shapes\(since=None\):(.*?)\n        binding_cohort", src, re.S)
    assert m
    assert "if rows is None:" in m.group(1) and "return None" in m.group(1), (
        "_shapes() must pass a failed probe through as None. Coercing to {} "
        "publishes 'we looked and found nothing' when nothing was looked at."
    )


# ── 5 · the surface documents itself ─────────────────────────────────────

def test_shape_documents_checkout_binding():
    assert "checkout_binding" in SHAPE["top_level"], (
        "top_level no longer lists checkout_binding; a caller reading `shape` "
        "would not know the field exists."
    )
    assert "checkout_binding" in SHAPE, "SHAPE carries no checkout_binding entry"
    doc = SHAPE["checkout_binding"]
    for token in ("writes_session_upgrades", "null"):
        assert token in doc, f"checkout_binding docs omit '{token}'"


def test_session_upgrades_signal_points_at_the_decomposition():
    src = _read(ACTIVATION)
    m = re.search(r'signal\("session_upgrades".*?\)\),', src, re.S)
    assert m, "the session_upgrades signal is gone"
    basis = m.group(0)
    assert "checkout_binding" in basis, (
        "the session_upgrades basis no longer points at checkout_binding. A "
        "reader hitting a 0 has to be told where to look, in the field itself "
        "— that redirect is the fix, not the new map."
    )
    assert "ONE OF SEVERAL BIND SHAPES" in basis, (
        "the basis no longer says the signal is one shape among several, which "
        "is the sentence that stops it being read as a funnel verdict."
    )
