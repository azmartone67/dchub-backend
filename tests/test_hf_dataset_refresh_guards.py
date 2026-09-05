"""The HF dataset exporter may publish a stale universe, never a partial one.

THE DEFECT THIS FOLLOWS FROM
----------------------------
huggingface.co/datasets/dchubcloud/dcpi-market-verdicts sat at as_of_date
2026-07-02/03 for 63 days while its card said the verdicts were "recomputed
daily". The index genuinely is daily; publishing the CSV was a hand-run step
with no script, so nothing carried fresh rows to HF. When it was finally
regenerated, 122 of 308 shared markets had changed verdict.

WHY THE GUARDS POINT THE WAY THEY DO
A stale snapshot is visibly stale — every row carries its own as_of_date, so a
reader can date it and the card now tells them to. A TRUNCATED snapshot is not:
a market missing from the file is indistinguishable from a market that is not
scored, and nothing downstream can tell the difference. So the exporter refuses
to write a partial universe and is content to write an old one.

★ THE PREVIEW FLAG IS NOT THE TRUNCATION SIGNAL, and the first draft of this
  exporter got that backwards. /api/v1/dcpi/scores returns `_preview_only: true`
  even for a key that reads the COMPLETE universe, because it reports FIELD-level
  gating (the Pro numerics come back null), not row loss. Refusing on the flag
  would have blocked every legitimate run — a guard that cannot pass is as
  useless as one that cannot fail. The invariant is row coverage:
  len(rows) == _total_available.
"""
from __future__ import annotations

import importlib.util
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "refresh_hf_dcpi_dataset.py")


def _mod():
    spec = importlib.util.spec_from_file_location("hf_refresh", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _payload(n_rows=327, total=327, preview=True, pro_value=None):
    row = {"market_slug": "x", "verdict": "BUILD"}
    if pro_value is not None:
        row["composite_score"] = pro_value
    return {"scores": [dict(row, market_slug=f"m{i}") for i in range(n_rows)],
            "_total_available": total,
            "_preview_only": preview,
            "_locked_fields": ["composite_score"]}


def _fetch(monkeypatch, payload):
    m = _mod()
    monkeypatch.setattr(m, "_get", lambda *a, **k: payload)
    return m


def test_a_complete_universe_passes_even_though_preview_is_true(monkeypatch):
    """★ THE CONTROL, and the one that pins the corrected reading.

    Without this, narrowing the guard back to `if _preview_only: refuse` would
    fail nothing — every other test here asserts a REFUSAL, and a guard that
    refuses everything satisfies all of them.
    """
    m = _fetch(monkeypatch, _payload())
    assert len(m.fetch_rows("k")) == 327


def test_a_truncated_universe_is_refused(monkeypatch):
    """The failure that must never publish: fewer rows than the API reports."""
    m = _fetch(monkeypatch, _payload(n_rows=10, total=327))
    with pytest.raises(SystemExit) as e:
        m.fetch_rows("k")
    assert "10 of 327" in str(e.value)


def test_unverifiable_coverage_is_refused(monkeypatch):
    """★ The 'cannot tell' branch, resolved toward REFUSAL.

    If _total_available is absent, coverage is unknown — and unknown must not
    read as healthy. Treating a missing denominator as success is how a
    truncated universe would ship looking fine.
    """
    p = _payload()
    del p["_total_available"]
    m = _fetch(monkeypatch, p)
    with pytest.raises(SystemExit) as e:
        m.fetch_rows("k")
    assert "_total_available" in str(e.value)


def test_a_populated_pro_field_is_refused(monkeypatch):
    """Paywall check on the INPUT, not only on the columns written out.

    The Pro numerics arrive null today. If the gate ever starts sending them,
    the free-tier dataset would silently inherit paid data, and a column-name
    check on the output would not notice because the column names never change.
    """
    m = _fetch(monkeypatch, _payload(pro_value=87))
    with pytest.raises(SystemExit) as e:
        m.fetch_rows("k")
    assert "composite_score" in str(e.value)


def test_only_free_columns_are_written():
    """The published column set is free-tier by construction."""
    m = _mod()
    assert set(m.FREE_COLUMNS).isdisjoint(m.PRO_FIELDS)
    rows = m.to_csv_rows([{"market_slug": "a", "verdict": "BUILD",
                           "composite_score": 91, "time_to_power_months": 12}])
    assert set(rows[0]) == set(m.FREE_COLUMNS)
    assert "composite_score" not in rows[0]


def test_the_output_paywall_check_can_fail():
    """A guard nobody has seen fail is not a guard."""
    m = _mod()
    clean = [{c: "" for c in m.FREE_COLUMNS}]
    m.check_paywall(clean)                       # must not raise
    with pytest.raises(SystemExit):
        m.check_paywall([dict(clean[0], composite_score=1)])


def test_the_workflow_publishes_only_on_a_real_change():
    """An empty commit every Monday turns the dataset history into a calendar.

    The point of the schedule is to record when the index MOVED. A run that
    finds no change must exit having pushed nothing.
    """
    wf = os.path.join(REPO, ".github", "workflows", "hf-dataset-refresh.yml")
    src = open(wf, encoding="utf-8").read()
    assert "git diff --quiet" in src, (
        "the publish step must short-circuit when the CSV is unchanged"
    )
    assert "workflow_dispatch" in src, "the refresh must be runnable on demand"


def test_the_workflow_installs_every_third_party_import_the_script_needs():
    """★ THE FIRST REAL RUN DIED HERE, on ModuleNotFoundError: requests.

    The generator originally used urllib and needed no install step. It moved to
    requests because scripts/regression_lint.py blocks urllib outright (a
    "Python-urllib" User-Agent is 403'd pre-worker by Cloudflare). That lint fix
    was correct and passed every gate — the lint, the unit tests, the guards
    here — because none of them run the workflow. The dependency changed and the
    environment that has to satisfy it did not.

    So this pins the pair rather than either half: every third-party module the
    script imports must be named in the workflow's install step. Comparing an
    import list to an install list is the only check that spans both files.
    """
    import ast as _ast

    wf = os.path.join(REPO, ".github", "workflows", "hf-dataset-refresh.yml")

    # ★ MATCH THE INSTALL COMMAND, NOT THE FILE TEXT. The first draft of this
    #   test did `mod in workflow` and passed with the install step DELETED —
    #   because the comment above that step explains why requests replaced
    #   urllib, and the word "requests" in that prose satisfied the check. A
    #   fence a comment can satisfy is a fence that fails open. Comments are
    #   stripped and only real `pip install` lines are considered.
    installs = " ".join(
        l.strip() for l in open(wf, encoding="utf-8").read().splitlines()
        if not l.lstrip().startswith("#") and "pip install" in l
    )

    tree = _ast.parse(open(SCRIPT, encoding="utf-8").read())
    imported = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, _ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    # Anything in the standard library needs no install; only third-party does.
    import sys as _sys
    stdlib = getattr(_sys, "stdlib_module_names", set())
    third_party = {m for m in imported if m not in stdlib and not m.startswith("_")}

    for mod in sorted(third_party):
        assert mod in installs, (
            f"scripts/refresh_hf_dcpi_dataset.py imports {mod!r}, which is not "
            f"stdlib and is not installed by hf-dataset-refresh.yml. The run "
            f"will die on ModuleNotFoundError — as it did for 'requests'. Add "
            f"it to the 'Install dependencies' step."
        )
