"""The Brain Layer-4 and Layer-5 admin gates must FAIL CLOSED when no admin
secret is configured — the empty-env branch must REJECT, never ALLOW.

WHY THIS EXISTS
---------------
Both gates captured the admin key ONCE at import:

    ADMIN_KEY = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")

and gated with `if ADMIN_KEY and provided != ADMIN_KEY: reject`. When BOTH env
vars were unset/empty at import (a bad deploy, or a mid-rotation gap), ADMIN_KEY
was falsy, the `if` never fired, and the gate ALLOWED any request:

  * layer5 `_admin_guard` returned None (= allowed) for any key;
  * layer4 `_require_admin` ran the wrapped handler for any key.

So /api/v1/brain/learn, /learn-code, /proposed-code/neutralize and every other
Layer-4/5 admin endpoint became unauthenticated under a missing-env condition.
Observed live during the 2026-08-08 DCHUB_ADMIN_KEY desync: learn-code returned
200 to a non-matching key while the fail-CLOSED gates (brain_mechanical_classifier
/ brain_inspector) correctly 403'd.

THE FIX (#2468 pattern, extended to layer4/5)
---------------------------------------------
Both gates now delegate to internal_auth.require_internal_or_admin(request),
which re-reads env at REQUEST time, _clean_key()s both sides, and returns False
when no secret is configured — fail-closed. The raw `provided == ADMIN_KEY`
compare is kept only as an ADDITIONAL accept path, guarded by `ADMIN_KEY`
truthiness so an empty key can never satisfy it.

WHAT THIS TEST PROVES
---------------------
Both gates are sliced out of the SHIPPED source with `ast` (no main.py, no Flask
app, no DB) and EXECUTED against the REAL internal_auth module and real
(monkeypatched) env — a comment or a mis-scoped `if` cannot satisfy it.

  1. fail-closed: with DCHUB_ADMIN_KEY *and* DCHUB_INTERNAL_KEY BOTH unset, an
     arbitrary key is REJECTED by both gates.
  2. regression control: a faithful re-impl of the OLD gate ALLOWS that same
     arbitrary key — proving the scenario reproduces the bypass and this test
     can tell fixed from broken.
  3. with a key set: a wrong key is rejected and the right key is accepted, by
     both gates.

Run:  python3 -m pytest tests/test_brain_layer45_fail_closed.py -v
"""

import ast
import functools
import os
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_L4 = _ROOT / "routes" / "brain_v2_layer4.py"
_L5 = _ROOT / "routes" / "brain_v2_layer5.py"

_KEY = "layer45-admin-key-abcDEF123456"
_WRONG = "definitely-not-the-admin-key-000"
_ARBITRARY = "any-old-key-an-attacker-might-send"


# ── tiny Flask-request stand-in ──────────────────────────────────────────────
class _Bag:
    def __init__(self, d):
        self._d = d

    def get(self, k, default=None):
        return self._d.get(k, default)


class _Req:
    def __init__(self, headers=None, args=None):
        self.headers = _Bag(headers or {})
        self.args = _Bag(args or {})


def _jsonify(*a, **k):  # the gates build an error body with this on reject
    return {"_body": (a, k)}


# ── ast extraction (asserts a real, non-empty body) ──────────────────────────
def _extract(path: pathlib.Path, name: str) -> str:
    src = path.read_text()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            seg = ast.get_source_segment(src, node)
            assert seg and seg.strip(), f"{name} extracted empty from {path}"
            assert len(node.body) >= 2, f"{name} body suspiciously short"
            return seg
    raise AssertionError(f"{name} not found in {path}")


def _load(path: pathlib.Path, name: str, glb: dict):
    seg = _extract(path, name)
    ns = dict(glb)
    exec(compile(seg, str(path), "exec"), ns)  # noqa: S102 — extracted shipped src
    fn = ns[name]
    assert fn.__code__.co_code, f"{name} compiled to empty code"
    return fn


def _module_admin_key():
    """Mirror brain_v2_layer4.py:92 exactly — the import-time captured key for
    the CURRENT (monkeypatched) env. None when both env vars are unset."""
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


