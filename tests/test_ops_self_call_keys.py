"""The app's own reads of its ops/admin surfaces carry a credential.

The ops read gate (#5510) refuses keyless reads of /api/v1/ops/*,
/api/v1/admin/*, /api/v1/mcp/funnel and /api/v1/mcp/retention once enforced.
These callers read those paths through the public edge, so without a key they
go blind (most of them fail soft: a 401 reads as "unmeasured" or as an empty
funnel). Each test drives the REAL fetch helper with the transport replaced by
a recorder and asserts the key is on the wire, and that a public page read by
the same helper stays anonymous.

No network: requests / urllib are replaced before any call is made.
"""
import ast
import os
import urllib.request

import pytest
import requests
import yaml

import internal_auth

KEY = "test-internal-key-not-a-secret"
ADMIN = "test-admin-key-not-a-secret"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Resp:
    status_code = 200
    text = "{}"

    def json(self):
        return {}


@pytest.fixture
def wire(monkeypatch):
    """Record (url, headers) for every requests.get / Session.get / urlopen."""
    for v in ("DCHUB_SYNC_KEY", "INTERNAL_WORKER_SECRET"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", KEY)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN)
    calls = []

    def fake_get(url, *a, headers=None, **k):
        calls.append((url, dict(headers or {})))
        return _Resp()

    def fake_session_get(self, url, *a, headers=None, **k):
        merged = dict(self.headers)
        merged.update(headers or {})
        calls.append((url, merged))
        return _Resp()

    class _U:
        def __init__(self):
            self.status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, *a, **k):
        calls.append((req.full_url, {k2.lower(): v for k2, v in req.header_items()}))
        return _U()

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests.Session, "get", fake_session_get)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def _key_on(calls, needle):
    hits = [h for u, h in calls if needle in u]
    assert hits, "no request to %s was made — the test observed nothing" % needle
    return [{k.lower(): v for k, v in h.items()}.get("x-internal-key") for h in hits]


# ─────────────────────────────────────────────────────────── the helper ──

@pytest.mark.parametrize("url,keyed", [
    ("/api/v1/mcp/funnel", True), ("https://dchub.cloud/api/v1/mcp/funnel?x=1", True),
    ("/api/v1/mcp/funnel-stages", True), ("/api/v1/mcp/retention/cohorts", True),
    ("/api/v1/ops/deadman", True), ("/api/v1/admin/visual-sentinel", True),
    ("/api/admin/usage-report", True), ("/admin/crm", True),
    ("/", False), ("/pricing", False), ("/api/v1/stats", False),
    ("/api/v1/mcp/handoff-funnel", False), ("/administer", False),
    ("https://dchub.cloud/sitemap.xml", False),
])
def test_self_call_headers_only_on_ops_paths(wire, url, keyed):
    h = internal_auth.self_call_headers(url)
    assert (h == {"X-Internal-Key": KEY}) is keyed, (url, h)


def test_self_call_key_falls_back_to_admin_key_and_is_accepted(monkeypatch):
    for v in ("DCHUB_INTERNAL_KEY", "DCHUB_SYNC_KEY", "INTERNAL_WORKER_SECRET"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN)
    assert internal_auth.self_call_key() == ADMIN
    assert internal_auth.is_valid_internal_key(internal_auth.self_call_key())


def test_no_key_configured_sends_no_header(monkeypatch):
    for v in ("DCHUB_INTERNAL_KEY", "DCHUB_SYNC_KEY", "INTERNAL_WORKER_SECRET",
              "DCHUB_ADMIN_KEY"):
        monkeypatch.delenv(v, raising=False)
    assert internal_auth.self_call_headers("/api/v1/mcp/funnel") == {}


# ─────────────────────────────────────────────────────── the self-calls ──

