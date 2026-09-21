"""The unit-tests step's no-network rule: the hook, the verdict, the register, the wiring.

.github/workflows/pre-merge.yml runs "Run pure-function tests" with
tests/_no_network on PYTHONPATH; that sitecustomize.py's docstring says why.
Every attempt this file makes on purpose runs in a child interpreter with its
own log, and goes to gate-probe.invalid (RFC 6761: never resolves) or to
192.0.2.1 (TEST-NET-1, RFC 5737: never routed). A broken hook therefore fails
fast here instead of reaching anyone, and the step's own log never sees them.
"""
import errno
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOOK_DIR = ROOT / "tests" / "_no_network"
REGISTER = ROOT / "tests" / "no_network_register.json"
HERE = "tests/test_no_network_gate.py"
REFUSED = "no network in the unit-tests step"


def _verdict_module():
    spec = importlib.util.spec_from_file_location("no_network_verdict", ROOT / "scripts" / "no_network_verdict.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hooked(args, log):
    env = {**os.environ, "PYTHONPATH": str(HOOK_DIR), "DCHUB_NO_NETWORK_LOG": str(log)}
    return subprocess.run([sys.executable, *args], env=env, capture_output=True, text=True, timeout=60)


def _entries(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


PROBE = r"""
import json, socket, urllib.request
out = {}
def attempt(name, fn):
    try:
        out[name] = ["ok", repr(fn())]
    except Exception as e:
        out[name] = [type(e).__name__, str(e), getattr(e, "errno", None)]
def connect():
    s = socket.socket(); s.settimeout(3)
    try: s.connect(("192.0.2.1", 8080))
    finally: s.close()
def connect_ex():
    s = socket.socket(); s.settimeout(3)
    try: return s.connect_ex(("192.0.2.1", 8080))
    finally: s.close()
attempt("getaddrinfo", lambda: socket.getaddrinfo("gate-probe.invalid", 443))
attempt("urlopen", lambda: urllib.request.urlopen("http://gate-probe.invalid/x", timeout=3))
attempt("connect", connect)
out["connect_ex"] = connect_ex()
server = socket.socket(); server.bind(("127.0.0.1", 0)); server.listen(1)
attempt("loopback", lambda: socket.create_connection(server.getsockname(), timeout=3).close())
server.close()
attempt("localhost", lambda: len(socket.getaddrinfo("localhost", 80)) > 0)
print(json.dumps(out))
"""


def test_the_hook_refuses_and_logs_every_lookup_and_connect_off_loopback(tmp_path):
    log = tmp_path / "hook.jsonl"
    r = _hooked(["-c", PROBE], log)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["getaddrinfo"][0] == "gaierror" and REFUSED in out["getaddrinfo"][1]
    assert out["urlopen"][0] == "URLError" and REFUSED in out["urlopen"][1]
    assert out["connect"][0] == "ConnectionRefusedError" and out["connect"][2] == errno.ECONNREFUSED
    assert out["connect_ex"] == errno.ECONNREFUSED
    assert out["loopback"] == ["ok", "None"] and out["localhost"] == ["ok", "True"]
    attempts = [(e["ev"], e["host"], e["file"]) for e in _entries(log) if e["ev"] != "loaded"]
    assert attempts == [("dns", "gate-probe.invalid", HERE)] * 2 + [("connect", "192.0.2.1", HERE)] * 2


def test_each_process_says_whether_it_runs_pytest(tmp_path):
    log = tmp_path / "loaded.jsonl"
    assert _hooked(["-c", "pass"], log).returncode == 0
    assert _hooked(["-m", "pytest", "--version"], log).returncode == 0
    assert [(e["ev"], e["pytest"]) for e in _entries(log)] == [("loaded", False), ("loaded", True)]


PYTEST = {"ev": "loaded", "pytest": True}
OTHER = {"ev": "loaded", "pytest": False}


def _dns(file, host):
    return {"ev": "dns", "host": host, "port": 443, "file": file, "test": f"{file}::test_x"}


@pytest.fixture
def judge(tmp_path):
    verdict = _verdict_module().verdict
    register = tmp_path / "register.json"
    register.write_text(json.dumps({"known": {"tests/test_old.py": ["dchub.cloud"]}}))
    log = tmp_path / "log.jsonl"

    def run(lines, **kw):
        if lines is not None:
            log.write_text("".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines))
        return verdict(str(log), str(register), **kw)

    return run


def test_the_verdict_passes_registered_debt_and_only_warns_when_an_entry_goes_quiet(judge):
    assert judge([OTHER, PYTEST, _dns("tests/test_old.py", "dchub.cloud")])[0] == 0
    code, lines = judge([PYTEST])
    assert code == 0
    assert any(line.startswith("::warning file=tests/test_old.py::") for line in lines)


def test_a_shard_log_skips_the_quiet_warning_and_still_fails_a_new_host(judge):
    """One `unit-tests shard N` log covers that shard's files only, so every
    registered file that ran in another shard looks quiet in it. partial=True
    drops that warning (the unit-tests job gives it once, over all shards' logs),
    and must drop nothing else."""
    code, lines = judge([PYTEST], partial=True)
    assert code == 0
    assert not [line for line in lines if line.startswith("::warning")]
    assert any("partial run" in line for line in lines)
    code, lines = judge([PYTEST, _dns("tests/test_new.py", "example.com")], partial=True)
    assert code == 1 and any(line.startswith("::error file=tests/test_new.py::") for line in lines)
    assert judge([OTHER], partial=True)[0] == 1  # no pytest process loaded the hook


@pytest.mark.parametrize("flag, warns", [("1", False), ("0", True), (None, True)])
def test_main_takes_partial_from_the_step_environment(tmp_path, monkeypatch, capsys, flag, warns):
    register = tmp_path / "register.json"
    register.write_text(json.dumps({"known": {"tests/test_old.py": ["dchub.cloud"]}}))
    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps(PYTEST) + "\n")
    if flag is None:
        monkeypatch.delenv("DCHUB_NO_NETWORK_PARTIAL_RUN", raising=False)
    else:
        monkeypatch.setenv("DCHUB_NO_NETWORK_PARTIAL_RUN", flag)
    assert _verdict_module().main(["no_network_verdict.py", str(log), str(register)]) == 0
    assert ("::warning file=tests/test_old.py::" in capsys.readouterr().out) is warns


