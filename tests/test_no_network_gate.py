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

    def run(lines):
        if lines is not None:
            log.write_text("".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines))
        return verdict(str(log), str(register))

    return run


def test_the_verdict_passes_registered_debt_and_only_warns_when_an_entry_goes_quiet(judge):
    assert judge([OTHER, PYTEST, _dns("tests/test_old.py", "dchub.cloud")])[0] == 0
    code, lines = judge([PYTEST])
    assert code == 0
    assert any(line.startswith("::warning file=tests/test_old.py::") for line in lines)


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
