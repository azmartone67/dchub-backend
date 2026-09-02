"""REPO-WIDE FENCE: a pytest node ID must not name the checkout it ran in.

The bug class
-------------
`pytest.mark.parametrize` copies a string argvalue verbatim into the node ID.
Parametrize over an absolute path and the ID carries this machine's checkout:

    tests/test_x.py::test_y[/Users/me/.claude/worktrees/hungry-brattain-0a65fd/
                            dchub_mcp_server.py]

Two runs from two checkouts then share NO test names for that file. That
defeats diffing failures by NAME against a baseline — the one reliable way to
tell a real regression from a pre-existing failure — because every test in the
file reads as "removed" and a same-named one as "added". With a worktree per
change that is the normal case here, and it makes CI-vs-local diffs noise too.

#3530 fixed the artifact instance (contracts/api_response_surface.json recorded
one machine's absolute path). #3542 fixed the test-ID instance in
tests/test_transmission_table_identity_guard.py. Both were single-site repairs.
This is the class fence: it is what stops instance #3.

TWO CHECKS, DELIBERATELY DIFFERENT IN KIND
-------------------------------------------
1. `test_no_collected_node_id_names_this_checkout` reads the REAL node IDs of
   everything pytest collected this run, so it judges outcomes rather than
   source. Its coverage is whatever the invocation collected — full under
   `pytest tests/`, narrower under `-k` or a single-file run.

   ★ It is NOT inference-free, and the first cut of this file claimed it was.
     Half of it is exact (does the ID contain THIS checkout's root?); the other
     half is a heuristic for OTHER machines' roots, and that heuristic read
     "s:/" inside "https://" as a Windows drive letter and failed unit-tests on
     54 URL-bearing IDs. `_REAL_URL_IDS` pins those shapes as controls.
2. `test_no_test_file_parametrizes_over_a_checkout_derived_path` statically
   sweeps every file under tests/ regardless of what was selected, so its
   coverage is total and floored. It is the one that cannot be narrowed by an
   invocation flag.

Neither alone is enough: (1) can be dodged by not running the file, (2) reasons
about source rather than outcomes. Together they cover both.

What counts as an offence — and what deliberately does NOT
----------------------------------------------------------
The property is "the ID must not VARY BY CHECKOUT", not "the ID must not
contain a slash".

  * Measured 2026-09-02: the 478 parametrize calls under tests/ contain 29
    distinct absolute-looking string literals, and every one is a URL path
    ('/api/v1/stats', '/pricing') or a traversal fixture ('/etc/passwd').
    Identical in every checkout — NOT offences.
  * An explicit `ids=` is the other sanctioned fix, and 8 files already use it
    (`ids=lambda p: p.name`, `ids=[os.path.relpath(p, _REPO) for p in ...]`).
    Those parametrize over absolute paths and are CORRECT, because the ID is
    built from the label, not the value. The static sweep treats an explicit
    `ids=` as satisfying the contract rather than trying to prove the labels
    are clean — and check (1) is what catches an `ids=` that is itself
    absolute, from the resulting ID rather than from the source.

Flagging either group would make this fence cry wolf across 12 files on day
one, and a fence that cries wolf gets deleted.

So the static sweep reports an offence only when the argvalues are
CHECKOUT-DERIVED — computed from __file__ / os.getcwd / abspath / realpath /
Path.resolve, directly or through a module-level constant or a local helper, or
a literal hardcoding a machine root — AND no explicit `ids=` renames them.

Why the controls exist
----------------------
BOTH SWEEPS ARE VACUOUS TODAY. There are zero offenders — #3542 fixed the last
one — so "nothing offends" is true of zero things and would stay green with the
detector broken, the taint analysis reverted, or the glob repointed at an empty
directory. That is exactly the shape tests/_scan_floors.py exists to stop. So
each detector is proved to FIRE first, against the real shapes that shipped,
and proved NOT to fire on the benign ones, before either sweep is trusted.

Run:  python3 -m pytest tests/test_parametrize_ids_are_checkout_independent.py -v
"""
from __future__ import annotations

import ast
import glob
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

# Calls whose result depends on where the checkout lives.
_SEED_CALLS = {"abspath", "realpath", "getcwd", "resolve", "cwd", "expanduser"}

