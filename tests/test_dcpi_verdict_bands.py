"""r-verdict-one-band (2026-08-08) — one verdict band table, not five.

WHAT THIS PINS
--------------
util/dcpi_method.VERDICT_BANDS is what /api/v1/dcpi/methodology publishes as
the official rule for turning two scores into BUILD / CAUTION / AVOID.
routes/dcpi.py::derive_verdict imports it. Nothing else may re-type it.

THE DEFECT THIS EXISTS TO CATCH
-------------------------------
dchub_self_heal.py carried FOUR more verdict band tables, hand-typed into
that file, no two alike and none matching VERDICT_BANDS, plus a fifth job
writing the label 'NODATA' which was never in the published alphabet. Three
of them wrote

    WHERE published = true OR tier_required IS NULL
          OR tier_required != 'lite-pro'

i.e. they relabelled the PUBLIC index. Two were armed and fired on every heal
cycle. Measured against live Neon on 2026-08-08: at 15:36 UTC, 221 of 324
published rows (68.2%) carried a verdict the published bands cannot produce,
and the band table in fix_repair_verdict_matrix reproduced 324 of 324 stored
verdicts exactly while the published bands reproduced 103.

That is the mechanism behind the "209 of 311 published markets (67%)" figure
util/dcpi_method.py's own docstring reports. It is the same bug class as
r-ws3-methodology's hand-copied weights, util/iso_taxonomy.py's duplicated
ISO map and util/dcpi_score_row.py's six hand-written column lists: a second
copy that nothing connects to the first.

THE RULE, AND ITS ONE EXEMPTION
-------------------------------
A statement that WRITES market_power_scores.verdict must bind a value the
caller computed (`verdict=%s` / `EXCLUDED.verdict`), not derive one in SQL
from thresholds typed into the statement.

The exemption is the lite path. The three lite writers compute a two-input
approximation that shares no weight, ceiling or band with the published
method, so they legitimately carry their own labels — but they are fenced by
util.dcpi_score_row.LITE_MAY_NOT_CLOBBER_FULL, which confines them to rows
the full method does not own. That is the pattern this guard accepts:
import the bands, or be gated so you cannot touch a full-method row.

WHY THE CENSUS IS READ OUT OF THE AST
-------------------------------------
Same reason as tests/test_dcpi_provenance_stamp.py, whose helpers this
mirrors deliberately: a band table that appears only in a comment must not
satisfy the guard, and a predicate reaching the SQL through a shared
constant still has to count. Tests never import main.py, so f-string holes
are rendered as their source expression rather than evaluated.
"""
import ast
import os
import re

import pytest


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_SKIP_DIRS = {".git", "tests", "node_modules", "__pycache__", ".venv", "venv",
              ".pytest_cache", "site-packages", ".claude"}

# Every label the four retired tables could emit, including the two that were
# never in the published alphabet at all.
_VERDICT_LABELS = ("build", "caution", "avoid", "nodata", "low_signal")
_LABEL_SET = {"BUILD", "CAUTION", "AVOID", "NODATA", "LOW_SIGNAL"}

# `COALESCE(excess_power_score,0) >= 60`, `constraint_score <= 40`, and the
# differential form `... - COALESCE(constraint_score,0) >= 25`.
_THRESHOLD_RE = re.compile(
    r"(?:constraint_score|excess_power_score)\s*(?:,\s*\d+\s*)?\)?"
    r"(?:\s*-\s*coalesce\([^)]*\))?\s*(?:>=|<=|>|<|=)\s*\d")


def _candidate_files():
    """Every repo .py that so much as mentions the table.

    Filtering on the text first keeps this off the ~600 unrelated modules and
    means an unrelated syntax error elsewhere cannot turn this into a blind
    pass.
    """
    root = _repo_root()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            if "market_power_scores" in text:
                yield os.path.relpath(path, root), text


def _sql_strings(tree):
    """(lineno, sql) for every string literal / f-string in a parsed module.

    An f-string hole is rendered as its SOURCE EXPRESSION, not evaluated, so
    a lite guard reaching the SQL as `{LITE_MAY_NOT_CLOBBER_FULL}` is still
    visible to _lite_fenced below.
    """
    claimed, out = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            buf = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    claimed.add(id(v))
                    buf.append(v.value)
                elif isinstance(v, ast.FormattedValue):
                    claimed.add(id(v))
                    buf.append(f" {ast.unparse(v.value)} ")
            out.append((node.lineno, "".join(buf)))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in claimed):
            out.append((node.lineno, node.value))
    return out


