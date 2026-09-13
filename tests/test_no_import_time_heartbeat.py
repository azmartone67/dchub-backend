"""Importing an extractor and exiting must not report a run to the source registry.

THE DEFECT (measured 2026-09-13, before this change)
  Ten modules ended with a "phase 92" block that registered an atexit hook at
  IMPORT time. The hook called dchub_heartbeat.heartbeat(<source>,
  status="success", metadata={"trigger": "atexit"}) with no row count, so ANY
  process that merely imported one of them reported a successful run of it when
  the process exited: a gunicorn worker recycling, a deploy, a scheduler.

  The public source registry (GET /api/v1/sources/<id>) showed it:
  backend-eia-api, -fiber-integration, -network-ix-ingestion, -news-engine and
  -subsea-cable logged `success` at the same instants, over and over, and every
  one of their 20 most recent runs had rows_affected AND duration_ms null.
  Separate extractors do not keep finishing together; one process exits.

  The beat now fires from each run's entry point (dchub_heartbeat.with_heartbeat),
  proven in tests/test_extractor_runs_beat_with_their_result.py.

WHAT THIS FILE PROVES
  1. Per module, behaviour: a fresh interpreter imports the module and exits
     normally, holding a credential and pointed at a local recorder standing in
     for the registry. Nothing may arrive except the harness's own exit probe.
  2. The harness can see the defect: a planted copy of the old block, imported
     the same way, MUST arrive, and before the probe.
  3. Statically, for every module in the repo: none that imports dchub_heartbeat
     registers an exit hook or sends a beat at module scope.

★ WHY EVERY RUN CARRIES AN EXIT PROBE. A child that died before its exit hooks
  ran (an import error, os._exit, a refused connection, a missing credential)
  would also record nothing, and "nothing arrived" would pass without having been
  able to fail. The probe is registered BEFORE the module under test, so atexit's
  LIFO order runs it AFTER the module's own hooks. Its arrival, last, proves the
  process got through them with a working credential and a reachable recorder.

★ HERMETIC. The child refuses every DNS lookup and every connection except to
  the recorder on 127.0.0.1, starts from a minimal environment (no DATABASE_URL,
  no real keys) and runs in a temporary directory.
"""
import ast
import http.server
import json
import os
import pathlib
import subprocess
import sys
import threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Every module that carried the import-time block, and the source it reported.
FORMERLY_BEAT_AT_EXIT = {
    "eia_api": "backend-eia-api",
    "eia_gas_bulk_loader": "backend-eia-bulk-loader",
    "facility_ingestion": "backend-facility-ingestion",
    "fiber_integration": "backend-fiber-integration",
    "network_ix_ingestion": "backend-network-ix-ingestion",
    "news_engine": "backend-news-engine",
    "news_facility_extractor": "backend-news-facility-extractor",
    "pipeline_deals_data_update": "backend-pipeline-deals-update",
    "seed_comprehensive_deals": "backend-seed-comprehensive-deals",
    "subsea_cable_ingestion": "backend-subsea-cable",
}

PROBE_SOURCE = "harness-exit-probe"
PLANTED_SOURCE = "harness-planted-phase92"

# The block those modules ended with until 2026-09-13, verbatim but the source.
PHASE92_BLOCK = '''
# === phase 92: source-registry heartbeat (auto-fires on clean module exit) ===
# Non-invasive: never crashes the script if the registry is unreachable.
_phase92_heartbeat_registered = True
try:
    import atexit as _phase92_atexit
    from dchub_heartbeat import heartbeat as _phase92_heartbeat
    def _phase92_emit():
        try:
            _phase92_heartbeat("%s", status="success",
                              metadata={"trigger": "atexit"})
        except Exception:
            pass
    _phase92_atexit.register(_phase92_emit)
except Exception:
    pass  # heartbeat module unavailable; extractor continues normally
''' % PLANTED_SOURCE

# Runs in the child interpreter: `python -c _CHILD <module>`.
_CHILD = r'''
import atexit, importlib, os, socket, sys

HOST, PORT = "127.0.0.1", int(os.environ["HB_HARNESS_PORT"])

def _refuse(what):
    raise OSError("import harness is hermetic: refused %s" % (what,))

_getaddrinfo = socket.getaddrinfo
def _guarded_getaddrinfo(host, *args, **kwargs):
    if host not in (HOST, "localhost"):
        _refuse("a DNS lookup of %r" % (host,))
    return _getaddrinfo(host, *args, **kwargs)
socket.getaddrinfo = _guarded_getaddrinfo

def _to_recorder(address):
    return isinstance(address, tuple) and tuple(address[:2]) == (HOST, PORT)

_connect, _connect_ex = socket.socket.connect, socket.socket.connect_ex
def _guarded_connect(self, address):
    if not _to_recorder(address):
        _refuse("a connection to %r" % (address,))
    return _connect(self, address)
def _guarded_connect_ex(self, address):
    if not _to_recorder(address):
        _refuse("a connection to %r" % (address,))
    return _connect_ex(self, address)
socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex

sys.path[:0] = os.environ["HB_HARNESS_PATH"].split(os.pathsep)
import dchub_heartbeat
atexit.register(dchub_heartbeat.heartbeat, os.environ["HB_HARNESS_PROBE"],
                status="success")
importlib.import_module(sys.argv[1])
print("IMPORTED", sys.argv[1], flush=True)
'''


