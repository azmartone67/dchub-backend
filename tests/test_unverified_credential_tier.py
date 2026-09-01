"""A syntactically-valid-but-NONEXISTENT credential must not resolve above anonymous.

2026-08-31. Two resolvers promoted callers on the mere PRESENCE of a
credential-shaped header. Measured live on dchub.cloud, before the fix:

  /api/v1/me/tier
    X-API-Key: <a real key>                            -> tier_index 1 (free)
    X-API-Key: dch_live_ffffffffffffffffffffffffffffffff -> tier_index 1 (free)
    X-API-Key: not_a_real_key_at_all                   -> tier_index 1 (free)
    X-API-Key: (empty) / no header                     -> tier_index 0

  /api/v1/map?all=true&limit=2000
    no header      -> _gated:true, _coord_precision_dp:2, 11 public fields
    X-API-Key: x   -> ungated, native 6dp coords, and power_mw (952/2000 rows),
                      provider (1953/2000), facility_type (1453/2000),
                      fiber_providers (1512/2000) — every field PR #2091/#2096
                      exists to withhold from anonymous callers

  /api/v1/search?q=data+center&limit=5000
    no header      -> 50 rows (the anonymous record_cap)
    X-API-Key: x   -> 5000 rows/page (TIER_LIMITS['pro']), offset-pageable

Two distinct causes, one pathology — "a header is present" treated as "the
caller is authenticated":

  1. routes.gating_routes._tier_from_api_key trusted
     mcp_upgrade_gate.validate_key_tier, whose DOCUMENTED default is "free"
     for an unknown key and for a DB error alike.
  2. map_tier_gating.detect_tier_failopen returns 'pro' whenever the tier
     resolves anonymous but has_auth_credential() is true — and that helper
     accepts ANY non-empty X-API-Key / ?api_key / `Authorization: Bearer …` /
     token cookie.

The tests below are DISCRIMINATION tests, not "returns anonymous" tests: each
asserts the unknown credential is refused AND that a genuine one still passes.
A resolver that always says anonymous fails these just as loudly as one that
always says pro — which is the point, because both were shipped at some stage.
"""
import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A key that is syntactically indistinguishable from a real self-serve key
# (dch_live_ + 32 hex) but has never existed in any table.
BOGUS_KEY = "dch_live_ffffffffffffffffffffffffffffffff"


class _FakeReq:
    """Minimal stand-in for a Flask request for has_auth_credential()."""

    def __init__(self, headers=None, args=None, cookies=None):
        self.headers = headers or {}
        self.args = args or {}
        self.cookies = cookies or {}


# ─────────────────────────────────────────────────────────────────────────
# Layer 1 — the /api/v1/me/tier oracle
# ─────────────────────────────────────────────────────────────────────────

def test_nonexistent_key_does_not_resolve_above_anonymous(monkeypatch):
    """The reported bug: an invented key resolved 'free' (tier_index 1)."""
    import mcp_upgrade_gate
    import routes.gating_routes as gr

    # validate_key_tier's REAL behaviour for an unknown key.
    monkeypatch.setattr(mcp_upgrade_gate, "validate_key_tier", lambda k="": "free")
    # The key exists in no table.
    monkeypatch.setattr(gr, "_api_key_is_known", lambda k: False)

    tier = gr._tier_from_api_key(BOGUS_KEY)
    assert tier == "anonymous", (
        f"a nonexistent key resolved {tier!r}; /api/v1/me/tier would report "
        f"tier_index {gr.TIER_INDEX.get(tier, 0)} for a key no account holds"
    )
    assert gr.TIER_INDEX.get(tier, 0) == 0


def test_real_free_key_still_resolves_free(monkeypatch):
    """The other half: the fix must not flatten genuine free keys to anonymous."""
    import mcp_upgrade_gate
    import routes.gating_routes as gr

    monkeypatch.setattr(mcp_upgrade_gate, "validate_key_tier", lambda k="": "free")
    monkeypatch.setattr(gr, "_api_key_is_known", lambda k: True)

    assert gr._tier_from_api_key("dch_live_a_real_free_key") == "free"


