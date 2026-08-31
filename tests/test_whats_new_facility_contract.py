"""/api/v1/whats-new must publish the facility keys its client reads, and must
not publish a de-duplication state under the name "verified".

★ THE TWO DEFECTS THIS ENCODES (measured live 2026-08-31)

1. A BANNED FIELD NAME, AND IT WAS THE BIGGEST NUMBER ON THE PAGE.
   The response carried `facilities_verified: 20019`, computed as
   COUNT(*) FROM discovered_facilities WHERE is_duplicate = 0.
   /api/v1/stats/canonical's own provenance block says of that family, verbatim:

       "Both of the last two are DE-DUPLICATION states, not source
        verifications — do not publish either as 'verified'."

   20,019 also sits ABOVE the 19,969 distinct-building count, so the banned
   field read as the most flattering figure available. The same defect was
   fixed in public_endpoints.py on 2026-08-01 and never swept here. The query
   was ALSO mislabelled twice over: COUNT(*) WHERE is_duplicate = 0 is canon's
   facilities_with_keeper, not facilities_verified (duplicate_of_id IS NULL),
   and `= 0` drops rows where is_duplicate IS NULL.

2. THE CARD RENDERED NOTHING, SILENTLY — FOURTH INSTANCE OF THE BUG CLASS.
   whats-new.html builds its facility lines from `it.distinct` and `it.records`
   and deliberately does not render `it.verified` (its comment says so, at
   whats-new.html:333). The server emitted `verified` and `tracked` and NEITHER
   of the two keys the client reads. `factLines` therefore built an empty string
   and the data-centers card showed no facility count at all. Both sides
   returned 200. The client half of the 2026-08-06 fix shipped; the server half
   did not.

   qa-api-contract.mjs exists in dchub-frontend for exactly this bug class and
   could not see this one: it is intra-procedural and tracks keys read off the
   fetch identifier, while `it` is a forEach loop variable over d.items[]. That
   blind spot is documented in its own header. This test covers the server side
   of it from here, where no cross-repo checkout is needed to run.

★ WHY A KEY LIST IS HARDCODED HERE. qa-api-contract.mjs argues correctly that a
hand-maintained (endpoint, keys) table is a second source of truth that rots.
That argument applies to a table nobody reads. This one names the client file
and line it was read from, and it fails LOUD rather than silently going green —
if the client stops reading `distinct`, this test still passes and costs
nothing; if the server stops emitting it, the card blanks and this fails.
"""
import ast

import pytest

SRC = "routes/infra_growth.py"

# Keys whats-new.html reads off the data_centers item, verbatim from
# dchub-frontend/whats-new.html:338-340:
#     if (it.distinct != null) factLines += ... n(it.distinct) + ' distinct facilities'
#     if (it.records  != null) factLines += ... n(it.records)  + ' source records'
CLIENT_READS = {"distinct", "records"}

# Names that publish a de-duplication state as a verification.
BANNED_SUBSTR = "verified"


def _tree():
    with open(SRC, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _whats_new_fn():
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == "whats_new":
            return node
    pytest.fail(f"{SRC} has no whats_new() — cannot verify a contract on a route that is gone")


def _item_keys(fn):
    """Every constant key assigned onto `item[...]` inside whats_new()."""
    keys = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if (isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.value, ast.Name) and tgt.value.id == "item"
                    and isinstance(tgt.slice, ast.Constant)
                    and isinstance(tgt.slice.value, str)):
                keys.add(tgt.slice.value)
    return keys


def _response_kwargs(fn):
    """Keyword names on the jsonify(...) that builds the response."""
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "jsonify"):
            return {kw.arg for kw in node.keywords if kw.arg}
    pytest.fail("whats_new() has no jsonify(...) call — cannot read the published field names")


def test_item_emits_every_key_the_client_renders():
    keys = _item_keys(_whats_new_fn())
    assert keys, "no item[...] assignments found — the parse failed, not the code"
    missing = CLIENT_READS - keys
    assert not missing, (
        f"/api/v1/whats-new does not emit {sorted(missing)} on the data_centers item, "
        f"but whats-new.html renders the facility lines from exactly those keys "
        f"(whats-new.html:338-340). Emitted: {sorted(keys)}. "
        "Both sides return 200 and the card silently renders no facility count.")


def test_no_dedup_state_is_published_as_verified():
    fn = _whats_new_fn()
    offenders = sorted(
        {k for k in _item_keys(fn) if BANNED_SUBSTR in k.lower()}
        | {k for k in _response_kwargs(fn) if BANNED_SUBSTR in k.lower()})
    assert not offenders, (
        f"/api/v1/whats-new publishes {offenders}. /api/v1/stats/canonical's provenance "
        'block: "Both of the last two are DE-DUPLICATION states, not source verifications '
        "— do not publish either as 'verified'.\" Publish facilities_distinct "
        "(COUNT(DISTINCT canonical_slug)) — the citable field.")


def _sql_literals():
    """Every string constant in the module, joined.

    Deliberately NOT the raw file text: the comment block above the fixed query
    quotes the retired one verbatim to explain why it is retired, and a raw-text
    scan fails on its own documentation. Comments are absent from the AST, so
    this reads what the code RUNS.
    """
    return "\n".join(
        n.value for n in ast.walk(_tree())
        if isinstance(n, ast.Constant) and isinstance(n.value, str))


def test_facility_count_mirrors_the_citable_canonical_query():
    """The count must be the distinct-buildings query, not a keeper/dedup count."""
    src = _sql_literals()
    assert "COUNT(DISTINCT canonical_slug)" in src, (
        f"{SRC} no longer runs COUNT(DISTINCT canonical_slug). That is the citable "
        "facility figure and the one /api/v1/stats/canonical publishes; any other "
        "query puts a second, different facility number on the site.")
    assert "WHERE is_duplicate = 0" not in src, (
        "COUNT(*) WHERE is_duplicate = 0 is back. It is a de-duplication state, it is "
        "canon's facilities_with_keeper rather than facilities_verified, and `= 0` "
        "drops rows where is_duplicate IS NULL (the fleet filter is COALESCE(is_duplicate,0)=0).")
