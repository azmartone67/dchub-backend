"""The unit-tests shards: tests/_shard.py, scripts/unit_test_shards_verdict.py, and
their wiring in .github/workflows/pre-merge.yml.

Splitting the suite is safe only while these hold, and each has a test here that
fails when it breaks:
  - every file runs in exactly one shard, and the tail guards run in every shard
    (the plan, the plugin end to end, and the verdict that re-checks it in CI);
  - the verdict FAILS a missing, overlapping, disagreeing or idle shard, and calls
    an absent report UNMEASURED rather than clean;
  - the workflow takes the index and count from strategy.job-index/job-total, and
    `unit-tests` stays the single judge and the ledger's single writer.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests import _shard

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "pre-merge.yml"
TAIL = "test_zz_tail.py"


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def v():
    return _script("unit_test_shards_verdict")


# ── the plan ──────────────────────────────────────────────────────────────────

SECONDS = {"a.py": 50.0, "b.py": 40.0, "c.py": 30.0, "d.py": 20.0, "e.py": 10.0}


def test_the_plan_puts_every_file_on_exactly_one_shard_and_uses_every_shard():
    files = sorted(SECONDS) + ["new.py"]
    owner, load = _shard.plan(files, SECONDS, 1.0, 3)
    assert sorted(owner) == sorted(files)
    assert set(owner.values()) == {0, 1, 2}
    assert sum(load) == pytest.approx(sum(SECONDS.values()) + 1.0)


def test_the_plan_is_longest_first_onto_the_lightest_shard_whatever_the_input_order():
    owner, load = _shard.plan(sorted(SECONDS), SECONDS, 1.0, 2)
    # a->0; b->1; c->1 (40<50); d->0 (50<70); e->0 (70=70, the tie goes to the lowest index)
    assert owner == {"a.py": 0, "b.py": 1, "c.py": 1, "d.py": 0, "e.py": 0}
    assert load == [80.0, 70.0]
    assert _shard.plan(sorted(SECONDS, reverse=True), SECONDS, 1.0, 2) == (owner, load)


def test_replicated_files_are_owned_by_no_shard_and_weigh_on_every_shard():
    owner, load = _shard.plan(sorted(SECONDS) + ["t.py"], {**SECONDS, "t.py": 5.0}, 1.0, 2, ["t.py"])
    assert "t.py" not in owner
    assert load == [85.0, 75.0]


def test_a_file_missing_from_the_table_weighs_the_default():
    assert _shard.plan(["x.py", "y.py"], {}, 2.5, 1)[1] == [5.0]


# ── the plugin, end to end, in a pytest of its own ────────────────────────────

@pytest.fixture
def suite(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "conftest.py").write_text(f"_TAIL_GUARDS = ({TAIL!r},)\n")
    for name in "abcdefg":
        (tmp_path / f"test_{name}.py").write_text("def test_one():\n    pass\n\n\ndef test_two():\n    pass\n")
    (tmp_path / TAIL).write_text("def test_tail():\n    pass\n")
    return tmp_path


def _pytest(suite, env_extra, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DCHUB_TEST_SHARD_")}
    env.update(env_extra)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(ROOT), env.get("PYTHONPATH", "")) if p)
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(suite), "-c", str(suite / "pytest.ini"), "-q",
         "-p", "no:cacheprovider", "-p", "tests._shard", *args],
        cwd=suite, env=env, capture_output=True, text=True, timeout=120)


def _shard_env(index, total, report=None):
    env = {"DCHUB_TEST_SHARD_INDEX": str(index), "DCHUB_TEST_SHARD_TOTAL": str(total)}
    if report:
        env["DCHUB_TEST_SHARD_REPORT"] = str(report)
    return env


def test_the_shards_run_every_file_once_and_every_shard_runs_the_tail_guards(suite, tmp_path_factory, v):
    out, total = tmp_path_factory.mktemp("shards"), 3
    for index in range(total):
        d = out / f"unit-tests-shard-{index}"
        d.mkdir()
        r = _pytest(suite, _shard_env(index, total, d / "unit-tests-shard.json"))
        assert r.returncode == 0, r.stdout + r.stderr
        (d / "unit-tests.log").write_text(r.stdout)
        assert f"unit-tests shard {index}/{total}: kept" in r.stdout
    reports, errors = v.load(str(out))
    assert len(reports) == total and not errors
    for rep in reports:
        assert rep["replicated"] == [TAIL] and TAIL in rep["kept_files"]
        assert rep["ran"] == rep["selected"] == 2 * (len(rep["kept_files"]) - 1) + 1
    owned = sorted(f for rep in reports for f in rep["kept_files"] if f != TAIL)
    assert owned == [f"test_{n}.py" for n in "abcdefg"]
    assert v.verdict(reports)[0] == 0
    assert v.main(["unit_test_shards_verdict.py", str(out)]) == 0


@pytest.mark.parametrize("env", [{}, _shard_env(3, 3), _shard_env("x", 3)], ids=["unset", "index-past-total", "not-a-number"])
def test_the_plugin_refuses_to_run_without_a_valid_shard(suite, env):
    r = _pytest(suite, env, "--collect-only")
    assert r.returncode == 4, r.stdout + r.stderr  # pytest's usage error: no tests ran, none were skipped silently
    assert "tests/_shard.py" in r.stdout + r.stderr


def test_the_plugin_refuses_to_shard_when_no_conftest_names_the_tail_guards(suite):
    (suite / "conftest.py").write_text("")
    r = _pytest(suite, _shard_env(0, 2), "--collect-only")
    assert r.returncode == 4, r.stdout + r.stderr
    assert "_TAIL_GUARDS" in r.stdout + r.stderr


# ── the verdict ───────────────────────────────────────────────────────────────

FILES = ["a.py", "b.py", "c.py", "tail.py"]


def _report(v, index, kept, total=2, ran=3):
    return {"index": index, "total": total, "collected": 9, "selected": 3, "n_files": len(FILES),
            "files_sha256": v._digest(FILES), "kept_files": sorted(kept), "replicated": ["tail.py"], "ran": ran}


def _good(v):
    return [_report(v, 0, ["a.py", "c.py", "tail.py"]), _report(v, 1, ["b.py", "tail.py"])]


def test_the_verdict_passes_a_complete_disjoint_split(v):
    code, lines = v.verdict(_good(v))
    assert code == 0 and not [line for line in lines if line.startswith("::error")]


@pytest.mark.parametrize("break_it, says", [
    (lambda v, rs: rs[:1], "no report from shard(s) [1]"),
    (lambda v, rs: [rs[0], {**rs[1], "kept_files": ["a.py", "b.py", "tail.py"]}], "more than one shard"),
    (lambda v, rs: [rs[0], {**rs[1], "kept_files": ["tail.py"]}], "ran in no shard"),
    (lambda v, rs: [rs[0], {**rs[1], "kept_files": ["b.py"]}], "not run in every shard"),
    (lambda v, rs: [rs[0], {**rs[1], "files_sha256": "0" * 64}], "different sets of test files"),
    (lambda v, rs: [rs[0], {**rs[1], "total": 3}], "disagree on the shard count"),
    (lambda v, rs: [rs[0], {**rs[1], "index": 0}], "duplicate or out-of-range"),
    (lambda v, rs: [rs[0], {**rs[1], "ran": 0}], "ran zero tests"),
    (lambda v, rs: [rs[0], {**rs[1], "ran": None}], "ran zero tests"),
], ids=["missing-shard", "overlap", "file-in-no-shard", "tail-guard-missing", "file-sets-differ",
        "totals-differ", "duplicate-index", "idle-shard", "no-log"])
def test_the_verdict_fails_every_broken_split(v, break_it, says):
    code, lines = v.verdict(break_it(v, _good(v)))
    assert code == 1
    assert [line for line in lines if line.startswith("::error") and says in line], lines


def test_no_report_at_all_is_unmeasured_not_clean(v, tmp_path):
    code, lines = v.verdict(*v.load(str(tmp_path)))
    assert code == 2 and "UNMEASURED" in lines[0]


@pytest.mark.parametrize("body", ["{not json", json.dumps({"index": 0})], ids=["unparseable", "keys-missing"])
def test_an_unreadable_report_is_unmeasured(v, tmp_path, body):
    (tmp_path / "unit-tests-shard-0").mkdir()
    (tmp_path / "unit-tests-shard-0" / "unit-tests-shard.json").write_text(body)
    code, lines = v.verdict(*v.load(str(tmp_path)))
    assert code == 2 and "UNMEASURED" in lines[0]


def test_ran_is_read_from_the_shards_own_log_the_way_the_beat_reads_it(v, tmp_path):
    d = tmp_path / "unit-tests-shard-0"
    d.mkdir()
    (d / "unit-tests-shard.json").write_text(json.dumps(_report(v, 0, FILES, total=1)))
    (d / "unit-tests.log").write_text("x PASSED\n==== 7 passed, 2 failed, 3 skipped in 1.0s ====\n")
    assert v.load(str(tmp_path))[0][0]["ran"] == 9
    (d / "unit-tests.log").write_text("Interrupted: 1 error during collection\n")
    assert v.load(str(tmp_path))[0][0]["ran"] == 0


# ── the durations table ───────────────────────────────────────────────────────

def test_each_gap_is_charged_to_the_later_file_and_the_largest_copy_wins():
    d = _script("shard_durations")
    log = [
        "﻿2026-09-21T08:00:00.0000000Z collecting ... collected 3 items\n",
        "2026-09-21T08:00:02.5000000Z tests/test_a.py::test_x PASSED [ 33%]\n",
        "2026-09-21T08:00:03.0000000Z tests/test_b.py::test_y PASSED [ 66%]\n",
        "2026-09-21T08:00:03.1000000Z \x1b[32mtests/test_b.py::test_z PASSED\x1b[0m [100%]\n",
    ]
    per = d.file_seconds(log)
    assert per == pytest.approx({"tests/test_a.py": 2.5, "tests/test_b.py": 0.6})
    table = d.table([per, {"tests/test_a.py": 4.0}], "src")
    assert table["seconds"] == {"tests/test_a.py": 4.0}  # test_b is under MIN_SECONDS
    assert table["default_seconds"] == 0.6 and table["files_measured"] == 2


def test_the_committed_table_is_one_the_plugin_can_read():
    seconds, default = _shard._durations()
    assert default > 0 and len(seconds) >= 50
    assert all(f.startswith("tests/") and f.endswith(".py") and s >= 1.0 for f, s in seconds.items())


# ── the wiring ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def jobs():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _code(run):
    return "\n".join(line for line in run.splitlines() if not line.strip().startswith("#"))


def _step(job, name):
    [step] = [s for s in job["steps"] if s.get("name") == name]
    return step


def _uses(job, action):
    [step] = [s for s in job["steps"] if str(s.get("uses", "")).startswith(action + "@")]
    return step


def _beats(job):
    return [s for s in job["steps"] if str(s.get("name", "")).startswith("Beat the gate")]


def test_the_shards_take_their_index_and_count_from_github_never_the_matrix(jobs):
    shard = jobs["unit-tests-shard"]
    assert shard["strategy"]["fail-fast"] is False
    labels = shard["strategy"]["matrix"]["shard"]
    assert len(labels) >= 2 and labels == list(range(len(labels)))
    run = _step(shard, "Run pure-function tests")
    assert run["env"]["DCHUB_TEST_SHARD_INDEX"] == "${{ strategy.job-index }}"
    assert run["env"]["DCHUB_TEST_SHARD_TOTAL"] == "${{ strategy.job-total }}"
    assert run["env"]["DCHUB_NO_NETWORK_PARTIAL_RUN"] == "1"
    assert " -p tests._shard " in _code(run["run"])
    assert "matrix.shard" in shard["name"]


def test_every_shard_hands_its_log_and_report_on_even_when_it_failed(jobs):
    shard = jobs["unit-tests-shard"]
    hand = _step(shard, "Hand this shard's log and report to the unit-tests job")
    assert hand["if"] == "always()"
    for f in ("/tmp/unit-tests.log", "unit-tests-no-network.jsonl", "unit-tests-shard.json"):
        assert f in _code(hand["run"])
    up = _uses(shard, "actions/upload-artifact")
    assert up["if"] == "always()" and up["with"]["overwrite"] is True
    assert up["with"]["name"] == "unit-tests-shard-${{ strategy.job-index }}"
    assert _uses(jobs["unit-tests"], "actions/download-artifact")["with"]["pattern"] == "unit-tests-shard-*"


def test_unit_tests_is_the_only_judge_and_the_only_ledger_writer(jobs):
    judge = jobs["unit-tests"]
    assert judge["needs"] == "unit-tests-shard" and judge["if"] == "always()"
    assert "strategy" not in judge
    assert [jid for jid, job in jobs.items() if job.get("name", jid) == "unit-tests"] == ["unit-tests"]
    step = _step(judge, "Judge the shards as one suite")
    run = _code(step["run"])
    assert "python3 scripts/unit_test_shards_verdict.py" in run
    assert "python3 scripts/no_network_verdict.py" in run
    assert step["env"]["SHARDS"] == "${{ needs.unit-tests-shard.result }}"
    assert "DCHUB_NO_NETWORK_PARTIAL_RUN" not in step["env"]
    assert _beats(jobs["unit-tests-shard"]) == []
    [beat] = _beats(judge)
    assert "needs.unit-tests-shard.result == 'cancelled'" in beat["env"]["JOB_STATUS"]
