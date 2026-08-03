"""#49 lane 4: one resolved identity, one answer (2026-08-02).

House rule: tests NEVER import main. Everything here imports leaf modules or
reads files directly, and nothing runs at module scope.

Measured repeatedly and never fixed: the agent portal says 62, reach says 99,
funnel says 99, and one payload carries real_external_7d=2637 next to
real_external_calls_7d=8641. Nothing is wrong with any single number — each
surface re-derives "how many agents" from raw call rows with its own WHERE
clause, and there is no node to make them the same number.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ── resolution: strongest signal wins, and it SAYS which ──────────────

def test_api_key_beats_platform_beats_ip():
    from routes.agent_identity import identity_key, KEYED, PLATFORM, ANONYMOUS
    assert identity_key({"api_key": "k1", "platform": "claude",
                         "ip_address": "1.2.3.4"}) == ("key:k1", KEYED)
    assert identity_key({"platform": "claude", "ip_address": "1.2.3.4"}) \
        == ("plat:claude", PLATFORM)
    assert identity_key({"ip_address": "1.2.3.4"}) == ("ip:1.2.3.4", ANONYMOUS)


def test_unidentifiable_rows_are_not_an_identity():
    """'Unidentifiable' is not a bucket. Rows with no signal must be skipped,
    not folded together into one enormous phantom agent."""
    from routes.agent_identity import identity_key
    assert identity_key({}) == ("", "")
    assert identity_key({"api_key": "", "platform": "", "ip_address": ""}) == ("", "")
    assert identity_key(None) == ("", "")


def test_synthetic_platforms_do_not_become_platform_identities():
    from routes.agent_identity import identity_key, ANONYMOUS
    key, kind = identity_key({"platform": "dchub-selfheal", "ip_address": "9.9.9.9"})
    assert kind == ANONYMOUS and key == "ip:9.9.9.9"


# ── behaviour: a crawler is not demand ────────────────────────────────

def test_enumeration_is_broad_and_shallow():
    """~75 of 99 'real external agents' hit essentially every tool ~50x —
    everything once, nothing twice."""
    from routes.agent_identity import classify, ENUMERATION
    assert classify(distinct_tools=60, calls=62) == ENUMERATION
    assert classify(distinct_tools=40, calls=50) == ENUMERATION


def test_integration_is_narrow_and_deep():
    from routes.agent_identity import classify, INTEGRATION
    assert classify(distinct_tools=3, calls=90) == INTEGRATION
    assert classify(distinct_tools=10, calls=40) == INTEGRATION


def test_a_new_arrival_is_sparse_not_a_crawler():
    """★A new integration's first day looks like nothing. Counting it as
    enumeration would make the north-star number punish exactly the arrivals
    it should be celebrating."""
    from routes.agent_identity import classify, SPARSE, demand_total, INTEGRATION
    assert classify(distinct_tools=2, calls=2) == SPARSE
    counts = {"ok": True, "behaviour": {INTEGRATION: 4, "enumeration": 70,
                                        SPARSE: 6}}
    assert demand_total(counts) == 10, "sparse arrivals belong in demand"


def test_demand_excludes_enumeration():
    from routes.agent_identity import demand_total
    counts = {"ok": True, "behaviour": {"integration": 5, "enumeration": 900,
                                        "sparse": 0}}
    assert demand_total(counts) == 5


def test_demand_of_an_unmeasured_count_is_zero_not_a_guess():
    from routes.agent_identity import demand_total
    assert demand_total({"ok": False, "error": "no db"}) == 0
    assert demand_total(None) == 0


def test_classify_survives_garbage():
    from routes.agent_identity import classify, SPARSE
    assert classify(None, None) == SPARSE
    assert classify("x", "y") == SPARSE
    assert classify(0, 0) == SPARSE


# ── the canonical count ───────────────────────────────────────────────

class _Cur:
    """Minimal cursor: answers the introspection probe, then the group-by."""

    def __init__(self, rows, tool_col="tool"):
        self._rows = rows
        self._tool_col = tool_col
        self._mode = None

    def execute(self, sql, args=None):
        if "information_schema" in sql:
            self._mode = "col"
            self._want = args[0] if args else ""
        else:
            self._mode = "rows"

    def fetchone(self):
        if self._mode == "col":
            return (1 if self._want == self._tool_col else 0,)
        return None

    def fetchall(self):
        return self._rows


def test_two_rows_that_resolve_to_one_agent_count_once():
    """THE point. A COUNT(DISTINCT ip_address) sees two; the canon sees one
    integration calling from two addresses under one key."""
    from routes.agent_identity import identity_counts, KEYED
    cur = _Cur([("k1", "claude", "1.1.1.1", 3, 40),
                ("k1", "claude", "2.2.2.2", 3, 50)])
    c = identity_counts(cur, days=7)
    assert c["ok"] is True
    assert c["total"] == 1
    assert c["resolved_by"][KEYED] == 1


def test_distinct_tools_are_maxed_not_summed():
    """Summing distinct-tool counts across overlapping row groups over-counts
    breadth and would mislabel a deep integration as a crawler."""
    from routes.agent_identity import identity_counts, INTEGRATION
    cur = _Cur([("k1", "", "1.1.1.1", 3, 45),
                ("k1", "", "2.2.2.2", 3, 45)])
    c = identity_counts(cur, days=7)
    assert c["behaviour"][INTEGRATION] == 1, c["behaviour"]


def test_the_total_never_travels_without_its_breakdown():
    """A surface printing one number without saying how it was resolved is how
    62 / 99 / 99 happened."""
    from routes.agent_identity import identity_counts
    cur = _Cur([("", "claude", "1.1.1.1", 40, 45)])
    c = identity_counts(cur, days=7)
    assert set(c["resolved_by"]) == {"keyed", "platform", "anonymous"}
    assert set(c["behaviour"]) == {"integration", "enumeration", "sparse"}
    assert "keyed" in c["note"] and "enumeration" in c["note"]


def test_unmeasurable_is_not_zero():
    from routes.agent_identity import identity_counts

    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("no db")

        def fetchone(self):
            raise RuntimeError("no db")

        def fetchall(self):
            raise RuntimeError("no db")

    c = identity_counts(_Boom(), days=7)
    assert c["ok"] is False and c["total"] == 0 and "UNMEASURED" in c["error"]


def test_tool_column_is_introspected_not_assumed():
    """The tree uses `tool` in ~163 places and `tool_name` in one."""
    from routes.agent_identity import identity_counts
    c = identity_counts(_Cur([("k", "", "1.1.1.1", 2, 9)], tool_col="tool_name"),
                        days=7)
    assert c["ok"] is True


def test_no_new_table_is_created():
    """★The fix for 'every surface computes its own answer' cannot be a fifth
    store that drifts from the other four."""
    src = _src("routes", "agent_identity.py")
    body = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))
    for verb in ("CREATE TABLE", "INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert verb not in body.upper(), f"the resolver writes: {verb}"


# ── the shell's lane ──────────────────────────────────────────────────

def test_lane_4_reports_the_canon_and_stays_red_until_surfaces_use_it():
    """The resolver existing is not the same as the surfaces calling it. Going
    green before those edits land is the green-by-silence this shell prevents."""
    from routes.graph_master_shell import _lane_identity

    class _NullCur:
        def execute(self, *a, **k):
            return None

        def fetchone(self):
            return None

        def fetchall(self):
            return []

    ids = {c["id"]: c for c in _lane_identity(_NullCur())}
    assert "identity_canon" in ids
    assert ids["identity_store"]["pass"] is None, (
        "absence of a materialised table is a decision, not a defect")


def test_lane_4_names_the_actuator():
    src = _src("routes", "graph_master_shell.py")
    assert "agent_identity.identity_counts()" in src
    assert "surfaces project the canon instead of a raw key" in src
    # The lane must state WHY it is red, not merely that it is.
    assert "The resolver now exists; RED until the" in src
    assert re.search(r'"identity_agrees",[^)]*?\n\s*False,', src), (
        "identity_agrees must be an explicit FAIL, not a computed spread")