def test_paid_key_is_never_downgraded(monkeypatch):
    """A tier above 'free' can only come from a DB row, so it is trusted as-is."""
    import mcp_upgrade_gate
    import routes.gating_routes as gr

    monkeypatch.setattr(mcp_upgrade_gate, "validate_key_tier", lambda k="": "enterprise")
    # Even if the existence probe fails (DB down), a resolved paid tier stands.
    monkeypatch.setattr(gr, "_api_key_is_known", lambda k: False)

    assert gr._tier_from_api_key("dchub_enterprise_real") == "enterprise"


class _FakeCursor:
    """Returns one queued result per execute(), like the two-query probe."""

    def __init__(self, rows):
        self._rows = list(rows)
        self._last = None

    def execute(self, sql, params=None):
        self._last = self._rows.pop(0) if self._rows else None

    def fetchone(self):
        return self._last


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)

    def close(self):
        pass


def _install_fake_driver(monkeypatch, rows=(), raise_on_connect=False):
    """Stub `import psycopg` so the probe runs its real query path in-process."""
    import types

    mod = types.ModuleType("psycopg")

    def connect(dsn, *a, **k):
        if raise_on_connect:
            raise RuntimeError("db down")
        return _FakeConn(list(rows))

    mod.connect = connect
    monkeypatch.setitem(sys.modules, "psycopg", mod)
    monkeypatch.setenv("NEON_DATABASE_URL", "postgres://stub/stub")


def test_key_existence_probe_fails_closed_without_a_dsn(monkeypatch):
    """No DSN => 'not known', never 'known'."""
    import routes.gating_routes as gr

    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert gr._api_key_is_known(BOGUS_KEY) is False
    assert gr._api_key_is_known("") is False


def test_key_existence_probe_fails_closed_when_the_key_is_absent(monkeypatch):
    """The load-bearing case: DB reachable, key in NEITHER table => False.

    This drives the probe all the way to its terminal return. The earlier
    no-DSN test cannot: it returns from the `if not dsn` branch, so a
    terminal `return False` -> `return True` mutation survives it.
    """
    import routes.gating_routes as gr

    _install_fake_driver(monkeypatch, rows=[None, None])
    assert gr._api_key_is_known(BOGUS_KEY) is False


def test_key_existence_probe_fails_closed_when_the_db_errors(monkeypatch):
    """A DB outage must not be read as 'this key is real'."""
    import routes.gating_routes as gr

    _install_fake_driver(monkeypatch, raise_on_connect=True)
    assert gr._api_key_is_known(BOGUS_KEY) is False


@pytest.mark.parametrize(
    "rows,table",
    [([(1,), None], "mcp_dev_keys"), ([None, (1,)], "api_keys")],
    ids=["mcp_dev_keys-raw-column", "api_keys-dual-key_hash"],
)
def test_key_existence_probe_finds_a_real_key(monkeypatch, rows, table):
    """Discrimination: a key present in EITHER storage convention is known.

    Both are required — validate_key_tier matches the raw api_key column while
    the other resolvers match sha256(key); a probe that checked only one would
    report half the real keys as forgeries.
    """
    import routes.gating_routes as gr

    _install_fake_driver(monkeypatch, rows=rows)
    assert gr._api_key_is_known(BOGUS_KEY) is True, f"missed a key in {table}"


# ─────────────────────────────────────────────────────────────────────────
# Layer 2 — the data gates (map field mask / coord precision, search cap)
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "req",
    [
        _FakeReq(headers={"X-API-Key": "x"}),
        _FakeReq(headers={"X-API-Key": BOGUS_KEY}),
        _FakeReq(headers={"Authorization": "Bearer totally.fake.jwt"}),
        _FakeReq(args={"api_key": "x"}),
        _FakeReq(cookies={"dchub_token": "garbage"}),
    ],
    ids=["1char-key", "bogus-live-key", "junk-bearer", "query-param", "junk-cookie"],
)
def test_data_gate_refuses_unverified_credential(monkeypatch, req):
    """Every credential shape has_auth_credential() accepts must still be anon."""
    import map_tier_gating as m

    # What _detect_caller_tier really returns for a key in no table.
    monkeypatch.setattr(m, "_detect_caller_tier", lambda f=None: ("anonymous", None))

    tier, info = m.detect_tier_for_data_gate(req=req)
    assert tier == "anonymous", (
        f"unverified credential resolved {tier!r}; TIER_LIMITS would grant it "
        f"that tier's record_cap and the paid field mask"
    )
    assert (info or {}).get("source") == "unverified_credential_denied"