def _lite_guard_aliases(tree):
    """Names bound to LITE_MAY_NOT_CLOBBER_FULL in this module, from its AST."""
    names = {"LITE_MAY_NOT_CLOBBER_FULL"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                "dcpi_score_row"):
            for alias in node.names:
                if alias.name == "LITE_MAY_NOT_CLOBBER_FULL":
                    names.add(alias.asname or alias.name)
    return names


def _norm(sql):
    return " ".join(sql.lower().split())


def _write_region(sql):
    """The part of a verdict-writing statement that assigns the value.

    Returns None when the statement does not write verdict at all. Scoped so
    that a READ filter (`AND verdict = 'LOW_SIGNAL'`, of which routes/dcpi.py
    has several) can never be mistaken for a write.
    """
    low = _norm(sql)
    if "market_power_scores" not in low:
        return None

    if "insert into market_power_scores" in low:
        head = low.split("insert into market_power_scores", 1)[1]
        cols = head.split("values", 1)[0]
        if "verdict" in cols:
            # The column list plus any ON CONFLICT ... DO UPDATE SET tail.
            return head
        if "on conflict" in head and "verdict" in head.split("on conflict", 1)[1]:
            return head.split("on conflict", 1)[1]
        return None

    if "update market_power_scores" in low:
        tail = low.split("update market_power_scores", 1)[1]
        if " set " not in tail:
            return None
        body = tail.split(" set ", 1)[1]
        # Stop at WHERE: the assignment is what matters, not the row filter.
        assigns = body.split(" where ", 1)[0]
        if "verdict" not in assigns:
            return None
        return body
    return None


def _python_band_tables(tree):
    """(lineno, description) for every band table written in PYTHON.

    ★ Added after mutation testing. The first version of this census only
    looked at SQL, and a mutation that stripped the lite fence off
    scripts/bulk_dcpi_score.py SURVIVED it — because none of the three
    surviving verdict writers derives its verdict in SQL at all. They all
    compute it in Python and bind the result, so a SQL-only census inspects
    three statements that can never carry a literal and reports success.

    All three shapes ship today, and all three are lite approximations:
        scripts/bulk_dcpi_score.py  compute_verdict()   4 labels, returns
        routes/dcpi.py              inline conditional  3 labels
        main.py                     inline conditional  3 labels (a hand-copy
                                    of routes/dcpi.py's, per its own comment)
    They are legitimate — a lite formula is allowed to disagree — but ONLY
    because LITE_MAY_NOT_CLOBBER_FULL confines them to rows the full method
    does not own. That fence is the single thing standing between these three
    band tables and the published index, which is exactly what this census
    must be able to see.
    """
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.IfExp):
            labs = {c.value for c in ast.walk(node)
                    if isinstance(c, ast.Constant) and isinstance(c.value, str)
                    and c.value in _LABEL_SET}
            if len(labs) >= 2:
                out.append((node.lineno,
                            "inline conditional -> " + ", ".join(sorted(labs))))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            labs = {r.value.value for r in ast.walk(node)
                    if isinstance(r, ast.Return)
                    and isinstance(r.value, ast.Constant)
                    and isinstance(r.value.value, str)
                    and r.value.value in _LABEL_SET}
            if len(labs) >= 2:
                out.append((node.lineno,
                            f"{node.name}() -> " + ", ".join(sorted(labs))))
    return out


def _verdict_writers():
    """(path, lineno, sql, region, fence_aliases, py_bands) per verdict write."""
    found = []
    for path, text in _candidate_files():
        try:
            tree = ast.parse(text)
        except SyntaxError:  # pragma: no cover - repo must parse anyway
            pytest.fail(f"{path} does not parse")
        aliases = _lite_guard_aliases(tree)
        py_bands = _python_band_tables(tree)
        for lineno, sql in _sql_strings(tree):
            region = _write_region(sql)
            if region is not None:
                found.append((path, lineno, sql, region, aliases, py_bands))
    return found


