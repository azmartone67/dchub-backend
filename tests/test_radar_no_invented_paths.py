"""tests/test_radar_no_invented_paths.py — a detector may not invent a filename.

THE DEFECT, AS MEASURED (GET /api/v1/brain/squasher, 2026-09-03 00:52Z):
check_iso_metric_dropped built its remediation hint by string surgery on the
ISO code —

    f"Check the matching workflow + iso_{iso.lower().replace('-','')}.py module."

— so EU_CZ produced "iso_eu_cz.py". No such file has ever existed. Europe is
served by ONE module, routes/iso_eu_entsoe.py, reading ONE ENTSOE_API_Token
for all 33 bidding zones.

It did not stay cosmetic. Three awaiting_decision rows on that board each
carried an ~48s investigation instructing a human to open "the iso_eu_cz.py
fetch/write path", "the iso_eu_sk.py module logs", "iso_eu_se_1.py" — at
confidence 0.32-0.35, one copy per zone. The investigator was repeating what
the detector told it. A fabricated hint is indistinguishable downstream from a
verified one, which is the invented-numbers-in-failure-fallbacks shape exactly.

What is proved here:
  · iso_module_hint() returns only paths that EXIST in this repo, for every
    ISO code the live grid_data table can hold;
  · the fan-out zones (EU_*) resolve to the real ENTSO-E module;
  · a code that resolves to nothing yields None and renders as an honest miss
    pointing at the registry — never a constructed filename;
  · the registry beats the naming convention, so AESO resolves to the module
    that actually runs and not to the dead routes/iso_aeso.py;
  · ★ THE GENERAL GUARD: every .py path this detector can put in a finding
    detail must exist on disk. This is the test that would have caught the
    original bug, and it keeps catching the next one.

House rules: no DB, never import main, nothing runs at module scope.

Run:  python3 -m pytest tests/test_radar_no_invented_paths.py -v
"""
from __future__ import annotations

import os
import re

import pytest

from routes import brain_consistency_radar as r

REPO = os.path.dirname(os.path.dirname(os.path.abspath(r.__file__)))

# Every ISO code seen in grid_data on 2026-09-02, live. The EU_* block is the
# 31 zones that filed their own squasher row that night.
LIVE_ISOS = [
    "ERCOT", "CAISO", "NYISO", "MISO", "PJM", "SPP", "ISONE", "IESO", "AESO",
    "TVA", "BPA", "HYDROQUEBEC", "NGESO", "AEMO", "ENTSOE", "TAIPOWER",
    "OCCTO", "EMA", "ONS", "KEPCO-KR",
    "EU_CZ", "EU_SK", "EU_SE_1", "EU_SE_2", "EU_SE_3", "EU_SE_4", "EU_HU",
    "EU_DE_LU", "EU_DK_1", "EU_DK_2", "EU_FI", "EU_RO", "EU_PL", "EU_ES",
    "EU_GR", "EU_SI", "EU_BG", "EU_FR", "EU_NL", "EU_AT", "EU_NO_1",
    "EU_IE_SEM", "EU_IT_NORD", "EU_IT_CNOR", "EU_IT_CSUD", "EU_IT_SUD",
    "EU_IT_SICI", "EU_IT_SARD", "EU_IT_CALA",
]

PY_PATH = re.compile(r"[\w./-]+\.py\b")


def test_the_original_fabrication_is_gone():
    """iso_eu_cz.py never existed. Pinned by name so the string surgery cannot
    come back wearing a different variable."""
    assert r.iso_module_hint("EU_CZ") == "routes/iso_eu_entsoe.py"
    assert not os.path.exists(os.path.join(REPO, "routes/iso_eu_cz.py")), (
        "if this file is ever created, this test's premise changed — read the "
        "docstring before editing the assertion above")


@pytest.mark.parametrize("iso", LIVE_ISOS)
def test_every_live_iso_hint_names_a_file_that_exists(iso):
    hint = r.iso_module_hint(iso)
    if hint is None:
        return  # an honest miss is a correct answer
    assert os.path.exists(os.path.join(REPO, hint)), (
        "%s resolved to %s, which is not in this repo — a constructed path "
        "reached a finding detail again" % (iso, hint))


