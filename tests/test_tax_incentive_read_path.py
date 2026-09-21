"""Every backend reader of state tax incentives goes through util.tax_incentives.

tax_incentives_neon froze on 2026-03-17 (50 rows, source_url NULL on all of
them). By 2026-09-21 nine states' programs had changed — AZ IL NE OH paused to
new applicants, NJ repealed, MN NC WA partially repealed, OK restricted — and
nine backend readers still queried the table directly: site/state briefs, grid
intelligence, land-power, persona briefs, site valuation, the build-out
simulator, and the backend MCP failover. So OH read "100% sales tax exempt"
on those surfaces while REST /api/v1/tax-incentives said "paused".

What is pinned here:
  (1) no live module queries the table except through the accessor, and the
      files allowed to are exactly the ones listed, each for a stated reason;
  (2) the precedence rule — a verified registry row supersedes the snapshot;
      a closed program prices at nothing; a silent registry flag falls back;
      an unverified state reads the snapshot untouched;
  (3) the RAG corpus, which cannot be repointed, drops superseded states.

House rules: no DB, never import main, nothing at module scope.
Run:  python3 -m pytest tests/test_tax_incentive_read_path.py -v
"""
from __future__ import annotations

import ast
import os
import re
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The nine states named in the 2026-09-21 audit.
AUDITED = {"AZ", "IL", "NE", "NJ", "WA", "NC", "MN", "OK", "OH"}

# Registry status -> does the program still take new applicants?
STATUS_VOCAB = {
    "paused_new_applicants": False,
    "repealed": False,
    "partially_repealed": True,
    "restricted": True,
}

# Modules that serve incentives and must read them through the accessor.
LIVE_READERS = (
    "dchub_mcp_server.py",
    "routes/grid_intelligence_routes.py",
    "routes/land_power_mcp.py",
    "routes/persona_briefs.py",
    "routes/site_brief.py",
    "routes/site_simulator.py",
    "routes/site_valuation_engine.py",
    "routes/state_brief.py",
)

# Files still allowed a literal `FROM tax_incentives_neon`, and why.
DIRECT_ALLOWED = {
    "scripts/tax_20_states.py": "the table's writer (insert-only ops script)",
    "routes/brain_rag.py": "RAG hydrate; gated by util.tax_incentives.snapshot_serve_gate",
    "dchub_mcp_server.py": "a table row-count probe, not an incentive read",
    "mcp_new_tools.py": "dead install snippet, imported by nothing",
    "mcp_server_patch.py": "dead install snippet, imported by nothing",
    "grid_intelligence_routes_patched.py": "superseded copy; routes/ one is registered",
}
DEAD = ("mcp_new_tools", "mcp_server_patch", "grid_intelligence_routes_patched")

_FROM_NEON = re.compile(r"FROM\s+tax_incentives_neon\b", re.I)


@pytest.fixture(autouse=True)
def _module_defaults_only(monkeypatch):
    """Other tests register the routes on throwaway apps with test overrides
    layered on; the accessor would read whichever registered last."""
    import tax_incentives_routes
    monkeypatch.setattr(tax_incentives_routes, "_SERVED", [None])


def _py_files():
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.split() if not p.startswith("tests/")]


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _defaults():
    from tax_incentives_routes import DEFAULT_INCENTIVES
    return {s["abbr"]: s for s in DEFAULT_INCENTIVES}


