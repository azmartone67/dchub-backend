"""No network under the test suite, for every test file: refuse, and log.

Two entry points, one implementation. .github/workflows/pre-merge.yml puts this
directory on PYTHONPATH for its "Run pure-function tests" step: Python imports
sitecustomize at startup, so there this runs in pytest and in every interpreter
its tests start, before any conftest, test module or import-time call — the
earliest point available, and the only one that covers a fetch at a test
module's own import. tests/conftest.py loads this same file by path when that
PYTHONPATH did not, which is what covers a plain local `python3 -m pytest
tests/`, and exports both env vars so the children of a local run are covered
too. install() is idempotent, so the two never double up. From then on:

- socket.getaddrinfo refuses any host that is not loopback (socket.gaierror).
  requests, urllib, http.client and httpx all resolve names through it.
- socket.socket.connect and connect_ex refuse any address that is not loopback.

Each refusal appends a JSON line to $DCHUB_NO_NETWORK_LOG naming the host, the
test file and the test, and each process appends one "loaded" line.
scripts/no_network_verdict.py then fails the step on any (file, host) that
tests/no_network_register.json does not list, and on a log that no pytest
process wrote, so a hook that never loaded cannot read as zero attempts.

Why: measured on 2026-09-14 with the network refused this way, 26 of 1,258
test files and one module reached dchub.cloud, the production backend and
third parties, and every test still passed, because the code they reach fails
soft. The register holds those as debt, so that a new one fails the step.

A test that monkeypatches socket.getaddrinfo or socket.socket.connect keeps
control while its patch is installed; monkeypatch restores these afterwards.
Out of reach: sockets opened from C (libpq) and non-Python child processes.
"""
import errno
import json
import os
import socket
import sys

_LOG = os.environ.get("DCHUB_NO_NETWORK_LOG")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REFUSED = "no network in the unit-tests step (tests/_no_network/sitecustomize.py)"


def _write(entry):
    line = json.dumps({"pid": os.getpid(), **entry}) + "\n"
    if not _LOG:
        if entry["ev"] != "loaded":
            sys.stderr.write(f"{_REFUSED}: {line}")
        return
    try:
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        sys.stderr.write(f"{_REFUSED}: cannot append to {_LOG} ({e})\n")


def _where():
    """(file, test): the running test, else the test module being collected,
    else the first repo module on the stack."""
    current = os.environ.get("PYTEST_CURRENT_TEST")  # "tests/test_x.py::test_y (call)"
    if current:
        nodeid = current.rsplit(" (", 1)[0]
        return nodeid.split("::", 1)[0], nodeid
    import traceback

    rels = [os.path.relpath(frame.filename, _ROOT) for frame in traceback.extract_stack()[:-1]]
    for rel in reversed(rels):
        if rel.startswith("tests" + os.sep) and os.path.basename(rel).startswith("test_"):
            return rel, None
    for rel in reversed(rels):
        if not rel.startswith(("..", "<", os.path.join("tests", "_no_network"))) and "site-packages" not in rel:
            return rel, None
    return None, None


def _local(host):
    if host is None:
        return True
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    name = str(host).strip("[]").split("%", 1)[0].lower().rstrip(".")
    if name in ("", "localhost", "localhost.localdomain") or name.endswith(".localhost"):
        return True
    import ipaddress

    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


def _refuse(ev, host, port):
    file, test = _where()
    _write({"ev": ev, "host": str(host), "port": port, "file": file, "test": test})


_real_getaddrinfo = socket.getaddrinfo
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def getaddrinfo(host, port, *args, **kwargs):
    if not _local(host):
        _refuse("dns", host, port)
        raise socket.gaierror(socket.EAI_NONAME, f"{_REFUSED}: lookup of {host!r}")
    return _real_getaddrinfo(host, port, *args, **kwargs)


def _off_loopback(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple) and address:
        if not _local(address[0]):
            return address[0], address[1] if len(address) > 1 else None
    return None


def connect(self, address):
    target = _off_loopback(self, address)
    if target:
        _refuse("connect", *target)
        raise ConnectionRefusedError(errno.ECONNREFUSED, f"{_REFUSED}: connect to {target[0]}:{target[1]}")
    return _real_connect(self, address)


def connect_ex(self, address):
    target = _off_loopback(self, address)
    if target:
        _refuse("connect", *target)
        return errno.ECONNREFUSED
    return _real_connect_ex(self, address)


_INSTALLED = "_dchub_no_network"


def installed():
    """True when this hook, or another copy of this file, already owns the
    socket entry points. Read off socket itself, not off a module global: CI
    loads this file as `sitecustomize` at interpreter startup and
    tests/conftest.py loads the same file by path, so the two copies share no
    state but do share `socket`."""
    return getattr(socket.getaddrinfo, _INSTALLED, False) is True


def install():
    """Refuse off-loopback DNS and connects. Returns whether it installed.

    Idempotent, and that is load-bearing in both directions. A second install
    would capture the first copy's wrappers as its `_real_*` and log a second
    "loaded" line for one process, which is the count scripts/no_network_verdict.py
    reads to decide the hook was watching at all. Bailing also leaves the first
    copy's `_real_*` — the genuine socket functions — as the only way out.
    """
    if installed():
        return False
    for wrapper in (getaddrinfo, connect, connect_ex):
        setattr(wrapper, _INSTALLED, True)
    socket.getaddrinfo = getaddrinfo
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    _write({"ev": "loaded", "pytest": any(os.path.basename(a) in ("pytest", "py.test") for a in getattr(sys, "orig_argv", ()))})
    return True


install()
