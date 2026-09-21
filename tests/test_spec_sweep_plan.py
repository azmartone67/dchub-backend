"""Guard plan_sweep — what an armed spec sweep would actually drive.

Every rule here exists because a dry run against live evidence on 2026-09-21
showed the previous behaviour doing the wrong thing:

  · The debt book's list is a 25-of-242 PREVIEW; iterating it saw 25.
  · 29 x iso_metric_count_zero_24h is ONE finding filed once per ISO code.
  · Its cause was fixed 2026-09-07 (#4097), four days after it was filed —
    yet the class is NOT dead: WACM and WAUW still fire. A fold that dropped
    the whole class as quiet would have hidden a real, live data gap.
  · The first planner ranked that class FIRST as "29 sites" when 26 were
    quiet. Only live sites may count.

plan_sweep is pure (no DB, no network, no clock), so it is AST-extracted and
exec'd — the real function, not a reimplementation. CI installs pytest only.
"""
import os
import ast

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "routes", "brain_spec_implementer.py")


def _load():
    src = open(MOD, encoding="utf-8").read()
    pieces = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_DRIVE_RANK" for t in node.targets):
            pieces.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.FunctionDef) and node.name == "plan_sweep":
            pieces.append(ast.get_source_segment(src, node))
    ns = {}
    exec(compile("\n\n".join(pieces), MOD, "exec"), ns)
    assert "plan_sweep" in ns and "_DRIVE_RANK" in ns, "AST extraction missed a name"
    return ns["plan_sweep"]


plan_sweep = _load()


def _doc(name, issue, url):
    return {"doc": name, "target": {"issue": issue, "url": url, "url_prefix": False}}


ISO = "iso_metric_count_zero_24h"
QUIET_CODES = ["SOCO", "PACE", "NEVP", "GVL", "DUK"]


def _iso_fixture(live=("WACM", "WAUW")):
    docs = [_doc(f"inv-{c}.md", ISO, f"grid_data: iso={c}") for c in QUIET_CODES]
    docs += [_doc(f"inv-{c}.md", ISO, f"grid_data: iso={c}") for c in live]
    verdicts = {(ISO, f"grid_data: iso={c}"): "quiet_unproven" for c in QUIET_CODES}
    verdicts.update({(ISO, f"grid_data: iso={c}"): "firing" for c in live})
    return docs, verdicts


def test_fan_out_folds_to_one_unit():
    docs, v = _iso_fixture()
    p = plan_sweep(docs, v)
    assert p["counts"]["classes"] == 1
    assert len(p["drive"]) == 1, "one finding filed per site must drive ONCE"


def test_only_live_sites_are_counted():
    """★ The inflation bug: 26 quiet + 2 firing reported as '29 sites'."""
    docs, v = _iso_fixture()
    item = plan_sweep(docs, v)["drive"][0]
    assert item["site_count"] == 2, (
        f"site_count={item['site_count']} — quiet sites are being counted, "
        "which ranks a 2-site problem as a 7-site one")
    assert item["quiet_sites"] == 5
    assert all("WACM" in s or "WAUW" in s for s in item["sites"])


def test_one_live_site_keeps_the_class_alive():
    """★ The fix for the ISO class worked for 26 sites and not for 2. A fold
    that dropped the class as quiet would hide WACM/WAUW — a real gap."""
    docs, v = _iso_fixture(live=("WACM",))
    p = plan_sweep(docs, v)
    assert p["drive"], "a class with one firing site was dropped as quiet"
    assert p["skipped_quiet"] == []


def test_a_class_quiet_at_every_site_is_skipped():
    docs, v = _iso_fixture(live=())
    p = plan_sweep(docs, v)
    assert p["drive"] == []
    assert p["skipped_quiet"] == [{"issue": ISO, "sites": len(QUIET_CODES)}]


def test_the_representative_is_a_live_member():
    """Driving a quiet member's spec would implement a fixed problem."""
    docs, v = _iso_fixture()
    rep = plan_sweep(docs, v)["drive"][0]["doc"]
    assert rep in ("inv-WACM.md", "inv-WAUW.md"), f"representative {rep} is quiet"


def test_firing_ranks_before_unmeasured():
    docs = [_doc("a.md", "big_but_unmeasured", "x1"),
            _doc("b.md", "big_but_unmeasured", "x2"),
            _doc("c.md", "big_but_unmeasured", "x3"),
            _doc("d.md", "small_but_firing", "y1")]
    v = {("big_but_unmeasured", u): "unmeasured" for u in ("x1", "x2", "x3")}
    v[("small_but_firing", "y1")] = "firing"
    order = [x["issue"] for x in plan_sweep(docs, v)["drive"]]
    assert order[0] == "small_but_firing", (
        "unmeasured outranked firing — a driven spec must be one we can justify")


def test_unmeasured_is_not_treated_as_quiet():
    docs = [_doc("a.md", "no_ledger_data", "u")]
    p = plan_sweep(docs, {("no_ledger_data", "u"): "unmeasured"})
    assert p["drive"] and p["skipped_quiet"] == [], (
        "unmeasured was skipped as quiet — absence of data is not evidence")


def test_prose_specs_go_to_a_human():
    docs = [{"doc": "agenda-1.md", "target": None}, _doc("b.md", "i", "u")]
    p = plan_sweep(docs, {("i", "u"): "firing"})
    assert p["needs_human"] == ["agenda-1.md"]
    assert p["counts"]["needs_human"] == 1


def test_empty_input_is_empty_not_an_error():
    p = plan_sweep([], {})
    assert p["drive"] == [] and p["counts"]["open_docs"] == 0