def test_published_truth_shell(wire):
    import routes.published_truth_master_shell as m
    m._get_json("/api/v1/mcp/funnel")
    m._get_json("/api/v1/ops/deadman")
    m._get_json("/api/v1/stats")
    assert _key_on(wire, "/api/v1/mcp/funnel") == [KEY]
    assert _key_on(wire, "/api/v1/ops/deadman") == [KEY]
    assert _key_on(wire, "/api/v1/stats") == [None]


def test_growth_integrity_shell(wire):
    import routes.growth_integrity_master_shell as m
    m._get_json("/api/v1/mcp/retention")
    m._get_json("/api/v1/stats")
    assert _key_on(wire, "/api/v1/mcp/retention") == [KEY]
    assert _key_on(wire, "/api/v1/stats") == [None]


def test_conversion_loop_shell(wire):
    import routes.conversion_loop_master_shell as m
    m._get_json("/api/v1/mcp/funnel")
    assert _key_on(wire, "/api/v1/mcp/funnel") == [KEY]


def test_metric_integrity_shell(wire):
    import routes.metric_integrity_master_shell as m
    m._fetch_json("/api/v1/mcp/funnel")
    m._fetch_json("/api/v1/stats")
    assert _key_on(wire, "/api/v1/mcp/funnel") == [KEY]
    assert _key_on(wire, "/api/v1/stats") == [None]


@pytest.mark.parametrize("fn", ["check_addressable_demand_unconverted",
                                "check_trial_to_paid_stagnation",
                                "check_tool_signal_to_conversion_leak"])
def test_brain_consistency_radar_funnel_reads(wire, fn):
    import routes.brain_consistency_radar as m
    getattr(m, fn)()
    assert _key_on(wire, "/api/v1/mcp/funnel") == [KEY]


def test_brain_fast_qa_keys_only_the_ops_url(wire):
    import routes.brain_fast_qa as m
    m._check_urls()
    assert _key_on(wire, "/api/v1/mcp/funnel") == [KEY]
    # the public pages on the same sweep stay anonymous
    assert set(_key_on(wire, "/pricing")) == {None}
    assert set(_key_on(wire, "/sitemap.xml")) == {None}


def test_contract_healer(wire):
    import routes.contract_healer as m
    m._fetch("/api/v1/mcp/funnel")
    m._fetch("/llms.txt", accept="text/plain")
    assert _key_on(wire, "/api/v1/mcp/funnel") == [KEY]
    assert _key_on(wire, "/llms.txt") == [None]


def test_site_qa(wire):
    import routes.site_qa as m
    m._fetch("/api/v1/mcp/funnel")
    m._fetch("/pricing")
    assert _key_on(wire, "/api/v1/mcp/funnel") == [KEY]
    assert _key_on(wire, "/pricing") == [None]


def test_site_automation_briefing(wire):
    import routes.site_automation as m
    m._self_get("/api/v1/admin/visual-sentinel", retries=0)
    m._self_get("/api/v1/stats", retries=0)
    assert _key_on(wire, "/api/v1/admin/visual-sentinel") == [KEY]
    assert _key_on(wire, "/api/v1/stats") == [None]


# ──────────────────────────────────────────────────────── tools / CI ──

def test_kill_switch_probe_reads_funnel_with_admin_key(wire, monkeypatch):
    import tools.kill_switch_probe as m
    m.observe_quota_wall()
    hits = [h for u, h in wire if "/api/v1/mcp/funnel" in u]
    assert hits and hits[0].get("X-Admin-Key") == ADMIN


def test_qa_superuser_keys_only_deadman(monkeypatch):
    import tools.qa_superuser.probe_web as m
    monkeypatch.setattr(m.C, "ADMIN_KEY", ADMIN)
    seen = {}

    def fake_fetch(url, *, headers=None, timeout=30, **k):
        seen[url] = headers
        return 200, {}, "{}"

    monkeypatch.setattr(m, "fetch", fake_fetch)
    m._probe_edge_vs_origin([])
    keyed = {u for u, h in seen.items() if h and h.get("X-Admin-Key") == ADMIN}
    assert keyed == {f"{m.C.EDGE}/api/v1/ops/deadman", f"{m.C.ORIGIN}/api/v1/ops/deadman"}
    assert len(seen) > len(keyed)  # the other paths were asked, anonymously


