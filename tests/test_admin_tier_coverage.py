"""
test_admin_tier_coverage.py — admin must be a first-class key on the
audited tier-dict containment surface.
(r-admin-row, 2026-07-30)

tier_registry.TIERS defines admin (rank 99, api_tier 'admin'), and every
tier dict audited by brain_consistency_radar's check_tier_dict_missing_keys
carried an explicit admin key EXCEPT paywall_middleware's TIER_HIERARCHY /
RATE_LIMITS / TIER_FEATURES. There, TIER_HIERARCHY.get('admin', 0) ranked
an admin caller as anonymous, RATE_LIMITS.get fell to 10 calls/day, and
TIER_FEATURES.get to {}. Nothing presents tier='admin' to those dicts
today — the module is unwired (nothing registers paywall_bp or calls its
init_app) and its own _get_user_tier() never returns 'admin' — but the
module sits on the audited containment surface precisely because this
repo's recurring bug class is "tier missing from a dict → .get() default
silently demotes" (identified at r32, founding at r43-H, starter at
r-starter-sweep). Admin was the last name missing from that surface.

This test locks the invariant three ways:

  1. tier_registry: admin strictly outranks every other tier, maps to
     api_tier 'admin', and stays excluded from paid_plans() (it is an
     operator credential, not a revenue cohort).
  2. Every dict on the radar's audit list carries an explicit 'admin'
     key. The list is AST-read from the radar source so this fence
     tracks the radar's surface instead of hand-copying it — a dict
     added to the radar without an admin row fails here first.
  3. paywall_middleware semantics (AST — that module imports stripe at
     module scope and the CI unit-tests venv has no stripe): admin tops
     TIER_HIERARCHY, admin RATE_LIMITS >= enterprise, and admin
     TIER_FEATURES grants every flag enterprise has.

Runnable two ways:  pytest tests/test_admin_tier_coverage.py
                    python3 tests/test_admin_tier_coverage.py
"""
import ast
import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_PAYWALL_DICTS = ('TIER_HIERARCHY', 'RATE_LIMITS', 'TIER_FEATURES')


def _radar_audit_list():
    """AST-extract TIER_DICTS_TO_CHECK from brain_consistency_radar so the
    fence follows the radar's audit surface automatically. Returns
    [(module_path, dict_attr), ...]."""
    path = os.path.join(ROOT, 'routes', 'brain_consistency_radar.py')
    tree = ast.parse(open(path, encoding='utf-8').read())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == 'TIER_DICTS_TO_CHECK'):
            entries = ast.literal_eval(node.value)
            # Vacuous-pass guard: the radar audits 16 dicts today. If this
            # ever shrinks below 10, either the radar was gutted or this
            # extraction broke — both must be looked at, not skipped.
            assert len(entries) >= 10, (
                f"radar TIER_DICTS_TO_CHECK parsed suspiciously small "
                f"({len(entries)} entries) — extraction or radar broken")
            return [(m, a) for m, a, *_ in entries]
    raise AssertionError(
        "TIER_DICTS_TO_CHECK not found in routes/brain_consistency_radar.py "
        "— renamed? Update this fence alongside the radar.")


def _paywall_dicts():
    """paywall_middleware's three tier dicts via AST, not import: the module
    imports stripe at module scope and the CI unit-tests venv deliberately
    does not install stripe (same approach as test_tier_consistency)."""
    src = open(os.path.join(ROOT, 'paywall_middleware.py'),
               encoding='utf-8').read()
    out = {}
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in _PAYWALL_DICTS
                and isinstance(node.value, ast.Dict)):
            out[node.targets[0].id] = ast.literal_eval(node.value)
    return out


