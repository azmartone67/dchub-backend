"""Regression guards: no security detector may query a relation that does not
exist, and no caller may report a detector count it did not earn.

The defect these pin (2026-08-17). check_repeated_admin_401 queried
`rate_limit_events` — a table nothing in this repo has ever CREATEd, no
migration defines, and `to_regclass('public.rate_limit_events')` returns NULL
for on the production Neon DB. Its own soft-existence check turned that into
`return []`, which is byte-identical, to every caller, to "looked and found
nothing". So:

  * /api/v1/sentinel/security reported `detectors_run: 5` — a literal minus
    the error count, and a detector that returns [] raises nothing;
  * the 15-min surveillance sweep graded security ok on that number;
  * main.py's on-demand scan documented "Six detectors" over a tuple of eight.

Four months of "5 detectors run, 0 findings" while one of the five was
structurally incapable of producing a finding. That is the registered ≠
functioning class, and the only thing that catches it early is a check that
the relations are real.

Two layers, neither vacuous:

  1. STATIC (always runs, no DB). Every relation named in a SECURITY_DETECTORS
     member's SQL must appear in _VERIFIED_LIVE_RELATIONS. Referencing a new
     table fails the suite until someone adds it here, which is the moment to
     check that it exists.
  2. LIVE (runs when SECURITY_RELATIONS_DSN is set). Every pin in
     _VERIFIED_LIVE_RELATIONS must actually resolve via to_regclass, so the
     pin cannot quietly go stale. Opt-in under its OWN env var, never
     DATABASE_URL — 58 test files gate on that name and waking them against
     an empty database takes a required check down.

House rules: static AST extraction, no import of routes.brain_security_detectors
(it self-probes over HTTP), nothing executes at module scope.
"""
import ast
import os
import re

import pytest

_HERE = os.path.dirname(__file__)
_DET = os.path.join(_HERE, "..", "routes", "brain_security_detectors.py")
_SWEEP = os.path.join(_HERE, "..", "routes", "surveillance_sweep.py")

# Relations a security detector is allowed to read, each verified present on
# the production Neon DB on the date noted. Adding an entry is a claim that
# you checked; the live layer below re-checks it wherever a DSN exists.
_VERIFIED_LIVE_RELATIONS = {
    # 4.69M rows, ~107k/day, columns ip_address/endpoint/status_code/
    # created_at(timestamptz). Verified 2026-08-17.
    "ai_requests",
    # Read by check_hosting_traffic_share and check_privacy_traffic_share.
    # Verified 2026-08-17.
    "mcp_tool_calls",
}

# Relations known ABSENT from production. A detector naming one of these is
# the exact bug this file exists for, so it gets its own louder assertion
# rather than only failing the allow-list.
_KNOWN_DEAD_RELATIONS = {
    # to_regclass('public.rate_limit_events') IS NULL (verified 2026-08-17).
    # Nothing CREATEs it: rate_limiter.py and middleware.py hold their token
    # buckets in memory and persist nothing.
    "rate_limit_events",
}

# SQL noise that a FROM/JOIN regex can pick up but which is not a relation.
_NOT_A_RELATION = {"select", "lateral", "unnest", "generate_series", "values"}

_RELATION_RE = re.compile(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
                          re.IGNORECASE)


def _tree(path: str, min_body: int = 10) -> ast.Module:
    with open(os.path.abspath(path), "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    # Guard the guard: a degenerate parse would vacuously pass every search
    # below (2026-07-28 lesson — assert it parsed, never just filter).
    assert isinstance(tree, ast.Module) and len(tree.body) > min_body, (
        f"{path} parsed to a degenerate module — the extraction harness is "
        "not looking at the real file")
    return tree


def _fn(tree: ast.Module, name: str, where: str = _DET) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f"{where} no longer defines {name}() — this guard needs updating, "
        "not deleting")


def _tuple_members(tree: ast.Module, varname: str) -> list:
    """Names in a module-level `VAR = (a, b, c)` of bare identifiers, or the
    string elements of a tuple of string literals."""
    out: list = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if not (isinstance(t, ast.Name) and t.id == varname):
                continue
            for elt in getattr(node.value, "elts", []):
                if isinstance(elt, ast.Name):
                    out.append(elt.id)
                elif isinstance(elt, ast.Constant) and \
                        isinstance(elt.value, str):
                    out.append(elt.value)
    return out


def _sql_strings(fn: ast.FunctionDef) -> list:
    """String constants in the function BODY that look like SQL, docstring
    excluded. A guard a docstring can satisfy guards nothing."""
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and \
            isinstance(body[0].value, ast.Constant) and \
            isinstance(body[0].value.value, str):
        body = body[1:]
    out = []
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Constant) and \
                    isinstance(node.value, str) and \
                    re.search(r"\bSELECT\b", node.value, re.IGNORECASE):
                out.append(node.value)
    return out


