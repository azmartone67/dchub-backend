"""loop-control's cron_liveness lane must honour the retirement allowlist.

THE BUG
-------
The lane's docstring has always claimed:

    "Same source and same threshold as
     brain_consistency_radar._check_cron_silently_dead."

On source and threshold it was right. But the radar ALSO skips
`_INTENTIONAL_STALE_CRONS`, and this lane never did — so five jobs that were
deliberately retired, with written reasons, kept the lane red for weeks:

    content-publish, global-intelligence, ai-outreach, ai-ecosystem
        retired 2026-08-07 — their only driver was heroic-reprieve's frozen
        dchub-scheduler-v4 zombie, whose every call 401'd after the 07-31 key
        rotation
    energy-discovery
        retired 2026-08-21 — its five HIFLD ArcGIS sources are dead (400/499)

Measured against the live table on 2026-08-31: counting retired jobs gives 5
past threshold; excluding them gives 0, and the worst remaining job is
gas-refresh at 26.6h, inside the 48h bar. The decision had been recorded and
executed — only this reader disagreed with it.

The fix imports the radar's set rather than re-declaring it, because a second
copy is how the two would drift apart again, and a duplicated allowlist is
worse than no allowlist: both look authoritative.
"""

import ast
import pathlib

SRC = (pathlib.Path(__file__).resolve().parents[1]
       / "routes" / "loop_control_master_shell.py")
TEXT = SRC.read_text()
TREE = ast.parse(TEXT)

RADAR = (pathlib.Path(__file__).resolve().parents[1]
         / "routes" / "brain_consistency_radar.py")
RADAR_TEXT = RADAR.read_text()


def _fn(name):
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _src(name):
    return ast.get_source_segment(TEXT, _fn(name))


# ── one list, imported, never copied ─────────────────────────────────

def test_the_allowlist_is_imported_not_redeclared():
    """A duplicated allowlist drifts, and both copies look authoritative."""
    s = _src("_retired_crons")
    assert "from routes.brain_consistency_radar import _INTENTIONAL_STALE_CRONS" in s
    # and the job names must NOT be re-typed anywhere in this module
    for job in ("content-publish", "global-intelligence", "ai-outreach",
                "ai-ecosystem", "energy-discovery"):
        assert TEXT.count(f'"{job}"') == 0, (
            f"{job} is re-declared in loop_control_master_shell — import the "
            f"radar's set instead of copying it")


def test_the_radar_still_exports_the_set_this_imports():
    """If the radar renames or removes it, this lane silently excludes nothing
    and the test above still passes. Pin the other side too."""
    assert "_INTENTIONAL_STALE_CRONS" in RADAR_TEXT
    found = None
    for node in ast.parse(RADAR_TEXT).body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "_INTENTIONAL_STALE_CRONS":
            found = ast.literal_eval(node.value)
        elif isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_INTENTIONAL_STALE_CRONS" for t in node.targets):
            found = ast.literal_eval(node.value)
    assert isinstance(found, set) and found, "_INTENTIONAL_STALE_CRONS missing or empty"
    assert "content-publish" in found


def test_import_failure_fails_closed():
    """An empty set excludes nothing, so the lane OVER-reports rather than
    certifying a genuinely dead cron as healthy. Never invert this."""
    s = _src("_retired_crons")
    assert "return set()" in s
    assert "fail-closed" in s.lower() or "fail closed" in s.lower()


# ── both queries filter, not just the count ──────────────────────────

def test_both_cron_queries_exclude_retired_jobs():
    """Filtering only the count would leave `worst_offender` naming a retired
    job — the lane would report 0 dead crons and 'content-publish silent 741h'
    in the same breath."""
    lane = _src("_lane_cron_liveness")
    assert lane.count("NOT (job_name = ANY(%(retired)s))") == 2, \
        "both the count and the worst-offender query must exclude retired jobs"


def test_the_docstring_no_longer_overclaims():
    """It promised parity with the radar while ignoring the radar's allowlist."""
    lane = _src("_lane_cron_liveness")
    assert "retirement allowlist" in lane, \
        "say that the allowlist is shared, now that it actually is"


# ── the percent trap ─────────────────────────────────────────────────

def test_row_helper_documents_both_modes():
    """_row's contract was 'LITERAL SQL only — NO PERCENT CHARACTERS', because
    a literal % in a paramless execute() is read as a substitution marker and
    500s. Adding a params path must not quietly delete that warning for the
    paramless callers that still rely on it."""
    s = _src("_row")
    assert "params=None" in s
    assert "NO PERCENT CHARACTERS" in s, \
        "the paramless contract must still be stated — most callers use it"
    assert "%%" in s, "the doubling rule for literal % under params must be stated"


def test_paramless_callers_still_pass_no_params():
    """Guard the mode boundary: _has_table and friends must stay literal."""
    s = _src("_has_table")
    assert "_row(c, f\"SELECT to_regclass" in s
    assert "params" not in s