# Literals that hardcode one machine's filesystem. Deliberately narrow: it must
# not match a URL path, which is what nearly every absolute literal here is.
# ★ The drive-letter half is the one that bit. `[A-Za-z]:[\\/]` searched
#   unanchored matches "s:/" inside "https://" and "g:/" inside "finding:/api",
#   which failed unit-tests on 54 node IDs whose params are URLs. Two guards,
#   either of which alone is sufficient, kept for defence in depth:
#     (?<![A-Za-z])  a drive letter is ONE letter — "https" has "p" before "s"
#     (?!/)          a drive path is "C:/", a URL scheme is "://"
_DRIVE = r"(?<![A-Za-z])[A-Za-z]:[\\/](?!/)"
_UNIX_MACHINE = r"/(?:Users|home|private|tmp|var/folders|Volumes)/"
_MACHINE_ROOT = r"(?:" + _DRIVE + r"|" + _UNIX_MACHINE + r")"
_MACHINE_PATH_RE = re.compile(r"^" + _MACHINE_ROOT)
_MACHINE_PATH_SEARCH_RE = re.compile(_MACHINE_ROOT)

# Real node IDs from the suite whose params are URLs or URL-ish text. These are
# the exact strings that made unit-tests red on the first cut of this file, kept
# verbatim so the regression cannot return quietly.
_REAL_URL_IDS = (
    "tests/test_bare_link_credit.py::test_destinations_count_as_links"
    "[See https://dchub.cloud/dcpi for the index]",
    "tests/test_claim_ledger.py::test_parse_metric"
    "[finding:/api/v1/admin/facility-dedup/analyze?country=FR status-expect1]",
    "tests/test_media_post_quality.py::"
    "test_a_call_to_action_ending_in_a_url_is_not_truncation"
    "[Browse the tool surface: https://dchub.cloud/capabilities]",
    "tests/test_claim_ledger.py::test_parse_metric[get:/api/v1/stats facilities-expect3]",
)

# The pre-#3542 shape, kept verbatim as the must-fail control. If the detector
# stops flagging THIS, the sweeps below protect nothing.
_SHIPPED_2026_09 = '''
import os
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INFRA = os.path.join(ROOT, "routes", "infrastructure_data_routes.py")
MCP = os.path.join(ROOT, "dchub_mcp_server.py")


@pytest.mark.parametrize("path", [INFRA, MCP])
def test_spatial_consumers_ship_a_coverage_block(path):
    assert path
'''

# The same defect reached through a helper — the shape the FIX introduced, so
# the fence has to see through one level of indirection or it would bless a
# regression written in the new idiom.
_VIA_HELPER = '''
import os
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _abs(rel):
    return os.path.join(ROOT, *rel.split("/"))


INFRA = _abs("routes/infrastructure_data_routes.py")


@pytest.mark.parametrize("path", [INFRA])
def test_x(path):
    assert path
'''

# The landed fix. Must stay silent, or the fence forbids its own remedy.
_FIXED = '''
import os
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REL_INFRA = "routes/infrastructure_data_routes.py"
REL_MCP = "dchub_mcp_server.py"


def _abs(rel):
    return os.path.join(ROOT, *rel.split("/"))


@pytest.mark.parametrize("rel,label", [
    (REL_INFRA, "paid Land & Power map layer"),
    (REL_MCP, "MCP transmission layer served to agents"),
])
def test_x(rel, label):
    assert _abs(rel)
'''

# The OTHER sanctioned fix, drawn from real call sites in this suite
# (test_shell_killswitch_never_5xx.py, test_ai_page_no_simulated_feed.py).
# Absolute argvalues, but the ID is built from the label.
_EXPLICIT_IDS = '''
import os
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHELLS = [os.path.join(_REPO, "routes", "a.py")]


@pytest.mark.parametrize("path", _SHELLS, ids=lambda p: os.path.basename(p))
def test_x(path):
    assert path


@pytest.mark.parametrize("path", _SHELLS,
                         ids=[os.path.relpath(p, _REPO) for p in _SHELLS])
def test_y(path):
    assert path
'''

