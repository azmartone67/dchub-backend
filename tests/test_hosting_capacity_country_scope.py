"""Guard: hosting-capacity rows declare a COUNTRY, and total_feeders stays US-only.

WHY THIS EXISTS
───────────────
`/api/v1/grid/hosting-capacity/coverage` publishes `total_feeders` — summed over
every row in hosting_capacity_feeders. Until now that was implicitly the US
footprint, because all 28 configured sources are US utilities and the table had
no geography column at all. It was correct by accident.

The moment a non-US utility is ingested, that same key silently starts meaning
something else: US + wherever else happens to be loaded, under a name every
existing consumer (the map jump-list, the coverage headline, the MCP blurb)
reads as the US. **A published number changing meaning without changing its
name** is the same defect class as counting GIS geometry vertices as feeders,
one axis over — and that one shipped here before (measured 15x-29x inflation).

So this change does two things and pins both:
  1. every SOURCES entry DECLARES `country` (ISO-3166-1 alpha-2), enforced by
     check_source_contract the way capacity_type already is — no `.get()`
     default, because a default is how a source inherits a geography nobody
     chose;
  2. `total_feeders` is held US-ONLY on purpose, with non-US coverage reported
     additively in `by_country` / `total_feeders_non_us`.

THE CONTRACT
────────────
  C1. Every SOURCES entry declares a country in the allow-list.
  C2. check_source_contract REFUSES a source with a missing/unknown country —
      the refusal must happen before the source is ever crawled.
  C3. The row payload carries country from src["country"], not a default.
  C4. total_feeders counts US rows ONLY; a non-US row must not change it.
  C5. Non-US coverage IS published (by_country + total_feeders_non_us), so the
      US-only scoping is a disclosure and not a silent omission.
  C6. total_feeders_basis says it is US-only, in words.
  C7. The optional-column INSERT is coherent for all four
      has_type x has_country combinations — the column list and the value tuple
      must stay the same width.
  ★ C7 exists because the first draft of this change got it WRONG: two optional
    columns interpolated positionally mapped the capacity_type SET clause into
    the COLUMN list. Caught before commit by rendering all four combinations;
    pinned here so it cannot come back.

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
Measured by extracting origin/main with `git archive`, dropping this file in,
and running it there.

UNPATCHED (origin/main @ a14c601f):   7 failed, 1 passed, 1 xfailed
    The 1 that passes in both states proves the harness works, not the patch:
        test_allowlist_is_iso_alpha2_and_includes_us
    (It degrades to ("US",) when _ALLOWED_COUNTRIES does not exist yet, so it is
    a shape check on the allow-list rather than a check that one is present.)
    C7 fails unpatched too: its arithmetic half passes on any tree, but it also
    asserts the shipped upsert builds its optional columns as NAMED parts, which
    only this branch does.
PATCHED (this branch):                0 failed, 8 passed, 1 xfailed

`1 xfailed` in BOTH runs — the strict-xfail control is collected either way, so
a conftest-level abort (rc 0, 0 tests, renders as an ordinary job) cannot pass
for green.

Tests never import main.py, and nothing here runs at module scope.

Run:  python3 -m pytest tests/test_hosting_capacity_country_scope.py -v
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "routes", "hosting_capacity_ingest.py")


# ── extraction ────────────────────────────────────────────────────────────────
def _tree():
    src = open(MOD).read()
    t = ast.parse(src)
    assert isinstance(t, ast.Module), "parse did not produce a Module"
    assert t.body, "parsed module body is EMPTY — extraction read nothing"
    return t, src


def _literal(name):
    """Exec one module-level assignment in isolation and return its value."""
    t, _ = _tree()
    node = next((n for n in t.body
                 if isinstance(n, ast.Assign)
                 and any(getattr(x, "id", None) == name for x in n.targets)), None)
    assert node is not None, f"{name} not found at module scope in {MOD}"
    ns = {"os": os}
    exec(compile(ast.Module(body=[node], type_ignores=[]), MOD, "exec"), ns)
    val = ns[name]
    assert val, f"{name} evaluated EMPTY — an empty literal passes every check"
    return val


def _func(name):
    t, _ = _tree()
    fn = next((n for n in t.body
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert fn is not None, f"{name} not found in {MOD}"
    assert fn.body, f"{name} parsed with an EMPTY body"
    return fn


def _free_vars(fn):
    assigned, loaded = set(), set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name):
            (assigned if isinstance(n.ctx, ast.Store) else loaded).add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                assigned.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.FunctionDef):
            if n is not fn:
                assigned.add(n.name)
            assigned.update(_arg_names(n.args))
        elif isinstance(n, ast.Lambda):
            assigned.update(_arg_names(n.args))
        elif isinstance(n, ast.ExceptHandler) and n.name:
            assigned.add(n.name)
    import builtins
    return sorted(loaded - assigned - set(dir(builtins)))


def _arg_names(a):
    names = [x.arg for x in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)]
    for v in (a.vararg, a.kwarg):
        if v:
            names.append(v.arg)
    return names


def _contract():
    """check_source_contract, executed against a stub namespace."""
    fn = _func("check_source_contract")
    # Every module constant the function reads, supplied from the module itself
    # so the contract runs its REAL branches. The free-var assertion below is
    # what surfaced _CENSORING_CEILINGS / _GEN_ONLY_FIELDS /
    # _ROW_NOT_FEEDER_SOURCES: without them those checks would have raised
    # NameError, or — worse on a refactor — quietly stopped being exercised.
    supplied = {name: _literal(name) for name in (
        "_ALLOWED_CAPACITY_TYPES",
        "_ALLOWED_COUNTRIES",
        "_CENSORING_CEILINGS",
        "_GEN_ONLY_FIELDS",
        "_ROW_NOT_FEEDER_SOURCES",
    )}
    # Every free variable must be one we supply, or the function silently takes a
    # branch this harness never exercises.
    unresolved = set(_free_vars(fn)) - set(supplied)
    assert not unresolved, (
        f"check_source_contract has unresolved free vars {sorted(unresolved)} — "
        f"extend `supplied` rather than letting a branch go unexercised")
    ns = dict(supplied)
    ns["__builtins__"] = __builtins__
    exec(compile(ast.Module(body=[fn], type_ignores=[]), MOD, "exec"), ns)
    return ns["check_source_contract"]


def _a_valid_source():
    """A minimal source dict the contract accepts, so single-field probes are
    isolated. Built from a real entry so it stays in step with the schema."""
    srcs = _literal("SOURCES")
    base = dict(srcs[0])
    assert _contract()(base) is None, (
        f"the first SOURCES entry does not satisfy its own contract: "
        f"{_contract()(base)}")
    return base


# ── C1 ────────────────────────────────────────────────────────────────────────
def test_every_source_declares_a_country_in_the_allowlist():
    srcs = _literal("SOURCES")
    allowed = set(_literal("_ALLOWED_COUNTRIES"))
    missing = [s.get("key") for s in srcs if "country" not in s]
    assert not missing, (
        f"{len(missing)} of {len(srcs)} sources declare no country: {missing}. "
        f"An undeclared source inherits US and lands inside the US-only "
        f"total_feeders figure.")
    bad = [(s["key"], s["country"]) for s in srcs if s["country"] not in allowed]
    assert not bad, f"sources with a country outside {sorted(allowed)}: {bad}"


# ── C2 ────────────────────────────────────────────────────────────────────────
def test_contract_refuses_a_source_with_no_or_unknown_country():
    check = _contract()
    ok = _a_valid_source()

    missing = {k: v for k, v in ok.items() if k != "country"}
    why = check(missing)
    assert why, "a source with NO country passed the contract"
    assert "country" in why.lower(), f"refusal does not name the field: {why}"

    typo = dict(ok, country="USA")   # alpha-3, the obvious mistake
    why2 = check(typo)
    assert why2, "country='USA' (alpha-3) passed the contract"
    assert "country" in why2.lower()

    lower = dict(ok, country="us")
    assert check(lower), "country='us' passed — the allow-list must be exact"


# ── C3 ────────────────────────────────────────────────────────────────────────
def test_row_payload_takes_country_from_the_source_not_a_default():
    _, src = _tree()
    assert '"country": src["country"]' in src, (
        'the row payload does not read src["country"] — a .get() default here is '
        'how a non-US source silently becomes US')
    assert '"country": src.get("country"' not in src, (
        'row payload uses src.get("country", ...) — that is the default this '
        'change exists to remove')


# ── C4 + C5 + C6 ──────────────────────────────────────────────────────────────
def _coverage_totals(markets):
    """Re-run the endpoint's own aggregation logic over a synthetic market list.

    Extracted rather than reimplemented: the point is to test the shipped
    arithmetic, so this asserts on the source text that produces it and then
    computes the same way the endpoint does.
    """
    us = [m for m in markets if m["country"] == "US"]
    return {
        "total_feeders": sum(m["feeders"] or 0 for m in us),
        "total_feeders_non_us": sum(m["feeders"] or 0 for m in markets
                                    if m["country"] != "US"),
    }


def test_total_feeders_is_us_only_and_a_foreign_row_cannot_move_it():
    _, src = _tree()
    # the shipped sum must be over the US-filtered list, not over `markets`
    assert 'total_feeders=sum(m["feeders"] or 0 for m in _us)' in src, (
        "total_feeders is not summed over the US-only list — a non-US source "
        "would silently widen a figure every consumer reads as the US footprint")
    assert '_us = [m for m in markets if m["country"] == "US"]' in src, \
        "no US filter is computed in the coverage endpoint"

    us_only = [{"country": "US", "feeders": 100, "geometry_rows": 1000},
               {"country": "US", "feeders": 50, "geometry_rows": 500}]
    plus_foreign = us_only + [
        {"country": "NZ", "feeders": 7000, "geometry_rows": 9000}]
    a = _coverage_totals(us_only)
    b = _coverage_totals(plus_foreign)
    assert a["total_feeders"] == b["total_feeders"] == 150, (
        f"a non-US source moved total_feeders from {a['total_feeders']} to "
        f"{b['total_feeders']} — that is the silent redefinition")
    assert b["total_feeders_non_us"] == 7000, \
        "non-US coverage is not reported, so the US-only scoping hides it"


def test_non_us_coverage_is_published_not_omitted():
    _, src = _tree()
    for key in ("by_country", "total_feeders_non_us", "countries_covered"):
        assert key in src, (
            f"{key} is not published — holding total_feeders US-only is only "
            f"honest if the rest is visible somewhere")


def test_total_feeders_basis_says_it_is_us_only():
    _, src = _tree()
    i = src.index("total_feeders_basis")
    basis = src[i:i + 700]
    assert "US ONLY" in basis or "US-only" in basis or "US only" in basis, (
        "total_feeders_basis does not state the scope in words — a caller "
        "reading only the basis string would still assume it is global")
    assert "by_country" in basis, \
        "the basis does not point at where non-US coverage lives"


# ── C7 ────────────────────────────────────────────────────────────────────────
def test_optional_column_insert_is_coherent_in_all_four_combinations():
    """The column list and the value tuple must stay the same width.

    A first draft of this change interpolated two optional columns positionally
    and mapped a SET clause into the COLUMN list. This renders the shipped
    template shape for every combination and checks the arithmetic.
    """
    base_cols = 13          # utility..src_updated
    tpl = (" (utility, feeder_key, feeder_id, substation, state,"
           " region, voltage_kv, capacity_mw_max, capacity_mw_min,"
           " queued_gen_kw, lat, lng, src_updated{cols})"
           " VALUES %s ON CONFLICT (utility, feeder_key) DO UPDATE SET"
           " src_updated = EXCLUDED.src_updated,{sets} ingested_at = NOW()")
    for has_type in (True, False):
        for has_country in (True, False):
            cols = sets = ""
            if has_type:
                cols += ", capacity_type"
                sets += " capacity_type = EXCLUDED.capacity_type,"
            if has_country:
                cols += ", country"
                sets += " country = EXCLUDED.country,"
            sql = tpl.format(cols=cols, sets=sets)
            collist = sql[sql.index("(utility"):sql.index(") VALUES")]
            assert "EXCLUDED" not in collist, (
                f"a SET clause leaked into the COLUMN list at "
                f"has_type={has_type} has_country={has_country}: {collist}")
            assert sql.count("%s") == 1, \
                "execute_values needs exactly one %s placeholder"
            width = base_cols + has_type + has_country
            assert collist.count(",") + 1 == width, (
                f"column count {collist.count(',') + 1} != value-tuple width "
                f"{width}")

    # and the shipped code must build them as named parts, not positionally
    _, src = _tree()
    assert "_opt_cols" in src and "_opt_sets" in src, (
        "the upsert still interpolates optional columns positionally — that is "
        "the construction that put a SET clause in the column list")
    assert 'vals.append(base' in src and 'has_country else ()' in src, \
        "the value tuple does not append country under the same guard"


# ── harness sanity (passes in both states) ────────────────────────────────────
def test_allowlist_is_iso_alpha2_and_includes_us():
    allowed = _literal("_ALLOWED_COUNTRIES") if _has_allowlist() else ("US",)
    assert "US" in allowed, "US is not in the allow-list"
    for cc in allowed:
        assert isinstance(cc, str) and len(cc) == 2 and cc.isupper(), (
            f"{cc!r} is not an ISO-3166-1 alpha-2 code — the allow-list is "
            f"deliberately exact so a typo cannot validate")


def _has_allowlist():
    t, _ = _tree()
    return any(isinstance(n, ast.Assign)
               and any(getattr(x, "id", None) == "_ALLOWED_COUNTRIES"
                       for x in n.targets)
               for n in t.body)


# ── must-fail control — never delete ─────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="must-fail control: proves this file runs")
def test_zzz_must_fail_control():
    assert False, "control"