class _Cur:
    """Serves snapshot rows for SELECT ... FROM tax_incentives_neon."""

    def __init__(self, rows, as_tuples=False, fail=False):
        self.rows, self.as_tuples, self.fail = rows, as_tuples, fail
        self.executed, self.rolled_back = [], False
        self.description = None
        self._out = []
        self.connection = self

    def rollback(self):
        self.rolled_back = True

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.fail:
            raise RuntimeError("relation is on fire")
        cols = [c.strip() for c in sql.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")]
        want = params[0] if params else None
        rows = [r for r in self.rows if want is None or r["state_abbr"] == want]
        self.description = [(c,) for c in cols]
        self._out = ([tuple(r.get(c) for c in cols) for r in rows] if self.as_tuples
                     else [{c: r.get(c) for c in cols} for r in rows])

    def fetchall(self):
        return self._out


def _snap(abbr, **kw):
    row = {"state_abbr": abbr, "state_name": abbr + "-name", "sales_tax_exempt": True,
           "property_tax_abatement": True, "energy_incentive": True,
           "data_center_specific": True, "incentive_details": "stale snapshot text",
           "qualifying_investment": "$1M+", "qualifying_jobs": "25+ new jobs",
           "duration_years": 15, "max_benefit": "100% sales tax exempt",
           "source_url": None, "last_updated": "2026-03-17"}
    row.update(kw)
    return row


# ── (1) the fence ─────────────────────────────────────────────────────────

def test_no_module_queries_the_snapshot_except_the_listed_ones():
    found = {p for p in _py_files() if _FROM_NEON.search(_src(p))}
    assert found - set(DIRECT_ALLOWED) == set(), (
        "these read tax_incentives_neon directly — it froze on 2026-03-17; "
        "use util.tax_incentives.state_incentive(): %s" % sorted(found - set(DIRECT_ALLOWED)))
    assert set(DIRECT_ALLOWED) - found == set(), (
        "allowlisted but no longer reading the table — remove from DIRECT_ALLOWED: %s"
        % sorted(set(DIRECT_ALLOWED) - found))


def test_the_live_files_on_the_allowlist_hold_only_their_stated_read():
    """File-level allowance is too coarse for the two live files: a new
    incentive query added beside the sanctioned one would pass the fence."""
    mcp = [ln.strip() for ln in _src("dchub_mcp_server.py").splitlines() if _FROM_NEON.search(ln)]
    assert mcp == ["('tax_incentives_neon', \"SELECT COUNT(*) FROM tax_incentives_neon\"),"], mcp
    rag = [ln.strip() for ln in _src("routes/brain_rag.py").splitlines() if _FROM_NEON.search(ln)]
    assert rag == ['"SELECT id, state_name, source_url FROM tax_incentives_neon "'], rag


def test_the_dead_copies_are_really_dead():
    pat = re.compile(r"^\s*(?:import|from)\s+(%s)\b" % "|".join(DEAD), re.M)
    wired = {p: pat.findall(_src(p)) for p in _py_files()}
    assert {p: m for p, m in wired.items() if m} == {}


@pytest.mark.parametrize("rel", LIVE_READERS)
def test_each_live_reader_uses_the_accessor(rel):
    assert "from util.tax_incentives import" in _src(rel), rel


# ── (2) the precedence rule ───────────────────────────────────────────────

def test_the_status_vocabulary_is_fully_classified():
    """An unclassified status is treated as CLOSED by util.tax_incentives.
    That is the safe default, but a new status should be a decision."""
    from util.tax_incentives import STILL_OPEN
    seen = {s["status"] for s in _defaults().values() if s.get("status")}
    assert seen <= set(STATUS_VOCAB), "classify in STATUS_VOCAB: %s" % sorted(seen - set(STATUS_VOCAB))
    for status, open_ in STATUS_VOCAB.items():
        assert (status in STILL_OPEN) is open_, status


def test_every_audited_state_is_superseded():
    from util.tax_incentives import superseded_states
    assert AUDITED <= set(superseded_states())


def test_a_paused_program_reads_closed_and_drops_the_stale_terms():
    from util.tax_incentives import state_incentive
    reg = _defaults()["OH"]
    assert reg["status"] == "paused_new_applicants"
    cur = _Cur([_snap("OH")])
    rec = state_incentive(cur, "oh")
    assert rec["provenance"] == "registry"
    assert rec["status"] == "paused_new_applicants" and rec["open_to_new_projects"] is False
    for col in ("sales_tax_exempt", "property_tax_abatement", "energy_incentive",
                "data_center_specific"):
        assert rec[col] is False, col
    assert rec["max_benefit"] is None and rec["duration_years"] is None
    assert rec["incentive_details"] == reg["summary"]
    assert rec["source_url"] == reg["source_url"] and rec["last_verified"]
    assert cur.executed == [], "a closed program needs nothing from the snapshot"


def test_the_registry_wins_every_flag_it_asserts():
    """MN's electricity exemption was repealed; the snapshot still says it."""
    from util.tax_incentives import state_incentive
    assert _defaults()["MN"]["electricity_tax"] is False
    rec = state_incentive(_Cur([_snap("MN", max_benefit="100% sales tax exempt on electricity")]), "MN")
    assert rec["energy_incentive"] is False
    assert rec["max_benefit"] is None
    assert rec["open_to_new_projects"] is True and rec["sales_tax_exempt"] is True


def test_a_flag_the_registry_is_silent_on_falls_back_to_the_snapshot():
    """OK's property-tax exemption stands (its status_note says so) but the
    registry row never recorded the flag. None is 'not recorded', not 'no'."""
    from util.tax_incentives import state_incentive
    assert _defaults()["OK"].get("property_tax") is None
    for as_tuples in (False, True):
        rec = state_incentive(_Cur([_snap("OK", property_tax_abatement=True)], as_tuples), "OK")
        assert rec["property_tax_abatement"] is True
        rec = state_incentive(_Cur([_snap("OK", property_tax_abatement=False)], as_tuples), "OK")
        assert rec["property_tax_abatement"] is False


def test_an_unverified_state_reads_the_snapshot_untouched():
    from util.tax_incentives import state_incentive, is_verified
    unverified = sorted(a for a, r in _defaults().items() if not is_verified(r))
    assert unverified, "every state verified — retire the snapshot and this test"
    abbr = unverified[0]
    snap = _snap(abbr)
    for as_tuples in (False, True):
        rec = state_incentive(_Cur([snap], as_tuples), abbr)
        assert rec["provenance"] == "snapshot" and rec["last_verified"] is None
        assert rec["as_of"] == "2026-03-17"
        for col in ("sales_tax_exempt", "incentive_details", "duration_years", "max_benefit"):
            assert rec[col] == snap[col], col


def test_the_registry_rows_are_not_mutated():
    from util.tax_incentives import all_state_incentives
    before = {a: dict(r) for a, r in _defaults().items()}
    out = all_state_incentives(_Cur([_snap(a) for a in before]))
    assert len(out) == len(before) == 50
    assert {a: dict(r) for a, r in _defaults().items()} == before


def test_a_snapshot_read_error_rolls_back_and_raises():
    from util.tax_incentives import state_incentive, is_verified
    abbr = next(a for a, r in sorted(_defaults().items()) if not is_verified(r))
    cur = _Cur([], fail=True)
    with pytest.raises(RuntimeError):
        state_incentive(cur, abbr)
    assert cur.rolled_back, "a failed read left the connection aborted for the caller"


@pytest.mark.parametrize("text,years", [
    ("Up to 20 years", 20), ("10–20 years", 10), ("20 yr + permanent", 20),
    ("Up to 25–50 years", 25), ("Varies", None), ("Varies by tier", None),
    ("Through 2050/2065", None), (None, None),
])
def test_term_years(text, years):
    from util.tax_incentives import term_years
    assert term_years(text) == years


# ── the readers price a closed program at nothing ─────────────────────────

def _extract(rel, name, env):
    tree = ast.parse(_src(rel))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = dict(env)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), rel, "exec"), ns)
    return ns[name]