# The benign majority, drawn from real call sites in this suite.
_BENIGN = '''
import pytest


@pytest.mark.parametrize("path", ["/api/v1/stats", "/pricing", "/etc/passwd"])
def test_urls(path):
    assert path


@pytest.mark.parametrize("n,expected", [(1, 2), (3, 4)])
def test_numbers(n, expected):
    assert n < expected
'''


# ── the node-ID detector (ground truth) ──────────────────────────────────────

def id_names_a_checkout(nodeid):
    """Does this node ID carry a filesystem location?

    Only the parametrised part is examined: the path BEFORE the first '[' is
    the test file's own repo-relative path, which is supposed to contain
    slashes and is identical in every checkout.
    """
    if "[" not in nodeid:
        return False
    params = nodeid[nodeid.index("["):]
    if _ROOT in params:
        return True
    return bool(_MACHINE_PATH_SEARCH_RE.search(params))


# ── the static detector ──────────────────────────────────────────────────────

def _call_name(node):
    """Terminal name of a call target: os.path.join -> 'join', _abs -> '_abs'."""
    fn = node.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return getattr(fn, "id", None)


def _is_derived(node, tainted_names, tainted_funcs, follow_funcs=True):
    """Does this expression depend on where the checkout lives?

    `follow_funcs` is the difference between the two questions this is asked.

    Tainting a module CONSTANT must follow local helpers, or `INFRA = _abs(...)`
    reads as clean and the whole indirection case is missed.

    Judging ARGVALUES must NOT, because "this helper touches the repo" says
    nothing about what it RETURNS. Real example: `parametrize("domain",
    sorted(_real_sources()))` in test_freshness_sources_are_real.py — the
    helper parses a source file and returns domain NAMES ('mna', 'news'). It is
    correct code, and following the call flags it. Helper-call argvalues are
    therefore left to `test_no_collected_node_id_names_this_checkout`, which
    judges the resulting ID instead of guessing from the source.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            if sub.id == "__file__" or sub.id in tainted_names:
                return True
        elif isinstance(sub, ast.Call):
            name = _call_name(sub)
            if name in _SEED_CALLS or (follow_funcs and name in tainted_funcs):
                return True
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if _MACHINE_PATH_RE.match(sub.value):
                return True
    return False


def _taint(tree):
    """Module-level names, and local helpers, that carry a checkout-derived value.

    Iterated to a fixpoint: ROOT taints INFRA, and `_abs` (which reads ROOT)
    taints anything assigned from a call to it.
    """
    names: set[str] = set()
    funcs: set[str] = set()
    for _ in range(10):
        before = (len(names), len(funcs))
        for node in tree.body:
            if isinstance(node, ast.Assign) and _is_derived(node.value, names, funcs):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        names.add(tgt.id)
            elif (isinstance(node, ast.AnnAssign) and node.value is not None
                    and isinstance(node.target, ast.Name)
                    and _is_derived(node.value, names, funcs)):
                names.add(node.target.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(_is_derived(stmt, names, funcs) for stmt in node.body):
                    funcs.add(node.name)
        if (len(names), len(funcs)) == before:
            break
    return names, funcs


def _parametrize_calls(tree):
    """Every parametrize call, paired with the test it decorates."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and _call_name(dec) == "parametrize":
                    out.append((dec, node.name))
        elif isinstance(node, ast.Assign):
            # module-level `pytestmark = [pytest.mark.parametrize(...)]`
            if any(isinstance(t, ast.Name) and t.id == "pytestmark"
                   for t in node.targets):
                for sub in ast.walk(node.value):
                    if isinstance(sub, ast.Call) and _call_name(sub) == "parametrize":
                        out.append((sub, "pytestmark"))
    return out


def _argvalues(call):
    """The argvalues expression, positional or by keyword."""
    if len(call.args) > 1:
        return call.args[1]
    for kw in call.keywords:
        if kw.arg == "argvalues":
            return kw.value
    return None


def _has_explicit_ids(call):
    return any(kw.arg == "ids" for kw in call.keywords)


def scan_source(src, path="<memory>"):
    """-> (offences, parametrize_calls_seen). Pure function of the source text."""
    tree = ast.parse(src, filename=path)
    tainted_names, tainted_funcs = _taint(tree)
    offences = []
    calls = _parametrize_calls(tree)
    for call, owner in calls:
        if _has_explicit_ids(call):
            continue          # the label, not the value, becomes the ID
        values = _argvalues(call)
        if values is None:
            continue
        if _is_derived(values, tainted_names, tainted_funcs, follow_funcs=False):
            offences.append(
                f"{os.path.basename(path)}::{owner} (line {call.lineno}) "
                "parametrizes over a checkout-derived path with no explicit "
                "ids=, so the node ID will carry this machine's checkout")
    return offences, len(calls)


