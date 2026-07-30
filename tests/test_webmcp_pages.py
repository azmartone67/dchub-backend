"""WebMCP page-instrumentation tests (2026-07-18, webmcp-proto).

All mocked (no DB, no network, never imports main). Contract under test:
  1. routes/_webmcp helper — env-token gate (unset ⇒ identity/empty),
     meta tag shape, feature-detected registration script, full-document
     vs head-less-fragment injection, idempotency, </script> safety
  2. the three instrumented pages — /radar, /phx, /integrations/mcp —
     render the meta tag + page-tool script when WEBMCP_ORIGIN_TRIAL_TOKEN
     is set and NEITHER when unset
  3. attribution markers ride every bound call (src=webmcp + X-DC-Source)
"""
import flask
import pytest

import routes._webmcp as wm

# Structurally arbitrary — the helper treats the token as opaque.
TOKEN = "QQwebmcpTESTtokenZZ=="

_TOOLS = [{
    "name": "get-test-data",
    "description": "Test tool.",
    "schema": {"type": "object", "properties": {}},
    "js_body": "return api('/api/v1/test');",
}]


@pytest.fixture
def token_set(monkeypatch):
    monkeypatch.setenv("WEBMCP_ORIGIN_TRIAL_TOKEN", TOKEN)


@pytest.fixture
def token_unset(monkeypatch):
    monkeypatch.delenv("WEBMCP_ORIGIN_TRIAL_TOKEN", raising=False)


# ── 1 · helper ────────────────────────────────────────────────────────

def test_no_token_everything_empty_or_identity(token_unset):
    assert wm.webmcp_meta_tag() == ""
    assert wm.webmcp_script(_TOOLS) == ""
    html = "<html><head></head><body>x</body></html>"
    assert wm.webmcp_inject(html, _TOOLS) == html


def test_meta_tag_shape(token_set):
    tag = wm.webmcp_meta_tag()
    assert tag == f'<meta http-equiv="origin-trial" content="{TOKEN}">'


def test_script_registers_with_feature_detection(token_set):
    js = wm.webmcp_script(_TOOLS)
    # entry points: document.modelContext (150+) preferred, navigator (149)
    assert "document.modelContext" in js
    assert "navigator.modelContext" in js
    assert "registerTool" in js
    # tool descriptor pieces
    assert '"get-test-data"' in js
    assert "inputSchema" in js
    assert "readOnlyHint" in js
    # attribution markers on every bound call
    assert "src=webmcp" in js
    assert "'X-DC-Source':'webmcp'" in js
    # marker id doubles as the idempotency guard
    assert wm._MARKER in js


def test_inject_full_document(token_set):
    html = "<html><head><title>t</title></head><body><p>x</p></body></html>"
    out = wm.webmcp_inject(html, _TOOLS)
    head = out.split("</head>")[0]
    assert 'http-equiv="origin-trial"' in head          # meta INSIDE head
    assert out.index(wm._MARKER) < out.index("</body>")  # script inside body


def test_inject_headless_fragment_prepends_meta(token_set):
    # /radar pages are fragments: _NAV div + body, no <head>/<body> tags.
    html = "<div>nav</div><article>edition</article>"
    out = wm.webmcp_inject(html, _TOOLS)
    # meta first ⇒ the parser hoists it into the synthesized <head>
    assert out.startswith('<meta http-equiv="origin-trial"')
    assert out.rstrip().endswith("</script>")


def test_inject_idempotent(token_set):
    html = "<html><head></head><body>x</body></html>"
    once = wm.webmcp_inject(html, _TOOLS)
    assert wm.webmcp_inject(once, _TOOLS) == once
    assert once.count(wm._MARKER) == 1


def test_json_strings_cannot_break_out_of_script(token_set):
    evil = [{"name": "t", "description": "</script><script>alert(1)",
             "schema": {}, "js_body": "return 'x';"}]
    js = wm.webmcp_script(evil)
    # one closing tag only — ours
    assert js.count("</script>") == 1


# ── 2 · pages ─────────────────────────────────────────────────────────

def _get(bp, path):
    app = flask.Flask(__name__)
    app.register_blueprint(bp)
    return app.test_client().get(path)


def _radar_client(monkeypatch):
    import routes.radar as radar
    monkeypatch.setattr(radar, "_render_edition",
                        lambda slug, tier, now=None: "<div>edition</div>")
    monkeypatch.setattr(radar, "_tier", lambda: "tease")
    return radar


def test_radar_instrumented_when_token_set(token_set, monkeypatch):
    radar = _radar_client(monkeypatch)
    body = _get(radar.radar_bp, "/radar").get_data(as_text=True)
    assert 'http-equiv="origin-trial"' in body
    assert wm._MARKER in body
    assert "get-grid-radar-brief" in body
    assert "/radar/'+encodeURIComponent(ed)+'.json" in body


def test_radar_clean_when_token_unset(token_unset, monkeypatch):
    radar = _radar_client(monkeypatch)
    body = _get(radar.radar_bp, "/radar").get_data(as_text=True)
    assert "origin-trial" not in body
    assert wm._MARKER not in body


def test_radar_edition_route_instrumented(token_set, monkeypatch):
    radar = _radar_client(monkeypatch)
    body = _get(radar.radar_bp, "/radar/capital").get_data(as_text=True)
    assert 'http-equiv="origin-trial"' in body
    assert wm._MARKER in body


def _phx_client(monkeypatch):
    import routes.phx_live as phx
    monkeypatch.setattr(phx, "_get_data", lambda: dict(phx._BASELINE))
    return phx


def test_phx_instrumented_when_token_set(token_set, monkeypatch):
    phx = _phx_client(monkeypatch)
    body = _get(phx.phx_bp, "/phx").get_data(as_text=True)
    assert 'http-equiv="origin-trial"' in body
    assert wm._MARKER in body
    assert "get-phoenix-market-stats" in body
    assert "/api/v1/dcpi/scores/phoenix" in body


def test_phx_clean_when_token_unset(token_unset, monkeypatch):
    phx = _phx_client(monkeypatch)
    body = _get(phx.phx_bp, "/phx").get_data(as_text=True)
    assert "origin-trial" not in body
    assert wm._MARKER not in body


def test_integrations_mcp_instrumented_when_token_set(token_set):
    import routes.integrations_landing as il
    body = _get(il.integrations_landing_bp,
                "/integrations/mcp").get_data(as_text=True)
    assert 'http-equiv="origin-trial"' in body
    assert wm._MARKER in body
    assert "get-grid-scoreboard" in body
    assert "rank-datacenter-markets" in body
    # meta landed inside the real <head>
    assert 'http-equiv="origin-trial"' in body.split("</head>")[0]


def test_integrations_mcp_clean_when_token_unset(token_unset):
    import routes.integrations_landing as il
    body = _get(il.integrations_landing_bp,
                "/integrations/mcp").get_data(as_text=True)
    assert "origin-trial" not in body
    assert wm._MARKER not in body


# ── 3 · drift-list contract ───────────────────────────────────────────

def test_backend_page_tool_bindings_in_drift_list():
    # routes/_webmcp.py docstring contract: every NEW API path bound by a
    # backend page tool is probed by the master shell's drift lane.
    import routes.webmcp_master_shell as wms
    for path in ("/api/v1/markets/phoenix", "/api/v1/dcpi/scores/phoenix",
                 "/api/v1/iso/comparison", "/api/v1/dcpi/trending"):
        assert any(p.split("?")[0] == path for p in wms.BOUND_API_PATHS), path
