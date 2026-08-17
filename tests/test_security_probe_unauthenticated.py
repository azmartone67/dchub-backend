"""Regression guards: the admin-gate audit must stay UNAUTHENTICATED.

routes/brain_security_detectors.py::check_admin_endpoint_open() POSTs every
admin endpoint in _ADMIN_ENDPOINTS_REQUIRING_AUTH with NO credentials and
treats 401/403 as the PASS condition — a 200 there is the security breach it
exists to catch. The probe is the auditor, not a caller: it must see exactly
what an anonymous stranger sees.

Why this needs a guard rather than a comment (2026-08-17). One of those
probes, POST /api/v1/admin/news-ner/run, is ALSO in main.py's
_WORKER_PROXY_POST_PATHS — the only path in both sets. So web relays it over
private networking and logs

    workerproxy: POST /api/v1/admin/news-ner/run → worker 403

on every surveillance sweep (~4x/hour). Read cold that line is
indistinguishable from a broken cron whose auth header rotted, and the
house-standard repair for a 403ing self-call is "add X-Internal-Key" (the
2026-07-31 radar re-stamp class). Applying that repair HERE inverts the
detector: _admin_ok() would start returning True, so

  1. the auditor would see 200 on every admin endpoint and either flag ~22
     false admin_endpoint_open breaches or, worse, silently validate nothing
     while reporting detectors_run: 5; and
  2. news-ner/run would EXECUTE — a ~52s NER scan writing
     news_discovered_entities (and, with ?promote, discovered_facilities) on
     every sweep instead of once daily from news-ner-discovery.yml.

The 403 is the product. These tests pin that.

House rules: static AST extraction, no import of routes.brain_security_detectors
(it self-probes over HTTP), nothing executes at module scope.
"""
import ast
import os

_DET = os.path.join(os.path.dirname(__file__), "..", "routes",
                    "brain_security_detectors.py")

# Header/param names that would authenticate the probe and blind the audit.
_CREDENTIAL_MARKERS = ("x-internal-key", "x-admin-key", "admin_key",
                       "authorization", "x-dc-internal-cron")
# Env vars holding those credentials.
_CREDENTIAL_ENV = ("DCHUB_INTERNAL_KEY", "DCHUB_ADMIN_KEY", "DCHUB_SYNC_KEY",
                   "INTERNAL_KEY", "BRAIN_ADMIN_KEY")


def _tree(path: str, min_body: int = 10) -> ast.Module:
    with open(os.path.abspath(path), "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    # Guard the guard: a degenerate parse would vacuously pass every search
    # below (2026-07-28 lesson — assert it parsed, never just filter).
    assert isinstance(tree, ast.Module) and len(tree.body) > min_body, (
        f"{path} parsed to a degenerate module — the extraction harness is "
        "not looking at the real file")
    return tree


def _fn(tree: ast.Module, name: str, where: str = _DET) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f"{where} no longer defines {name}() — this audit guard needs "
        "updating, not deleting")


def _body_strings(fn: ast.FunctionDef) -> list:
    """String constants in the function BODY, docstring excluded. A guard a
    docstring can satisfy guards nothing (the 07-19 'comments satisfy grep'
    lesson, applied to AST)."""
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and \
            isinstance(body[0].value, ast.Constant) and \
            isinstance(body[0].value.value, str):
        body = body[1:]
    out = []
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.append(node.value)
    return out


def _env_reads(fn: ast.FunctionDef) -> list:
    """Names passed to os.environ.get(...) / os.getenv(...) in the body."""
    names = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        f = node.func
        target = None
        if isinstance(f, ast.Attribute):
            target = f.attr
        if target in ("get", "getenv") and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            src = ast.dump(f)
            if "environ" in src or "getenv" in src:
                names.append(node.args[0].value)
    return names


def _probe_is_anonymous(fn: ast.FunctionDef) -> bool:
    """The predicate under test: _probe() attaches no credential of any kind."""
    for s in _body_strings(fn):
        if s.strip().lower() in _CREDENTIAL_MARKERS:
            return False
    for name in _env_reads(fn):
        if name in _CREDENTIAL_ENV:
            return False
    return True


def _accepted_statuses(fn: ast.FunctionDef) -> set:
    """Integer literals in the `if status in (...)` membership test — the set
    the detector treats as 'gate is holding, not a finding'."""
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare) and node.ops and \
                isinstance(node.ops[0], ast.In):
            for cmp_node in node.comparators:
                if isinstance(cmp_node, (ast.Tuple, ast.List, ast.Set)):
                    for elt in cmp_node.elts:
                        if isinstance(elt, ast.Constant) and \
                                isinstance(elt.value, int):
                            out.add(elt.value)
    return out


