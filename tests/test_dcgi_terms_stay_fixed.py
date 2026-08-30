"""The three DCGI defects must stay fixed, and an unpriced state must stay UNSCORED.

Replaces tests/test_gas_index_disabled_while_defective.py, which was a
WITHDRAWAL fence: it asserted no composite was served while the terms were
broken, and its own dead-man's switch required deleting it once every fence
skipped. This is the forward half — it asserts the terms are still right,
which is the invariant that outlives the outage.

The 2026-08-08 audit, and where each defect is now fenced:

  1. interstate_share compared a lowercase literal against capitalised EIA
     TYPEPIPE values and scored ~0 for every published state.   -> here
  2. the price was chosen by a non-deterministic tie-break across the PIN and
     PEU series, which carry different margins.                 -> here
  3. nine states including Texas scored on a hardcoded `cost = 50.0`.
     Root cause was the loader dropping them on casing
     (tests/test_eia_gas_prices_state_mapping.py); the SCORING half — that a
     state we cannot price is UNSCORED and never neutral-scored — is fenced
     here, functionally, not just by reading the source.
  4. gas-fired $/MWh disagreeing across five surfaces by up to 5.5x is NOT
     fixed and NOT re-enabled. DCHUB_GAS_TO_GRID_ENABLED stays off; that
     defect is out of scope for this file.

Calibration of the cost scale is fenced separately in tests/test_dcgi_cost_scale.py.
"""
import ast
import importlib
import os
import re

import pytest

DCGI_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "routes", "dcgi.py")
ROLLUP_FN = "_gas_state_rollup"


# ── Source-level detectors (carried over from the withdrawal fence) ─────────

def _rollup():
    src = open(DCGI_SRC, encoding="utf-8").read()
    node = next((n for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.FunctionDef) and n.name == ROLLUP_FN), None)
    return node, (ast.get_source_segment(src, node) if node else "")


def test_the_detectors_still_have_their_anchor():
    """A fence that cannot find the code it fences must say so, not pass."""
    node, body = _rollup()
    assert node is not None, (
        f"{ROLLUP_FN}() not found in routes/dcgi.py — every assertion below "
        "would be inspecting a file it never located.")
    assert "_PRICE_CEIL" in body, "cost term no longer in the rollup"


def test_interstate_is_compared_case_insensitively_at_every_site():
    """Defect 1. BOTH paths carry the comparison — the lat/lng derivation and
    the legacy state-column GROUP BY — so a fix to one leaves the other dead."""
    node, body = _rollup()
    sites = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Compare) and isinstance(sub.ops[0], ast.Eq):
            parts = [sub.left] + list(sub.comparators)
            lits = [c for c in parts
                    if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            if any(c.value.lower() == "interstate" for c in lits):
                txt = ast.unparse(sub)
                sites.append((txt, any(f in txt for f in (".lower()", ".upper()",
                                                          ".casefold()"))))
    for m in re.finditer(r"([A-Za-z_().]*pipeline_type[A-Za-z_().]*)\s*=\s*'(\w+)'",
                         body, re.IGNORECASE):
        if m.group(2).lower() == "interstate":
            sites.append((m.group(0), "lower(" in m.group(1).lower()
                          or "upper(" in m.group(1).lower()))
    assert len(sites) >= 2, (
        f"expected >= 2 interstate-comparison sites, found {len(sites)}. Either "
        "a code path was removed or the detector no longer matches it.")
    dead = [t for t, ok in sites if not ok]
    assert not dead, (
        "case-SENSITIVE interstate comparison is back. EIA writes 'Interstate' "
        f"capitalised, so this term silently scores ~0:\n  " + "\n  ".join(dead))


def test_no_numeric_constant_is_ever_assigned_to_cost():
    """Defect 3, the permanent form. `cost = 50.0` was indistinguishable in the
    payload from a measured 50.0 — the reader could not tell knowledge from a
    placeholder. No literal may stand in for a measurement again."""
    node, _ = _rollup()
    offenders = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assign):
            for tgt in sub.targets:
                if getattr(tgt, "id", "") == "cost" and isinstance(sub.value, ast.Constant) \
                        and isinstance(sub.value.value, (int, float)):
                    offenders.append(ast.unparse(sub))
    assert not offenders, (
        "a numeric constant is being assigned to the cost term:\n  "
        + "\n  ".join(offenders)
        + "\nAn unpriced state is UNSCORED. It is not scored 50.")


def test_the_price_pick_is_total_and_prefers_one_series():
    """Defect 2. Ordering only by period leaves the planner free to return
    either series for a state holding both at the same period."""
    _, body = _rollup()
    order = re.search(r"ORDER BY\s+UPPER\(state\),(.*?)\"\"\"", body, re.S)
    assert order, "the price query's ORDER BY is no longer recognisable"
    clause = order.group(1)
    assert "PEU" in clause, (
        "the ORDER BY no longer expresses a series preference, so a state with "
        "both PIN and PEU can come back either way")
    assert clause.count(",") >= 2, (
        "the ORDER BY is not a total order — with only `period` after the "
        "state, same-period ties are broken by the planner:\n" + clause)