class _Registry(http.server.BaseHTTPRequestHandler):
    """Stands in for POST /api/v1/sources/<id>/heartbeat; records each beat."""

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.beats.append((self.path, json.loads(body or b"{}")))
        reply = b'{"status": "recorded"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


@pytest.fixture
def registry():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Registry)
    server.beats = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _beat_path(source):
    return f"/api/v1/sources/{source}/heartbeat"


def _import_then_exit(registry, module, workdir, extra_path=()):
    """Import `module` in a fresh interpreter that then exits normally.

    Returns every beat the recorder received, after proving the run could have
    seen one (clean exit, completed import, probe last and exactly once).
    """
    port = registry.server_address[1]
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT")
           if k in os.environ}
    env.update({
        "HB_HARNESS_PORT": str(port),
        "HB_HARNESS_PROBE": PROBE_SOURCE,
        "HB_HARNESS_PATH": os.pathsep.join([str(ROOT), *map(str, extra_path)]),
        # A credential and a registry, so a beat CAN land. Without them
        # heartbeat() skips, and "nothing arrived" would prove nothing.
        "DCHUB_ADMIN_KEY": "import-harness-placeholder",
        "DCHUB_HEARTBEAT_BASE": "http://127.0.0.1:%d/api/v1/sources" % port,
        "DCHUB_HEARTBEAT_ALLOW_IN_TESTS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, module],
        cwd=str(workdir), env=env, capture_output=True, text=True, timeout=120,
    )
    beats = list(registry.beats)
    paths = [path for path, _ in beats]
    detail = "exit=%s\nstdout:\n%s\nstderr:\n%s\nbeats: %r" % (
        proc.returncode, proc.stdout[-2000:], proc.stderr[-4000:], beats)
    assert proc.returncode == 0, "the child did not exit cleanly\n" + detail
    assert "IMPORTED %s" % module in proc.stdout, (
        "the import did not complete\n" + detail)
    assert paths.count(_beat_path(PROBE_SOURCE)) == 1 and \
        paths[-1] == _beat_path(PROBE_SOURCE), (
        "the exit probe did not arrive exactly once and last, so this run could "
        "not have seen an exit-time beat either\n" + detail)
    return beats


@pytest.mark.parametrize("module", sorted(FORMERLY_BEAT_AT_EXIT))
def test_importing_an_extractor_and_exiting_reports_no_run(module, registry, tmp_path):
    beats = _import_then_exit(registry, module, tmp_path)
    reported = [beat for beat in beats if beat[0] != _beat_path(PROBE_SOURCE)]
    assert not reported, (
        "importing %s and exiting reported a run nobody made (%s): %r"
        % (module, FORMERLY_BEAT_AT_EXIT[module], reported))


def test_the_harness_sees_an_exit_time_beat_when_a_module_registers_one(registry, tmp_path):
    (tmp_path / "planted_phase92_module.py").write_text(PHASE92_BLOCK)
    beats = _import_then_exit(registry, "planted_phase92_module", tmp_path,
                              extra_path=[tmp_path])
    assert beats[0] == (_beat_path(PLANTED_SOURCE),
                        {"status": "success", "metadata": {"trigger": "atexit"}}), (
        "the planted phase-92 block must reach the recorder, ahead of the probe, "
        "exactly as the registry recorded the real ones: %r" % (beats,))


def exit_or_import_time_beats(source, filename="<string>"):
    """[(line, what)] for each way a module could report a run it did not make.

    Only a module that imports dchub_heartbeat can beat, so only such a module is
    judged:
      * any exit hook it registers (atexit.register, called or as a decorator);
      * any heartbeat() / tracked_run() it calls outside a function body, which
        fires the moment the module is imported.
    Decorating an entry point with @with_heartbeat(...) runs at import but sends
    nothing until that entry point is called, so it is allowed.
    """
    tree = ast.parse(source, filename=filename)
    imports_client = False
    beat_names, client_modules, atexit_modules, register_names = set(), set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "dchub_heartbeat":
            imports_client = True
            beat_names.update(a.asname or a.name for a in node.names
                              if a.name in ("heartbeat", "tracked_run"))
        elif isinstance(node, ast.ImportFrom) and node.module == "atexit":
            register_names.update(a.asname or a.name for a in node.names
                                  if a.name == "register")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "dchub_heartbeat":
                    imports_client = True
                    client_modules.add(alias.asname or alias.name)
                elif alias.name == "atexit":
                    atexit_modules.add(alias.asname or alias.name)
    if not imports_client:
        return []

    def is_register(func):
        return ((isinstance(func, ast.Attribute) and func.attr == "register"
                 and isinstance(func.value, ast.Name) and func.value.id in atexit_modules)
                or (isinstance(func, ast.Name) and func.id in register_names))

    def is_beat(func):
        return ((isinstance(func, ast.Name) and func.id in beat_names)
                or (isinstance(func, ast.Attribute)
                    and func.attr in ("heartbeat", "tracked_run")
                    and isinstance(func.value, ast.Name)
                    and func.value.id in client_modules))

    findings = []

    def visit(node, in_function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # Decorators and argument defaults run when the def runs.
            for deco in getattr(node, "decorator_list", ()):
                if is_register(deco):
                    findings.append((deco.lineno, "registers an exit hook"))
                visit(deco, in_function)
            for default in node.args.defaults + [d for d in node.args.kw_defaults if d]:
                visit(default, in_function)
            body = [node.body] if isinstance(node, ast.Lambda) else node.body
            for child in body:
                visit(child, True)
            return
        if isinstance(node, ast.Call):
            if is_register(node.func):
                findings.append((node.lineno, "registers an exit hook"))
            elif not in_function and is_beat(node.func):
                findings.append((node.lineno, "sends a heartbeat at import"))
        for child in ast.iter_child_nodes(node):
            visit(child, in_function)

    visit(tree, False)
    return sorted(findings)


@pytest.mark.parametrize("source, expected", [
    (PHASE92_BLOCK, ["registers an exit hook"]),
    ("import atexit\nfrom dchub_heartbeat import heartbeat\n"
     "@atexit.register\ndef _emit():\n    heartbeat('s')\n",
     ["registers an exit hook"]),
    ("from atexit import register as on_exit\nimport dchub_heartbeat as hb\n"
     "on_exit(lambda: hb.heartbeat('s'))\n",
     ["registers an exit hook"]),
    ("from dchub_heartbeat import heartbeat\nheartbeat('s', status='success')\n",
     ["sends a heartbeat at import"]),
    ("from dchub_heartbeat import tracked_run\nwith tracked_run('s'):\n    pass\n",
     ["sends a heartbeat at import"]),
    ("import dchub_heartbeat\nclass Loader:\n    dchub_heartbeat.heartbeat('s')\n",
     ["sends a heartbeat at import"]),
    # The shape this change moved every entry point to: a beat only when called.
    ("from dchub_heartbeat import heartbeat, with_heartbeat\n"
     "@with_heartbeat('s', rows_key='n')\ndef run():\n    heartbeat('t')\n    return {}\n",
     []),
    # Out of scope by design: a module that cannot beat.
    ("import atexit\natexit.register(print)\n", []),
], ids=["phase92-block", "atexit-decorator", "from-atexit-import-register",
        "module-scope-heartbeat", "module-scope-tracked-run", "class-body-heartbeat",
        "with-heartbeat-entry-point-allowed", "atexit-without-client-out-of-scope"])
def test_the_scan_flags_each_way_a_module_can_beat_without_running(source, expected):
    assert [what for _, what in exit_or_import_time_beats(source)] == expected


_SKIP_PARTS = {".git", ".claude", "__pycache__", "node_modules", ".venv", "venv",
               "dchub-frontend"}


def test_no_module_can_report_a_run_at_import_or_exit():
    scanned, clients, offenders = 0, set(), {}
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if _SKIP_PARTS & set(rel.parts):
            continue
        scanned += 1
        text = path.read_text(errors="replace")
        if "dchub_heartbeat" not in text:
            continue
        tree = ast.parse(text, filename=str(rel))
        if any(isinstance(n, ast.ImportFrom) and n.module == "dchub_heartbeat"
               or isinstance(n, ast.Import) and any(a.name == "dchub_heartbeat"
                                                    for a in n.names)
               for n in ast.walk(tree)):
            clients.add(rel.as_posix())
        findings = exit_or_import_time_beats(text, str(rel))
        if findings:
            offenders[rel.as_posix()] = findings

    # A scan that found nothing to judge would pass the same way a clean repo does.
    assert scanned >= 2000, "only %d .py files scanned; the walk lost the repo" % scanned
    anchors = {"subsea_cable_ingestion.py", "network_ix_ingestion.py",
               "news_engine.py", "news_facility_extractor.py",
               "eia_gas_bulk_loader.py", "fiber_integration.py",
               "autonomous_brain.py", "routes/iso_caiso.py"}
    assert anchors <= clients and len(clients) >= 20, (
        "the scan no longer sees the modules that import dchub_heartbeat: "
        "missing %s, %d found" % (sorted(anchors - clients), len(clients)))
    assert not offenders, (
        "these modules would report a run that did not happen (a beat at import, "
        "or from an exit hook that fires whenever ANY process that imported them "
        "exits): %r" % (offenders,))
