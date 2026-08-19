"""Guard: every Stripe route is CLASSIFIED, and the gated ones keep their gate.

Why this exists (all verified against production 2026-08-19, before the fix):

  POST /api/v1/stripe/webhook-convert   — no signature check, no key, no auth.
      Read `email` and `plan` from the request body and ran
      `UPDATE users SET plan = %s WHERE email = %s` plus an api_keys tier grant.
      Anyone could award any address any plan, up to enterprise.
  GET  /api/v1/stripe/_webhook-debug    — docstring claimed an admin_key gate;
      there was none. Served STRIPE_SECRET_KEY as `sk_live...M60c (len=107)` and
      STRIPE_WEBHOOK_SECRET as `whsec_u...QXUL (len=38)` to anonymous callers.
  GET  /api/v1/stripe/webhook-list, /webhook-diagnostics, /conversions-audit,
       /api/stripe/webhook-test — all 200 unauthenticated; webhook config,
      signing-secret inventory, revenue counts, and recent paid users.

★ WHY THE EXISTING ROUTE-AUTH SHELL DID NOT CATCH THESE. Two reasons, both
worth remembering because they generalise:

  1. Its unauthenticated-mutation lane is keyed on the function-name vocabulary
     run/sync/ingest/extract. A handler called `_stripe_webhook_convert` that
     grants paid plans is out of scope BY CONSTRUCTION, not by judgement.
  2. Its secret-leak lane advertises "a handler that returns value[:4]+…+
     value[-2:] leaks secret shape to any caller" and reported
     `0 env-value preview site(s) [none]` on the same tick that
     /_webhook-debug was serving exactly that shape.

So this file does not pattern-match intent. It requires every Stripe route to be
NAMED in one of two lists. A new one fails the suite until a human classifies
it, which is the only property that survives a detector's vocabulary going
stale.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Routes that MUST carry a Stripe signature check or an admin gate.
# value = why it is dangerous, so a future reader can re-judge it.
MUST_BE_GATED = {
    "/api/v1/stripe/webhook":             "provisions plan + key on real payment",
    "/api/v1/stripe/webhook-mcp":         "mints the durable MCP key",
    "/api/v1/stripe/webhook-convert":     "UPDATEs users.plan + api_keys tier from the body",
    "/api/v2/stripe/webhook":             "provisions plan + key, sends no email",
    "/api/stripe/webhook":                "legacy provisioning webhook",
    "/api/stripe/tier-webhook":           "tier mutation",
    "/api/v1/stripe/_webhook-debug":      "previews live secret fingerprints",
    "/api/v1/stripe/webhook-list":        "discloses Stripe webhook configuration",
    "/api/v1/stripe/webhook-diagnostics": "discloses which signing secrets are set",
    "/api/v1/stripe/conversions-audit":   "discloses conversion + revenue counts",
    "/api/stripe/webhook-test":           "discloses recent paid users, plans, statuses",
    "/api/stripe/webhook/replay":         "re-drives provisioning from Stripe events",
    "/report-to-stripe":                  "writes metered usage",
    "/api/v1/_env_stripe_check":          "env inspection",
}

# Deliberately reachable without an admin key. Each entry states WHY, because
# "it was already public" is not a reason.
PUBLIC_BY_DESIGN = {
    "/api/stripe/config":            "publishable key + list prices; the checkout page needs it",
    "/api/v2/stripe/config":         "same, v2 surface",
    "/api/stripe/create-checkout":   "customer-initiated checkout; uses the secret key, never returns it",
    "/api/v2/stripe/create-checkout": "same, v2 surface (carries its own user auth)",
    "/api/stripe/portal":            "customer-initiated billing portal",
    "/api/stripe/subscription":      "customer reads their own subscription",
    "/api/stripe/webhook-alert":     ("dashboard.html recovery path; verifies against Stripe "
                                      "server-side before granting, and privilege now fails "
                                      "closed at 'pro' rather than trusting the posted plan"),
}

_SIG = ("construct_event",)
_GATE = ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "_admin_ok", "X-Admin-Key",
         "_admin_authorized", "_stripe_diag_admin_ok", "require_admin",
         "is_valid_internal_key")

_SKIP_DIRS = (".claude/", "worktrees/", "node_modules/", "/tests/", "/.git/")

# Decorator-level gates. Reading only the function BODY misses these entirely —
# /api/v1/_env_stripe_check is protected by @_require_internal and nothing else,
# and an early draft of this guard reported it as wide open. A gate you cannot
# see is indistinguishable from a gate that is not there, in both directions.
_GATE_DECORATORS = ("_require_internal", "require_internal", "require_admin",
                    "admin_required", "login_required", "internal_only")


def _importable(path):
    """A module whose filename is not a valid Python identifier cannot be
    imported by `import`, so its @app.route decorators never execute and its
    handlers are not reachable. stripe-webhook-fix.py is one of these — a
    leftover snippet whose docstring says "Replace the stripe_webhook function
    in main.py with this fixed version". This is a STRUCTURAL exclusion, not a
    judgement call about whether the file looks dead."""
    return path.stem.isidentifier()


def _stripe_routes():
    """(path, file, lineno, source) for every Stripe-decorated handler.
    `source` includes the decorators, so decorator-level gates are visible."""
    found = []
    for f in ROOT.rglob("*.py"):
        rel = str(f)
        if any(d in rel.replace("\\", "/") for d in _SKIP_DIRS):
            continue
        if not _importable(f):
            continue
        try:
            src = f.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src)
        except Exception:
            continue
        for n in ast.walk(tree):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for d in n.decorator_list:
                if not isinstance(d, ast.Call):
                    continue
                for a in d.args:
                    if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                            and "stripe" in a.value.lower()):
                        # ★ FunctionDef.lineno points at `def`, NOT at the first
                        # decorator, so get_source_segment EXCLUDES decorators.
                        # An earlier revision of this guard read only the body
                        # and reported @_require_internal-gated routes as wide
                        # open. Splice the decorator names back in.
                        decos = " ".join(ast.unparse(d) for d in n.decorator_list)
                        body = ast.get_source_segment(src, n) or ""
                        found.append((a.value, f.relative_to(ROOT), n.lineno,
                                      decos + "\n" + body))
    return found


def test_the_walk_finds_the_routes():
    """Non-vacuity floor. If the AST walk breaks, every assertion below passes
    for the wrong reason — which is the failure mode this whole file is about."""
    routes = _stripe_routes()
    assert len(routes) >= 15, (
        f"found only {len(routes)} Stripe route handlers — the walk is broken"
    )


def test_every_stripe_route_is_classified():
    """★ The load-bearing assertion. A NEW Stripe route fails this suite until a
    human puts it in one list or the other. That is deliberate: the previous
    detector missed these because its vocabulary did not happen to match, and a
    list cannot go stale silently the way a regex can."""
    known = set(MUST_BE_GATED) | set(PUBLIC_BY_DESIGN)
    seen = {p for p, _f, _l, _s in _stripe_routes()}
    unclassified = sorted(seen - known)
    assert not unclassified, (
        "unclassified Stripe route(s): %s\n"
        "Add each to MUST_BE_GATED (with why it is dangerous) or to "
        "PUBLIC_BY_DESIGN (with why it is safe) in this file. Do not guess — a "
        "route that mutates users.plan/api_keys or returns env material is "
        "MUST_BE_GATED." % ", ".join(unclassified)
    )


@pytest.mark.parametrize("path", sorted(MUST_BE_GATED))
def test_dangerous_stripe_routes_are_gated(path):
    """Each dangerous route must verify a Stripe signature or check an admin key."""
    impls = [(f, l, s) for p, f, l, s in _stripe_routes() if p == path]
    assert impls, (
        f"{path} is listed in MUST_BE_GATED but no handler was found — either it "
        "was deleted (remove the entry) or the walk stopped seeing it"
    )
    ungated = [f"{f}:{l}" for f, l, s in impls
               if not (any(k in s for k in _SIG)
                       or any(k in s for k in _GATE)
                       or any(k in s for k in _GATE_DECORATORS))]
    assert not ungated, (
        f"{path} ({MUST_BE_GATED[path]}) has NO signature check and NO admin "
        f"gate at: {', '.join(ungated)}"
    )


def test_convert_endpoint_fails_closed_without_a_secret():
    """★ Specific regression. The canonical webhook falls back to
    `json.loads(payload)` and accepts UNSIGNED input when no secret is set. On a
    route that grants paid entitlements, 'cannot verify' must mean 'refuse'."""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "_stripe_webhook_convert"), None)
    assert fn is not None, "_stripe_webhook_convert not found in main.py"
    body = ast.get_source_segment(src, fn) or ""
    assert "construct_event" in body, "signature verification is gone"
    assert "503" in body and "if not _secrets" in body, (
        "the no-secret branch must REFUSE (503), not fall through to accepting "
        "unsigned input the way the canonical webhook does"
    )
    # The verified event must be the data source. Re-parsing the raw body would
    # reintroduce the whole hole while leaving the signature check in place.
    assert "request.get_json(force=True)" not in body, (
        "handler re-reads the UNVERIFIED request body — the signature check "
        "above it is then decorative"
    )


def test_plan_privilege_never_comes_from_the_client():
    """webhook-alert verified that *a* subscription existed but let the caller
    choose WHICH tier via the posted plan string. Any customer with any active
    sub could ask for enterprise."""
    src = (ROOT / "webhook_alert_endpoint.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_resolve_plan_tier"), None)
    assert fn is not None, "_resolve_plan_tier not found"
    body = ast.get_source_segment(src, fn) or ""
    for bad in ("return 'enterprise'", 'return "enterprise"',
                "return 'founding'", 'return "founding"'):
        idx = body.find(bad)
        while idx != -1:
            # Only the metadata/price-derived branches may return a high tier.
            before = body[:idx]
            assert ("metadata" in before.rsplit("\n\n", 1)[-1]
                    or "price_id" in before.rsplit("\n\n", 1)[-1]
                    or "enterprise_prices" in before.rsplit("\n\n", 1)[-1]
                    or "founding_price" in before.rsplit("\n\n", 1)[-1]), (
                "a high tier is returned from a branch not derived from Stripe "
                "metadata or price — if that branch reads fallback_plan, the "
                "caller is choosing their own entitlement"
            )
            idx = body.find(bad, idx + 1)
    assert "if 'enterprise' in fallback_plan:" not in body, (
        "client-supplied fallback_plan selects the enterprise tier again"
    )
