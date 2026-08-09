"""The brain autonomy gate and the layer5 gate must accept the IDENTICAL admin
credential — including a DCHUB_ADMIN_KEY re-pasted with trailing pollution.

WHY THIS EXISTS
---------------
On 2026-08-08T23:42Z the brain-autonomy + brain-inspector crons went RED: every
POST /api/v1/brain/autonomy/tick returned 403 'admin only'. The IDENTICAL repo
secret (secrets.DCHUB_ADMIN_KEY, sent as X-Admin-Key) kept working for
brain-layer5.yml, cron-heartbeat and dchub-jobs — so the secret was NOT globally
stale. The divergence was purely in the two gates' normalisation:

  * layer5 (routes/brain_v2_layer5._admin_guard): raw ``provided == ADMIN_KEY``.
    Tolerates a key whose stored value carries trailing whitespace / an appended
    shell path, because BOTH the sent value and the env value carry the same
    pollution (raw == raw).

  * autonomy (routes/brain_mechanical_classifier._admin_ok, imported by
    brain_autonomy_loop / brain_automerge / brain_smoke_regression + ~10 other
    modules): ``sent = header.strip()`` then ``sent in _INTERNAL_KEYS`` where the
    set holds the RAW, un-cleaned DCHUB_ADMIN_KEY captured at import. Stripping
    the header made ``sent`` no longer equal the polluted set member → 403.

The 2026-08-08 rotation re-pasted DCHUB_ADMIN_KEY with exactly the kind of
trailing token internal_auth._clean_key already documents
("DCHUB_INTERNAL_KEY=5GyW...\\n~/dchub-frontend"). The fix routes both brain
gates through internal_auth.require_internal_or_admin, which _clean_key()s BOTH
sides and re-reads env at request time.

WHAT THIS TEST PROVES
---------------------
Both gates are sliced out of the SHIPPED source with ``ast`` (no main.py, no
Flask app, no DB) and EXECUTED against the REAL internal_auth module and real
(monkeypatched) env vars — so a comment or a mis-scoped ``if`` cannot satisfy it.

  1. parity: for the credential the cron actually sends, the mechanical gate and
     the layer5 gate AGREE (both accept), for a clean key AND for a key whose
     stored value carries trailing pollution.
  2. regression control: a faithful re-implementation of the OLD mechanical gate
     is shown to REJECT the polluted credential — proving the scenario really
     reproduces the 403 and that this test can tell fixed from broken.
  3. the fixed mechanical gate is robust in BOTH directions (clean header vs
     polluted env, the shape brain_consistency_radar's send-side _clean() hits).
  4. a wrong key, and a missing header, are rejected by both gates.

Run:  python3 -m pytest tests/test_brain_admin_gate_parity.py -v
"""

import ast
import os
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_MECH = _ROOT / "routes" / "brain_mechanical_classifier.py"
_L5 = _ROOT / "routes" / "brain_v2_layer5.py"

# A clean admin key, and the same key re-pasted with a trailing newline — the
# shape that fits the observed 2026-08-08 facts. `.strip()` (what the old
# mechanical gate did to the header) removes trailing whitespace, so the
# stripped header no longer equalled the RAW polluted value held in the
# import-time set → 403; layer5's raw==raw compare kept accepting it. (An
# interior-appended path like "key\n~/path" is NOT the trigger — `.strip()`
# leaves interior newlines alone, so the old gate tolerated that shape; only
# internal_auth._clean_key's first-token rule, which the fix now uses, covers
# both.) The meaningful token is identical; only the stored form differs.
_CLEAN = "rot-2026-08-08-abcDEF123456"
_POLLUTED = _CLEAN + "\n"
_WRONG = "definitely-not-the-admin-key-000"


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


def _jsonify(*a, **k):  # layer5 gate builds an error body with this on reject
    return {"_body": (a, k)}


# ── ast extraction (asserts a real, non-empty body) ──────────────────────────
def _extract(path: pathlib.Path, name: str) -> str:
    src = path.read_text()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            seg = ast.get_source_segment(src, node)
            assert seg and seg.strip(), f"{name} extracted empty from {path}"
            # a real gate is more than a bare `return True`
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


