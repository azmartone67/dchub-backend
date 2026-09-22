"""/llms.txt names /api/v1/whats-new as the machine-readable changelog
(owner directive, 2026-09-21 PM).

WHY. Agents that do not speak MCP read /llms.txt to learn which URLs to GET.
/whats-new now serves its headline counts in its HTML, and /api/v1/whats-new is
the same data as JSON (counts + as_of + recent items). If the index agents read
first never names that URL, an agent has to scrape the HTML page for what the
JSON already states. One line, in the keyless list, where an agent looks for
URLs it may call without a key.

Renders the REAL route on a bare Flask app (no network, no DB), the same way
tests/test_free_api_list_is_a_tier_claim.py does.
"""
import pytest

DOOR = "https://dchub.cloud/api/v1/whats-new"


@pytest.fixture(scope="module")
def llms_txt():
    flask = pytest.importorskip("flask")
    from ai_discovery_routes import register_discovery_routes
    app = flask.Flask(__name__)
    register_discovery_routes(app)
    r = app.test_client().get("/llms.txt")
    assert r.status_code == 200
    return r.get_data(as_text=True)


def _free_block(text):
    start = text.find("## FREE API")
    end = text.find("## KEY REQUIRED", start)
    assert start != -1 and end != -1, "the FREE API block moved — this test would scan nothing"
    return text[start:end]


def test_llms_names_the_whats_new_machine_door_once_in_the_keyless_list(llms_txt):
    lines = [l for l in _free_block(llms_txt).split("\n") if f"({DOOR})" in l]
    assert len(lines) == 1, (
        f"/llms.txt's keyless list names {DOOR} {len(lines)} times; want exactly one line")
    line = lines[0]
    assert "machine-readable changelog" in line
    for field in ("as_of", "counts"):
        assert field in line, f"the line does not say the JSON carries {field!r}"
