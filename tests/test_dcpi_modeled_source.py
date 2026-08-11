"""tests/test_dcpi_modeled_source.py — the modeled provenance must name the
right operator (2026-08-11).

`data_basis_source` is the ONE field an analyst reads to answer "where does this
number come from". It used to be a single hard-coded literal published for all
323 markets:

    "2024 ENTSO-E/AEMO/EirGrid grid reports + published market conditions"

So Dallas (ERCOT) and Atlanta (SOCO/SERC) both told a reader their modeled
inputs came from ENTSO-E, AEMO and EirGrid — the European, Australian and Irish
system operators. Verified live before the fix. For every US market the
published provenance named the wrong continent.

The rule: name the operator whose published disclosures the per-ISO defaults
were calibrated against, always say the value is an estimate rather than a
measurement, and when the ISO is unknown or deliberately unnamed fail
VAGUE-BUT-TRUE rather than specific-but-wrong.

House rules: no DB, no network, never import main.py.

Run:  python3 -m pytest tests/test_dcpi_modeled_source.py -v
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from routes.dcpi import (_ISO_MODELED_REFERENCE, _MODELED_SOURCE_UNNAMED,
                         modeled_source_for)

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "routes" / "dcpi.py").read_text(encoding="utf-8", errors="replace")

# Operators that must never appear in a US market's provenance.
_FOREIGN = ("ENTSO-E", "ENTSOE", "AEMO", "EirGrid", "Nord Pool")

_US_ISOS = ("ERCOT", "PJM", "CAISO", "MISO", "NYISO", "ISONE", "SPP",
            "WECC", "SERC", "TVA", "SOCO", "FRCC")


def _iso_defaults_keys():
    """Keys of the function-local `iso_defaults` dict, read from source.

    It is defined inside gather_metrics_for_market, so it cannot be imported —
    and parsing it is the point: the fence must track the REAL dict, not a copy
    that drifts.
    """
    i = SRC.find("iso_defaults = {")
    assert i > 0, "iso_defaults literal not found — did it move or get renamed?"
    j = SRC.find("\n    }", i)
    assert j > i
    keys = re.findall(r'"([A-Za-z0-9\-_.]+)":\s*\{"queue_wait_months"', SRC[i:j])
    assert len(keys) > 40, f"only parsed {len(keys)} ISO keys — parser is stale"
    return keys


# ─── the fence: no ISO may silently miss an attribution ──────────────────

def test_every_iso_default_has_a_reference_entry():
    """★THE FENCE. A newly added ISO must not inherit a wrong or generic
    attribution by omission — mapping it to None is allowed, forgetting it is
    not."""
    missing = [k for k in _iso_defaults_keys() if k not in _ISO_MODELED_REFERENCE]
    assert not missing, (
        f"{len(missing)} ISO key(s) in iso_defaults have no _ISO_MODELED_REFERENCE "
        f"entry: {missing}. Add each one — map to None if the operator is "
        f"genuinely ambiguous.")


def test_reference_map_has_no_entries_for_isos_that_do_not_exist():
    """Keeps the map honest in the other direction too — a stale entry is a
    claim about an ISO we no longer score."""
    keys = set(_iso_defaults_keys())
    extra = [k for k in _ISO_MODELED_REFERENCE if k not in keys]
    assert not extra, f"_ISO_MODELED_REFERENCE has stale keys: {extra}"


# ─── US markets must not be attributed to foreign operators ──────────────

@pytest.mark.parametrize("iso", _US_ISOS)
def test_us_iso_provenance_names_no_foreign_operator(iso):
    src = modeled_source_for(iso)
    for bad in _FOREIGN:
        assert bad not in src, (
            f"{iso} provenance still names {bad!r}: {src!r}")


def test_ercot_names_ercot():
    assert "ERCOT" in modeled_source_for("ERCOT")


def test_soco_names_southern_company_not_entsoe():
    src = modeled_source_for("SOCO")
    assert "Southern Company" in src
    assert "ENTSO" not in src


def test_european_isos_may_name_entsoe():
    """The original string was not wrong everywhere — for an ENTSO-E market it
    was right. Over-correcting would lose real provenance."""
    assert "ENTSO-E" in modeled_source_for("ENTSOE-DE")
    assert "Germany" in modeled_source_for("ENTSOE-DE")


# ─── failing vague-but-true ──────────────────────────────────────────────

def test_unknown_iso_claims_no_operator():
    for iso in ("", None, "NOT_AN_ISO", "   "):
        src = modeled_source_for(iso)
        assert src == _MODELED_SOURCE_UNNAMED, f"{iso!r} -> {src!r}"


def test_deliberately_unnamed_isos_claim_no_operator():
    """KEPCO, TPM and WAPA are ambiguous keys. Naming a specific operator would
    repeat the original defect in a new place."""
    for iso in ("KEPCO", "TPM", "WAPA"):
        assert _ISO_MODELED_REFERENCE[iso] is None
        assert modeled_source_for(iso) == _MODELED_SOURCE_UNNAMED


def test_fail_open_wecc_default_is_disclosed_not_attributed():
    """`iso_defaults.get(iso, iso_defaults["WECC"])` fails OPEN, so a market with
    an unmatched ISO silently inherits Western-US parameters. It must NOT then be
    told its numbers were calibrated from WECC as if that were chosen for it."""
    src = modeled_source_for("ERCOT", iso_default_matched=False)
    assert "WECC" not in src
    assert "ERCOT" not in src
    assert "no ISO-specific calibration matched" in src


@pytest.mark.parametrize("iso", list(_ISO_MODELED_REFERENCE))
def test_every_provenance_string_says_it_is_an_estimate(iso):
    """Whatever else it claims, it must never read as a measurement."""
    src = modeled_source_for(iso)
    assert "estimate" in src.lower()
    assert "not a per-market measurement" in src


# ─── the retired literal, and scope correctness ──────────────────────────

def test_the_single_hardcoded_literal_is_gone():
    """Matched against CODE only: the fix documents the retired literal in a
    comment on purpose, and asserting against raw source would false-fail on
    that explanation — or tempt someone to delete it to go green."""
    code = "\n".join(l for l in SRC.splitlines() if not l.strip().startswith("#"))
    assert "2024 ENTSO-E/AEMO/EirGrid grid reports" not in code, (
        "the one-literal-for-all-323-markets provenance string is back in CODE")


def test_modeled_source_call_site_has_its_variables_in_scope():
    """`_MODELED_SOURCE = modeled_source_for(iso, _iso_default_matched)` sits
    ~700 lines below where both names are bound. A scope error here compiles
    fine and only fails at runtime, during the daily recompute, for every
    market at once — so assert the binding statically."""
    tree = ast.parse(SRC)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "gather_metrics_for_market"), None)
    assert fn is not None, "gather_metrics_for_market not found"

    assigned, call_line = set(), None
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            assigned.add(node.id)
        elif isinstance(node, ast.Tuple):
            for elt in node.elts:
                if isinstance(elt, ast.Name) and isinstance(elt.ctx, ast.Store):
                    assigned.add(elt.id)
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "modeled_source_for"):
            call_line = node.lineno
            args = [a.id for a in node.args if isinstance(a, ast.Name)]
            for a in args:
                assert a in assigned or a == "iso", (
                    f"modeled_source_for({a}) — {a} is not bound in "
                    f"gather_metrics_for_market")
    assert call_line, "modeled_source_for is never called from the scorer"
    assert "iso" in assigned, "iso is not unpacked in gather_metrics_for_market"
    assert "_iso_default_matched" in assigned, (
        "_iso_default_matched is not bound in gather_metrics_for_market")
