"""Split the suite across the `unit-tests shard N` CI jobs, by whole FILE.

Loaded only by the unit-tests shards in .github/workflows/pre-merge.yml
(`-p tests._shard`, with DCHUB_TEST_SHARD_INDEX / DCHUB_TEST_SHARD_TOTAL set from
`strategy.job-index` / `strategy.job-total`). A plain `pytest tests/` never loads
it, so local runs are unchanged.

★ Every shard COLLECTS THE WHOLE SUITE, then keeps only its own files. It is not
handed a file list. Collection imports every test module, so each shard's process
starts from the state a single run's did — the import-time sys.modules ledger
that test_no_import_time_module_stubs.py reads is complete in every shard. That
costs one full collection (~40s) per shard; a file list would be faster and
would quietly change what every shard's process has imported.

★ Whole files, never split. Tests in a file share module state and module
fixtures, and a file split across processes runs its tests out of order.

★ The tail guards (tests/conftest.py `_TAIL_GUARDS`) run in EVERY shard. Each one
judges state that the files before it left in the SAME process — the scan-floor
observation table, the stub ledger — so a single copy in one shard would judge
a sixth of the suite and call it clean. Replicated, each copy judges its own
shard's files, and every other file runs in exactly one shard, so together the
copies judge all of them. The list is read from conftest, never copied here.

Assignment is longest-processing-time-first over tests/shard_durations.json:
each file, heaviest first, goes onto the currently lightest shard. It is
deterministic, so every shard computes the same plan on its own. A file missing
from the table weighs `default_seconds`; a stale table costs balance, never
coverage.

Each shard writes a report (DCHUB_TEST_SHARD_REPORT). The `unit-tests` job hands
all of them to scripts/unit_test_shards_verdict.py, which fails unless every
file ran in exactly one shard. So a bug here turns CI red; it cannot quietly
skip tests.
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

DURATIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shard_durations.json")
_INDEX, _TOTAL, _REPORT = "DCHUB_TEST_SHARD_INDEX", "DCHUB_TEST_SHARD_TOTAL", "DCHUB_TEST_SHARD_REPORT"


def plan(files, seconds, default, total, replicated=()):
    """Assign every non-replicated file to one shard. Returns (owner, load).

    owner maps file -> shard index. load[k] is shard k's planned seconds, and
    it includes the replicated files, which every shard carries.
    """
    replicated = set(replicated)
    weight = lambda f: seconds.get(f, default)  # noqa: E731
    load = [sum(weight(f) for f in files if f in replicated)] * total
    owner = {}
    for f in sorted((f for f in files if f not in replicated), key=lambda f: (-weight(f), f)):
        k = min(range(total), key=load.__getitem__)  # first minimum: ties go to the lowest index
        owner[f] = k
        load[k] += weight(f)
    return owner, load


def files_digest(files):
    return hashlib.sha256("\n".join(sorted(files)).encode()).hexdigest()


def _file(item):
    return item.nodeid.split("::", 1)[0]


def _shard_from_env():
    raw = {name: os.environ.get(name, "") for name in (_INDEX, _TOTAL)}
    try:
        index, total = int(raw[_INDEX]), int(raw[_TOTAL])
    except ValueError:
        raise pytest.UsageError(
            f"tests/_shard.py needs integer {_INDEX} and {_TOTAL}; got {raw!r}") from None
    if not 0 <= index < total:
        raise pytest.UsageError(f"tests/_shard.py: shard index {index} is outside 0..{total - 1}")
    return index, total


def _tail_guards(config):
    """Basenames every shard must run, from the `_TAIL_GUARDS` of the loaded conftest(s)."""
    guards = set()
    for plugin in config.pluginmanager.get_plugins():
        guards.update(getattr(plugin, "_TAIL_GUARDS", None) or ())
    if not guards:
        raise pytest.UsageError(
            "tests/_shard.py found no _TAIL_GUARDS in any loaded conftest. Sharding without "
            "them would leave each tail guard judging one shard's files.")
    return guards


def _durations():
    try:
        with open(DURATIONS, encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(k): float(v) for k, v in data["seconds"].items()}, float(data["default_seconds"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise pytest.UsageError(f"tests/_shard.py cannot read {DURATIONS}: {e}") from None


def pytest_configure(config):
    # Fail before a ~40s collection, not after it.
    config._dchub_shard = _shard_from_env()


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    # trylast: tests/conftest.py's hook has already moved the tail guards to the
    # end. Filtering keeps the relative order, so they stay last in every shard.
    index, total = config._dchub_shard
    seconds, default = _durations()
    tail = _tail_guards(config)
    files = sorted({_file(i) for i in items})
    replicated = sorted(f for f in files if os.path.basename(f) in tail)
    owner, load = plan(files, seconds, default, total, replicated)
    keep, drop = [], []
    for item in items:
        f = _file(item)
        (keep if f in replicated or owner[f] == index else drop).append(item)
    # From what was KEPT, never from the plan: a report that restated the plan
    # would certify a tail guard the filter had dropped.
    kept_files = sorted({_file(i) for i in keep})
    report = {
        "index": index,
        "total": total,
        "collected": len(items),
        "selected": len(keep),
        "n_files": len(files),
        "files_sha256": files_digest(files),
        "kept_files": kept_files,
        "replicated": replicated,
        "planned_seconds": round(load[index], 1),
    }
    config._dchub_shard_report = report
    path = os.environ.get(_REPORT)
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh)
    if drop:
        config.hook.pytest_deselected(items=drop)
    items[:] = keep


def pytest_terminal_summary(terminalreporter, config):
    r = getattr(config, "_dchub_shard_report", None)
    if r:
        terminalreporter.write_line(
            f"unit-tests shard {r['index']}/{r['total']}: kept {r['selected']} of {r['collected']} "
            f"collected tests, {len(r['kept_files'])} of {r['n_files']} files "
            f"(the {len(r['replicated'])} tail guard file(s) run in every shard); "
            f"planned {r['planned_seconds']}s")