def test_the_verdict_fails_a_new_file_and_a_new_host_for_a_registered_file(judge):
    code, lines = judge([PYTEST, _dns("tests/test_new.py", "dchub.cloud")])
    assert code == 1
    assert any(line.startswith("::error file=tests/test_new.py::") and "dchub.cloud (1x)" in line for line in lines)
    code, lines = judge([PYTEST, _dns("tests/test_old.py", "dchub.cloud"), _dns("tests/test_old.py", "api.github.com")])
    assert code == 1
    assert any(line.startswith("::error file=tests/test_old.py::") and "api.github.com (1x)" in line for line in lines)


@pytest.mark.parametrize("lines", [None, [], [OTHER], [OTHER, _dns("tests/test_old.py", "dchub.cloud")]])
def test_the_verdict_fails_when_no_pytest_process_loaded_the_hook(judge, lines):
    code, out = judge(lines)
    assert code == 1
    assert any("loaded in no pytest process" in line for line in out)


def test_the_verdict_fails_an_unparseable_log_and_an_unreadable_register(judge, tmp_path):
    assert judge([PYTEST, '{"ev": "dn'])[0] == 1
    assert _verdict_module().verdict(str(tmp_path / "log.jsonl"), str(tmp_path / "missing.json"))[0] == 1


def test_every_register_entry_names_a_file_that_exists_with_sorted_hosts():
    known = json.loads(REGISTER.read_text())["known"]
    missing = [name for name in known if not (ROOT / name).is_file()]
    assert not missing, f"registered files that no longer exist; remove or rename their entries: {missing}"
    assert list(known) == sorted(known)
    assert all(hosts and hosts == sorted(set(hosts)) for hosts in known.values())


def test_the_unit_tests_step_loads_the_hook_and_runs_the_verdict_on_its_log():
    workflow = (ROOT / ".github" / "workflows" / "pre-merge.yml").read_text()
    parts = workflow.split("- name: Run pure-function tests\n")
    assert len(parts) == 2
    step = re.split(r"\n\s*- name: ", parts[1], maxsplit=1)[0]
    run = "\n".join(line for line in step.splitlines() if not line.strip().startswith("#")) + "\n"
    assert re.search(
        r'PYTHONPATH="\$PWD/tests/_no_network[^"\n]*" DCHUB_NO_NETWORK_LOG="\$log" '
        r"python3 -m pytest tests/ [^\n]*\| tee /tmp/unit-tests\.log \|\| status=\$\?\n",
        run,
    )
    assert re.search(r'\n\s*python3 scripts/no_network_verdict\.py "\$log" \|\| status=1\n\s*exit \$status\n', run)


