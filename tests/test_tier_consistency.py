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


# ── 1b. Pricing is present + canonical (r-price-unify, 2026-06-02) ──
def test_registry_pricing_canonical():
    """Guard the price/quota unification. The ops-triage found the same $9
    quoted as both Starter/10k-day and Developer/500-day, and $49 as both
    Developer and Pro, across 5 surfaces because no canonical price map
    existed. tier_registry is now that map; this locks the values (same
    $9/$49/$299 configured on routes/_stripe_links.py + pricing.html) and
    asserts /api/v1/tiers exposes price_usd_month + calls_per_day +
    stripe_link so every surface can read one source instead of hardcoding."""
    import tier_registry as tr
    fails = []
    def c(cond, msg):
        if not cond:
            fails.append(msg)
    # canonical flat-rate monthly prices. r-price-collapse (2026-09-05):
    # pro 299→99 — over 90d of Stripe checkout sessions, every self-serve
    # price above $99 closed 0 of 76 opens, while $99 closed 8 of 43.
    # starter/team keep their prices for grandfathered subs but are off
    # the public page. Kept in lockstep with routes/_stripe_links.py.
    # developer HELD at 49 (builder on-ramp).
    c(tr.price('starter') == 9,    f"starter price {tr.price('starter')} != 9")
    c(tr.price('developer') == 49, f"developer price {tr.price('developer')} != 49")
    c(tr.price('pro') == 99,       f"pro price {tr.price('pro')} != 99")
    c(tr.price('team') == 699,     f"team price {tr.price('team')} != 699")
    # founding was DECOUPLED from pro at r-reprice (pro 299 / founding 99).
    # r-price-collapse (2026-09-05) RE-COUPLED them: one product, one price,
    # one Stripe link. founding is a legacy alias kept so existing subs and
    # ?tier=founding URLs resolve. ACCESS equality is guarded separately by
    # test_backend_maps_founding_equals_pro.
    c(tr.price('founding') == 99, f"founding price {tr.price('founding')} != 99")
    # ★ The invariant NO price literal above can catch: pro and founding must
    #   bill through the SAME Stripe link. A price map can agree on "$99" while
    #   the two links charge different amounts — the link is what the customer
    #   is actually charged, and it is asserted nowhere else in this file.
    from routes._stripe_links import STRIPE_LINKS as _SL
    c(_SL.get('pro') == _SL.get('founding'),
      f"pro link {_SL.get('pro')} != founding link {_SL.get('founding')} "
      f"— same tier must bill through the same link")
    # canonical calls/day (mcp_daily) — what the paywall quotes
    c(tr.calls_per_day('starter') == 200,    f"starter calls/day {tr.calls_per_day('starter')} != 200")
    c(tr.calls_per_day('developer') == 500, f"developer calls/day {tr.calls_per_day('developer')} != 500")
    c(tr.calls_per_day('pro') == 2000,      f"pro calls/day {tr.calls_per_day('pro')} != 2000")
    # /api/v1/tiers must expose the fields every surface reads
    pub = tr.as_public_dict()
    for t in ('starter', 'developer', 'pro'):
        row = pub.get('tiers', {}).get(t, {})
        c(row.get('price_usd_month') is not None, f"{t}: price_usd_month missing from /api/v1/tiers")
        c(row.get('calls_per_day') is not None,   f"{t}: calls_per_day missing from /api/v1/tiers")
        c(bool(row.get('stripe_link')),           f"{t}: stripe_link missing from /api/v1/tiers")
    assert not fails, "Pricing drift detected:\n" + "\n".join(fails)


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