def _relations_used(fn: ast.FunctionDef) -> set:
    """Relation names a detector reads: FROM/JOIN targets in its SQL, plus any
    relation named inside a to_regclass(...) existence check."""
    found = set()
    for sql in _sql_strings(fn):
        for name in _RELATION_RE.findall(sql):
            if name.lower() not in _NOT_A_RELATION:
                found.add(name)
        for name in re.findall(r"to_regclass\(\s*'(?:public\.)?([a-zA-Z_]\w*)'",
                               sql, re.IGNORECASE):
            found.add(name)
    return found


def _detector_relations() -> dict:
    """{detector_name: {relations}} for every SECURITY_DETECTORS member."""
    tree = _tree(_DET)
    names = _tuple_members(tree, "SECURITY_DETECTORS")
    assert names, ("SECURITY_DETECTORS did not parse to a tuple of detector "
                   "names — this guard is looking at nothing")
    return {n: _relations_used(_fn(tree, n)) for n in names}


# ── layer 1: static, always runs ────────────────────────────────────────────

def test_every_detector_relation_is_a_verified_live_one():
    """No detector may read a relation outside the verified-live pin."""
    for name, relations in _detector_relations().items():
        unknown = relations - _VERIFIED_LIVE_RELATIONS
        assert not unknown, (
            f"{name}() queries {sorted(unknown)}, which is not in "
            f"_VERIFIED_LIVE_RELATIONS. Confirm the relation exists in "
            f"production (SELECT to_regclass('public.<name>')) and add it "
            f"with the date, or point the detector at a table that does. A "
            f"detector reading a nonexistent relation still counts toward "
            f"detectors_run — that is the whole defect this guard exists for.")


def test_no_detector_reads_a_known_dead_relation():
    """The specific tombstone: rate_limit_events does not exist."""
    for name, relations in _detector_relations().items():
        dead = relations & _KNOWN_DEAD_RELATIONS
        assert not dead, (
            f"{name}() queries {sorted(dead)} — verified ABSENT from the "
            f"production DB. This is the exact shape that made "
            f"check_repeated_admin_401 a permanent no-op counted as healthy "
            f"from 2026-05-23 to 2026-08-17. The live request log is "
            f"`ai_requests`.")


def test_admin_scan_detector_reads_the_live_request_log():
    """Positive pin: the repaired detector actually reads ai_requests, so a
    revert to the dead table cannot pass by merely deleting a reference."""
    relations = _relations_used(_fn(_tree(_DET), "check_repeated_admin_401"))
    assert "ai_requests" in relations, (
        "check_repeated_admin_401() no longer reads ai_requests — the only "
        "live request log carrying ip_address + endpoint + status_code + a "
        "timestamptz created_at. If it was repointed, verify the new relation "
        "exists and update _VERIFIED_LIVE_RELATIONS.")


def test_admin_scan_detector_raises_rather_than_returning_empty():
    """A missing relation must be LOUD. The original returned [] — which every
    caller counts as a detector that ran and found nothing."""
    fn = _fn(_tree(_DET), "check_repeated_admin_401")
    raised = {n.exc.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
              and isinstance(n.exc.func, ast.Name)}
    assert "DetectorUnavailable" in raised, (
        "check_repeated_admin_401() no longer raises DetectorUnavailable. If "
        "it cannot reach its relation it must RAISE, so surveillance_sweep "
        "records an error and detectors_run drops. Returning [] restores the "
        "silent no-op: 'ran, found nothing' and 'could not look' become the "
        "same answer.")


def test_self_probe_exclusion_is_by_user_agent_not_only_ip():
    """Our own probes arrive on rotating Cloudflare POP IPs, so an IP-prefix
    allow-list cannot exclude them. In the 14 days to 2026-08-17 the only
    group to reach the threshold was dchub-contract-guard/1.0 on
    172.69.74.144 — our own audit, firing our own detector (the #2796 class).
    """
    tree = _tree(_DET)
    patterns = _tuple_members(tree, "_SELF_PROBE_UA_PATTERNS")
    assert patterns, (
        "_SELF_PROBE_UA_PATTERNS is gone. Self-traffic must be excluded by "
        "user-agent fingerprint; the egress /24 list alone never sufficed "
        "because our probes reach the origin via rotating CF POP IPs.")
    assert any("dchub" in p.lower() for p in patterns), (
        f"_SELF_PROBE_UA_PATTERNS {patterns} no longer matches our own "
        "'dchub-*' probe user-agents — the detector would fire on its own "
        "sibling detectors' unauthenticated admin probes.")
    fn = _fn(tree, "check_repeated_admin_401")
    sql = " ".join(_sql_strings(fn)).lower()
    assert "user_agent" in sql, (
        "check_repeated_admin_401()'s SQL no longer filters on user_agent")
    assert "platform" not in sql, (
        "check_repeated_admin_401()'s SQL filters on ai_requests.platform. "
        "detect_platform() buckets every generic HTTP-lib UA as 'internal', "
        "so 100% of admin-401 rows (20,656 over 7 days, 159 distinct IPs) "
        "carry platform='internal' — that predicate returns zero rows "
        "forever, which is this same dead detector in a new costume.")


