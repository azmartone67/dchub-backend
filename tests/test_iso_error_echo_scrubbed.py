"""Fence: no routes/iso_*.py may echo a raw exception into summary["error"].

Every ISO extractor's summary is returned by the orchestrator at
/api/v1/iso/all/extract, whose whole response body is printed verbatim
by .github/workflows/data-pulse.yml into a PUBLIC Actions log. So any
string reaching summary["error"] reaches a public log.

#2075 fixed the one case where that actually leaked: iso_isone embedded
ISONE_USERNAME/ISONE_PASSWORD in a URL, and the resulting

    InvalidURL: nonnumeric port: '<password>@webservices.iso-ne.com'

was published on every run. Because our own code built that value into
a string rather than passing it through a GitHub secret, Actions'
secret masking never redacted it.

This fence closes the remaining surface. No other ISO extractor puts a
credential in a URL today (verified by repo-wide grep), so the ten
routes wrapped alongside this test are hardening, not live leaks — the
point is that the NEXT adapter to hold a credential inherits the
scrubbing instead of re-learning it in public.

WHY AST AND NOT GREP: iso_pjm.py aligns its assignment operator —

    summary["error"]       = f"{type(e).__name__}: {e}"

— so the obvious `summary\\["error"\\] = f"` grep silently misses it. It
was missed exactly that way when this work was scoped. Parse, don't
pattern-match.

Tests never import main.py (it opens DB pools and registers ~200
blueprints), and per CLAUDE.md nothing here runs at module scope.
"""
import ast
import glob
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Functions that render an exception safe to publish. Both live in
# routes/_iso_common.py and redact by VALUE, which is what works on a
# message like "nonnumeric port: '<pw>@host'" that carries no parseable
# URL for a structural regex to grab.
SCRUBBERS = {"scrub_secrets", "scrub_attempt"}

# Every extractor that assigns the singular summary["error"]. Named, not
# counted, so deleting a file fails the fence instead of shrinking it.
EXPECTED_SINGULAR = {
    "iso_aeso.py", "iso_bpa.py", "iso_caiso.py", "iso_ercot.py",
    "iso_ieso.py", "iso_isone.py", "iso_miso.py", "iso_nyiso.py",
    "iso_pjm.py", "iso_spp.py", "iso_tva.py",
}

# KNOWN GAP, deliberately not fixed in this pass. These use the plural
# list shape — summary["errors"].append(f"...{e}") — which lands in the
# same public log but is a wider change than this PR's scope. Pinned by
# name so a NEW unscrubbed extractor fails this fence rather than
# quietly joining the backlog. Shrinking it is the follow-up.
KNOWN_UNSCRUBBED_APPEND = {
    "iso_aeso_intl.py", "iso_au_aemo.py", "iso_br_ons.py",
    "iso_eu_entsoe.py", "iso_hydroquebec.py", "iso_jp_denkiyoho.py",
    "iso_kr_kpx.py", "iso_nordpool_intl.py", "iso_sg_nems.py",
    "iso_tw_taipower.py", "iso_uk_elexon.py",
}


def _iso_files():
    """All ISO route modules. Raises if the glob ever comes back empty."""
    paths = sorted(glob.glob(os.path.join(ROOT, "routes", "iso_*.py")))
    assert paths, "glob matched no routes/iso_*.py — fence would be vacuous"
    return paths


def _subscript_key(node):
    """Return the literal key of `x[...]`, or None if it isn't a literal."""
    if not isinstance(node, ast.Subscript):
        return None
    sl = node.slice
    if isinstance(sl, ast.Index):          # py<3.9 shape, harmless to keep
        sl = sl.value
    if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
        return sl.value
    return None


def _is_summary_error_target(node):
    return (
        _subscript_key(node) == "error"
        and isinstance(node.value, ast.Name)
        and node.value.id == "summary"
    )


def _interpolates(node):
    """True if the expression can embed a runtime value.

    A bare f-string with no {…} placeholders cannot carry a credential,
    so it is not worth failing over; anything that formats, concatenates
    or stringifies is.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.FormattedValue):
            return True
        if isinstance(sub, (ast.Call, ast.BinOp, ast.Name, ast.Attribute)):
            return True
    return False


def _scrubbed(node):
    """True if `node` is a call to an allowlisted scrubber."""
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
    return name in SCRUBBERS


def _singular_assignments():
    """[(basename, lineno, value_node)] for every summary["error"] = ..."""
    out = []
    for path in _iso_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if _is_summary_error_target(tgt):
                    out.append((os.path.basename(path), node.lineno, node.value))
    return out


def test_no_raw_exception_echo_in_summary_error():
    """The fence proper: every summary["error"] is scrubbed at assignment."""
    offenders = []
    for fname, lineno, value in _singular_assignments():
        if _scrubbed(value):
            continue
        if not _interpolates(value):
            continue                        # literal string, cannot leak
        offenders.append(f"{fname}:{lineno}")
    assert not offenders, (
        "unscrubbed exception echo reaches a PUBLIC Actions log via "
        "/api/v1/iso/all/extract — wrap in scrub_secrets(...): "
        + ", ".join(sorted(offenders))
    )


def test_fence_actually_sees_every_extractor():
    """Anti-vacuous: the detector must find the sites it claims to guard.

    Without this, a change to the AST shape (or a rename of `summary`)
    would make the fence above pass by finding nothing at all.
    """
    found = {fname for fname, _, _ in _singular_assignments()}
    missing = EXPECTED_SINGULAR - found
    assert not missing, (
        "fence no longer detects summary[\"error\"] in: "
        + ", ".join(sorted(missing))
        + " — the detector broke, or the assignment shape changed"
    )


def test_every_detected_site_is_a_scrubber_call():
    """Census, not presence-check.

    test_no_raw_exception_echo passes for a literal too. This asserts the
    stronger property that every real site routes through a scrubber, so
    re-introducing ONE raw echo cannot hide behind its ten fixed peers.
    """
    unscrubbed = [
        f"{fname}:{lineno}"
        for fname, lineno, value in _singular_assignments()
        if not _scrubbed(value)
    ]
    assert not unscrubbed, (
        "summary[\"error\"] assigned without a scrub_secrets/scrub_attempt "
        "call: " + ", ".join(sorted(unscrubbed))
    )


def test_plural_append_gap_does_not_grow():
    """Pin the known-unscrubbed backlog so it can only shrink.

    These extractors append to summary["errors"] instead of assigning
    summary["error"]. Same public log, wider blast radius than this
    change took on. A NEW file joining this set fails here.
    """
    found = set()
    for path in _iso_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "append"):
                continue
            if _subscript_key(node.func.value) != "errors":
                continue
            if node.args and _interpolates(node.args[0]) \
                    and not _scrubbed(node.args[0]):
                found.add(os.path.basename(path))
    new = found - KNOWN_UNSCRUBBED_APPEND
    assert not new, (
        "new unscrubbed summary[\"errors\"].append site(s): "
        + ", ".join(sorted(new))
        + " — wrap in scrub_secrets(...) rather than extending the backlog"
    )
    # Shrinking is the goal; when it happens, trim the pinned set so the
    # fence keeps its grip instead of guarding names that no longer leak.
    stale = KNOWN_UNSCRUBBED_APPEND - found
    assert not stale, (
        "these no longer echo raw — remove from KNOWN_UNSCRUBBED_APPEND: "
        + ", ".join(sorted(stale))
    )