def test_deadman_watch_ledger_read_sends_admin_key():
    """AST: the one urllib Request to /api/v1/ops/deadman in watch.py must pass
    a headers dict that carries X-Admin-Key (conditionally on ADMIN)."""
    src = open(os.path.join(ROOT, "tools", "deadman", "watch.py"), encoding="utf-8").read()
    found = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "Request"
                and node.args and "/api/v1/ops/deadman" in ast.unparse(node.args[0])):
            found.append(node)
    assert len(found) == 1, "expected exactly one ledger Request, found %d" % len(found)
    hdr = next(k.value for k in found[0].keywords if k.arg == "headers")
    assert "'X-Admin-Key': ADMIN" in ast.unparse(hdr)


def _step(workflow, name):
    wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", workflow),
                             encoding="utf-8"))
    for job in wf["jobs"].values():
        for st in job["steps"]:
            if st.get("name") == name:
                return st
    raise AssertionError("step %r not found in %s" % (name, workflow))


def test_agent_pay_board_read_is_keyed():
    st = _step("agent-pay-shell-tick.yml", "Prove the beat actually landed")
    assert st["env"]["ADMIN_KEY"] == "${{ secrets.DCHUB_ADMIN_KEY }}"
    run = st["run"]
    curl = run[run.index("if ! curl"):run.index("/tmp/board.json")]
    assert '"${AUTH[@]}"' in curl and "/api/v1/ops/deadman" in curl
    assert 'AUTH=(-H "X-Admin-Key: $ADMIN_KEY")' in run


def test_deploy_integrity_is_keyed_and_fails_on_refusal():
    st = _step("deploy-integrity.yml", "Live route smoke (200 + non-blank)")
    assert st["env"]["ADMIN_KEY"] == "${{ secrets.DCHUB_ADMIN_KEY }}"
    run = st["run"]
    assert '-H "X-Admin-Key: $ADMIN_KEY"' in run
    assert 'if code in ("401", "403"):' in run
    body = run[run.index('if code in ("401", "403"):'):]
    assert body.split("\n")[2].strip() == "sys.exit(1)"


def test_failover_warm_uses_a_real_credential_not_the_probe_marker():
    st = _step("failover-warm.yml", "Warm the Railway-failover KV cache")
    run = st["run"]
    assert st["env"]["INTERNAL_KEY"] == "${{ secrets.DCHUB_INTERNAL_KEY }}"
    assert 'AUTH=(-H "X-Internal-Key: $INTERNAL_KEY")' in run
    assert '"${AUTH[@]}"' in run
    assert "X-DC-Probe" not in run.split("# A refused credential")[0].split("AUTH=()")[1]


def test_static_pages_forward_the_key():
    for page, paths in (("static/mcp-dashboard.html", ("/api/v1/mcp/funnel", "/api/v1/mcp/retention")),
                        ("static/retention.html", ("/api/v1/mcp/retention",))):
        src = open(os.path.join(ROOT, page), encoding="utf-8").read()
        assert 'const OPS_HDR = OPS_KEY ? {"X-Admin-Key": OPS_KEY} : {};' in src
        for p in paths:
            lines = [ln for ln in src.splitlines() if "fetch('" + p in ln]
            assert lines, (page, p)
            assert all("headers: OPS_HDR" in ln for ln in lines), (page, p)
    src = open(os.path.join(ROOT, "routes", "brain_backlog_admin.py"), encoding="utf-8").read()
    lines = [ln for ln in src.splitlines() if "fetch('/api/v1/mcp/funnel'" in ln]
    assert lines and all("{headers: HDR}" in ln for ln in lines)
