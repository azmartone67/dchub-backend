"""r-restatement-notice-cap (2026-08-08) — a correction must outlive the cap.

WHAT THIS PINS
--------------
#2442 stopped routes/agent_broadcast.py republishing DCPI verdict
RESTATEMENTS as market moves, and committed that the suppressed rows are not
discarded silently: they are summarised as one `dcpi_restatement` item so an
agent which cached yesterday's verdict still learns the label changed.

THE DEFECT THIS EXISTS TO CATCH
-------------------------------
That commitment was false on /api/v1/agent-broadcast/today the moment it
deployed. The notice is weighted 60 on purpose — below every genuine shift
(70 for a CAUTION-only move, 90 when BUILD or AVOID is involved) so it can
never bury the signal it protects. But the payload cap is applied across ALL
kinds after one global weight sort, and press_release sits at 85.

Measured live 2026-08-08 18:56 UTC, minutes after #2442 deployed:

    /api/v1/agent-broadcast/today -> item_count 78, items returned 50,
    MINIMUM weight in the returned slice 85, kinds {dcpi_verdict_shift: 20,
    press_release: 30}. The weight-60 restatement notice was produced and
    then truncated away.

So the suppression worked (no false "A -> B" item shipped) while the
disclosure did not. That is the worse half to lose silently: the feed goes
quiet about a 113-market relabel rather than wrong about it, and quiet is
indistinguishable from "nothing happened".

WHY NOT JUST RAISE THE WEIGHT
-----------------------------
Because tests/test_agent_broadcast_restatement.py::
test_restatement_never_outranks_a_genuine_shift forbids it, correctly — a
correction that outranks the content it corrects buries real moves on exactly
the days there are few of them. A qualifying notice has to outlive truncation
WITHOUT winning the ranking, which is a different property from weight and
needs its own mechanism.
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "routes", "agent_broadcast.py")


def _shipped():
    """exec _cap_items + its module constants out of the shipped source."""
    with open(SRC, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=SRC)

    wanted, body = {"_cap_items"}, []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            body.append(node)
        elif isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if names & {"_ITEM_CAP", "_UNTRUNCATABLE_KINDS"}:
                body.append(node)

    ns = {"frozenset": frozenset}
    mod = ast.Module(body=body, type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), SRC, "exec"), ns)  # noqa: S102
    for name in ("_cap_items", "_ITEM_CAP", "_UNTRUNCATABLE_KINDS"):
        assert name in ns, (
            f"{name} not found in {SRC}. If the cap was renamed or inlined, "
            "move this guard with it — it is the only thing keeping a "
            "restatement notice in a full payload."
        )
    return ns


def _build_broadcast_items_expr():
    """The AST expression _build_broadcast returns as its "items" value."""
    with open(SRC, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=SRC)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef)
                and node.name == "_build_broadcast"):
            continue
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Return)
                    and isinstance(sub.value, ast.Dict)):
                continue
            for k, v in zip(sub.value.keys, sub.value.values):
                if isinstance(k, ast.Constant) and k.value == "items":
                    return ast.unparse(v)
    raise AssertionError(
        "_build_broadcast no longer returns a dict with an 'items' key — "
        "move this guard with it rather than deleting it.")


def _filler(n, kind="press_release", weight=85):
    return [{"kind": kind, "weight": weight, "title": f"{kind}-{i}"}
            for i in range(n)]


NOTICE = {"kind": "dcpi_restatement", "weight": 60,
          "title": "113 DCPI verdicts restated, not moved"}


def _sorted(items):
    return sorted(items, key=lambda x: -int(x.get("weight") or 0))


# ─────────────────────────────────────────────────────────────────────────

def test_build_broadcast_actually_routes_items_through_the_cap():
    """★ ADDED AFTER A SURVIVING MUTATION, and it is the load-bearing test.

    Every other case here execs _cap_items directly, so all of them pass
    happily while _build_broadcast returns a plain `items[:50]` and the
    function sits unused. That exact mutation survived the first version of
    this file. Same failure mode as
    tests/test_agent_broadcast_verdict_shift_source.py's SOURCE guard:
    banning the bad behaviour is not enough if bypassing the good code also
    satisfies the ban.
    """
    expr = _build_broadcast_items_expr()
    assert "_cap_items" in expr, (
        "_build_broadcast returns its items as `%s`, bypassing _cap_items. "
        "The plain slice drops the dcpi_restatement notice out of any "
        "payload with 50+ heavier items — measured live on /today as 50 "
        "returned items with a minimum weight of 85 against a notice at 60."
        % expr)


def test_cap_still_caps():
    """The rescue must not turn the cap into a no-op."""
    ns = _shipped()
    out = ns["_cap_items"](_sorted(_filler(400)))
    assert len(out) == ns["_ITEM_CAP"], len(out)


def test_notice_survives_a_full_payload_of_heavier_items():
    """★ THE REGRESSION. Reproduces the measured /today shape: 78 items,
    30 of them press_release at weight 85, notice at 60."""
    ns = _shipped()
    items = _sorted(_filler(30) + _filler(48, "dcpi_verdict_shift", 90)
                    + [NOTICE])
    out = ns["_cap_items"](items)
    assert len(out) == ns["_ITEM_CAP"], len(out)
    kinds = [i["kind"] for i in out]
    assert "dcpi_restatement" in kinds, (
        "the restatement notice was truncated out of a full payload. #2442 "
        "commits that suppressed shifts are summarised rather than dropped; "
        f"returned kinds were {sorted(set(kinds))}"
    )


def test_notice_still_does_not_outrank_a_genuine_shift():
    """The other half. Rescuing it must not promote it."""
    ns = _shipped()
    items = _sorted(_filler(30) + _filler(48, "dcpi_verdict_shift", 90)
                    + [NOTICE])
    out = ns["_cap_items"](items)
    idx = [n for n, i in enumerate(out) if i["kind"] == "dcpi_restatement"][0]
    shifts = [n for n, i in enumerate(out) if i["kind"] == "dcpi_verdict_shift"]
    assert shifts and idx > max(shifts), (
        "the rescued notice now sorts above a genuine verdict shift — that is "
        "the burial tests/test_agent_broadcast_restatement.py forbids.")


def test_untouched_when_payload_fits():
    """No reordering or loss on the common path."""
    ns = _shipped()
    items = _sorted(_filler(10) + [NOTICE])
    assert ns["_cap_items"](items) == items


def test_multiple_notices_all_survive():
    """_UNTRUNCATABLE_KINDS is a set, not a single sentinel — if a second
    qualifying kind is added later, the rescue must not silently keep one."""
    ns = _shipped()
    extra = dict(NOTICE, title="another notice")
    items = _sorted(_filler(80) + [NOTICE, extra])
    out = ns["_cap_items"](items)
    assert len(out) == ns["_ITEM_CAP"]
    assert sum(1 for i in out if i["kind"] == "dcpi_restatement") == 2, (
        "only one of two qualifying notices survived the cap")


@pytest.mark.parametrize("n_filler", [49, 50, 51, 60])
def test_boundary_sizes(n_filler):
    """Off-by-one around the cap: the notice survives on both sides."""
    ns = _shipped()
    items = _sorted(_filler(n_filler) + [NOTICE])
    out = ns["_cap_items"](items)
    assert len(out) <= ns["_ITEM_CAP"]
    assert any(i["kind"] == "dcpi_restatement" for i in out), (
        f"notice lost with {n_filler} filler items")