def _carries_band_literals(region):
    """True when the statement decides the verdict itself.

    Two independent signals, because a future table might use either:
      * a quoted verdict LABEL — every retired table ended `THEN 'BUILD'`
      * a numeric THRESHOLD against one of the two score columns
    A statement that binds a computed value (`verdict=%s`) has neither.
    """
    labels = [w for w in _VERDICT_LABELS if f"'{w}'" in region]
    thresholds = bool(_THRESHOLD_RE.search(region))
    return labels, thresholds


def _lite_fenced(sql, aliases):
    """True when this statement carries the lite no-clobber fence."""
    return any(a in sql for a in aliases)


# ─────────────────────────────────────────────────────────────────────────
# The census
# ─────────────────────────────────────────────────────────────────────────

def test_census_is_not_vacuous():
    """The guard below is only meaningful if it finds the writes at all.

    Without this, renaming the table or reshaping the SQL turns the census
    into a silent pass — the failure mode that let weekly-shadow-audit report
    success for two weeks.
    """
    writers = _verdict_writers()
    assert writers, "found no verdict-writing statements at all — census blind"
    paths = {w[0] for w in writers}
    assert any("bulk_dcpi_score" in p for p in paths), (
        "the lite bulk writer is a known verdict writer and the census no "
        f"longer sees it; it only found {sorted(paths)}"
    )
    # And it must see the band tables, not just the writes. A SQL-only census
    # inspects three statements that cannot carry a literal and passes blind
    # — that is a real mutation this suite survived before _python_band_tables
    # existed.
    assert any(w[5] for w in writers), (
        "the census sees verdict writes but no band table anywhere. Every "
        "surviving writer derives its verdict in Python, so a census that "
        "cannot see a Python band table has nothing left to check."
    )


def test_the_three_lite_writers_are_all_fenced():
    """The fence is what makes the lite band tables acceptable — pin it.

    These three carry their own thresholds and always will; a lite formula is
    allowed to disagree with the published method. What it may not do is
    overwrite a row the full method owns and inherit that row's
    method_version, which would leave it advertising a method that did not
    produce its numbers (util/dcpi_score_row.py's lite note).
    """
    unfenced = [
        f"{p}:{ln}" for p, ln, sql, _r, al, bands in _verdict_writers()
        if bands and not _lite_fenced(sql, al)
    ]
    assert not unfenced, (
        "a module holding its own Python verdict band table writes "
        f"market_power_scores.verdict without the lite fence: {unfenced}. "
        "Add `WHERE {LITE_MAY_NOT_CLOBBER_FULL}` (imported from "
        "util.dcpi_score_row) so it cannot touch a full-method row."
    )


def test_no_verdict_write_carries_its_own_band_literals():
    """THE GUARD. A verdict write may not decide the verdict in SQL.

    Fails on all four retired dchub_self_heal.py tables. Passes for the
    canonical writer (util/dcpi_score_row.py binds verdict=%s from one
    generated column list) and for the lite writers, which carry their own
    labels but are confined by LITE_MAY_NOT_CLOBBER_FULL to rows the full
    method does not own.
    """
    offenders = []
    for path, lineno, sql, region, aliases, py_bands in _verdict_writers():
        if _lite_fenced(sql, aliases):
            # Fenced to rows the full method does not own. A lite formula may
            # legitimately disagree; it just may not overwrite a full score.
            continue
        labels, thresholds = _carries_band_literals(region)
        why = []
        if labels:
            why.append("SQL verdict labels " + ", ".join(sorted(labels)))
        if thresholds:
            why.append("numeric band thresholds on the score columns")
        for bl, desc in py_bands:
            why.append(f"a Python band table at line {bl} ({desc})")
        if why:
            offenders.append(f"{path}:{lineno} — {'; '.join(why)}")

    assert not offenders, (
        "a statement writes market_power_scores.verdict from band literals "
        "typed into itself:\n  " + "\n  ".join(sorted(offenders))
        + "\n\nThe verdict has ONE definition: util/dcpi_method.VERDICT_BANDS, "
          "applied by routes/dcpi.py::derive_verdict. Either bind the value "
          "that produces (verdict=%s), or — if this is a lite approximation "
          "that legitimately differs — fence it with "
          "util.dcpi_score_row.LITE_MAY_NOT_CLOBBER_FULL so it cannot touch a "
          "row the full method owns. Retyping the bands is how 68% of the "
          "published index came to carry a verdict the published rule could "
          "not produce (r-verdict-one-band)."
    )


