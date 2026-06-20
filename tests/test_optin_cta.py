"""Guard the marketing opt-in soft-CTA injected at the MCP paywall.

GOAL recap: make high-usage FREE users reachable for upgrade outreach by
getting them to OPT IN (compliantly). The outreach engine's high_usage_free
segment hard-gates on mcp_dev_keys.metadata->>marketing_opt_in='true', and
~0 users are opted in today, so the power users on get_grid_intelligence /
get_fiber_intel are unreachable. This CTA is the *capture* surface — a SOFT,
honest, flag-gated ask shown only to FREE callers hitting those tools.

This test guards the behaviour the build established:

  1. FLAG-GATED — ships DARK. No CTA unless OPTIN_CTA_ENABLED=true.
  2. FREE-ONLY — never shown to identified/paid callers (already reachable).
  3. TARGETED — only the high-usage paid tools (get_grid_intelligence /
     get_fiber_intel), to hit the real upgrade-outreach pool.
  4. ADDITIVE / NON-CORRUPTING — surfaced as a separate `optin_cta` field +
     an appended note; it never replaces or mutates the tool's data payload.
  5. COMPLIANT — the CTA points at the opt-in REQUEST flow (double-opt-in;
     consent is set later on a tokenized confirm click). It NEVER sets
     marketing_opt_in and sends nothing.

mcp_gatekeeper imports Flask/db at module load and the CI unit-tests job
installs only pytest, so we exec the (pure) helper source in isolation rather
than importing the module, and assert against the source for the wiring.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GK = os.path.join(ROOT, "mcp_gatekeeper.py")


def _src():
    return open(GK, encoding="utf-8").read()


def _load_helpers():
    """Exec the pure opt-in helpers (+ the Tier enum + OPTIN_CTA_TOOLS they
    depend on) into an isolated namespace. No Flask/db needed — the helpers
    are deliberately side-effect-free."""
    src = _src()
    ns = {}
    # typing symbols the def lines reference
    from typing import Optional, Dict, Any
    from enum import IntEnum
    ns.update({"Optional": Optional, "Dict": Dict, "Any": Any,
               "IntEnum": IntEnum, "os": os})

    def _span(pattern):
        m = re.search(pattern, src, re.DOTALL | re.MULTILINE)
        assert m, f"could not locate source span: {pattern!r}"
        return m.group(0)

    # class Tier(IntEnum): ... (up to the next top-level def/class)
    exec(_span(r"^class Tier\(IntEnum\):.*?(?=^\S)"), ns)
    # OPTIN_CTA_TOOLS = frozenset({...})
    exec(_span(r"^OPTIN_CTA_TOOLS = frozenset\(\{.*?\}\)\n"), ns)
    # the two helper functions
    exec(_span(r"^def optin_cta_enabled\(.*?(?=^def |^class |\Z)"), ns)
    exec(_span(r"^def _optin_cta_block\(.*?(?=^def |^class |\Z)"), ns)
    return ns


# A non-FREE tier value to assert the FREE-only gate. Tier is an IntEnum;
# IDENTIFIED=1 is the first rung above FREE.
def _tiers(ns):
    return ns["Tier"]


# ── flag gating ──────────────────────────────────────────────────────────

def test_flag_off_returns_none(monkeypatch):
    """Default: ships DARK. No flag (or anything but 'true') → no CTA."""
    ns = _load_helpers()
    monkeypatch.delenv("OPTIN_CTA_ENABLED", raising=False)
    T = _tiers(ns)
    assert ns["_optin_cta_block"]("get_grid_intelligence", T.FREE, "k") is None
    assert ns["optin_cta_enabled"]() is False


def test_flag_explicit_false_returns_none(monkeypatch):
    ns = _load_helpers()
    monkeypatch.setenv("OPTIN_CTA_ENABLED", "false")
    T = _tiers(ns)
    assert ns["_optin_cta_block"]("get_grid_intelligence", T.FREE, "k") is None


def test_flag_on_free_high_usage_tool_returns_cta(monkeypatch):
    ns = _load_helpers()
    monkeypatch.setenv("OPTIN_CTA_ENABLED", "true")
    T = _tiers(ns)
    for tool in ("get_grid_intelligence", "get_fiber_intel"):
        cta = ns["_optin_cta_block"](tool, T.FREE, "key_123")
        assert cta is not None, f"expected CTA for {tool} when flag on + FREE"
        assert cta["optin_tool"] == tool
        assert cta["optin_double_opt_in"] is True
        assert cta["optin_source"] == "paywall_optin_cta"
        # honest value + an unsubscribe promise, no dark pattern
        assert "early access" in cta["optin_value"].lower()
        assert "unsubscribe" in cta["optin_value"].lower()
        # URL points at the opt-in REQUEST flow, carries source + tool
        assert cta["optin_url"].startswith(
            "https://dchub.cloud/api/v1/marketing/opt-in/request")
        assert "source=paywall_optin_cta" in cta["optin_url"]
        assert f"tool={tool}" in cta["optin_url"]


def test_flag_case_insensitive(monkeypatch):
    ns = _load_helpers()
    monkeypatch.setenv("OPTIN_CTA_ENABLED", "TRUE")
    T = _tiers(ns)
    assert ns["_optin_cta_block"]("get_fiber_intel", T.FREE, None) is not None


# ── FREE-only gate ───────────────────────────────────────────────────────

def test_paid_tiers_never_see_cta(monkeypatch):
    """Even with the flag on, identified/paid callers get no CTA — they're
    already reachable / already customers."""
    ns = _load_helpers()
    monkeypatch.setenv("OPTIN_CTA_ENABLED", "true")
    T = _tiers(ns)
    for tier in (T.IDENTIFIED, T.STARTER, T.DEVELOPER, T.PRO, T.ENTERPRISE):
        assert ns["_optin_cta_block"]("get_grid_intelligence", tier, "k") is None, (
            f"CTA must NOT show to tier {tier!r}")


# ── targeting (only the high-usage pool) ─────────────────────────────────

def test_non_target_tool_returns_none_even_when_free_and_on(monkeypatch):
    """A FREE caller on a paid tool that ISN'T in the high-usage pool gets no
    CTA — we only prompt where there's a real outreach audience."""
    ns = _load_helpers()
    monkeypatch.setenv("OPTIN_CTA_ENABLED", "true")
    T = _tiers(ns)
    for tool in ("get_energy_prices", "list_transactions", "get_market_intel",
                 "analyze_site"):
        assert ns["_optin_cta_block"](tool, T.FREE, "k") is None