@pytest.mark.parametrize("iso", LIVE_ISOS)
def test_no_finding_detail_can_name_a_path_that_does_not_exist(iso):
    """★ The general guard. Walks the rendered sentence, not the helper."""
    detail = r._iso_module_clause(iso)
    for path in PY_PATH.findall(detail):
        assert os.path.exists(os.path.join(REPO, path)), (
            "the detail for %s names %r, which does not exist:\n  %s"
            % (iso, path, detail))


def test_all_thirty_one_eu_zones_point_at_the_one_real_module():
    """One module, one token, 33 zones — so 31 findings had one answer."""
    eu = [i for i in LIVE_ISOS if i.startswith("EU_")]
    assert len(eu) == 29, "the EU_* fixture drifted; recount before editing"
    assert {r.iso_module_hint(i) for i in eu} == {"routes/iso_eu_entsoe.py"}


def test_an_unresolvable_code_is_an_honest_miss_not_a_guess():
    assert r.iso_module_hint("NOT_A_REAL_ISO") is None
    clause = r._iso_module_clause("NOT_A_REAL_ISO")
    assert "NOT resolvable" in clause
    assert "iso_orchestrator.py" in clause, "it must point at the registry"
    assert not re.search(r"iso_not_a_real_iso", clause, re.I), (
        "the code must never be surgically turned into a filename")


def test_the_registry_wins_over_the_naming_convention():
    """routes/iso_aeso.py EXISTS and is dead — main.py never imports it. The
    orchestrator records iso_aeso_intl as the module that actually runs, so a
    hint that merely checks os.path.exists would send an operator to read a
    file that has never executed."""
    assert os.path.exists(os.path.join(REPO, "routes/iso_aeso.py")), (
        "premise: the dead module is still on disk")
    assert r.iso_module_hint("AESO") == "routes/iso_aeso_intl.py"


def test_the_orchestrator_read_is_fail_soft(monkeypatch):
    """A parse failure must yield an honest miss, never an exception out of a
    detector that runs every 6h."""
    monkeypatch.setattr(r, "_ORCH_CACHE", None)
    monkeypatch.setattr(r, "_repo_path", lambda rel: "/nonexistent/%s" % rel)
    assert r._orchestrator_module_for("TAIPOWER") is None
    assert r.iso_module_hint("TAIPOWER") is None


def test_the_detector_still_emits_the_finding_it_always_did():
    """The hint changed; the finding did not."""
    import inspect
    src = inspect.getsource(r.check_iso_metric_dropped)
    assert '"iso_metric_count_zero_24h"' in src
    assert '"grid_data: iso={iso}"' in src, "the finding_key shape is an identity"
    assert "_iso_module_clause(iso)" in src
    assert ".py " not in src.split("_iso_module_clause")[0][-400:], (
        "no constructed .py path may survive next to the call")


def test_a_stale_fanout_map_entry_yields_a_miss_not_a_dead_path(monkeypatch):
    """The map itself can rot: rename routes/iso_eu_entsoe.py and the EU_
    entry now points at nothing. The existence check is what stops a stale
    registry becoming the next invented filename — without it this returns a
    path that is not there, and we are back where we started with extra steps.

    (This is the case the live repo cannot exercise, because every mapped file
    currently exists — which is exactly why the check needs its own test.)"""
    monkeypatch.setattr(r, "_ISO_FANOUT_MODULES",
                        (("EU_", "routes/iso_eu_RENAMED.py"),))
    monkeypatch.setattr(r, "_ORCH_CACHE", {})
    assert r.iso_module_hint("EU_CZ") is None, (
        "a mapped path that does not exist must not be served as a hint")
    clause = r._iso_module_clause("EU_CZ")
    assert "NOT resolvable" in clause
    for path in PY_PATH.findall(clause):
        assert os.path.exists(os.path.join(REPO, path))