def test_self_heal_no_longer_rewrites_published_verdicts():
    """Pins the retirement itself.

    Narrower than the census on purpose: this is the file that held all four
    tables, and the thing that made them dangerous was not the literals alone
    but that they ran unattended against `published = true` on every cycle.
    """
    root = _repo_root()
    with open(os.path.join(root, "dchub_self_heal.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    for lineno, sql in _sql_strings(tree):
        region = _write_region(sql)
        assert region is None, (
            f"dchub_self_heal.py:{lineno} writes market_power_scores.verdict "
            "again. This module must never be a verdict writer: it runs "
            "unattended on a 6h cycle against the public index, and its four "
            "previous band tables relabelled 220 of 324 published markets "
            "away from the published rule (r-verdict-one-band)."
        )


def test_registered_fixes_do_not_include_a_verdict_rewriter():
    """The dispatch half of the same retirement.

    FIXES keys reach production three ways — PATTERNS, main.py's
    _heal_master_cycle ORDER list, and the admin
    /api/v1/admin/self-heal/<action> door — so a re-registered rewriter is
    reachable even with no pattern pointing at it.

    ★ A dict key is not a declaration. The retired file registered
    FIXES["recompute_verdict_diff"] twice, ~180 lines apart, and the second
    assignment silently rebound it to a DIFFERENT function; 35,780 logged
    events named a fixer that had not run in months. Assert on the key names.
    """
    root = _repo_root()
    with open(os.path.join(root, "dchub_self_heal.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    registered = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if (isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.value, ast.Name)
                    and tgt.value.id == "FIXES"
                    and isinstance(tgt.slice, ast.Constant)):
                registered.add(str(tgt.slice.value))

    assert registered, "no FIXES registrations found — census blind"
    rewriters = sorted(k for k in registered
                       if "verdict" in k and k != "relax_verdict")
    assert not rewriters, (
        f"dchub_self_heal.py registers verdict rewriter(s): {rewriters}. "
        "Only 'relax_verdict' may remain, and only as a REPORT-ONLY spread "
        "detector — it must not write. The verdict's one producer is "
        "routes/dcpi.py::derive_verdict."
    )


def test_relax_verdict_detector_survives_but_cannot_write():
    """The one survivor is the honest half: it measures, and stops.

    Its finding (a degenerate BUILD/CAUTION/AVOID spread) is real and worth
    reporting. Relabelling to fix the histogram it measures is the defect.
    """
    import dchub_self_heal as sh

    assert "relax_verdict" in sh.FIXES
    src = ast.unparse(ast.parse(
        __import__("inspect").getsource(sh.fix_relax_verdict_thresholds)))
    low = src.lower()
    assert "update market_power_scores" not in low, (
        "the spread detector writes again — it must report and stop"
    )
    assert "select" in low, "the detector no longer measures anything"


def test_canonical_producer_imports_the_bands_rather_than_retyping_them():
    """The other direction: the producer itself must not grow a copy."""
    root = _repo_root()
    with open(os.path.join(root, "routes", "dcpi.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                "dcpi_method"):
            for alias in node.names:
                if alias.name == "VERDICT_BANDS":
                    imported = True
    assert imported, (
        "routes/dcpi.py no longer imports VERDICT_BANDS from util/dcpi_method. "
        "derive_verdict is the only producer of a stored verdict; if it "
        "retypes its thresholds, the published methodology and the live index "
        "disagree with nothing to detect it."
    )


def test_published_alphabet_has_no_unwritable_member():
    """NODATA and LOW_SIGNAL were written only by the retired tables.

    LOW_SIGNAL keeps a composite multiplier and a filter value and is
    documented as unreachable, which is honest. NODATA was never in the
    published alphabet at all — a stored 'NODATA' can only mean a fifth band
    table came back.
    """
    from util.dcpi_method import (VERDICT_BANDS, VERDICT_FALLBACK,
                                  COMPOSITE_VERDICT_MULTIPLIERS)

    producible = {v for v, _ in VERDICT_BANDS} | {VERDICT_FALLBACK}
    assert producible == {"BUILD", "CAUTION", "AVOID"}
    assert "NODATA" not in COMPOSITE_VERDICT_MULTIPLIERS, (
        "NODATA is being re-admitted to the published alphabet; it was only "
        "ever written by dchub_self_heal.fix_nodata_verdicts (retired)"
    )