def _internal_keys_like_module() -> set:
    """Rebuild _INTERNAL_KEYS exactly as brain_mechanical_classifier does at
    import (accepted_internal_keys() UNION raw env vars) against CURRENT env."""
    from internal_auth import accepted_internal_keys
    ks = accepted_internal_keys()
    for n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        v = os.environ.get(n)
        if v:
            ks.add(v)
    return ks


# The OLD mechanical gate, verbatim — the regression control. If the scenario
# below does NOT make this return False, the test isn't reproducing the bug.
def _old_mech_admin_ok(req, internal_keys) -> bool:
    sent = (req.headers.get("X-Internal-Key")
            or req.headers.get("X-Admin-Key")
            or req.args.get("admin_key") or "").strip()
    return sent in internal_keys


def _mech_accepts(req) -> bool:
    fn = _load(_MECH, "_admin_ok",
               {"request": req, "_INTERNAL_KEYS": _internal_keys_like_module()})
    return fn() is True


def _l5_accepts(req, admin_key) -> bool:
    fn = _load(_L5, "_admin_guard",
               {"request": req, "ADMIN_KEY": admin_key, "jsonify": _jsonify})
    return fn() is None  # _admin_guard returns None iff the request is allowed


@pytest.fixture
def clean_env(monkeypatch):
    """Isolate to a controlled admin-only environment (drop any real secrets so
    only the fabricated DCHUB_ADMIN_KEY of each test decides accept/reject)."""
    for n in ("DCHUB_INTERNAL_KEY", "DCHUB_SYNC_KEY", "INTERNAL_WORKER_SECRET",
              "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        monkeypatch.delenv(n, raising=False)
    return monkeypatch


def test_parity_clean_key(clean_env):
    """Baseline: with a clean stored key, both gates accept the sent key."""
    clean_env.setenv("DCHUB_ADMIN_KEY", _CLEAN)
    req = _Req(headers={"X-Admin-Key": _CLEAN})
    assert _l5_accepts(req, os.environ["DCHUB_ADMIN_KEY"]) is True
    assert _mech_accepts(req) is True


def test_parity_polluted_key_is_the_regression(clean_env):
    """The 2026-08-08 case: DCHUB_ADMIN_KEY stored with trailing pollution, the
    cron sends the same secret bytes. layer5 accepts (raw==raw); the FIXED
    mechanical gate must ALSO accept — and the OLD gate must have REJECTED it."""
    clean_env.setenv("DCHUB_ADMIN_KEY", _POLLUTED)
    req = _Req(headers={"X-Admin-Key": _POLLUTED})

    # layer5 tolerated it all along
    assert _l5_accepts(req, os.environ["DCHUB_ADMIN_KEY"]) is True

    # control: the OLD mechanical gate is what 403'd — proves this reproduces it
    assert _old_mech_admin_ok(req, _internal_keys_like_module()) is False

    # the fix: mechanical gate now agrees with layer5
    assert _mech_accepts(req) is True


def test_fixed_gate_robust_when_send_side_cleans(clean_env):
    """Belt-and-suspenders: even if a caller _clean()s on send (clean header)
    while the env stays polluted, the fixed mechanical gate still accepts —
    internal_auth cleans both sides. (layer5's raw compare would 403 here, which
    is why we route through the shared gate rather than copying layer5.)"""
    clean_env.setenv("DCHUB_ADMIN_KEY", _POLLUTED)
    req = _Req(headers={"X-Admin-Key": _CLEAN})
    assert _mech_accepts(req) is True


def test_wrong_key_rejected_by_both(clean_env):
    clean_env.setenv("DCHUB_ADMIN_KEY", _CLEAN)
    req = _Req(headers={"X-Admin-Key": _WRONG})
    assert _l5_accepts(req, os.environ["DCHUB_ADMIN_KEY"]) is False
    assert _mech_accepts(req) is False


def test_missing_header_rejected_by_both(clean_env):
    clean_env.setenv("DCHUB_ADMIN_KEY", _CLEAN)
    req = _Req(headers={})
    assert _l5_accepts(req, os.environ["DCHUB_ADMIN_KEY"]) is False
    assert _mech_accepts(req) is False