# ── 1. the detectors must FIRE — both sweeps are vacuous without this ────────

def test_detector_flags_the_shape_that_actually_shipped():
    """The pre-#3542 decorator, verbatim."""
    offences, seen = scan_source(_SHIPPED_2026_09, "test_shipped.py")
    assert seen == 1, "the control stopped being a parametrize call at all"
    assert offences, (
        "the detector no longer flags parametrizing over `INFRA`, the exact "
        "shape #3542 removed — every other assertion in this file is vacuous")


def test_detector_sees_through_the_helper_indirection():
    """`INFRA = _abs("rel")` is still checkout-derived — one hop, same defect.

    This is the shape the FIX introduced, so a fence blind to it would bless
    the regression written in the current idiom.
    """
    offences, seen = scan_source(_VIA_HELPER, "test_helper.py")
    assert seen == 1
    assert offences, (
        "the taint analysis does not follow a local helper that closes over a "
        "checkout-derived constant")


def test_detector_flags_a_hardcoded_machine_path():
    src = ('import pytest\n\n'
           '@pytest.mark.parametrize("p", ["/Users/someone/repo/x.py"])\n'
           'def test_x(p):\n    assert p\n')
    offences, _ = scan_source(src, "test_hardcoded.py")
    assert offences, "a literal /Users/... path in an argvalue is not flagged"


def test_node_id_detector_flags_the_id_that_actually_shipped():
    """The ID observed on 2026-09-01, before #3542."""
    shipped = (
        "tests/test_transmission_table_identity_guard.py::"
        "test_spatial_consumers_ship_a_coverage_block"
        "[/Users/jonathanmartone/dchub-backend/.claude/worktrees/"
        "hungry-brattain-0a65fd/dchub_mcp_server.py]")
    assert id_names_a_checkout(shipped), (
        "the node-ID detector no longer flags the exact ID that shipped — the "
        "ground-truth sweep is now decoration")
    assert id_names_a_checkout(f"tests/t.py::test_x[{_ROOT}/routes/a.py]"), (
        "an ID carrying THIS checkout's root is not flagged")


# ── 2. and they must stay SILENT on the benign majority ──────────────────────

def test_detector_does_not_flag_the_landed_fix():
    offences, seen = scan_source(_FIXED, "test_fixed.py")
    assert seen == 1
    assert offences == [], (
        f"the fence forbids its own remedy — the repo-relative form is what "
        f"#3542 landed and what this file tells people to write: {offences}")


def test_detector_does_not_flag_explicit_ids():
    """8 files already fix this the other sanctioned way. Flagging them would
    demand a pointless rewrite of correct code."""
    offences, seen = scan_source(_EXPLICIT_IDS, "test_ids.py")
    assert seen == 2
    assert offences == [], (
        f"an explicit ids= already keeps the checkout out of the node ID, but "
        f"the sweep flags it anyway: {offences}")


def test_detector_does_not_flag_url_paths_or_plain_data():
    """29 of the absolute literals under tests/ are URLs. Flagging them would
    make this fence cry wolf on day one."""
    offences, seen = scan_source(_BENIGN, "test_benign.py")
    assert seen == 2
    assert offences == [], f"benign argvalues were flagged: {offences}"


def test_detector_does_not_flag_a_helper_that_returns_labels():
    """The real shape from test_freshness_sources_are_real.py: a helper that
    parses a repo file and returns domain NAMES. Correct code — and the reason
    argvalues analysis does not follow helper calls. The dynamic sweep owns
    that case; see _is_derived's `follow_funcs`."""
    src = ('import os\n'
           'import pytest\n\n'
           '_ROOT = os.path.dirname(os.path.abspath(__file__))\n\n\n'
           'def _real_sources():\n'
           '    return {"mna": "a", "news": "b"} and open(_ROOT) and {}\n\n\n'
           '@pytest.mark.parametrize("domain", sorted(_real_sources()))\n'
           'def test_x(domain):\n    assert domain\n')
    offences, seen = scan_source(src, "test_freshness.py")
    assert seen == 1
    assert offences == [], (
        f"a helper that reads the repo but returns labels is flagged: {offences}")


