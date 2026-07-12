"""_error_mitigation envelope (2026-07-12) — Gemini co-design, error_version 1.

The guarantees ARE the tests: server-side templating (no unfilled
placeholders), suggested_params whitelist (the zero-hallucination binding —
a hint can never introduce a phantom param), computed reality (values come
from the caller, not statics), and unknown codes never fabricated.
"""

import pathlib

from routes.error_mitigation import (build_mitigation, registry_public,
                                     ERROR_VERSION, _REGISTRY)

DOC = (pathlib.Path(__file__).resolve().parent.parent / "static" / "error-codes.html").read_text()


def test_ttp_reference_case_computed_not_static():
    m = build_mitigation("zero_row_ttp_cut",
                         computed={"min_satisfiable": 34},
                         suggested={"max_ttp_months": 34})
    assert m["error_version"] == ERROR_VERSION
    assert m["error_code"] == "zero_row_ttp_cut"
    assert m["severity"] == "parameter_adjustment"
    assert "34" in m["deterministic_hint"]          # the COMPUTED floor, not "36"
    assert m["suggested_params"] == {"max_ttp_months": 34}


def test_suggested_params_whitelist_blocks_phantom_keys():
    # the zero-hallucination binding: only whitelisted keys survive, so a hint
    # can never introduce a parameter that doesn't exist on the tool.
    m = build_mitigation("zero_row_ttp_cut",
                         computed={"min_satisfiable": 34},
                         suggested={"max_ttp_months": 34, "candidate_sites": "PHANTOM"})
    assert "candidate_sites" not in m["suggested_params"]
    assert set(m["suggested_params"]) == {"max_ttp_months"}


def test_no_unfilled_placeholder_when_computed_missing():
    # a missing computed value must NEVER surface a raw {placeholder} to the agent
    m = build_mitigation("zero_row_ttp_cut", computed={}, suggested={})
    assert "{" not in m["deterministic_hint"] and "}" not in m["deterministic_hint"]


def test_unknown_code_is_never_fabricated():
    assert build_mitigation("totally_made_up_code") is None


def test_registry_severities_are_the_three_states():
    pub = registry_public()
    assert pub["error_version"] == ERROR_VERSION
    assert set(pub["severities"]) == {"parameter_adjustment", "transient_backoff", "fatal"}
    for code, spec in _REGISTRY.items():
        assert spec["severity"] in pub["severities"]
        # every suggested_params key is documented in the public registry
        assert pub["codes"][code]["suggested_params"] == spec["params"]


def test_doc_mirrors_registry():
    # every registered code + severity appears in the normative doc page
    for code in _REGISTRY:
        assert code in DOC, code
    for sev in ("parameter_adjustment", "transient_backoff", "fatal"):
        assert sev in DOC
    assert "error_version" in DOC and "zero-hallucination" in DOC.lower()