# ── 1. Registry: admin is the strict top, an operator credential ────
def test_registry_admin_is_top_rank():
    import tier_registry as tr
    fails = []
    top_other = max(meta['rank'] for name, meta in tr.TIERS.items()
                    if name != 'admin')
    if 'admin' not in tr.TIERS:
        fails.append("tier_registry.TIERS: 'admin' missing entirely")
    else:
        if tr.rank('admin') <= top_other:
            fails.append(f"admin rank {tr.rank('admin')} does not strictly "
                         f"outrank every other tier (max other = {top_other})")
        if tr.api_tier('admin') != 'admin':
            fails.append(f"api_tier('admin') = {tr.api_tier('admin')!r} != 'admin'")
    if 'admin' in tr.paid_plans():
        fails.append("paid_plans() includes 'admin' — operator credential "
                     "counted as a revenue cohort")
    lim_admin = tr.TIER_LIMITS.get('admin', {})
    lim_ent = tr.TIER_LIMITS.get('enterprise', {})
    if lim_admin.get('rate_limit', -1) < lim_ent.get('rate_limit', 0):
        fails.append(f"registry admin rate_limit {lim_admin.get('rate_limit')!r} "
                     f"< enterprise {lim_ent.get('rate_limit')!r}")
    assert not fails, "tier_registry admin drift:\n" + "\n".join(fails)


# ── 2. Every audited tier dict carries an explicit admin key ────────
def test_audited_tier_dicts_carry_admin():
    fails = []
    paywall = _paywall_dicts()
    for modpath, attr in _radar_audit_list():
        if modpath == 'paywall_middleware':
            d = paywall.get(attr)
            if not isinstance(d, dict):
                fails.append(f"paywall_middleware.{attr}: dict literal not "
                             f"found by AST — renamed or made dynamic?")
                continue
        else:
            try:
                mod = importlib.import_module(modpath)
            except Exception as e:
                # An import failure means the guard cannot see its subject:
                # dead guard, CI must go red (same rule as
                # test_gating_maps_match_tier_registry).
                fails.append(f"{modpath}: import failed ({str(e)[:80]}) — "
                             f"this guard is dead, fix the import")
                continue
            parts = attr.split('.')
            d = getattr(mod, parts[0], None)
            for part in parts[1:]:
                d = d.get(part) if isinstance(d, dict) else None
            if not isinstance(d, dict):
                fails.append(f"{modpath}.{attr}: did not resolve to a dict")
                continue
        if 'admin' not in d:
            fails.append(f"{modpath}.{attr}: missing explicit 'admin' key — "
                         f"admin falls to the .get() default like a walk-in")
    assert not fails, "Audited tier dicts missing admin:\n" + "\n".join(fails)


# ── 3. paywall_middleware admin semantics (the r-admin-row rows) ────
def test_paywall_admin_semantics():
    d = _paywall_dicts()
    fails = []
    for name in _PAYWALL_DICTS:
        if name not in d:
            fails.append(f"paywall_middleware.{name}: dict literal not found by AST")
    if fails:
        raise AssertionError("\n".join(fails))

    hier = d['TIER_HIERARCHY']
    top_other = max(v for k, v in hier.items() if k != 'admin')
    if hier.get('admin', -1) <= top_other:
        fails.append(f"TIER_HIERARCHY['admin'] = {hier.get('admin')!r} does not "
                     f"strictly top the ladder (max other = {top_other})")

    rates = d['RATE_LIMITS']
    if rates.get('admin', -1) < rates.get('enterprise', 0):
        fails.append(f"RATE_LIMITS['admin'] = {rates.get('admin')!r} "
                     f"< enterprise {rates.get('enterprise')!r}")

    feats = d['TIER_FEATURES']
    admin_flags = feats.get('admin', {})
    for flag, granted in feats.get('enterprise', {}).items():
        if granted and not admin_flags.get(flag):
            fails.append(f"TIER_FEATURES['admin'][{flag!r}] = "
                         f"{admin_flags.get(flag)!r} but enterprise has it True")
    if not admin_flags:
        fails.append("TIER_FEATURES['admin'] missing or empty")

    assert not fails, "paywall_middleware admin semantics drift:\n" + "\n".join(fails)


if __name__ == "__main__":
    rc = 0
    for fn in (test_registry_admin_is_top_rank,
               test_audited_tier_dicts_carry_admin,
               test_paywall_admin_semantics):
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            rc = 1
            print(f"FAIL  {fn.__name__}\n      {e}")
    sys.exit(rc)
