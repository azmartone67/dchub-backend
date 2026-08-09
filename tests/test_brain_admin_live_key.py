"""brain_mechanical_classifier._admin_ok — survives a key rotation (2026-08-08).

The 403 that darked the self-healing brain: _INTERNAL_KEYS is frozen at
module import, so a post-boot DCHUB_ADMIN_KEY rotation left the frozen set
holding the OLD admin key. brain-autonomy + brain-inspector (and ~8 other
modules sharing this gate) then 403'd the CURRENT admin key that layer5's
live-reading gate accepted. These tests pin the live admin-key check so the
divergence cannot recur; the mutation test proves the check can actually fail.

CI-SAFETY: pure Flask request context; no DB, no network.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def mech(monkeypatch):
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import brain_mechanical_classifier as m
    return m


def _ctx(mech, headers):
    from flask import Flask
    return Flask(__name__).test_request_context(headers=headers)


def test_rotated_admin_key_still_authorizes(mech, monkeypatch):
    # Frozen set holds only the PRE-rotation values (NOT the new admin key).
    monkeypatch.setattr(mech, "_INTERNAL_KEYS", {"OLD-admin", "internal-key-1"})
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "NEW-rotated-admin-key")
    with _ctx(mech, {"X-Admin-Key": "NEW-rotated-admin-key"}):
        assert mech._admin_ok() is True   # the live check saves it


def test_internal_key_in_frozen_set_still_works(mech, monkeypatch):
    monkeypatch.setattr(mech, "_INTERNAL_KEYS", {"internal-key-1"})
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "whatever")
    with _ctx(mech, {"X-Internal-Key": "internal-key-1"}):
        assert mech._admin_ok() is True   # fast path unchanged


def test_wrong_key_is_still_rejected(mech, monkeypatch):
    monkeypatch.setattr(mech, "_INTERNAL_KEYS", {"internal-key-1"})
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "NEW-rotated-admin-key")
    with _ctx(mech, {"X-Admin-Key": "attacker-guess"}):
        assert mech._admin_ok() is False


def test_empty_and_missing_are_rejected(mech, monkeypatch):
    monkeypatch.setattr(mech, "_INTERNAL_KEYS", {"internal-key-1"})
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "NEW-rotated-admin-key")
    with _ctx(mech, {}):
        assert mech._admin_ok() is False
    with _ctx(mech, {"X-Admin-Key": "   "}):
        assert mech._admin_ok() is False


def test_no_admin_env_does_not_authorize_empty(mech, monkeypatch):
    # If DCHUB_ADMIN_KEY is unset, the live check must not turn a blank key or
    # a stray value into a pass.
    monkeypatch.setattr(mech, "_INTERNAL_KEYS", {"internal-key-1"})
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    with _ctx(mech, {"X-Admin-Key": ""}):
        assert mech._admin_ok() is False
    with _ctx(mech, {"X-Admin-Key": "anything"}):
        assert mech._admin_ok() is False


def test_gate_matches_layer5_semantics(mech, monkeypatch):
    """The whole bug was divergence from layer5. Pin that the same X-Admin-Key
    == DCHUB_ADMIN_KEY that layer5 accepts also passes here."""
    monkeypatch.setattr(mech, "_INTERNAL_KEYS", set())   # nothing frozen
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "the-one-true-admin-key")
    with _ctx(mech, {"X-Admin-Key": "the-one-true-admin-key"}):
        assert mech._admin_ok() is True
