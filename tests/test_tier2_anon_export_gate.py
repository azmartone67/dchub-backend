"""The Tier-2 report/export blueprint must not serve the registry to anonymous callers.

2026-09-06. Measured live on dchub.cloud BEFORE the fix, with no key, no
cookie and no header:

  GET /api/v1/mcp/tools/export_facility_csv?limit=10000
    -> HTTP 200, text/csv, 10,000 rows, 1,003,630 bytes, X-Limit-Applied: 10000
       4,641 rows carried coordinates (2,824 at >=6dp ~0.1m, 41 at 7dp),
       6,935 provider, 1,378 power_mw, 10,000 source.

  GET /api/v1/mcp/tools/create_site_report?facility_id=7539
    -> HTTP 200, {"tier": "pro", ...} and a retrievable report.

Both are the SAME table and the SAME fields that PR #2091/#2096 withhold from
anonymous callers on /api/v1/map, served at higher fidelity through a route
the 2026-08-01 anon-bulk audit never swept.

The whole "gate" was `limit = max(1, min(10000, int(args.get("limit", 100))))`
— a ceiling. The 100 rows a bare probe gets is the DEFAULT ARGUMENT, not a cap,
which is why this looked like a working free preview for four months. Any test
here that omits `?limit=` is testing the default and proves nothing; the probes
below all pass limit=10000 explicitly.

These are DISCRIMINATION tests: each asserts the anonymous/unverified caller is
REFUSED and that a verified one is SERVED. A gate that 401s everybody fails
these as loudly as one that 401s nobody.
"""
import ast
import os
import sys
from contextlib import contextmanager

import pytest
from flask import Flask

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import routes.mcp_tier2_reports as t2  # noqa: E402

CSV_PATH = "/api/v1/mcp/tools/export_facility_csv?limit=10000"
REPORT_PATH = "/api/v1/mcp/tools/create_site_report?facility_id=7539"

# The exact fields the anonymous dump was leaking; the CSV header must carry
# them, otherwise a positive control could pass against a stripped export and
# tell us nothing about the columns that actually mattered.
LEAKED_COLUMNS = ("latitude", "longitude", "provider", "power_mw", "source")


@pytest.fixture
def client():
    app = Flask(__name__)
    app.register_blueprint(t2.mcp_tier2_bp)
    app.config["TESTING"] = True
    return app.test_client()


def _as_tier(monkeypatch, tier):
    """Force the tier resolver's verdict, without touching a database."""
    import map_tier_gating
    monkeypatch.setattr(
        map_tier_gating, "detect_tier_for_data_gate",
        lambda **kw: (tier, {"source": "test"}), raising=True,
    )


@pytest.fixture
def verified(monkeypatch):
    """A real PAID caller — must be served."""
    _as_tier(monkeypatch, "pro")


@pytest.fixture
def free_key(monkeypatch):
    """A real but FREE account. Verified, yet below the gate.

    claim_free_key mints emailless dch_live_ keys self-serve, so if a free key
    cleared this gate the export would be one anonymous tool call away — a
    speed bump, not a gate. It would also contradict /ai/learn, which
    publishes the free tier as '3 results/basic fields'.
    """
    _as_tier(monkeypatch, "free")


def _fake_conn(rows):
    """Stand in for _conn() so the positive control never needs a database."""
    class _Cur:
        description = True

        def execute(self, *a, **k):
            pass

        def fetchall(self):
            return rows

        def fetchone(self):
            return rows[0] if rows else None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _C:
        def cursor(self, *a, **k):
            return _Cur()

    @contextmanager
    def _c():
        yield _C()

    return _c


# ─────────────────────────────────────────────────────────────────────────
# Layer 1 — the anonymous caller is refused
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [CSV_PATH, REPORT_PATH])
def test_anonymous_is_refused(client, path):
    r = client.get(path)
    assert r.status_code == 401, (
        f"{path} served HTTP {r.status_code} to a caller with no credential. "
        f"This is the measured 2026-09-06 exposure: 10,000 rows / 1,003,630 "
        f"bytes of the facility registry, free to anyone."
    )
    assert b"id,name,provider" not in r.data, "CSV body served to an anonymous caller"


