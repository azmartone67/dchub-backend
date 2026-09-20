"""The curated /openapi.json is a PRODUCT, not documentation.

Third-party catalogues (AnythingMCP and the like) generate a connector straight
from this spec: each operation's `description` becomes the tool description an
agent picks by, and each `security` block decides whether the generated tool
even asks for a key. A wrong or missing field here ships a broken tool into
someone else's catalogue, where we cannot fix it.

The GATED map below is MEASURED against live dchub.cloud, not derived from the
spec — that is the whole point, so the spec cannot vouch for itself. Re-measure
with:

    curl -s -o /dev/null -w '%{http_code}\n' "https://dchub.cloud<path>?_=$(date +%s)"

and update the map if a gate genuinely moves.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "ai_discovery_routes.py"

# path -> status code returned COLD, unauthenticated. Measured 2026-09-19.
#
# getFacilityDetail is deliberately NOT here. The spec declared it keyed with a
# 401, an earlier revision of this map copied that 401 across, and the test then
# passed by agreeing with the artefact it was supposed to be checking. Measured,
# it answers 200 cold with no key. A map that reads the spec is not a check.
GATED = {
    "/api/v1/pipeline": "403",
    "/api/site-score": "402",
    "/api/grid/fuel-mix": "403",
    "/api/energy/prices/{state}": "403",
}

# Endpoints that answer 200 COLD, unauthenticated. Measured 2026-09-19.
UNGATED = [
    "/api/v1/stats",
    "/api/v1/facilities",
    "/api/v1/markets",
]


class _Placeholder(ast.NodeTransformer):
    """canon_text(...) and _ver resolve at request time; stand them in so the
    literal structure of the dict can be read without importing Flask."""

    def visit_Call(self, node):
        return ast.Constant(value="<call>")

    def visit_Name(self, node):
        return ast.Constant(value="<name>")


@pytest.fixture(scope="module")
def spec():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    node = None
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name == "serve_openapi_json":
            for st in ast.walk(fn):
                if isinstance(st, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "spec" for t in st.targets
                ):
                    node = st.value
    assert node is not None, "no `spec = {...}` in serve_openapi_json"
    return ast.literal_eval(ast.fix_missing_locations(_Placeholder().visit(node)))


def _op(spec, path):
    return next(v for k, v in spec["paths"][path].items() if k in ("get", "post"))


@pytest.mark.parametrize("path,code", sorted(GATED.items()))
def test_gated_endpoints_declare_their_gate(spec, path, code):
    """An endpoint that 402/403s cold must say so, or the generated tool
    presents as free and fails for every caller."""
    assert path in spec["paths"], f"{path} missing from the curated spec"
    op = _op(spec, path)
    assert "security" in op, f"{path} answers {code} cold but declares no security"
    assert code in op["responses"], f"{path} does not document its {code}"
    assert op.get("tags") == ["Pro"], f"{path} is gated but tagged {op.get('tags')}"


def test_every_operation_has_a_description(spec):
    """Agents pick tools by description. An empty one is a dead tool."""
    missing = [
        f"{m.upper()} {p}"
        for p, ops in spec["paths"].items()
        for m, op in ops.items()
        if m in ("get", "post") and not (op.get("description") or "").strip()
    ]
    assert not missing, f"operations with no description: {missing}"


def test_free_key_door_is_reachable_from_the_spec(spec):
    """The claim endpoint is what makes DC Hub demonstrable without an account.
    Absent from the spec, a generated connector has no way to obtain a key."""
    assert "/api/v1/keys/claim" in spec["paths"]
    desc = _op(spec, "/api/v1/keys/claim").get("description", "")
    assert "client_name" in desc, (
        "the claim description must explain the (client_name, IP) idempotency — "
        "a constant client_name behind a shared egress collapses every caller "
        "onto one key and one quota"
    )


def test_auth_hint_points_at_the_free_key_not_only_the_paywall(spec):
    desc = spec["components"]["securitySchemes"]["apiKey"].get("description", "")
    assert "keys/claim" in desc, "auth hint sends users to /pricing with no free path"


def test_required_query_params_are_marked_required(spec):
    """compareMarkets answers 400 with no `markets` — an importer that generates
    a no-argument call gets a tool that always fails."""
    params = _op(spec, "/api/v1/markets/compare").get("parameters", [])
    markets = [p for p in params if p["name"] == "markets"]
    assert markets and markets[0].get("required"), "`markets` must be required"


@pytest.mark.parametrize("path", UNGATED)
def test_ungated_endpoints_do_not_claim_a_gate(spec, path):
    """The mirror of the gated case. Declaring auth on an endpoint that answers
    cold costs the one property that makes DC Hub demonstrable without an
    account: an importer generates a tool that demands a key nobody needs."""
    assert path in spec["paths"], f"{path} missing from the curated spec"
    op = _op(spec, path)
    assert "security" not in op, f"{path} answers 200 cold but declares security"
    assert "401" not in op.get("responses", {}), f"{path} documents a 401 it never sends"


def test_facility_detail_is_published_with_optional_auth(spec):
    """Republished after #4862 gated the branch that leaked it.

    Measured live 2026-09-20, cache-busted:

        anonymous   -> 200, 8 basic fields, _upgrade present
        unknown id  -> 404

    Two things this pins. The BARE path, because OpenAPI templating never
    appends a trailing slash and the bare form is what a generated client
    calls. And OPTIONAL auth — `security` must offer the empty alternative
    alongside apiKey, or a generated client refuses to call without
    credentials and the endpoint's whole free preview becomes unreachable
    from the catalogue.
    """
    p = "/api/v1/facilities/{facility_id}"
    assert p in spec["paths"], "getFacilityDetail is not published"
    assert p + "/" not in spec["paths"], "publish the bare path, not the slashed one"
    op = spec["paths"][p]["get"]
    sec = op.get("security")
    assert sec is not None, "optional auth must be explicit, not inherited"
    assert {} in sec, (
        "security must include the empty alternative — without it the "
        "generated client treats a key as required and never makes the "
        "anonymous call the endpoint supports"
    )
    assert any("apiKey" in alt for alt in sec if alt), "the keyed alternative is missing"
    assert "404" in op["responses"], "the unknown-id 404 is undocumented"
    desc = op["description"].lower()
    # The fact an integrator loses a day to: claiming the free key advertised
    # everywhere else does NOT widen this endpoint.
    assert "free claimed key does not lift" in desc, (
        "the description must say a free key does not lift this endpoint — "
        "otherwise an integrator claims one and cannot explain the preview"
    )
    assert "branch on `_upgrade`" in desc, (
        "the description must name the field to branch on, not just mention it"
    )


def test_claim_describes_the_ip_metered_bind_gate(spec):
    """Measured 2026-09-19: a fresh claim can return a key that is ALREADY
    gated — bind_required / gate='bind_email_required' — because the free
    unbound-call allowance meters on source IP, carries across re-mints, and a
    new client_name does not reset it.

    A hosted integration proxying many end users through one egress IP is the
    exact shape that trips this, and it is the shape that generates connectors
    from this spec. If the description omits it, the integrator reads
    "free key, one POST" and ships something that works for the first caller
    and demands an email from everyone after.

    Each fact below is asserted on its own. An earlier revision used `or`
    between two spellings of the same fact, so a mutation deleting one half
    passed on the other — three of four mutations went undetected.
    """
    op = _op(spec, "/api/v1/keys/claim")
    desc = op["description"].lower()
    body_200 = op["responses"]["200"]["description"].lower()

    # The allowance is metered on IP, not on the caller's chosen name.
    assert "metered per source ip" in desc, (
        "claim description must say the allowance meters per SOURCE IP"
    )
    # A new client_name does not buy a new allowance.
    assert "does not reset it" in desc, (
        "claim description must say a re-mint does not reset the counter"
    )
    # Hosted, many-users-one-egress integrations need the explicit instruction.
    assert "bind an email per end user" in desc, (
        "claim description must tell shared-egress integrators to bind per user"
    )
    # A 200 is not proof the key is usable.
    assert "bind_required" in body_200, "200 must mention bind_required"
    assert "bind_email_required" in body_200, "200 must name the gate value"
    # daily_calls is on the reuse path only.
    assert "daily_calls" not in body_200, (
        "daily_calls is returned on the reuse path only — do not promise it "
        "on a fresh mint"
    )