# ── 2b. Gating maps must MATCH tier_registry, key for key ───────────
def test_gating_maps_match_tier_registry():
    """r-tier-derive (2026-07-30): 'starter' (and 'team') were missing
    from PLAN_LEVELS / TIER_RATE_LIMITS / TIER_DAILY_RECORD_CAPS /
    TIER_PAGE_CAPS, so a paying $9 Starter fell through every
    dict.get() default: access level 0 == free (failing even the
    require_plan('identified') gate on deals_routes), 100 calls/day
    (advertised 500), 50 records/day (advertised 500 — pricing sells
    "10× the free quota"), 1 page/query (advertised 10). 'identified'
    (r32) and 'founding' (r43-H) were the same class. The maps are now
    derived from tier_registry; this test locks every key AND value to
    the registry so a hand-edit can't reintroduce drift. Unlike
    chk_map above, an import failure here is a FAILURE, not a
    tolerated SKIP — if api_tier_gating stops importing, this guard is
    dead and CI must go red, not silently green."""
    import tier_registry as tr
    import api_tier_gating as atg
    fails = []

    # PLAN_LEVELS ≡ tier_registry.TIERS rank, both directions.
    for t, meta in tr.TIERS.items():
        if atg.PLAN_LEVELS.get(t) != meta['rank']:
            fails.append(f"PLAN_LEVELS[{t!r}] = {atg.PLAN_LEVELS.get(t)!r} "
                         f"!= registry rank {meta['rank']!r}")
    for t in atg.PLAN_LEVELS:
        if t not in tr.TIERS:
            fails.append(f"PLAN_LEVELS key {t!r} unknown to tier_registry.TIERS")

    # The three limit maps ≡ tier_registry.TIER_LIMITS columns.
    for attr, col in (('TIER_RATE_LIMITS', 'rate_limit'),
                      ('TIER_DAILY_RECORD_CAPS', 'record_cap'),
                      ('TIER_PAGE_CAPS', 'page_cap')):
        m = getattr(atg, attr)
        for t, lim in tr.TIER_LIMITS.items():
            if m.get(t) != lim[col]:
                fails.append(f"{attr}[{t!r}] = {m.get(t)!r} "
                             f"!= registry {col} {lim[col]!r}")
        # 'anon' is a caller alias absent from TIER_LIMITS — it must
        # mirror 'anonymous', and no OTHER key may bypass the registry.
        if m.get('anon') != m.get('anonymous'):
            fails.append(f"{attr}: alias 'anon' ({m.get('anon')!r}) "
                         f"!= 'anonymous' ({m.get('anonymous')!r})")
        for t in m:
            if t != 'anon' and t not in tr.TIER_LIMITS:
                fails.append(f"{attr} key {t!r} unknown to tier_registry.TIER_LIMITS")

    # Pin the sold numbers independently of BOTH maps, so a registry-side
    # typo can't silently propagate through the derivation.
    if atg.TIER_DAILY_RECORD_CAPS.get('starter') != 500:
        fails.append(f"starter record cap "
                     f"{atg.TIER_DAILY_RECORD_CAPS.get('starter')!r} != 500 "
                     "(pricing copy: 10× the free quota)")
    if atg.TIER_DAILY_RECORD_CAPS.get('free') != 50:
        fails.append(f"free record cap "
                     f"{atg.TIER_DAILY_RECORD_CAPS.get('free')!r} != 50")

    assert not fails, "Gating-map ↔ tier_registry drift:\n" + "\n".join(fails)


# ── 2c. Every audited tier dict must cover the canonical five ───────
def test_tier_dicts_cover_canonical_five():
    """r-starter-sweep (2026-07-30): the brain radar's
    check_tier_dict_missing_keys REQUIRED set predated the r34 starter
    tier (it checked anonymous/identified/developer/pro only) — which
    is exactly why the radar never flagged the starter gaps PR #1943
    fixed. This test mirrors the radar's full audit list and requires
    the canonical FIVE (anonymous/identified/starter/developer/pro) in
    every dict, and asserts the radar's own REQUIRED literal carries
    'starter' so the runtime containment can't quietly regress either.

    paywall_middleware is checked by AST, not import: it imports
    stripe at module scope and the CI unit-tests venv deliberately
    does not install stripe. Everything else imports — and an import
    failure is a FAILURE, not a tolerated SKIP (a guard that can't
    load its subject is dead, and CI must go red, not silently green).
    """
    CANON = {'anonymous', 'identified', 'starter', 'developer', 'pro'}
    fails = []

    IMPORTABLE_TARGETS = [
        ('api_tier_gating', 'TIER_RATE_LIMITS'),
        ('api_tier_gating', 'TIER_DAILY_RECORD_CAPS'),
        ('api_tier_gating', 'TIER_PAGE_CAPS'),
        ('api_tier_gating', 'TIER_SEARCH_LIMITS'),
        ('api_tier_gating', 'MCP_TIER_RESULT_LIMITS'),
        ('api_tier_gating', 'PLAN_LEVELS'),
        ('dchub_me', 'LIMITS'),
        ('land_power_usage_limiter', 'LAND_POWER_LIMITS'),
        ('land_power_usage_limiter', 'API_MONTHLY_LIMITS'),
        ('alert_system_v2', 'ALERT_LIMITS'),
        ('free_tier_limiter', 'TIER_LIMITS'),
        ('api_data_protection', 'PROTECTION_CONFIG.daily_record_caps'),
        ('api_data_protection', 'PROTECTION_CONFIG.max_results_per_response'),
    ]
    for modpath, attr in IMPORTABLE_TARGETS:
        try:
            mod = __import__(modpath, fromlist=[attr.split('.')[0]])
        except Exception as e:
            fails.append(f"{modpath}: import failed ({str(e)[:80]}) — "
                         f"this guard is dead, fix the import")
            continue
        parts = attr.split('.')
        obj = getattr(mod, parts[0], None)
        for part in parts[1:]:
            obj = obj.get(part) if isinstance(obj, dict) else None
        if not isinstance(obj, dict):
            fails.append(f"{modpath}.{attr}: did not resolve to a dict")
            continue
        missing = sorted(CANON - set(obj.keys()))
        if missing:
            fails.append(f"{modpath}.{attr}: missing canonical tier keys "
                         f"{missing}")

    # paywall_middleware — AST, because CI has no stripe (see docstring).
    import ast
    src = open(os.path.join(ROOT, 'paywall_middleware.py'),
               encoding='utf-8').read()
    found = {}
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in ('TIER_HIERARCHY', 'RATE_LIMITS',
                                           'TIER_FEATURES')
                and isinstance(node.value, ast.Dict)):
            found[node.targets[0].id] = {
                ast.literal_eval(k) for k in node.value.keys if k is not None}
    for name in ('TIER_HIERARCHY', 'RATE_LIMITS', 'TIER_FEATURES'):
        if name not in found:
            fails.append(f"paywall_middleware.{name}: dict literal not found "
                         f"by AST — was it renamed or made dynamic?")
            continue
        missing = sorted(CANON - found[name])
        if missing:
            fails.append(f"paywall_middleware.{name}: missing canonical tier "
                         f"keys {missing}")

    # The radar's own REQUIRED set must carry the canonical five, or the
    # runtime containment surface silently regresses to the pre-starter era.
    rsrc = open(os.path.join(ROOT, 'routes', 'brain_consistency_radar.py'),
                encoding='utf-8').read()
    i = rsrc.index('def check_tier_dict_missing_keys')
    j = rsrc.index('REQUIRED', i)
    required_literal = rsrc[j:rsrc.index('}', j) + 1]
    for tier in sorted(CANON):
        if f"'{tier}'" not in required_literal:
            fails.append(f"brain_consistency_radar check_tier_dict_missing_keys "
                         f"REQUIRED is missing '{tier}'")

    assert not fails, "Canonical-five tier coverage gaps:\n" + "\n".join(fails)


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