def test_probe_attaches_no_credentials():
    """_probe() must stay anonymous — it is the auditor, not a caller."""
    fn = _fn(_tree(_DET), "_probe")
    assert _probe_is_anonymous(fn), (
        "_probe() now attaches a credential. That authenticates the admin-gate "
        "audit into uselessness (it would see 200 everywhere) and makes "
        "POST /api/v1/admin/news-ner/run actually RUN a ~52s NER scan on every "
        "surveillance sweep. If a 403 in the workerproxy log brought you here: "
        "that 403 is this detector PASSING. Do not key it.")


def test_probe_user_agent_is_not_an_internal_marker():
    """UA must not look like internal infra, or tier_gate waves it through and
    the detector audits a gate it already bypassed (round21, 2026-05-23)."""
    fn = _fn(_tree(_DET), "_probe")
    uas = [s for s in _body_strings(fn) if "/" in s and "audit" in s.lower()]
    assert uas, ("_probe() no longer sets a recognisable audit User-Agent — "
                 "the anonymous-probe contract is unverifiable")
    for ua in uas:
        assert "dchub-" not in ua.lower(), (
            f"probe UA {ua!r} contains the internal marker 'dchub-' — "
            "tier_gate's _is_internal check would privilege it and the audit "
            "would report a gate it never actually tested")


def test_admin_gate_audit_treats_401_403_as_passing():
    """401/403 are the expected, correct outcome — never a finding."""
    fn = _fn(_tree(_DET), "check_admin_endpoint_open")
    accepted = _accepted_statuses(fn)
    for code in (401, 403):
        assert code in accepted, (
            f"check_admin_endpoint_open() no longer accepts HTTP {code} as a "
            "passing admin gate. A rejected anonymous probe is the SUCCESS "
            f"case; treating {code} as a finding inverts the detector.")


def test_news_ner_run_is_still_audited():
    """The one probed path that is also worker-proxied — the reason the 403
    surfaces in workerproxy logs, and the one whose accidental authentication
    would execute a real production job."""
    tree = _tree(_DET)
    listed = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and \
                        t.id == "_ADMIN_ENDPOINTS_REQUIRING_AUTH":
                    for elt in getattr(node.value, "elts", []):
                        parts = [e.value for e in getattr(elt, "elts", [])
                                 if isinstance(e, ast.Constant)]
                        if len(parts) == 2:
                            listed.append((parts[0], parts[1]))
    assert listed, ("_ADMIN_ENDPOINTS_REQUIRING_AUTH did not parse to "
                    "(method, path) pairs — the guard is looking at nothing")
    assert ("POST", "/api/v1/admin/news-ner/run") in listed, (
        "POST /api/v1/admin/news-ner/run left the admin-gate audit. If it was "
        "removed to silence the recurring workerproxy 403, that 403 was the "
        "audit passing — restore it.")


def test_guard_rejects_a_credentialed_probe():
    """Must-fail control: prove _probe_is_anonymous() can actually FAIL, so a
    green run above means 'no credential' and not 'the check looked at
    nothing' (silent-green class, 2026-07-28)."""
    header_mutant = ast.parse(
        'def _probe(path):\n'
        '    """Docstring mentioning X-Admin-Key must NOT satisfy this."""\n'
        '    req = _req.Request(path)\n'
        '    req.add_header("X-Internal-Key", "abc")\n'
        '    return req\n')
    env_mutant = ast.parse(
        'def _probe(path):\n'
        '    req = _req.Request(path)\n'
        '    req.add_header("X-Admin-Key", os.environ.get("DCHUB_ADMIN_KEY"))\n'
        '    return req\n')
    clean = ast.parse(
        'def _probe(path):\n'
        '    """X-Admin-Key named only in the docstring."""\n'
        '    req = _req.Request(path)\n'
        '    req.add_header("User-Agent", "dc-security-audit/1.0")\n'
        '    return req\n')
    assert not _probe_is_anonymous(_fn(header_mutant, "_probe", "<mutant>")), \
        "guard passed a probe that sends X-Internal-Key — it cannot fail"
    assert not _probe_is_anonymous(_fn(env_mutant, "_probe", "<mutant>")), \
        "guard passed a probe that reads DCHUB_ADMIN_KEY — it cannot fail"
    assert _probe_is_anonymous(_fn(clean, "_probe", "<mutant>")), \
        "guard rejected a clean probe — it is over-tight and will false-alarm"


def test_status_accept_guard_can_fail():
    """Must-fail control for the 401/403 leg."""
    mutant = ast.parse(
        'def check_admin_endpoint_open():\n'
        '    if status in (0, 404, 405):\n'
        '        pass\n')
    accepted = _accepted_statuses(_fn(mutant, "check_admin_endpoint_open",
                                      "<mutant>"))
    assert 403 not in accepted and 401 not in accepted, (
        "_accepted_statuses() reported 401/403 present in a tuple that omits "
        "them — the extraction is not reading the real membership test")
