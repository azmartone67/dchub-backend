#!/usr/bin/env python3
"""tests/test_loader_status_accepts_internal_key.py — the only window onto the
substation and pipeline loaders must open for the callers that actually knock.

NO NETWORK, NO DB, NO main.py IMPORT. The real handler body is lifted out of
main.py with `ast` and executed against stubs, per this repo's testing rule.

WHAT WENT WRONG (2026-09-02). `/api/admin/loader-status` read only the
`X-Admin-Key` header:

    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    provided = (request.headers.get("X-Admin-Key") or request.args.get("admin_key"))

while BOTH workflows that fire the phase12g loaders send `X-Internal-Key`
(data-sync.yml:210, dchub-osm-refresh.yml:54). Every status read 401'd, and both
steps `exit 0` over it. Confirmed live in run 33599180707: loaders fired, then
`Final loader status: {"error":"unauthorized"}`.

Those loaders are the only doors still writing to `substations` — the canonical
HIFLD lane is deliberately blocked — so nobody has ever seen their result.

★ The `or` chain on `expected` is the subtle half: it collapses two configured
credentials into ONE accepted value. With DCHUB_ADMIN_KEY set, the internal key
was rejected even when sent in the header the endpoint did read. Fixing only the
header would leave that half in place, so both are fenced here.

Run standalone:   python3 tests/test_loader_status_accepts_internal_key.py
Run under pytest: pytest tests/test_loader_status_accepts_internal_key.py
"""
import ast
import os
import pathlib
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"

ADMIN_KEY = "admin-secret-aaa"
INTERNAL_KEY = "internal-secret-bbb"


class _Req:
    def __init__(self, headers=None, args=None):
        self.headers = headers or {}
        self.args = args or {}


def _load_handler():
    """Execute the real `phase12g_loader_status` body against stubs.

    ★ The handler does `import os` in its own body, so a stub `os` in the exec
    namespace is immediately shadowed by the real module. The environment is
    therefore controlled by patching os.environ itself, not by injecting a fake.
    """
    tree = ast.parse(MAIN.read_text())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "phase12g_loader_status"), None)
    assert fn is not None, "phase12g_loader_status not found in main.py"
    fn.decorator_list = []

    def jsonify(*a, **kw):
        return dict(kw) if kw else (a[0] if a else {})

    ns = {"jsonify": jsonify, "phase12g_loader_state": {"substations": "ok"}}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<main>", "exec"), ns)
    return ns["phase12g_loader_status"], ns


def _call(environ, headers=None, args=None):
    handler, ns = _load_handler()
    ns["request"] = _Req(headers, args)
    with mock.patch.dict(os.environ, environ, clear=True):
        result = handler()
    status = result[1] if isinstance(result, tuple) else 200
    return status, (result[0] if isinstance(result, tuple) else result)


BOTH = {"DCHUB_ADMIN_KEY": ADMIN_KEY, "DCHUB_INTERNAL_KEY": INTERNAL_KEY}


def test_internal_key_header_is_accepted():
    """THE REGRESSION: the header both real callers actually send."""
    status, body = _call(BOTH, headers={"X-Internal-Key": INTERNAL_KEY})
    assert status == 200, "X-Internal-Key rejected (%s) — this is the live bug" % (body,)
    assert body.get("success") is True


def test_admin_key_header_still_accepted():
    status, _ = _call(BOTH, headers={"X-Admin-Key": ADMIN_KEY})
    assert status == 200


def test_admin_header_carrying_the_internal_key_is_accepted():
    """The `or` collapse: two configured credentials must both be valid."""
    status, body = _call(BOTH, headers={"X-Admin-Key": INTERNAL_KEY})
    assert status == 200, "internal key rejected because ADMIN_KEY shadowed it (%s)" % (body,)


def test_query_param_still_accepted():
    status, _ = _call(BOTH, args={"admin_key": ADMIN_KEY})
    assert status == 200


def test_wrong_key_is_still_rejected():
    """Widening the accepted headers must not weaken the gate."""
    for headers in ({"X-Internal-Key": "nope"}, {"X-Admin-Key": "nope"}, {}):
        status, _ = _call(BOTH, headers=headers)
        assert status == 401, "gate opened for %r" % (headers,)


def test_only_internal_configured_still_gates():
    env = {"DCHUB_INTERNAL_KEY": INTERNAL_KEY}
    assert _call(env, headers={"X-Internal-Key": INTERNAL_KEY})[0] == 200
    assert _call(env, headers={"X-Internal-Key": "nope"})[0] == 401


def test_no_key_configured_leaves_endpoint_open():
    """Pre-existing behaviour, pinned so a change to it is deliberate."""
    assert _call({}, headers={})[0] == 200


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
