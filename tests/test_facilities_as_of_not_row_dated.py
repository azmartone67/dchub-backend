"""The facilities listing must NOT publish a collection-level `as_of`.

WHY THIS EXISTS (found live 2026-08-28, authenticated, limit=25 per slice)
`search_facilities` reported a `provenance.as_of` that was not a collection
date. This endpoint serves ``SELECT * FROM facilities`` ordered by
``confidence DESC, power_mw DESC`` — nothing to do with time — so every row's
own ``last_updated`` shipped on the wire, and the MCP gateway
(dchub-mcp-server lib/attribution.mjs) promoted the OLDEST of them to the
dataset's data date. The same registry then answered 2026-01 for a GB slice and
2026-07 for an IE slice: the number was an artifact of which rows came back.

Measured on live at the time:
  * ``facilities.last_updated`` is **TEXT**, in five serializations —
    ISO-T micros no TZ (14,161 rows) · NULL (7,269, 31.7%) · DATE-ONLY (1,425)
    · ISO-T seconds with a Z (28) · space-separated seconds (15).
  * ~5 canonical rows/day are re-stamped, on a 22,898-row table.
  * ``discovered_facilities.last_updated`` — a DIFFERENT table — is a real
    ``timestamp without time zone``, 0 NULL, max = today. The daily rebuild
    feeds that one; this endpoint does not read it.

THE CONTRACT PINNED HERE: this wiring passes no ``as_of``. Not MIN over the
page (the original defect), and not MAX either — a fresher-looking number would
only hide a half-dead column. If a collection date is ever genuinely measured
for this surface (e.g. the ``facility-snapshot-daily`` run that rebuilt the
corpus, visible at /api/v1/ops/deadman), pass THAT and update this test with
the measurement — do not derive one from row dates.

This is the backend half of the fence. The gateway half lives in
dchub-mcp-server test/as-of-record-scope.test.mjs; it is needed on BOTH sides
because attribution.mjs ``mergeProvenance`` lets a backend ``as_of`` override
the gateway's honest UNMEASURED block.

House rules: no DB, no network, never import main — main.py is parsed as
source with ``ast``.

Run:  python3 -m pytest tests/test_facilities_as_of_not_row_dated.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from routes.provenance import provenance_block

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"

# The two wiring sites: the authenticated arm (reads `facilities`) and the
# freemium arm (reads `discovered_facilities`, basic fields only).
FACILITY_LIST_FUNCS = ("_list_facilities_full", "_list_facilities_free")


def _func_nodes():
    """{name: ast.FunctionDef} for the facility-listing functions in main.py.

    ★ An empty result must FAIL, never pass: a rename would otherwise turn this
    guard into a no-op that reports green forever (the AST-finds-nothing trap).
    """
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"), filename=str(MAIN_PY))
    found = {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name in FACILITY_LIST_FUNCS
    }
    missing = set(FACILITY_LIST_FUNCS) - set(found)
    assert not missing, (
        f"main.py no longer defines {sorted(missing)} — this guard just went "
        f"vacuous. Re-point it at whatever now serves /api/v1/facilities."
    )
    return found


def _attach_provenance_calls(fn):
    return [
        c for c in ast.walk(fn)
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Name)
        and c.func.id.startswith("_pv_attach")
    ]


@pytest.mark.parametrize("fname", FACILITY_LIST_FUNCS)
def test_facility_listing_wires_provenance_at_all(fname):
    """Anchor check — the sites this guard inspects must actually exist."""
    calls = _attach_provenance_calls(_func_nodes()[fname])
    assert calls, (
        f"{fname} no longer calls attach_provenance; this guard inspects a "
        f"call that is not there any more."
    )


@pytest.mark.parametrize("fname", FACILITY_LIST_FUNCS)
def test_facility_listing_publishes_no_collection_as_of(fname):
    """No `as_of=` on the facilities provenance wiring — MIN or MAX alike."""
    for call in _attach_provenance_calls(_func_nodes()[fname]):
        kwargs = {k.arg for k in call.keywords if k.arg}
        assert "as_of" not in kwargs, (
            f"{fname} passes as_of= to attach_provenance. The rows this "
            f"endpoint returns are dated individually and arbitrarily ordered "
            f"(confidence DESC, power_mw DESC), so no value derived from them "
            f"— oldest OR newest — is the collection's vintage. If a real "
            f"collection date is now measured, say which run produced it here."
        )
        # The positional arms are (payload, source, method); a 4th positional
        # would land on as_of and slip past the keyword check above.
        assert len(call.args) <= 3, (
            f"{fname} passes {len(call.args)} positional args to "
            f"attach_provenance; the 4th positional IS as_of."
        )


def test_provenance_block_never_invents_an_as_of():
    """The helper omits the key entirely rather than defaulting to today."""
    blk = provenance_block("src", "method", default_v="tracked")
    assert "as_of" not in blk

    # …and a falsy/blank as_of must not become a key either.
    for empty in (None, "", "   "):
        assert "as_of" not in provenance_block(
            "src", "method", as_of=empty, default_v="tracked")


def test_a_real_as_of_still_passes_through():
    """Narrowing is about ROW dates — a measured collection date still binds."""
    blk = provenance_block("src", "method", as_of="2026-08-28T06:44:00Z",
                           default_v="tracked")
    assert blk["as_of"] == "2026-08-28T06:44:00Z"