# ── Functional: the UNSCORED path, exercised through the real scoring ───────

class _Cur:
    """Fake cursor. ★ Emulates the psycopg2 BINDING step first: a literal
    percent-sign beside %s params raises client-side, before the SQL is sent,
    and a stub more forgiving than the driver certifies code the driver
    refuses."""
    def __init__(self, rows_for):
        self.rows_for, self._rows = rows_for, []

    def execute(self, sql, params=None):
        if params is not None:
            sql % tuple(params)
        self._rows = self.rows_for(sql)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


class _Conn:
    def __init__(self, rows_for):
        self.rows_for = rows_for

    def cursor(self):
        return _Cur(self.rows_for)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        pass

    def rollback(self):
        pass


# Two Texas-area points (priced) and two Nevada-area points (deliberately not).
_PIPE_ROWS = [
    (31.5, -97.5, "Op A", "Interstate", "eia_geodot_lines"),
    (31.6, -97.6, "Op B", "Intrastate", "eia_geodot_lines"),
    (39.5, -117.0, "Op C", "Interstate", "eia_geodot_lines"),
    (39.6, -117.1, "Op D", "Interstate", "eia_geodot_lines"),
]
_PRICE_ROWS = [("TX", 2.0, "electric_power", "2026-05", "PEU")]   # NV absent


def _rows_for(sql):
    s = " ".join(sql.split()).upper()
    if "FROM GAS_PIPELINES" in s and "COUNT(" not in s:
        return list(_PIPE_ROWS)
    if "EIA_GAS_PRICES" in s:
        return list(_PRICE_ROWS)
    return []


@pytest.fixture
def dcgi(monkeypatch):
    import routes.dcgi as m
    importlib.reload(m)
    monkeypatch.setattr(m, "conn", lambda: _Conn(_rows_for))
    return m


def test_an_unpriced_state_is_unscored_not_neutral_scored(dcgi):
    states, err = dcgi._gas_state_rollup()
    assert err is None, err
    assert "NV" in states and "TX" in states, sorted(states)
    nv, tx = states["NV"], states["TX"]

    assert nv["dcgi"] is None, f"unpriced NV was scored {nv['dcgi']}"
    assert nv["gas_cost_score"] is None
    assert nv["verdict"] == dcgi._UNSCORED
    assert nv.get("unscored_reason"), "an unscored state must say why"
    assert nv["gas_access_score"] is not None, (
        "access is MEASURED for NV — withholding it too would discard a real "
        "number because a different one is missing")

    assert tx["dcgi"] is not None and tx["verdict"] != dcgi._UNSCORED


def test_the_old_defect_value_is_not_what_an_unpriced_state_gets(dcgi):
    """The specific regression: 50.0 must never appear as a cost score."""
    states, _ = dcgi._gas_state_rollup()
    assert states["NV"]["gas_cost_score"] != 50.0
    assert states["NV"]["gas_cost_score"] is None


def test_unscored_states_sort_last_and_never_crash_the_ranking(dcgi):
    """`key=lambda s: s["dcgi"]` raises TypeError the moment one is None, and
    three separate call sites used it."""
    rows = [{"state": "NV", "dcgi": None}, {"state": "TX", "dcgi": 81.9},
            {"state": "AK", "dcgi": None}, {"state": "OH", "dcgi": 61.5}]
    order = [s["state"] for s in sorted(rows, key=dcgi._dcgi_sort_key)]
    assert order == ["TX", "OH", "AK", "NV"], order


def test_unscored_states_are_excluded_from_the_leaderboard(dcgi, monkeypatch):
    monkeypatch.setattr(dcgi, "_operator_rollup", lambda: ([], 0))
    monkeypatch.setattr(dcgi, "gas_index_enabled", lambda: True)
    p = dcgi._report_payload()
    tops = [s["state"] for s in p["top_gas_advantaged"]]
    assert "NV" not in tops, "an unscored state reached a leaderboard"


def test_unscored_is_advertised_in_the_verdict_legend(dcgi):
    assert dcgi._UNSCORED in dcgi._VERDICTS, (
        "a consumer switching on verdict would meet an undocumented value")


def test_the_correction_record_is_published_while_the_index_is_LIVE(dcgi, monkeypatch):
    """Restoring quietly would orphan every citation of a pre-withdrawal score."""
    from flask import Flask
    monkeypatch.setattr(dcgi, "gas_index_enabled", lambda: True)
    app = Flask(__name__)
    app.register_blueprint(dcgi.dcgi_bp)
    body = app.test_client().get("/api/v1/dcgi/methodology").get_json()
    corr = body["methodology"]["corrections"]
    actions = {c["action"]: c["date"] for c in corr}
    assert actions.get("withdrawn") == "2026-08-08"
    assert actions.get("restored") == "2026-08-30"
    assert "still_withdrawn" in body["methodology"], (
        "gas-to-grid $/MWh is a SEPARATE unfixed defect from the same audit; "
        "the payload must not imply the whole audit is closed")
