#!/usr/bin/env python3
"""The flusher must read every status that means "not yet delivered".

★ THE DEFECT, measured 2026-09-20 the moment a real CRM provider was finally
configured. flush_outbound_queue selected:

    WHERE status = 'queued'

The stub path parks rows in 'queued_export' (crawler_scheduler: "so the CSV
export endpoint can vacuum them later"), and the CSV export reads BOTH. So
every row accumulated during stub mode was invisible to the one job meant to
drain it — permanently.

Live proof: 31 rows, all 'queued_export', 24 paid_conversion, oldest 105 days.
CRM_PROVIDER=hubspot was set; `POST /crm/flush?limit=1` returned

    {"ok": true, "pushed": 0, "failed": 0, "skipped": 0, "provider": "hubspot"}

Green, and it had looked at zero rows.

MUST-FAIL CONTROLS included.
"""
from __future__ import annotations

import inspect
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture()
def m():
    import routes.crm_reverse_etl as mod
    return mod


@pytest.fixture()
def src(m):
    return inspect.getsource(m)


def _sql_of(src: str, fn_name: str) -> str:
    """The body of one function, comments stripped — a comment that names a
    status would otherwise satisfy a check about the query."""
    i = src.index("def %s(" % fn_name)
    j = src.find("\ndef ", i + 1)
    body = src[i:j if j > 0 else len(src)]
    return "\n".join(ln for ln in body.splitlines()
                     if not ln.lstrip().startswith("#"))


def test_queued_export_is_unsent_not_terminal(m):
    assert "queued" in m.UNSENT_STATUSES
    assert "queued_export" in m.UNSENT_STATUSES, (
        "queued_export dropped out of UNSENT — the stub-mode backlog goes "
        "invisible to the flusher again")


def test_the_flusher_reads_the_whole_backlog(m, src):
    """THE REGRESSION. A literal `status = 'queued'` here is the bug."""
    sql = _sql_of(src, "flush_outbound_queue")
    assert "UNSENT_STATUSES" in sql, (
        "flush_outbound_queue no longer uses the shared definition")
    assert not re.search(r"status\s*=\s*'queued'", sql), (
        "the flusher is back to selecting only 'queued' — every row parked by "
        "stub mode is unreachable")


def test_flush_and_csv_export_and_health_share_ONE_definition(m, src):
    """Three readers of "unsent". They drifted once; one definition now."""
    for fn in ("flush_outbound_queue", "admin_export_csv"):
        assert "UNSENT_STATUSES" in _sql_of(src, fn), f"{fn} has its own list"
    # no second hard-coded pair anywhere in the module
    pairs = re.findall(r"IN\s*\(\s*'queued'\s*,\s*'queued_export'\s*\)", src)
    assert not pairs, (
        "a literal ('queued','queued_export') pair is back — that is the "
        "second list this constant exists to remove")


def test_every_queued_status_the_module_writes_is_in_UNSENT(m, src):
    """★ Forward guard. A future 'queued_retry' would be written, never
    flushed, and never counted — exactly how queued_export behaved."""
    written = set(re.findall(r"['\"](queued[a-z_]*)['\"]", src))
    unknown = sorted(written - set(m.UNSENT_STATUSES))
    assert not unknown, (
        f"status(es) {unknown} are written but not in UNSENT_STATUSES — they "
        f"will accumulate invisibly. Add them, or give them a terminal meaning.")


def test_the_attempts_ceiling_survives(m, src):
    """Widening the status filter must not also un-cap retries."""
    assert "push_attempts < 5" in _sql_of(src, "flush_outbound_queue")


def test_health_counts_the_same_set_it_calls_stalled(m, src):
    """`queued` in the health report has to mean the same rows the flusher
    would pick up, or the number and the verdict describe different systems."""
    h = _sql_of(src, "admin_health")
    assert "UNSENT_STATUSES" in h, "health counts its own idea of queued"
    assert 'startswith("queued")' not in h, (
        "prefix matching is back — it silently absorbs any new queued_* status")


def test_no_file_keeps_a_PRIVATE_copy_of_the_unsent_set():
    """★ Cross-file. The /api/v1/health crm_export lane lives in main.py and had
    its own hard-coded ('queued','queued_export') pair. A local copy in ANY file
    is how the flusher and the parker drifted apart to begin with — main.py now
    imports UNSENT_STATUSES like everyone else."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    for rel in ("main.py", "routes/crm_reverse_etl.py", "crawler_scheduler.py"):
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            continue
        body = open(path, encoding="utf-8").read()
        code = "\n".join(ln for ln in body.splitlines()
                          if not ln.lstrip().startswith("#"))
        if re.search(r"IN\s*\(\s*'queued'\s*,\s*'queued_export'\s*\)", code):
            offenders.append(rel)
    assert not offenders, (
        f"{offenders} hard-code the unsent-status pair instead of importing "
        f"UNSENT_STATUSES — that is the second list this constant removed")


def test_main_py_health_lane_imports_the_shared_constant():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    body = open(os.path.join(root, "main.py"), encoding="utf-8").read()
    assert "from routes.crm_reverse_etl import UNSENT_STATUSES" in body, (
        "main.py's crm_export lane no longer shares the definition")
