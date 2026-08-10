"""Guard: what an UNAUTHENTICATED caller can pull, and what it must never see.

Every claim below was measured live on 2026-08-10 against dchub.cloud from a
clean client with no key, no cookie and no referer.

──────────────────────────────────────────────────────────────────────────
THREE PII LEAKS (severity order). None exposed key MATERIAL — verified: zero
dch_live_* strings in any payload — but all three exposed WHO the customers are.

  /api/v1/outreach/log        NO auth at all. 52 rows, 22 real recipient
                              addresses — named individuals at coreweave.com,
                              deepmind.com, nvidia.com, groq.com, mistral.ai,
                              perplexity.ai, nlr.gov — plus subject lines naming
                              the person and their plan ("Anthony — your DC Hub
                              Pro account"), send status and resend IDs.
                              Effectively the customer/prospect list.
  /api/v1/observability/dev-keys    OPTIONAL gate (`if admin_token:`) with
                              TOP_USERS_TOKEN unset ⇒ public. 200 key records,
                              24 owner emails, tier + last_used_at.
  /api/v1/observability/top-users   same optional-gate bug, 9 emails.

An optional gate on PII is a gate that is open by default. All three now fail
CLOSED — if no admin credential is configured, they refuse rather than open.

──────────────────────────────────────────────────────────────────────────
TWO MONETISATION GAPS

  /api/v1/map     ?limit=20000 returned all 19,969 facilities, and TWELVE
                  consecutive pulls were all HTTP 200 with zero blocked: the
                  path sits in free_tier_gate.ALWAYS_OPEN_PREFIXES so it never
                  reaches a limiter. The uncapped sweep is the legitimate public
                  SEO render, so the cap is on REPEATS per IP per day, not on
                  the sweep itself.
  /api/v1/search  ?limit=200 returned 100 rows while tier_registry declares
                  record_cap=50 for anonymous — 2x the published contract.

And a config that lied: '/api/v1/map' was in BOTH GATED_PREFIXES ("Pro/
Enterprise only") and ALWAYS_OPEN_PREFIXES ("Never gated"). is_always_open() is
checked first and returns, so the restrictive entry could never fire while
anyone reading it believed the map was paywalled.

No network. Pure source assertions, so this runs in CI.

Run locally:
    python3 -m pytest tests/test_anon_bulk_gating.py -v
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


# ── PII endpoints must fail closed ───────────────────────────────────────────

def test_outreach_log_requires_admin():
    """It exposes recipient addresses. It shipped with no auth whatsoever."""
    s = _src("main.py")
    i = s.index('@app.route("/api/v1/outreach/log"')
    body = s[i:i + 2000]
    assert "DCHUB_ADMIN_KEY" in body and "unauthorized" in body, \
        "/api/v1/outreach/log must require the admin key"
    assert body.index("unauthorized") < body.index("DATABASE_URL"), \
        "the auth check must run BEFORE the query, not after"


def test_observability_pii_gate_is_mandatory_not_optional():
    """`if admin_token:` made the gate vanish whenever the env var was unset."""
    s = _src("routes/observability_routes.py")
    assert "def _require_obs_admin" in s
    assert s.count("_require_obs_admin()") >= 2, \
        "both dev-keys and top-users must call the shared gate"
    assert "admin_token = os.environ.get('TOP_USERS_TOKEN')\n        if admin_token:" not in s, \
        "the optional-gate pattern is back — PII would be public again"


def test_obs_gate_refuses_when_nothing_is_configured():
    """Fail CLOSED. The old code did the opposite: no token configured meant
    no check at all."""
    s = _src("routes/observability_routes.py")
    fn = s[s.index("def _require_obs_admin"):]
    fn = fn[:fn.index("\n\n\n")]
    assert "if got and (" in fn, \
        "an empty credential must never authorise"
    assert "return None" in fn and "401" in fn


# ── the config that lied ─────────────────────────────────────────────────────

def test_map_is_not_in_both_gate_lists():
    """'/api/v1/map' in GATED_PREFIXES was dead code — is_always_open() returns
    first — but it made the file assert a paywall that did not exist."""
    s = _src("free_tier_gate.py")
    gated = s[s.index("GATED_PREFIXES = ["):s.index("]", s.index("GATED_PREFIXES = ["))]
    always = s[s.index("ALWAYS_OPEN_PREFIXES = ["):
               s.index("]", s.index("ALWAYS_OPEN_PREFIXES = ["))]
    in_gated = "'/api/v1/map'" in gated
    in_always = "'/api/v1/map'" in always
    assert not (in_gated and in_always), (
        "/api/v1/map is in BOTH lists again. is_always_open() short-circuits, "
        "so the GATED entry is unreachable and the config misleads its reader.")


# ── bulk-harvest cap on the map ──────────────────────────────────────────────

def test_map_has_a_per_ip_bulk_cap():
    s = _src("main.py")
    i = s.index("def api_v1_map")
    body = s[i:i + 12000]
    assert "MAP_ANON_BULK_PER_DAY" in body, "no per-IP bulk cap on /api/v1/map"
    assert "bulk_rate_limited" in body and "429" in body


def test_map_bulk_cap_fails_open():
    """Anti-harvesting, not authentication. It must never blank the SEO map."""
    s = _src("main.py")
    i = s.index("MAP_ANON_BULK_PER_DAY")
    tail = s[i:i + 3000]
    assert "except Exception:" in tail and "never let the limiter break the map" in tail


def test_map_bulk_cap_exempts_genuine_viewport_requests():
    """A bbox request is a map pan, not a harvest; it is already row-capped."""
    s = _src("main.py")
    i = s.index("MAP_ANON_BULK_PER_DAY")
    assert "not _map_viewport" in s[i:i + 1200], \
        "viewport requests must not consume the bulk budget"


# ── search must honour its own published contract ────────────────────────────

def test_search_clamps_to_the_declared_record_cap():
    s = _src("main.py")
    i = s.index("def search_facilities")
    body = s[i:i + 3000]
    assert "record_cap" in body, \
        "search must clamp to tier_registry record_cap, not a bare literal"
    assert "min(request.args.get('limit', 25, type=int), 100)" not in body, \
        "the hardcoded 100 cap is back — 2x the declared anonymous contract"


def test_search_cap_fails_closed_to_anonymous():
    """An unresolvable caller must get the TIGHTEST cap, never the loosest."""
    s = _src("main.py")
    i = s.index("def search_facilities")
    body = s[i:i + 3000]
    assert "TIER_LIMITS['anonymous']" in body or "'anonymous'" in body, \
        "unresolved tier must fall back to anonymous limits"


def test_declared_anonymous_record_cap_is_still_50():
    """If this moves, the search clamp silently moves with it."""
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tier_registry import TIER_LIMITS
    assert TIER_LIMITS["anonymous"]["record_cap"] == 50