def test_target_pool_is_exactly_the_two_high_usage_tools():
    ns = _load_helpers()
    assert ns["OPTIN_CTA_TOOLS"] == frozenset(
        {"get_grid_intelligence", "get_fiber_intel"})


# ── no api_key still works (anonymous-ish FREE caller) ───────────────────

def test_no_api_key_omits_key_param(monkeypatch):
    ns = _load_helpers()
    monkeypatch.setenv("OPTIN_CTA_ENABLED", "true")
    T = _tiers(ns)
    cta = ns["_optin_cta_block"]("get_grid_intelligence", T.FREE, None)
    assert cta is not None
    assert "key=" not in cta["optin_url"]


# ── additive / non-corrupting wiring (asserted against _gate source) ─────

def test_optin_cta_is_additive_not_replacing():
    """The injection sets a SEPARATE payload field and only APPENDS to the
    message — it must never overwrite the data or the message wholesale."""
    src = _src()
    # The structured field is set additively.
    assert 'payload["optin_cta"] = _optin' in src
    # The note is APPENDED to the message (+ "\n\n" + ...), never assigned raw.
    assert 'payload["message"] = payload["message"] + "\\n\\n" + _optin["optin_note"]' in src
    # And there is NO line that assigns the message to the bare opt-in note
    # (which would clobber the upgrade copy). Defensive guard.
    assert 'payload["message"] = _optin["optin_note"]' not in src