def test_sweep_detector_count_is_derived_not_hardcoded():
    """detectors_run must come from what ran. It was `5 - len(errors)`, which
    reported full coverage for a detector that could never find anything."""
    tree = _tree(_SWEEP)
    listed = _tuple_members(tree, "_SWEEP_SECURITY_DETECTORS")
    assert listed, ("_SWEEP_SECURITY_DETECTORS is not a module-level tuple of "
                    "detector names — the sweep's count has no single source")
    fn = _fn(tree, "_sec_response", _SWEEP)
    for node in ast.walk(fn):
        if isinstance(node, ast.keyword) and node.arg == "detectors_run":
            assert not isinstance(node.value, (ast.Constant, ast.BinOp)), (
                "detectors_run is a literal (or literal arithmetic) again. It "
                "must be len() of the detectors that actually completed — a "
                "count asserted independently of what ran will drift from it, "
                "which is how '5 detectors run' outlived a dead detector by "
                "three months.")
            break
    else:
        raise AssertionError(
            "_sec_response() no longer reports detectors_run — the sweep "
            "grades security coverage on that field")


def test_sweep_runs_the_repaired_detector():
    """The sweep must still include the admin-scan detector; dropping it to
    make the count 'honest' would trade one silence for another."""
    listed = _tuple_members(_tree(_SWEEP), "_SWEEP_SECURITY_DETECTORS")
    assert "check_repeated_admin_401" in listed, (
        "check_repeated_admin_401 left the sweep's detector list. It reads a "
        "live table now — if it was removed because it looked dead, it is "
        "not dead any more.")


# ── layer 2: live, opt-in under its own env var ─────────────────────────────

def test_pinned_relations_exist_in_a_live_database():
    """Re-verify the pin against a real DB so it cannot go stale unnoticed.

    Own env var on purpose: DATABASE_URL gates 58 other test files and
    setting it here would wake them against whatever DB is handy."""
    dsn = os.environ.get("SECURITY_RELATIONS_DSN")
    if not dsn:
        pytest.skip("SECURITY_RELATIONS_DSN unset — live relation check "
                    "skipped (layer 1 still ran)")
    psycopg2 = pytest.importorskip("psycopg2")
    missing = []
    with psycopg2.connect(dsn, connect_timeout=10) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for rel in sorted(_VERIFIED_LIVE_RELATIONS):
                cur.execute("SELECT to_regclass(%s)", (f"public.{rel}",))
                if not (cur.fetchone() or [None])[0]:
                    missing.append(rel)
            # And prove the probe can say NO, so an all-green result above is
            # evidence and not a broken query returning truthy junk.
            cur.execute("SELECT to_regclass(%s)",
                        ("public.__definitely_not_a_table__",))
            control = (cur.fetchone() or [None])[0]
    assert control is None, (
        "to_regclass returned non-NULL for a table that cannot exist — the "
        "live probe is not measuring what it claims")
    assert not missing, (
        f"_VERIFIED_LIVE_RELATIONS names {missing}, which to_regclass says "
        f"do not exist. Either the pin is stale or a detector is reading a "
        f"dropped table.")


# ── must-fail controls ──────────────────────────────────────────────────────

def test_relation_extraction_can_actually_fail():
    """Prove _relations_used() finds the dead table in a detector shaped like
    the original, so a green run above means 'no dead relations' rather than
    'the extractor read nothing' (silent-green class, 2026-07-28)."""
    mutant = ast.parse(
        'def check_repeated_admin_401():\n'
        '    """Docstring naming rate_limit_events must NOT be enough."""\n'
        '    cur.execute("SELECT to_regclass(\'public.rate_limit_events\')")\n'
        '    cur.execute("SELECT ip_address FROM rate_limit_events '
        'WHERE status_code = 401")\n')
    found = _relations_used(_fn(mutant, "check_repeated_admin_401", "<mutant>"))
    assert "rate_limit_events" in found, (
        "the extractor missed a FROM rate_limit_events in a detector body — "
        "it cannot detect the bug it exists for")
    assert found & _KNOWN_DEAD_RELATIONS, \
        "the dead-relation intersection cannot fire"

    docstring_only = ast.parse(
        'def check_repeated_admin_401():\n'
        '    """SELECT * FROM rate_limit_events — named only in prose."""\n'
        '    cur.execute("SELECT 1 FROM ai_requests")\n')
    found2 = _relations_used(
        _fn(docstring_only, "check_repeated_admin_401", "<mutant>"))
    assert found2 == {"ai_requests"}, (
        f"a relation named only in the DOCSTRING was extracted ({found2}) — "
        "the guard would fail on prose and pass on real SQL")


def test_hardcoded_count_control():
    """Prove the detectors_run literal check can fail on the original code."""
    mutant = ast.parse(
        'def _sec_response(stale=False):\n'
        '    return jsonify(detectors_run=5 - len(_SEC_CACHE["errors"]))\n')
    fn = _fn(mutant, "_sec_response", "<mutant>")
    kw = [n for n in ast.walk(fn)
          if isinstance(n, ast.keyword) and n.arg == "detectors_run"]
    assert kw, "control mutant did not parse a detectors_run keyword"
    assert isinstance(kw[0].value, (ast.Constant, ast.BinOp)), (
        "the literal-detector does not recognise `5 - len(errors)` as "
        "hardcoded — it would have passed the original bug")