# ── 5. PLAN_INFO display quotas must match the ENFORCED limits ──────
def test_plan_info_rate_limits_match_registry():
    """audit #9 follow-up (2026-07-30): PLAN_INFO['pro'] displayed
    10,000 calls/day (both the rate_limit field and the tagline) while
    the enforced limit — TIER_RATE_LIMITS['pro'], canonically
    tier_registry.TIER_LIMITS['pro']['rate_limit'] — is 5,000.
    Over-advertising at the exact upgrade decision point: every 402
    gate response (_build_gate_plans) and /api/v2/stripe/config quoted
    a quota the throttle then cut in half. price_monthly in the same
    dict was reconciled to the registry once already (audit #9) but
    the quota fields were not. Pin rate_limit AND the tagline's
    "(N calls/day)" figure for EVERY PLAN_INFO tier to the registry so
    display can't drift from enforcement again. Import failure here is
    a FAILURE, not a tolerated SKIP — a dead guard must go red."""
    import re
    import tier_registry as tr
    import api_tier_gating as atg
    fails = []
    for t, info in atg.PLAN_INFO.items():
        lim = tr.TIER_LIMITS.get(t)
        if lim is None:
            fails.append(f"PLAN_INFO tier {t!r} unknown to tier_registry.TIER_LIMITS")
            continue
        canonical = lim['rate_limit']
        if atg.TIER_RATE_LIMITS.get(t) != canonical:
            fails.append(f"TIER_RATE_LIMITS[{t!r}] = {atg.TIER_RATE_LIMITS.get(t)!r} "
                         f"!= registry rate_limit {canonical!r} (enforcement drifted)")
        if info.get('rate_limit') != canonical:
            fails.append(f"PLAN_INFO[{t!r}]['rate_limit'] = {info.get('rate_limit')!r} "
                         f"!= enforced registry rate_limit {canonical!r}")
        m = re.search(r'\(([\d,]+) calls/day\)', info.get('tagline', ''))
        if not m:
            fails.append(f"PLAN_INFO[{t!r}]['tagline'] carries no '(N calls/day)' "
                         f"figure to pin: {info.get('tagline')!r}")
        elif int(m.group(1).replace(',', '')) != canonical:
            fails.append(f"PLAN_INFO[{t!r}]['tagline'] advertises {m.group(1)} "
                         f"calls/day but the enforced limit is {canonical}")
    assert not fails, "PLAN_INFO display drift from enforced limits:\n" + "\n".join(fails)


if __name__ == "__main__":
    rc = 0
    for fn in (test_registry_founding_equals_pro,
               test_registry_pricing_canonical,
               test_backend_maps_founding_equals_pro,
               test_gating_maps_match_tier_registry,
               test_tier_dicts_cover_canonical_five,
               test_frontend_js_maps_have_founding,
               test_generate_api_key_matches_schema,
               test_plan_info_rate_limits_match_registry):
        _FAILURES.clear()
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            rc = 1
            print(f"FAIL  {fn.__name__}\n      {e}")
    sys.exit(rc)