@pytest.mark.parametrize("path", [CSV_PATH, REPORT_PATH])
def test_unverified_credential_is_refused(client, path):
    """`X-API-Key: x` is not evidence of an account (cf. #3512)."""
    r = client.get(path, headers={"X-API-Key": "x"})
    assert r.status_code == 401, (
        f"{path} accepted the one-character key `x`, which no account has ever "
        f"held. The gate is on detect_tier_failopen, not "
        f"detect_tier_for_data_gate."
    )


def test_refusal_names_the_tier_and_where_to_get_it(client):
    body = client.get(CSV_PATH).get_json()
    assert body.get("min_tier") == t2._MIN_TIER
    assert body.get("signup_url") and body.get("pricing_url")
    # Must NOT send an agent to claim_free_key: a free key does not clear
    # this gate, so that pointer would be a documented dead end.
    assert "claim_free_key" not in str(body), (
        "the 401 advertises a remedy that does not work at this min_tier"
    )


@pytest.mark.parametrize("path", [CSV_PATH, REPORT_PATH])
def test_verified_free_key_is_refused_with_402(client, free_key, path):
    r = client.get(path)
    assert r.status_code == 402, (
        f"{path} served a FREE-tier key HTTP {r.status_code}. claim_free_key "
        f"mints emailless keys self-serve, so this is the whole exposure again "
        f"with one extra call in front of it."
    )
    body = r.get_json()
    assert body.get("your_tier") == "free"
    assert body.get("upgrade_url"), "402 must say where to upgrade"


def test_free_key_refusal_is_not_confused_with_anonymous(client, free_key):
    """A signed-in free user must not be sent to the signup page they finished."""
    assert client.get(CSV_PATH).status_code == 402
    assert client.get(CSV_PATH).status_code != 401


# ─────────────────────────────────────────────────────────────────────────
# Layer 2 — the verified caller is still served (else the gate is just a wall)
# ─────────────────────────────────────────────────────────────────────────

def test_verified_caller_gets_the_csv(client, verified, monkeypatch):
    row = {"id": 1, "name": "X", "provider": "P", "city": "C", "state": "S",
           "country": "US", "latitude": 39.0, "longitude": -77.0,
           "power_mw": 10, "status": "operating", "source": "src"}
    monkeypatch.setattr(t2, "_conn", _fake_conn([row]), raising=True)
    r = client.get(CSV_PATH)
    assert r.status_code == 200, f"verified caller refused: {r.status_code}"
    assert "text/csv" in r.headers["Content-Type"]
    header_line = r.data.decode().splitlines()[0]
    for col in LEAKED_COLUMNS:
        assert col in header_line, f"paid export lost the {col} column"


def test_verified_caller_gets_a_report(client, verified, monkeypatch):
    monkeypatch.setattr(t2, "_get_facility",
                        lambda fid: {"name": "X", "city": "C", "state": "S",
                                     "status": "operating"}, raising=True)
    monkeypatch.setattr(t2, "_get_market_context",
                        lambda c, s: {}, raising=True)
    r = client.get(REPORT_PATH)
    assert r.status_code == 200, f"verified caller refused: {r.status_code}"
    assert r.get_json().get("report_id")


# ─────────────────────────────────────────────────────────────────────────
# Layer 3 — the gate runs before the database
# ─────────────────────────────────────────────────────────────────────────

def test_refusal_costs_no_database_connection(client, monkeypatch):
    """An anonymous flood must not be able to make us open connections."""
    def _boom():
        raise AssertionError("_conn() reached on an anonymous request — the "
                             "gate runs after the query")
    monkeypatch.setattr(t2, "_conn", _boom, raising=True)
    monkeypatch.setattr(t2, "_get_facility", lambda fid: _boom(), raising=True)
    assert client.get(CSV_PATH).status_code == 401
    assert client.get(REPORT_PATH).status_code == 401