def test_node_id_detector_does_not_flag_url_bearing_ids():
    """REGRESSION CONTROL. The first cut of this file failed unit-tests on 54
    real node IDs because the drive-letter branch matched "s:/" inside
    "https://". These are four of those IDs, verbatim.

    A fence that cries wolf gets deleted, and this one cried wolf on its own
    first CI run.
    """
    for nodeid in _REAL_URL_IDS:
        assert not id_names_a_checkout(nodeid), (
            f"the drive-letter branch is matching inside a URL again: {nodeid}")


def test_node_id_detector_still_flags_a_real_windows_drive_path():
    """The narrowing must not cost the detector the case it was there for."""
    for nodeid in ("tests/t.py::test_x[C:/repo/dchub-backend/a.py]",
                   "tests/t.py::test_x[D:\\repo\\a.py]"):
        assert id_names_a_checkout(nodeid), nodeid


def test_node_id_detector_does_not_flag_ordinary_ids():
    for benign in (
        "tests/test_x.py::test_y",
        "tests/test_x.py::test_y[routes/infrastructure_data_routes.py-"
        "paid Land & Power map layer]",
        "tests/test_x.py::test_y[dchub_mcp_server.py]",
        "tests/test_x.py::test_y[/api/v1/stats]",
        "tests/test_x.py::test_y[/etc/passwd]",
    ):
        assert not id_names_a_checkout(benign), benign


# ── 3. only now, the sweeps ──────────────────────────────────────────────────

def test_no_collected_node_id_names_this_checkout(request):
    """GROUND TRUTH: the real IDs pytest built for everything collected here.

    Coverage is whatever this invocation collected — full under `pytest tests/`
    (which is what CI runs), narrower under `-k` or a single-file run. The
    static sweep below is the one whose coverage no flag can narrow, so this
    one carries no floor; it reports what it saw.
    """
    items = list(getattr(request.session, "items", []))
    offenders = sorted({i.nodeid for i in items if id_names_a_checkout(i.nodeid)})
    assert not offenders, (
        f"{len(offenders)} collected node ID(s) name the checkout they ran in, "
        f"out of {len(items)} inspected. Two runs from two worktrees will "
        "share no name for these, so a failure diff by name reports them as "
        "removed-and-re-added:\n  - " + "\n  - ".join(offenders[:20]))


def test_no_test_file_parametrizes_over_a_checkout_derived_path():
    """TOTAL COVERAGE: every file under tests/, whatever was selected to run."""
    files = sorted(glob.glob(os.path.join(_HERE, "*.py")))

    offences = []
    calls_seen = 0
    parsed = 0
    for path in files:
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        try:
            found, n = scan_source(src, path)
        except SyntaxError:
            continue          # a broken file is syntax-check's job, not this one
        parsed += 1
        calls_seen += n
        offences.extend(found)

    # Vacuity floors. A sweep that inspected nothing is byte-identical to one
    # that found everything clean, and this file is the only thing standing
    # between the repo and instance #3. Measured 2026-09-02: 809 files, 478
    # parametrize calls; floors set ~20% under, as collapse detectors.
    assert parsed >= 640, (
        f"only {parsed} test files parsed (expected ~809). The glob is stale "
        "or the files moved — this sweep is no longer covering the suite.")
    assert calls_seen >= 380, (
        f"only {calls_seen} parametrize calls were inspected (expected ~478). "
        "The detector's decorator matching has gone blind — it is passing "
        "without checking anything.")

    assert not offences, (
        "these parametrize decorators put the checkout location into the "
        "pytest node ID, so a failure diff by test NAME across two checkouts "
        "reports the whole file as removed-and-re-added:\n  - "
        + "\n  - ".join(offences)
        + "\n\nFix, either way: parametrize over the repo-RELATIVE path and "
        "resolve it inside the test body (see "
        "tests/test_transmission_table_identity_guard.py, #3542), or pass an "
        "explicit ids= built from the basename or relative path (see "
        "tests/test_shell_killswitch_never_5xx.py).")
