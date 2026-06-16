"""Security tests for the C1 L22 cron_if_mismatched auto-merge gate (recipe #2).

Like route_alias_404, this path can AUTO-MERGE a change to a tracked file
(.github/workflows/*.yml), so its safety invariants are tested explicitly with
no network and no DB (the gh helpers are monkeypatched):

  1. Master OFF switch: with L22_AUTO_MERGE_ENABLE unset the feature is inert —
     _list_l22_cron_prs() returns [].
  2. The cron-line matcher accepts ONLY quoted `cron:` lines.
  3. _gate_cron_l22 accepts ONLY a single +1/-1 plain-integer-minute stagger in a
     non-schedule-guarded .github/workflows file, and rejects everything else
     (wrong path, evolve-cron.yml, multi-line, multi-file, */N minute,
     github.event.schedule guard, invalid YAML).
"""
import os
import importlib

import pytest

# sentinel_auto_merge imports flask at top level; the minimal pre-merge
# unit-tests env has only pytest, so skip cleanly there and run where flask is
# present (local dev). The full gate logic still runs everywhere flask exists.
pytest.importorskip("flask")


def _mod():
    os.environ.pop("L22_AUTO_MERGE_ENABLE", None)
    import routes.sentinel_auto_merge as S
    return importlib.reload(S)


def test_cron_feature_off_by_default():
    S = _mod()
    assert S._l22_automerge_on() is False
    assert S._list_l22_cron_prs() == []   # inert when flag off


def test_cron_line_regex():
    S = _mod()
    rx = S._L22_CRON_LINE_RE
    good = [
        "- cron: '13 * * * *'",
        '- cron: "5 6 * * *"',
        "cron: '43 */6 * * *'",
    ]
    bad = [
        "name: Some workflow",
        "    run: curl https://evil",
        "- cron: 13 * * * *",            # unquoted
        "schedule: '13 * * * *'",        # not a cron: line
    ]
    for line in good:
        assert rx.match(line.strip()), f"should ACCEPT cron line: {line!r}"
    for line in bad:
        assert not rx.match(line.strip()), f"should REJECT: {line!r}"


def _patch_gh(S, patch_text, content_text):
    S._pr_file_patch = lambda pr, path: patch_text
    S._pr_patch_for_file = lambda pr, path: content_text


_VALID_PATCH = (
    "@@ -10,7 +10,7 @@\n"
    " on:\n"
    "   schedule:\n"
    "-    - cron: '8 * * * *'\n"
    "+    - cron: '13 * * * *'\n"
    "   workflow_dispatch: {}\n"
)
_VALID_YAML = "on:\n  schedule:\n    - cron: '13 * * * *'\njobs:\n  x:\n    runs-on: ubuntu-latest\n"


def test_gate_accepts_valid_cron_stagger():
    S = _mod()
    _patch_gh(S, _VALID_PATCH, _VALID_YAML)
    files = [{"path": ".github/workflows/foo.yml", "additions": 1, "deletions": 1}]
    ok, reason, _ = S._gate_cron_l22(1, files)
    assert ok is True, reason


def test_gate_rejects_evolve_cron():
    S = _mod()
    _patch_gh(S, _VALID_PATCH, _VALID_YAML)
    files = [{"path": ".github/workflows/evolve-cron.yml", "additions": 1, "deletions": 1}]
    ok, reason, _ = S._gate_cron_l22(1, files)
    assert ok is False and "evolve-cron" in reason


def test_gate_rejects_non_workflow_path():
    S = _mod()
    _patch_gh(S, _VALID_PATCH, _VALID_YAML)
    files = [{"path": "main.py", "additions": 1, "deletions": 1}]
    ok, reason, _ = S._gate_cron_l22(1, files)
    assert ok is False


def test_gate_rejects_multifile():
    S = _mod()
    _patch_gh(S, _VALID_PATCH, _VALID_YAML)
    files = [{"path": ".github/workflows/a.yml", "additions": 1, "deletions": 1},
             {"path": ".github/workflows/b.yml", "additions": 1, "deletions": 1}]
    ok, reason, _ = S._gate_cron_l22(1, files)
    assert ok is False


def test_gate_rejects_wrong_line_counts():
    S = _mod()
    _patch_gh(S, _VALID_PATCH, _VALID_YAML)
    files = [{"path": ".github/workflows/foo.yml", "additions": 2, "deletions": 1}]
    ok, reason, _ = S._gate_cron_l22(1, files)
    assert ok is False and "+1/-1" in reason


def test_gate_rejects_non_integer_minute():
    S = _mod()
    bad_patch = _VALID_PATCH.replace("'13 * * * *'", "'*/15 * * * *'")
    _patch_gh(S, bad_patch, _VALID_YAML.replace("'13 * * * *'", "'*/15 * * * *'"))
    files = [{"path": ".github/workflows/foo.yml", "additions": 1, "deletions": 1}]
    ok, reason, _ = S._gate_cron_l22(1, files)
    assert ok is False and "plain integer" in reason


def test_gate_rejects_schedule_guard():
    S = _mod()
    guarded = _VALID_YAML + "    if: github.event.schedule == '13 * * * *'\n"
    _patch_gh(S, _VALID_PATCH, guarded)
    files = [{"path": ".github/workflows/foo.yml", "additions": 1, "deletions": 1}]
    ok, reason, _ = S._gate_cron_l22(1, files)
    assert ok is False and "schedule-guarded" in reason


def test_gate_rejects_invalid_yaml():
    S = _mod()
    _patch_gh(S, _VALID_PATCH, "on: [unterminated\n  bad: : :\n")
    files = [{"path": ".github/workflows/foo.yml", "additions": 1, "deletions": 1}]
    ok, reason, _ = S._gate_cron_l22(1, files)
    assert ok is False and "YAML" in reason