def test_data_gate_passes_through_a_genuinely_resolved_paid_tier(monkeypatch):
    """The fix must not re-open the 2026-06-20 incident (paid user downgraded)."""
    import map_tier_gating as m

    monkeypatch.setattr(
        m, "_detect_caller_tier", lambda f=None: ("pro", {"source": "api_key"})
    )
    tier, info = m.detect_tier_for_data_gate(
        req=_FakeReq(headers={"X-API-Key": "dchub_pro_real"})
    )
    assert tier == "pro"
    assert (info or {}).get("source") == "api_key"


def test_unverified_credential_gets_the_tightest_record_cap(monkeypatch):
    """Bind the resolver to the number it actually controls on /api/v1/search."""
    import map_tier_gating as m
    from tier_registry import TIER_LIMITS

    monkeypatch.setattr(m, "_detect_caller_tier", lambda f=None: ("anonymous", None))
    tier, _ = m.detect_tier_for_data_gate(req=_FakeReq(headers={"X-API-Key": "x"}))

    cap = int(TIER_LIMITS.get(tier, TIER_LIMITS["anonymous"])["record_cap"])
    assert cap == int(TIER_LIMITS["anonymous"]["record_cap"]), (
        f"an invented header buys record_cap {cap}"
    )
    assert cap < int(TIER_LIMITS["pro"]["record_cap"]), (
        "the measured bug: cap was TIER_LIMITS['pro'] = "
        f"{TIER_LIMITS['pro']['record_cap']} rows/page for `X-API-Key: x`"
    )


# ─────────────────────────────────────────────────────────────────────────
# Layer 3 — wiring fence: the data gates must not go back to the fail-open
# ─────────────────────────────────────────────────────────────────────────

# (file, enclosing function) for every gate that decides what DATA a caller sees.
DATA_GATES = [
    ("main.py", "api_v1_map"),
    ("main.py", "search_facilities"),
    ("power_plant_intel.py", "_pp_tier"),
    ("routes/infrastructure_data_routes.py", None),
]


def _functions_calling(tree, name):
    """Names of functions containing a call to `name` (or importing it)."""
    hits = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.ImportFrom) and any(
                a.name == name for a in sub.names
            ):
                hits.add(node.name)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                if sub.func.id == name:
                    hits.add(node.name)
    return hits


@pytest.mark.parametrize("path,func", DATA_GATES, ids=[f"{p}:{f}" for p, f in DATA_GATES])
def test_data_gate_uses_the_verified_resolver(path, func):
    src = open(os.path.join(REPO, path)).read()
    tree = ast.parse(src)

    failopen = _functions_calling(tree, "detect_tier_failopen")
    verified = _functions_calling(tree, "detect_tier_for_data_gate")

    if func is None:
        assert not failopen, (
            f"{path} calls detect_tier_failopen in {sorted(failopen)}; it promotes "
            f"any credential-shaped header to 'pro'"
        )
        assert verified, f"{path} no longer resolves a tier for its data gate"
    else:
        assert func not in failopen, (
            f"{path}:{func} calls detect_tier_failopen — that returns 'pro' for "
            f"`X-API-Key: x`, which is how the map field mask and the search "
            f"record cap were bypassed (measured live 2026-08-31)"
        )
        assert func in verified, (
            f"{path}:{func} must resolve its tier via detect_tier_for_data_gate"
        )


def test_failopen_still_documents_that_it_trusts_presence():
    """If someone makes detect_tier_failopen safe, this fence should be revisited."""
    src = open(os.path.join(REPO, "map_tier_gating.py")).read()
    assert "credentialed_failopen" in src, (
        "detect_tier_failopen no longer tags its unverified promotion; "
        "detect_tier_for_data_gate keys off that tag and would silently stop "
        "filtering"
    )