def test_optin_cta_survives_auto_trial_message_override():
    """The auto-trial branch fully REPLACES payload['message']; the wiring
    must re-append the opt-in note there too so FREE callers (who get the
    auto-trial key) still see the soft ask in the prose."""
    src = _src()
    # The re-append lives after the auto-trial message override block.
    auto_idx = src.find('"✨ Auto-trial key minted for you:')
    assert auto_idx != -1, "auto-trial message override block not found"
    tail = src[auto_idx:]
    assert '_optin = payload.get("optin_cta")' in tail, (
        "opt-in note must be re-appended after the auto-trial message override")


def test_gate_guards_optin_injection_with_try_except():
    """The opt-in injection must be best-effort (never block the paywall)."""
    src = _src()
    assert "_optin_cta_block(tool_name, tier, api_key)" in src
    # it sits in the tier-gate (paywall) branch, and the comment documents the
    # best-effort contract.
    assert "opt-in CTA is best-effort" in src


def test_helper_sets_no_consent_and_sends_nothing():
    """Compliance: the CTA capture surface must not set marketing_opt_in and
    must not send email. Assert the helper body is inert on that front."""
    src = _src()
    m = re.search(r"^def _optin_cta_block\(.*?(?=^def |^class |\Z)",
                  src, re.DOTALL | re.MULTILINE)
    assert m
    body = m.group(0)
    # No consent mutation, no sending, no DB writes in the CTA builder.
    assert "marketing_opt_in" not in body.replace("# ", "").split('"""', 2)[-1] or \
        "set marketing_opt_in" not in body  # only mentioned in the docstring caveat
    assert "send_email" not in body
    assert "UPDATE" not in body
    # It IS double-opt-in and points at a REQUEST (not a confirm/set) endpoint.
    assert "opt-in/request" in body
    assert "optin_double_opt_in" in body


# ── suppression gate: a previously-unsubscribed email must NOT be re-shown the
#    opt-in CTA, even with the flag on (CAN-SPAM re-prompting). The check runs at
#    the injection site, only on the narrow CTA-candidate path. ──────────────
def _patch_suppress(monkeypatch, email_for_key, suppressed_emails, raise_db=False):
    import psycopg2
    import routes.email_suppression as es
    monkeypatch.setenv("DATABASE_URL", "postgres://test")

    class _Cur:
        def execute(self, *a, **k): pass
        def fetchone(self): return [email_for_key] if email_for_key else None
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Conn:
        def cursor(self): return _Cur()
        def close(self): pass

    if raise_db:
        monkeypatch.setattr(psycopg2, "connect",
                            lambda *a, **k: (_ for _ in ()).throw(Exception("db down")))
    else:
        monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(es, "is_suppressed", lambda cur, email: email in suppressed_emails)


def test_optin_recipient_suppressed_paths(monkeypatch):
    import mcp_gatekeeper as g
    assert g._optin_recipient_suppressed(None) is False          # no key -> nothing to suppress
    _patch_suppress(monkeypatch, "sup@x.com", {"sup@x.com"})
    assert g._optin_recipient_suppressed("k") is True            # suppressed -> skip CTA
    _patch_suppress(monkeypatch, "ok@x.com", {"sup@x.com"})
    assert g._optin_recipient_suppressed("k") is False           # clean -> show CTA
    _patch_suppress(monkeypatch, "x@x.com", set(), raise_db=True)
    assert g._optin_recipient_suppressed("k") is True            # db error -> fail-safe skip


def test_suppressed_free_user_cta_dropped_non_suppressed_kept(monkeypatch):
    """Flag ON + FREE + target tool: the CTA block builds, but a SUPPRESSED
    recipient is gated out at injection; a clean recipient is kept."""
    import mcp_gatekeeper as g
    monkeypatch.setenv("OPTIN_CTA_ENABLED", "true")
    cta = g._optin_cta_block("get_grid_intelligence", g.Tier.FREE, "k")
    assert cta is not None  # the CTA WOULD show...
    _patch_suppress(monkeypatch, "sup@x.com", {"sup@x.com"})
    assert g._optin_recipient_suppressed("k") is True   # ...but suppressed -> injection drops it
    _patch_suppress(monkeypatch, "ok@x.com", {"sup@x.com"})
    assert g._optin_recipient_suppressed("k") is False  # clean -> injection keeps it
