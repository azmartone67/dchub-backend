"""HIFLD sentinel scrubbing (2026-08-15).

718 rows in fiber_routes are named "NOT AVAILABLE -999999kV Line - Columbus".
Both halves are HIFLD "we don't know" sentinels. The old ingest guard was
`attrs.get('VOLTAGE', 0) or 0` — which LOOKS like a null-check and is not one:
-999999 is truthy, so `or 0` never fires. It only ever caught a voltage that was
already 0, i.e. the one sentinel HIFLD does not use.

★These tests pin the TRUTHINESS trap directly, because that is the bug — not the
formatting. A future refactor that reintroduces `or 0` fails here.

Per repo convention, tests never import main.py; the functions under test are
pulled out of the source with ast and executed against stubs.
"""
import ast
import pathlib


def _load(path, names):
    """Execute only the named module-level defs/assigns from `path`."""
    src = pathlib.Path(__file__).resolve().parents[1] / path
    tree = ast.parse(src.read_text())
    keep = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            keep.append(node)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in names:
                    keep.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            keep.append(node)
    ns = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<extract>", "exec"), ns)
    return ns


def _ingest():
    return _load("infrastructure_discovery.py",
                 {"hifld_voltage", "hifld_owner", "hifld_line_name",
                  "_HIFLD_NULL_STRINGS"})


def test_the_truthiness_trap_minus_999999_is_not_falsy():
    """THE BUG, stated as an assertion: the sentinel passes `or 0` untouched."""
    assert bool(-999999) is True          # why `attrs.get('VOLTAGE', 0) or 0` failed
    assert (-999999 or 0) == -999999      # the exact expression that shipped
    assert _ingest()["hifld_voltage"](-999999) is None   # what it must do instead


def test_voltage_sentinels_all_collapse_to_none():
    hv = _ingest()["hifld_voltage"]
    for bad in (-999999, -999998, 0, "0", "-999999", None, "", "NOT AVAILABLE", "abc"):
        assert hv(bad) is None, bad


def test_real_voltages_survive_unchanged():
    hv = _ingest()["hifld_voltage"]
    assert hv(345) == 345
    assert hv("500") == 500
    assert hv(69.0) == 69


def test_owner_sentinel_strings_are_caught_case_insensitively():
    ho = _ingest()["hifld_owner"]
    # The live data carries this in caps — a case-sensitive check would miss it.
    assert ho("NOT AVAILABLE") == "Unknown"
    assert ho("not available") == "Unknown"
    assert ho("  N/A  ") == "Unknown"
    assert ho("") == "Unknown"
    assert ho(None) == "Unknown"
    assert ho("-999999") == "Unknown"


def test_owner_falls_through_to_operator_then_unknown():
    ho = _ingest()["hifld_owner"]
    assert ho("NOT AVAILABLE", "Dominion") == "Dominion"
    assert ho(None, None) == "Unknown"
    assert ho("Dominion", "Ignored") == "Dominion"


def test_name_omits_what_is_unknown_rather_than_printing_it():
    ns = _ingest()
    name = ns["hifld_line_name"]
    assert name("Unknown", None, "Columbus") == "Unknown Line - Columbus"
    assert name("Dominion", 500.0, "Ashburn") == "Dominion 500kV Line - Ashburn"
    # never a trailing ".0" on a whole-number kV
    assert "500.0" not in name("Dominion", 500.0, "Ashburn")


def test_the_exact_live_defect_no_longer_reproduces():
    ns = _ingest()
    attrs = {"VOLTAGE": -999999, "OWNER": "NOT AVAILABLE", "OPERATOR": ""}
    out = ns["hifld_line_name"](
        ns["hifld_owner"](attrs.get("OWNER"), attrs.get("OPERATOR")),
        ns["hifld_voltage"](attrs.get("VOLTAGE")),
        "Columbus")
    assert out == "Unknown Line - Columbus"
    assert "-999999" not in out
    assert "NOT AVAILABLE" not in out


# ── the backfill side ────────────────────────────────────────────────────────

def _repair():
    return _load("routes/fiber_name_quality.py", {"repair_name", "_VOLT_RE", "_OWNER_RE"})["repair_name"]


def test_repair_rewrites_the_live_row_shape():
    r = _repair()
    assert r("NOT AVAILABLE -999999kV Line - Columbus [6faf59c85f0e]") == \
        "Unknown Line - Columbus [6faf59c85f0e]"


def test_repair_preserves_the_segment_fingerprint():
    """SH52-054 folded a geometry fingerprint into the name to keep distinct
    physical segments distinct under UNIQUE(name, provider). Stripping it would
    re-collapse them — so the suffix must survive verbatim."""
    r = _repair()
    out = r("NOT AVAILABLE -999999kV Line - Columbus [40f403bbc02f]")
    assert out.endswith("[40f403bbc02f]")


def test_repair_is_idempotent():
    r = _repair()
    once = r("NOT AVAILABLE -999999kV Line - Columbus [abc]")
    assert r(once) == once


def test_repair_leaves_clean_names_byte_identical():
    r = _repair()
    for good in ("Dominion 500kV Line - Ashburn [ab12]",
                 "Zayo Metro Route - Dallas",
                 "Unknown Line - Columbus",
                 ""):
        assert r(good) == good


def test_repair_never_empties_a_name():
    r = _repair()
    assert r("NOT AVAILABLE -999999kV") not in ("", None)
