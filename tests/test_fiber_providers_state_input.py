"""tests/test_fiber_providers_state_input.py — ?state=TX must not silently
return zero providers (2026-08-13).

Measured live before the fix:

    /api/v1/fiber/providers?state=TX  ->  {"ok": true, "providers": []}
    /api/v1/fiber/providers?state=48  ->  100 providers
    ?state=CA -> 0   ?state=06 -> 85
    ?state=VA -> 0   ?state=51 -> 63

`fcc_fiber_hex.state_fips` holds FIPS codes and the query parameter went in raw,
so every human-shaped input matched nothing. The response was `ok: true` with an
empty list — SUCCESS, EMPTY — so a caller reads "no fiber providers in Texas"
and has no way to distinguish that from "you used the wrong format". Everyone
reaching for this endpoint types TX, not 48.

★The 200-with-empty-list degradation is DELIBERATE and must stay: the
land-power map polls this endpoint in a viewport-refresh loop, and a non-200
filled the browser console with errors (r48.3, 2026-06-05). So the fix cannot be
"return 400". It is: normalise the input, and when a token cannot be resolved
say so EXPLICITLY in the body rather than returning a bare empty list.

House rules: no DB, no network, never import main.py. The unrecognised-token
path returns before any query, so it is reachable with no database at all.

Run:  python3 -m pytest tests/test_fiber_providers_state_input.py -v
"""
from __future__ import annotations

import pathlib

import pytest
from flask import Flask

from routes.fcc_bdc_fiber import fcc_bdc_fiber_bp
from util.state_codes import VALID_FIPS, to_fips

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "routes" / "fcc_bdc_fiber.py").read_text(encoding="utf-8",
                                                       errors="replace")


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(fcc_bdc_fiber_bp)
    return app.test_client()


# ─── to_fips ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("token,expected", [
    ("TX", "48"), ("tx", "48"), (" Tx ", "48"),      # the reported bug
    ("CA", "06"), ("VA", "51"),                       # the other two measured
    ("48", "48"), ("06", "06"), ("6", "06"), (6, "06"),
    ("Texas", "48"), ("new york", "36"),
    ("DC", "11"), ("PR", "72"),                       # both are in the loader
])
def test_to_fips_resolves_every_shape_a_caller_might_send(token, expected):
    assert to_fips(token) == expected


@pytest.mark.parametrize("token", [None, "", "   ", "ZZ", "999", "00",
                                   "Westeros", "3", "0"])
def test_to_fips_returns_none_for_unresolvable(token):
    """★None means UNRECOGNISED. It must never be confused with 'no rows' —
    conflating those two is the entire defect."""
    assert to_fips(token) is None


def test_every_mapped_value_is_a_real_fips_code():
    for tok in ("AL", "WY", "DC", "PR", "TX"):
        assert to_fips(tok) in VALID_FIPS


def test_the_two_measured_pairs_agree():
    """The live symptom, as a test: the abbreviation and the code a caller
    could have used instead must resolve identically."""
    assert to_fips("TX") == to_fips("48")
    assert to_fips("CA") == to_fips("06")
    assert to_fips("VA") == to_fips("51")


# ─── the endpoint ────────────────────────────────────────────────────────

def test_unrecognised_state_is_labelled_not_silently_empty(client):
    """The core regression. An unresolvable token must SAY so."""
    r = client.get("/api/v1/fiber/providers?state=Westeros")
    body = r.get_json()
    assert body["state_unrecognized"] is True
    assert body["providers"] == []
    assert "Westeros" in body["error"]
    assert body["state_input"] == "Westeros"
    assert body["state_fips"] is None
    # The error must tell the caller what IS accepted, not merely that they
    # were wrong.
    assert "TX" in body["error"] and "48" in body["error"]


def test_unrecognised_state_still_returns_200_and_ok(client):
    """★The map contract. r48.3 made this endpoint degrade to 200 + empty on
    failure because the land-power map polls it in a viewport loop; a non-200
    floods the console. Fixing the silent-empty bug must NOT reintroduce that."""
    r = client.get("/api/v1/fiber/providers?state=Westeros")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_a_recognised_state_is_not_flagged_unrecognised(client):
    """Guard against over-correcting into flagging everything."""
    body = client.get("/api/v1/fiber/providers?state=TX").get_json()
    assert not body.get("state_unrecognized")
    assert body.get("state_fips") == "48"
    assert body.get("state_input") == "TX"


def test_response_echoes_input_alongside_resolved_code(client):
    """A caller must be able to SEE the normalisation rather than infer it from
    the size of the result."""
    body = client.get("/api/v1/fiber/providers?state=texas").get_json()
    assert body["state_input"] == "texas"
    assert body["state_fips"] == "48"


def test_bbox_requests_are_untouched_by_the_state_path(client):
    """The map's viewport mode passes no state at all — it must not start
    getting an unrecognised-state error."""
    body = client.get(
        "/api/v1/fiber/providers?bbox=-97.5,32.5,-96.5,33.0").get_json()
    assert body["ok"] is True
    assert not body.get("state_unrecognized")
    assert "bbox" in body


# ─── the endpoint must actually use the normaliser ───────────────────────

def test_endpoint_normalises_rather_than_querying_the_raw_token():
    assert "from util.state_codes import to_fips" in SRC
    assert "to_fips(_state_in)" in SRC, (
        "list_providers must resolve the token before querying; passing the raw "
        "value into WHERE state_fips=%s is the original bug")


def _list_providers_source():
    """Just the body of list_providers.

    ★Scoped deliberately: `WHERE state_fips=%s` appears in several OTHER
    functions in this module (the loader, the deleter, the footprint query), so
    a whole-file offset comparison compares the guard against an unrelated
    function and is meaningless. That mistake is what this helper exists to
    prevent recurring.
    """
    import ast
    tree = ast.parse(SRC)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "list_providers"),
              None)
    assert fn is not None, "list_providers not found"
    lines = SRC.splitlines()
    return "\n".join(lines[fn.lineno - 1:fn.end_lineno])


def test_unrecognised_token_short_circuits_before_the_query():
    """If the early return were removed, an unresolved token would fall through
    to `WHERE state_fips=NULL` and return an empty list again — silent, and
    indistinguishable from the bug we just fixed."""
    body = _list_providers_source()
    assert "state_unrecognized" in body
    i_guard = body.find("state_unrecognized")
    i_query = body.find("FROM fcc_fiber_hex WHERE state_fips=%s")
    assert i_query > 0, "the state query moved — update this guard"
    assert i_guard < i_query, (
        "the unrecognised-state guard must precede the query, or it cannot "
        "prevent the silent-empty result")