def test_site_brief_no_longer_calls_a_paused_program_exempt():
    fn = _extract("routes/site_brief.py", "_tax_for_state", {})
    out = fn(_Cur([_snap("OH")]), "OH")
    assert out["sales_tax_exempt"] is False and out["status"] == "paused_new_applicants"


def test_site_valuation_prices_no_premium_for_a_paused_program():
    import contextlib

    @contextlib.contextmanager
    def _db_conn():
        yield _Conn()

    class _Conn:
        def cursor(self):
            return _Cur([_snap("OH"), _snap("VA")])

    fn = _extract("routes/site_valuation_engine.py", "_fetch_tax_abatement", {"_db_conn": _db_conn})
    oh = fn("OH")
    assert oh["abatement"] is False and oh["pct"] == 0.0 and "paused" in oh["note"]


# ── (3) the RAG corpus ────────────────────────────────────────────────────

def test_the_rag_drops_every_superseded_state():
    from routes import brain_rag
    from util.tax_incentives import superseded_states
    gate = brain_rag.serve_gates()["tax_incentives_neon"]
    for abbr in superseded_states():
        assert "'%s'" % abbr in gate, abbr
    spec = brain_rag._HYDRATE["tax_incentives_neon"]
    assert gate in brain_rag._gated_sql("tax_incentives_neon", spec[0])


def test_the_rag_fails_closed_when_the_registry_cannot_be_read(monkeypatch):
    from routes import brain_rag
    import util.tax_incentives as ti

    def boom():
        raise ImportError("registry unavailable")
    monkeypatch.setattr(ti, "snapshot_serve_gate", boom)
    assert brain_rag.serve_gates()["tax_incentives_neon"] == "FALSE"