# ─────────────────────────────────────────────────────────────────────────
# Layer 4 — wiring fence: never regress to the credential-presence fail-open
# ─────────────────────────────────────────────────────────────────────────

def _resolvers_referenced(tree):
    """Resolver names actually IMPORTED or CALLED — not merely mentioned.

    A substring scan cannot do this job here: the module's own comment
    explains why detect_tier_failopen is the wrong helper, so it names it.
    A guard that greps for the string fails on the prose that documents the
    fix (measured: it did, first run).
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
    return names


def test_module_uses_the_verified_resolver():
    tree = ast.parse(open(os.path.join(REPO, "routes/mcp_tier2_reports.py")).read())
    used = _resolvers_referenced(tree)
    assert "detect_tier_for_data_gate" in used, (
        "the blueprint no longer resolves a tier through the verified resolver"
    )
    assert "detect_tier_failopen" not in used, (
        "detect_tier_failopen returns 'pro' for any credential-shaped header; "
        "calling it here reopens the export to `X-API-Key: x`"
    )


@pytest.mark.parametrize("func", ["export_facility_csv", "create_site_report"])
def test_both_generating_routes_call_the_gate(func):
    """AST, not substring: the call must be INSIDE the view function."""
    tree = ast.parse(open(os.path.join(REPO, "routes/mcp_tier2_reports.py")).read())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == func), None)
    assert fn is not None, f"{func} disappeared from the blueprint"
    calls = [s.func.id for s in ast.walk(fn)
             if isinstance(s, ast.Call) and isinstance(s.func, ast.Name)]
    assert "_require_key" in calls, f"{func} no longer calls _require_key()"


# ─────────────────────────────────────────────────────────────────────────
# Layer 5 — the published copy must not re-acquire the false claim
# ─────────────────────────────────────────────────────────────────────────

def test_published_copy_does_not_claim_an_unenforced_tier():
    """llms.txt / llms-full.txt are generated here; both described this route
    as paid while it enforced nothing. Whatever they say must match the gate."""
    src = open(os.path.join(REPO, "ai_discovery_routes.py")).read()
    i = src.find("export_facility_csv?limit=")
    assert i != -1, "llms-full.txt no longer documents the bulk export"
    block = src[i:i + 400]
    assert "Auth: Pro or Enterprise" not in block, (
        "llms-full.txt claims 'Pro or Enterprise' for a route that any "
        "verified key clears — the claim that was false for four months"
    )
    assert "X-API-Key required" in block
    assert "Developer plan or higher" in block, (
        "llms-full.txt must name the tier the gate actually enforces "
        f"(_MIN_TIER={t2._MIN_TIER})"
    )

    j = src.find("[Bulk Export](")
    assert j != -1, "llms.txt no longer lists the bulk export"
    line = src[j:src.find("\n", j)]
    assert "tiered limits" not in line.lower(), (
        "llms.txt says 'tiered limits'; nothing in export_facility_csv reads "
        "a tier — that wording is what made the open route look intentional"
    )


def test_default_min_tier_is_the_advertised_paid_plan(monkeypatch):
    """The env var is a relief valve, not the policy. If the DEFAULT drifts
    back to 'free', both published claims (llms.txt 'Developer $49/mo' and
    /ai/learn 'free: 3 results/basic fields') silently become false again.

    monkeypatch.delenv, not os.environ.pop: the latter does not restore the
    variable, so a real TIER2_EXPORT_MIN_TIER in the environment would be
    silently destroyed for every test ordered after this one.
    """
    import importlib
    monkeypatch.delenv("TIER2_EXPORT_MIN_TIER", raising=False)
    m = importlib.reload(t2)
    try:
        assert m._MIN_TIER == "developer", (
            f"default min tier is {m._MIN_TIER!r}; a free key would then pull "
            f"all 10,000 rows"
        )
    finally:
        # Reload once more so later tests bind to a module built from the
        # real environment, not this test's stripped one.
        importlib.reload(t2)
