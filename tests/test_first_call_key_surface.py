"""Guard the FIRST-CALL durable-key surface injected on the MCP SUCCESS path.

GOAL recap: multi-day MCP retention is ~5% — agents chain calls within ONE
session (64%) but ~never return on a LATER day. Root cause = CREDENTIAL FRICTION:
a new/anonymous caller succeeds via the inline auto-trial (anon-grace) but the
durable key is never SURFACED on the success response, so the agent doesn't
persist it and the human can't return tomorrow without re-provisioning. The
fix additively surfaces the caller's OWN durable auto-trial key + a "save it,
reuse across sessions, bind your email to keep it" note on the FIRST successful
call(s) for a caller with no durable bound key.

This test guards the behaviour the build established:

  1. FLAG-GATED — ships DARK. No surface unless FIRST_CALL_KEY_SURFACE_ENABLED=true.
  2. FIRST-CALL ONLY — fires only when THIS request's anon-grace minted/reused an
     auto-trial key (grace_meta carries auto_trial_key) for a sub-Developer caller;
     suppressed when the caller arrives on their own durable/bound key (no
     grace_meta) and suppressed for established paid (>= Developer) callers.
  3. ADDITIVE / NON-CORRUPTING — surfaced as a separate _meta.first_call_key_offer
     field + an appended message note; it never replaces or mutates tool data.
  4. DURABLE + HONEST — the surfaced key is the caller's auto-trial (≥7d unbound,
     365d bound); expiry + cap are read straight off the mint result, never an
     inflated promise.
  5. NO LEAK — the key comes ONLY from this request's own grace_meta (minted for
     this caller); never another caller's key.

mcp_gatekeeper imports Flask/db at module load and the CI unit-tests job installs
only pytest, so we exec the (pure) helpers in isolation rather than importing the
module, and assert against the source for the wiring.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GK = os.path.join(ROOT, "mcp_gatekeeper.py")


def _src():
    return open(GK, encoding="utf-8").read()


def _load_helpers():
    """Exec the pure first-call helpers (+ the Tier enum they depend on) into an
    isolated namespace. No Flask/db needed — the helpers are side-effect-free."""
    src = _src()
    ns = {}
    from typing import Optional, Dict, Any
    from enum import IntEnum
    ns.update({"Optional": Optional, "Dict": Dict, "Any": Any,
               "IntEnum": IntEnum, "os": os})

    def _span(pattern):
        m = re.search(pattern, src, re.DOTALL | re.MULTILINE)
        assert m, f"could not locate source span: {pattern!r}"
        return m.group(0)

    exec(_span(r"^class Tier\(IntEnum\):.*?(?=^\S)"), ns)
    exec(_span(r"^def first_call_key_surface_enabled\(.*?(?=^def |^class |\Z)"), ns)
    exec(_span(r"^def _first_call_key_surface_block\(.*?(?=^def |^class |\Z)"), ns)
    return ns


def _T(ns):
    return ns["Tier"]


def _grace(key="dch_trial_abc123", daily=15, exp="2026-06-26T00:00:00+00:00"):
    """A grace_meta as the anon-grace path stashes it for THIS request."""
    return {
        "anon_grace_used": True,
        "auto_trial_key": key,
        "auto_trial_daily_calls": daily,
        "auto_trial_expires_at": exp,
    }


# ── flag gating ──────────────────────────────────────────────────────────

def test_flag_off_returns_none(monkeypatch):
    """Default: ships DARK. No flag (or anything but 'true') → no surface."""
    ns = _load_helpers()
    monkeypatch.delenv("FIRST_CALL_KEY_SURFACE_ENABLED", raising=False)
    T = _T(ns)
    assert ns["_first_call_key_surface_block"](
        "search_facilities", T.FREE, None, _grace()) is None
    assert ns["first_call_key_surface_enabled"]() is False


def test_flag_explicit_false_returns_none(monkeypatch):
    ns = _load_helpers()
    monkeypatch.setenv("FIRST_CALL_KEY_SURFACE_ENABLED", "false")
    T = _T(ns)
    assert ns["_first_call_key_surface_block"](
        "search_facilities", T.FREE, None, _grace()) is None


def test_flag_case_insensitive(monkeypatch):
    ns = _load_helpers()
    monkeypatch.setenv("FIRST_CALL_KEY_SURFACE_ENABLED", "TRUE")
    T = _T(ns)
    assert ns["_first_call_key_surface_block"](
        "search_facilities", T.FREE, None, _grace()) is not None


# ── first-call surface fires (flag on + sub-Developer + grace-minted key) ──

def test_surface_appears_on_first_call_with_grace_key(monkeypatch):
    ns = _load_helpers()
    monkeypatch.setenv("FIRST_CALL_KEY_SURFACE_ENABLED", "true")
    T = _T(ns)
    for tier in (T.FREE, T.IDENTIFIED, T.STARTER):
        out = ns["_first_call_key_surface_block"](
            "search_facilities", tier, None,
            _grace(key="dch_trial_OWNKEY", daily=15,
                   exp="2026-06-26T00:00:00+00:00"))
        assert out is not None, f"expected surface for sub-Dev tier {tier!r}"
        # surfaces the caller's OWN minted key + the reuse header
        assert out["api_key"] == "dch_trial_OWNKEY"
        assert out["header"] == "X-API-Key"
        # honest durability read straight off the mint result (not inflated)
        assert out["daily_calls"] == 15
        assert out["expires_at"] == "2026-06-26T00:00:00+00:00"
        # bind CTA present so the human can keep it permanently
        assert out["bind_url"].endswith("/api/v1/keys/auto-trial/bind")
        assert out["bind_body"]["api_key"] == "dch_trial_OWNKEY"
        # note tells the agent to SAVE + REUSE across sessions + BIND email
        note = out["note"].lower()
        assert "dch_trial_ownkey" in note
        assert "reuse" in note and "session" in note
        assert "bind" in note and "email" in note


# ── FIRST-CALL ONLY: suppressed without a grace-minted key ─────────────────

def test_no_grace_meta_returns_none(monkeypatch):
    """A caller arriving on their OWN durable/bound key passes the gate without
    anon-grace → no grace_meta → surface suppressed (not shown every call)."""
    ns = _load_helpers()
    monkeypatch.setenv("FIRST_CALL_KEY_SURFACE_ENABLED", "true")
    T = _T(ns)
    assert ns["_first_call_key_surface_block"](
        "search_facilities", T.FREE, "dchub_dev_durable", None) is None
    # grace dict present but with NO minted key (mint failed / bot) → suppressed
    assert ns["_first_call_key_surface_block"](
        "search_facilities", T.FREE, None, {"anon_grace_used": True}) is None
    assert ns["_first_call_key_surface_block"](
        "search_facilities", T.FREE, None,
        {"auto_trial_key": ""}) is None


# ── established paid callers never see the surface ─────────────────────────

def test_developer_and_above_never_see_surface(monkeypatch):
    """Established paid callers already hold a durable key + are customers."""
    ns = _load_helpers()
    monkeypatch.setenv("FIRST_CALL_KEY_SURFACE_ENABLED", "true")
    T = _T(ns)
    for tier in (T.DEVELOPER, T.PRO, T.ENTERPRISE):
        assert ns["_first_call_key_surface_block"](
            "search_facilities", tier, "dchub_dev_x", _grace()) is None, (
            f"surface must NOT show to established paid tier {tier!r}")


# ── NO LEAK: only the caller's OWN grace key is ever surfaced ───────────────

def test_surface_only_uses_callers_own_grace_key(monkeypatch):
    """The surfaced key must come ONLY from this request's grace_meta — i.e.
    the key minted for THIS caller. It must never echo the passed api_key or
    invent a key, so it can't leak another caller's credential."""
    ns = _load_helpers()
    monkeypatch.setenv("FIRST_CALL_KEY_SURFACE_ENABLED", "true")
    T = _T(ns)
    out = ns["_first_call_key_surface_block"](
        "search_facilities", T.FREE,
        "dchub_dev_SOMEONE_ELSE",          # a DIFFERENT key on the request
        _grace(key="dch_trial_MINE"))
    assert out is not None
    # The surfaced key is the grace-minted one, NOT the unrelated api_key arg.
    assert out["api_key"] == "dch_trial_MINE"
    assert "SOMEONE_ELSE" not in out["api_key"]
    assert "SOMEONE_ELSE" not in out["note"]


# ── honest durability: tolerates a mint that didn't report cap/expiry ──────

def test_surface_honest_when_terms_missing(monkeypatch):
    ns = _load_helpers()
    monkeypatch.setenv("FIRST_CALL_KEY_SURFACE_ENABLED", "true")
    T = _T(ns)
    out = ns["_first_call_key_surface_block"](
        "search_facilities", T.FREE, None,
        {"auto_trial_key": "dch_trial_x", "auto_trial_daily_calls": None,
         "auto_trial_expires_at": None})
    assert out is not None
    # No invented cap/expiry — the fields echo what the mint reported (None).
    assert out["daily_calls"] is None
    assert out["expires_at"] is None
    # Still surfaces the key + reuse instruction.
    assert out["api_key"] == "dch_trial_x"
    assert "reuse" in out["reuse_instruction"].lower()


# ── additive / non-corrupting wiring (asserted against _finalize source) ────

def test_finalize_surface_is_additive_not_replacing():
    """_finalize injects a SEPARATE _meta field and only APPENDS to the message
    — it must never overwrite the tool's data keys or the message wholesale."""
    src = _src()
    # structured field set additively under _meta
    assert 'data["_meta"]["first_call_key_offer"] = _fck' in src
    # note APPENDED to the message (never assigned raw)
    assert 'data["message"] = _msg + "\\n\\n" + _fck["note"]' in src
    # never clobbers the message with just the note
    assert 'data["message"] = _fck["note"]' not in src
    # injection is best-effort (wrapped so it never blocks the response).
    # Anchor on the actual _finalize wiring line (the assignment), not the
    # earlier docstring/comment mentions of the field name.
    idx = src.find('data["_meta"]["first_call_key_offer"] = _fck')
    assert idx != -1
    head = src[max(0, idx - 600):idx]
    assert "try:" in head
    assert "_grace = get_pending_grace_meta()" in head