# ── The rule applies to a local run too, not only to the CI step ─────────────
#
# Everything above tests the hook as the workflow loads it: in a subprocess,
# with PYTHONPATH set by hand. These test the second entry point — that
# tests/conftest.py installed the same hook in the process running THIS test,
# and exported it to the children a test starts. Without them the local half
# is only a comment: the suite would pass just as well with the conftest block
# deleted, because the CI step sets PYTHONPATH itself.
OPT_OUT = os.environ.get("DCHUB_NO_NETWORK") == "0"
no_opt_out = pytest.mark.skipif(OPT_OUT, reason="DCHUB_NO_NETWORK=0 turned the hook off for this run")


@no_opt_out
def test_the_hook_is_installed_in_the_process_running_this_test(tmp_path):
    """Either entry point satisfies this: PYTHONPATH in CI, conftest locally.

    The probe is logged to a file of our own, not to the run's log. Sending it
    there instead would put an unregistered host under this file's name in
    every run, and scripts/no_network_verdict.py would fail the unit-tests step
    on a probe that by RFC 6761 can never resolve. Redirecting reaches the
    INSTALLED copy through the wrapper's own globals, because which copy that
    is differs by entry point — `sitecustomize` in CI, `dchub_no_network` from
    tests/conftest.py — and the copy read the path at its import.
    """
    import socket

    assert getattr(socket.getaddrinfo, "_dchub_no_network", False) is True, (
        "no-network hook is not installed in this pytest process; tests/conftest.py "
        "installs it when the unit-tests step's PYTHONPATH has not"
    )
    hook = socket.getaddrinfo.__globals__
    log = tmp_path / "in_process.jsonl"
    outer = hook["_LOG"]
    hook["_LOG"] = str(log)
    try:
        with pytest.raises(socket.gaierror) as e:
            socket.getaddrinfo("gate-inprocess-probe.invalid", 443)
        assert socket.getaddrinfo("localhost", 80), "loopback must still resolve"
    finally:
        hook["_LOG"] = outer
    assert REFUSED in str(e.value)
    assert [(x["ev"], x["host"], x["file"]) for x in _entries(log)] == [
        ("dns", "gate-inprocess-probe.invalid", HERE)
    ], "the refusal was not logged in this process, or not against this file"


@no_opt_out
def test_a_child_process_inherits_the_refusal():
    """The env, not the import, is what crosses a process boundary.

    tests/test_app_contract_gate.py boots main.py in a subprocess and the
    register lists it reaching two hosts. An in-process hook sees none of them,
    so if this stops holding, that traffic comes back with nothing to report it.
    Deliberately does NOT set PYTHONPATH the way _hooked() does — inheriting
    os.environ unchanged is the whole assertion.
    """
    probe = (
        "import socket, sys;"
        "sys.exit(0 if getattr(socket.getaddrinfo, '_dchub_no_network', False) is True else 1)"
    )
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (
        "a child of the test process does not load the no-network hook; "
        f"PYTHONPATH={os.environ.get('PYTHONPATH')!r} stderr={r.stderr!r}"
    )


@no_opt_out
def test_installing_the_hook_again_changes_nothing(tmp_path):
    """install() is idempotent, and that keeps the verdict honest.

    Both entry points can fire in one process. A second install would capture
    the first copy's wrappers as its own `_real_*` — making the genuine socket
    functions unreachable — and would log a second "loaded" line for one
    process, which is the count the verdict reads to decide anything was
    watching at all.
    """
    import socket

    spec = importlib.util.spec_from_file_location("gate_hook_copy", HOOK_DIR / "sitecustomize.py")
    copy = importlib.util.module_from_spec(spec)
    before = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex
    log = tmp_path / "second.jsonl"
    # Point the copy at a log of our own so a stray "loaded" line is visible here
    # rather than buried in the run's. Restored, not popped: the rest of the
    # session logs refusals to whatever was already set.
    outer = os.environ.get("DCHUB_NO_NETWORK_LOG")
    os.environ["DCHUB_NO_NETWORK_LOG"] = str(log)   # the copy reads this at import
    try:
        spec.loader.exec_module(copy)               # imports, and calls install()
        assert copy.installed() is True
        assert copy.install() is False, "install() claimed to install over an installed hook"
    finally:
        if outer is None:
            os.environ.pop("DCHUB_NO_NETWORK_LOG", None)
        else:
            os.environ["DCHUB_NO_NETWORK_LOG"] = outer
    assert (socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex) == before
    assert not log.exists(), f"a second copy logged itself as a separate process: {_entries(log)}"
