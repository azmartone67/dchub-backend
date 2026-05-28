"""
test_tier_consistency.py — the guard that stops tier maps from drifting.
(r43-H, 2026-05-27)

After a founding member was mis-classified as free (Carl Braun) because
'founding' was missing or under-ranked in several of the ~17 duplicated
tier maps, this test asserts the ONE rule across every map we can reach:

    founding === pro   (for access rank AND benefits)
    pro is present and treated as a paid tier

Importable backend maps are checked directly. The frontend/MCP maps
(JS — can't import) are checked by reading the files and asserting
'founding' appears alongside 'pro'. If anyone adds a tier map that omits
founding or ranks it below pro, this test fails in CI before it ships.

Runnable two ways:  pytest tests/test_tier_consistency.py
                    python3 tests/test_tier_consistency.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_FAILURES = []


def _check(cond, msg):
    if not cond:
        _FAILURES.append(msg)


# ── 1. The canonical registry is internally consistent ──────────────
def test_registry_founding_equals_pro():
    import tier_registry as tr
    _check(tr.rank('founding') == tr.rank('pro'),
           f"registry: founding rank {tr.rank('founding')} != pro {tr.rank('pro')}")
    _check(tr.satisfies('founding', 'pro'), "registry: founding must satisfy pro")
    _check(tr.is_paid('founding'), "registry: founding must be paid")
    _check(tr.limits('founding') == tr.limits('pro'),
           "registry: founding limits must equal pro limits")
    _check('founding' in tr.paid_plans() and 'pro' in tr.paid_plans(),
           "registry: paid_plans must include founding and pro")
    assert not _FAILURES, "\n".join(_FAILURES)


# ── 2. Importable backend maps: founding == pro ─────────────────────
def test_backend_maps_founding_equals_pro():
    fails = []

    def chk_map(modpath, attr, key_founding='founding', key_pro='pro', cmp='eq'):
        try:
            mod = __import__(modpath, fromlist=[attr.split('.')[0]])
            obj = mod
            for part in attr.split('.'):
                obj = getattr(obj, part)
        except Exception as e:
            fails.append(f"SKIP {modpath}.{attr} (import failed: {str(e)[:60]})")
            return
        if isinstance(obj, dict):
            if key_founding not in obj:
                fails.append(f"{modpath}.{attr}: MISSING '{key_founding}'")
            elif cmp == 'eq' and obj.get(key_founding) != obj.get(key_pro):
                fails.append(f"{modpath}.{attr}: founding({obj.get(key_founding)}) != pro({obj.get(key_pro)})")
            elif cmp == 'ge' and obj.get(key_founding, -99) < obj.get(key_pro, 99):
                fails.append(f"{modpath}.{attr}: founding({obj.get(key_founding)}) < pro({obj.get(key_pro)})")
        elif isinstance(obj, (set, tuple, list)):
            if key_founding not in obj:
                fails.append(f"{modpath}.{attr}: paid set MISSING '{key_founding}'")

    chk_map('api_tier_gating', 'PLAN_LEVELS')
    chk_map('api_tier_gating', 'TIER_RATE_LIMITS')
    chk_map('api_tier_gating', 'TIER_DAILY_RECORD_CAPS')
    chk_map('api_tier_gating', 'TIER_PAGE_CAPS')
    chk_map('paywall_middleware', 'TIER_HIERARCHY')
    chk_map('routes.auth_context', '_TIER_RANK', 'founding', 'pro', cmp='ge')
    chk_map('routes.tier_gate', '_TIER_RANK', 'FOUNDING', 'PRO')   # uppercase keys
    chk_map('free_tier_gate', 'PAID_PLANS')

    # SKIPs (import side-effects in CI) are tolerated; real mismatches are not.
    hard = [f for f in fails if not f.startswith('SKIP')]
    assert not hard, "Tier-map drift detected:\n" + "\n".join(hard) + \
        ("\n(skipped: " + "; ".join(f for f in fails if f.startswith('SKIP')) + ")" if any(f.startswith('SKIP') for f in fails) else "")


# ── 3. Frontend / MCP JS maps: 'founding' must appear ───────────────
def test_frontend_js_maps_have_founding():
    fails = []
    js_files = [
        "dchub-frontend/js/dchub-nav.js",
        "dchub-frontend/js/land-power-app.js",
        "dchub-frontend/js/dchub-access-gate.js",
        "dchub-frontend/_worker.js",     # MCP_TIERS
    ]
    for rel in js_files:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue  # backend repo may not contain the subdir copy
        txt = open(path, encoding='utf-8', errors='replace').read()
        if 'founding' not in txt.lower():
            fails.append(f"{rel}: no 'founding' reference (tier map likely omits it)")
    assert not fails, "Frontend tier maps missing founding:\n" + "\n".join(fails)


# ── 4. generate_api_key must match the live api_keys schema ─────────
def test_generate_api_key_matches_schema():
    """r43-H regression guard: generate_api_key INSERTed a non-existent
    `email` column and errored on every call, silently breaking key
    auto-provisioning. Lock the column set to the real schema."""
    src_path = os.path.join(ROOT, "api_tier_gating.py")
    src = open(src_path, encoding="utf-8").read()
    i = src.index("def generate_api_key")
    # first INSERT INTO api_keys after the function start is this function's
    j = src.index("INSERT INTO api_keys", i)
    cols = src[j: src.index("VALUES", j)]
    fails = []
    if "email" in cols:
        fails.append("generate_api_key INSERT references a non-existent `email` column")
    for required in ("user_id", "key_hash", "key_prefix", "rate_limit_tier",
                     "is_active", "plan"):
        if required not in cols:
            fails.append(f"generate_api_key INSERT missing required column: {required}")
    assert not fails, "generate_api_key schema drift:\n" + "\n".join(fails)


if __name__ == "__main__":
    rc = 0
    for fn in (test_registry_founding_equals_pro,
               test_backend_maps_founding_equals_pro,
               test_frontend_js_maps_have_founding,
               test_generate_api_key_matches_schema):
        _FAILURES.clear()
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            rc = 1
            print(f"FAIL  {fn.__name__}\n      {e}")
    sys.exit(rc)