# ── gate runners (execute the SHIPPED bodies) ────────────────────────────────
def _l5_allows(req) -> bool:
    fn = _load(_L5, "_admin_guard",
               {"request": req, "ADMIN_KEY": _module_admin_key(), "jsonify": _jsonify})
    return fn() is None  # _admin_guard returns None iff allowed


def _l4_allows(req) -> bool:
    fn = _load(_L4, "_require_admin",
               {"request": req, "ADMIN_KEY": _module_admin_key(), "jsonify": _jsonify,
                "wraps": functools.wraps})
    sentinel = object()
    wrapped = fn(lambda *a, **k: sentinel)  # decorator → wrap a sentinel handler
    return wrapped() is sentinel            # sentinel iff the handler actually ran


# ── the OLD gates, verbatim — the regression control ─────────────────────────
def _old_l5_allows(req, admin_key) -> bool:
    provided = req.headers.get("X-Admin-Key") or req.args.get("admin_key")
    if admin_key and provided != admin_key:
        return False
    return True  # <- the fail-OPEN branch: allows when admin_key is falsy


def _old_l4_allows(req, admin_key) -> bool:
    provided = req.headers.get("X-Admin-Key") or req.args.get("admin_key")
    if admin_key and provided != admin_key:
        return False
    return True  # <- same fail-OPEN branch


@pytest.fixture
def no_secret(monkeypatch):
    """Both admin secrets absent — the missing-env condition that opened the gate.
    Drop every env var require_internal_or_admin / _clean_key would honour."""
    for n in ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "DCHUB_SYNC_KEY",
              "INTERNAL_WORKER_SECRET", "INTERNAL_KEY"):
        monkeypatch.delenv(n, raising=False)
    return monkeypatch


@pytest.fixture
def with_secret(monkeypatch):
    """Only DCHUB_ADMIN_KEY set to a clean value; no internal keys."""
    for n in ("DCHUB_INTERNAL_KEY", "DCHUB_SYNC_KEY",
              "INTERNAL_WORKER_SECRET", "INTERNAL_KEY"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", _KEY)
    return monkeypatch


# ── 1. fail-closed when NO secret is configured ──────────────────────────────
def test_layer5_fails_closed_when_no_secret(no_secret):
    """The bug: layer5 must REJECT an arbitrary key when both env vars are unset."""
    req = _Req(headers={"X-Admin-Key": _ARBITRARY})
    assert _module_admin_key() is None          # import would capture a falsy key
    assert _l5_allows(req) is False             # FIXED gate rejects
    assert _old_l5_allows(req, _module_admin_key()) is True  # control: OLD allowed


def test_layer4_fails_closed_when_no_secret(no_secret):
    """The bug: layer4's decorator must NOT run the handler when both env unset."""
    req = _Req(headers={"X-Admin-Key": _ARBITRARY})
    assert _module_admin_key() is None
    assert _l4_allows(req) is False             # FIXED gate rejects
    assert _old_l4_allows(req, _module_admin_key()) is True  # control: OLD allowed


def test_both_gates_reject_missing_header_when_no_secret(no_secret):
    """No header at all, no secret — still rejected (not a crash, not allowed)."""
    req = _Req(headers={})
    assert _l5_allows(req) is False
    assert _l4_allows(req) is False


# ── 2. with a secret set: wrong rejected, right accepted ─────────────────────
def test_layer5_wrong_key_rejected_right_key_accepted(with_secret):
    assert _l5_allows(_Req(headers={"X-Admin-Key": _WRONG})) is False
    assert _l5_allows(_Req(headers={"X-Admin-Key": _KEY})) is True


def test_layer4_wrong_key_rejected_right_key_accepted(with_secret):
    assert _l4_allows(_Req(headers={"X-Admin-Key": _WRONG})) is False
    assert _l4_allows(_Req(headers={"X-Admin-Key": _KEY})) is True


def test_key_accepted_via_query_arg_and_internal_header(with_secret):
    """Parity with the shared gate's accepted slots: ?admin_key= and X-Internal-Key
    both authenticate the same configured admin secret (require_internal_or_admin
    honours DCHUB_ADMIN_KEY as an internal credential too)."""
    assert _l5_allows(_Req(args={"admin_key": _KEY})) is True
    assert _l4_allows(_Req(headers={"X-Internal-Key": _KEY})) is True