def test_finalize_drains_grace_meta_once():
    """_finalize reads the per-request grace via get_pending_grace_meta()
    (read-once + auto-clears) so flag-off requests don't leak grace state into
    the next request on the thread."""
    src = _src()
    assert "_grace = get_pending_grace_meta()" in src
    assert "_first_call_key_surface_block(tool_name, tier, api_key, _grace)" in src


def test_grace_meta_stashes_honest_minted_terms():
    """The anon-grace stash must carry the REAL minted cap/expiry (off the mint
    result), not an inflated constant — overpromising is the retention leak."""
    src = _src()
    # honest values pulled from the mint result
    assert '"auto_trial_daily_calls":       _daily,' in src
    assert '"auto_trial_expires_at":        _exp,' in src
    # and the old inflated hardcode is gone from that stash
    assert '"auto_trial_daily_calls":       200,' not in src


def test_flag_default_off_in_source():
    """FIRST_CALL_KEY_SURFACE_ENABLED defaults to 'false' (ships dark)."""
    src = _src()
    assert ("os.environ.get(\n        \"FIRST_CALL_KEY_SURFACE_ENABLED\", \"false\")"
            in src) or (
        'os.environ.get("FIRST_CALL_KEY_SURFACE_ENABLED", "false")' in src)


def test_gate_drains_stale_grace_at_entry():
    """NO-LEAK boundary: _gate() must drain any grace left on the thread by a
    PRIOR request (one that set grace but never reached _finalize — e.g. a tool
    handler that raised after the gate passed) BEFORE it does anything else.
    Without this, a stale caller's auto-trial key could be surfaced to the next
    free caller on a reused WSGI thread. Assert the drain call sits at the very
    top of _gate, ahead of the tier resolution."""
    src = _src()
    start = src.find("def _gate(tool_name")
    assert start != -1, "could not find _gate definition"
    body = src[start:start + 1600]
    drain = body.find("get_pending_grace_meta()")
    resolve = body.find("tier = resolve_tier(api_key)")
    assert drain != -1, "_gate must drain grace_meta at entry (no-leak boundary)"
    assert resolve != -1, "expected the tier resolution in _gate"
    assert drain < resolve, (
        "_gate must drain stale grace BEFORE resolving tier / setting new grace, "
        "so _finalize can only ever see grace minted during THIS request")


def test_get_pending_grace_meta_auto_clears():
    """The drain primitive itself: a second read returns None (read-once), so a
    drained thread can't re-surface a prior request's key."""
    import importlib
    gk = importlib.import_module("mcp_gatekeeper")
    gk._set_pending_grace_meta({"auto_trial_key": "dch_trial_leaky"})
    assert gk.get_pending_grace_meta() == {"auto_trial_key": "dch_trial_leaky"}
    assert gk.get_pending_grace_meta() is None  # cleared on first read
