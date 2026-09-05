"""map_layer_probe must be able to FAIL — and on the exact cases its
predecessor read as healthy.

The detector this replaces was not wrong in some subtle way; it was structurally
incapable of firing. Its ladder —

    if status in (200, 304, 400): continue
    if status == 0: ...    elif status in (401, 403): ...    elif status >= 500: ...

— matched nothing for 404, 402 or any 3xx, so a DELETED ROUTE produced no
finding. Two of its eight targets were already 404 when this was written.

So these tests are not a description of the implementation; they drive the real
`_check_one` with synthetic upstream responses and assert a finding comes out.
Each case is a mutation that MUST be caught. If someone reverts the allow-set
back to a failure enumeration, `test_status_ladder_regression` goes red.
"""
import json

import pytest

from routes import map_layer_probe as mlp


def _probe(coverage="global", rows_field="features"):
    return mlp.Probe(
        key="t", path="/x?lat={lat}&lng={lng}", layers="Test layer",
        coverage=coverage, rows=mlp._rows(rows_field),
    )


def _run(monkeypatch, status, body, coverage="global", where="us",
         rows_field="features"):
    monkeypatch.setattr(mlp, "_fetch", lambda path, timeout=20.0: (status, body))
    return mlp._check_one(_probe(coverage, rows_field), 1.0, 2.0, where)


OK_BODY = json.dumps({"features": [1, 2, 3]})


# ── the regression that motivated the rewrite ─────────────────────────

@pytest.mark.parametrize("status", [404, 402, 301, 308, 401, 403, 500, 503])
def test_status_ladder_regression(monkeypatch, status):
    """Every status outside the allow-set fires — including the four the old
    ladder silently ignored (404, 402, 301, 308)."""
    f = _run(monkeypatch, status, "{}")
    assert f is not None, f"HTTP {status} produced NO finding — the allow-set regressed"
    assert f["issue"] == "map_layer_bad_status"
    assert str(status) in f["detail"]


def test_404_is_the_specific_case_the_old_detector_missed(monkeypatch):
    f = _run(monkeypatch, 404, '{"error":"404 Not Found"}')
    assert f is not None and f["issue"] == "map_layer_bad_status"


# ── the probe must not cry wolf ───────────────────────────────────────

def test_healthy_response_produces_no_finding(monkeypatch):
    assert _run(monkeypatch, 200, OK_BODY) is None


def test_us_only_layer_with_zero_rows_abroad_is_not_a_finding(monkeypatch):
    """Zero rows outside a US-only dataset's coverage is CORRECT, not a defect.
    If this fires, the probe would report a permanent false positive forever."""
    f = _run(monkeypatch, 200, json.dumps({"features": []}),
             coverage="us", where="non_us")
    assert f is None


# ── status alone is not health ────────────────────────────────────────

def test_zero_rows_inside_coverage_fires(monkeypatch):
    """The actual user-visible symptom: 200 OK, empty map. A status-only check
    calls this healthy."""
    f = _run(monkeypatch, 200, json.dumps({"features": []}),
             coverage="global", where="non_us")
    assert f is not None and f["issue"] == "map_layer_empty_in_coverage"


def test_global_layer_must_have_rows_at_both_canaries(monkeypatch):
    for where in ("us", "non_us"):
        f = _run(monkeypatch, 200, json.dumps({"features": []}),
                 coverage="global", where=where)
        assert f is not None, f"global layer empty at {where} must fire"


def test_200_carrying_an_upgrade_envelope_fires_bad_shape(monkeypatch):
    """A paywall/error envelope at HTTP 200 is how monitoring gets fooled."""
    body = json.dumps({"error": "upgrade_required", "gate": "map_session_cap"})
    f = _run(monkeypatch, 200, body)
    assert f is not None and f["issue"] == "map_layer_bad_shape"


def test_row_field_present_but_not_a_list_fires_bad_shape(monkeypatch):
    f = _run(monkeypatch, 200, json.dumps({"features": "lots"}))
    assert f is not None and f["issue"] == "map_layer_bad_shape"


def test_non_json_body_fires_bad_shape(monkeypatch):
    f = _run(monkeypatch, 200, "<html>gateway timeout</html>")
    assert f is not None and f["issue"] == "map_layer_bad_shape"


def test_unreachable_fires(monkeypatch):
    f = _run(monkeypatch, 0, "URLError: connection refused")
    assert f is not None and f["issue"] == "map_layer_unreachable"


def test_us_only_layer_erroring_abroad_fires_its_own_class(monkeypatch):
    """Measured live: power-plants/nearby returns 400 'Could not determine state
    from coordinates' at Frankfurt. Zero rows abroad is fine; an ERROR is not."""
    f = _run(monkeypatch, 400, '{"error":"Could not determine state from coordinates"}',
             coverage="us", where="non_us")
    assert f is not None and f["issue"] == "map_layer_outside_coverage_error"


# ── the scalar extractor (transmission-proximity) ─────────────────────

def test_line_count_extractor_distinguishes_zero_from_missing():
    assert mlp._rows_line_count({"line_count": 0}) == 0
    assert mlp._rows_line_count({"line_count": 8}) == 8
    assert mlp._rows_line_count({}) is None, "missing field must be a SHAPE error, not a zero"
    assert mlp._rows_line_count({"line_count": True}) is None, "bool is not a count"
    assert mlp._rows_line_count("nope") is None


# ── wiring: registered is not the same as running ─────────────────────

def test_probe_is_registered_in_cron_dispatch():
    """The predecessor's whole failure was having no caller. Assert the
    scheduling half exists, by reading the dispatch table's source."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "routes", "cron_heartbeat.py"), encoding="utf-8").read()
    assert '"map_layer_probe"' in src, "probe is not in _DISPATCH — it would never run"
    assert "/api/v1/jobs/map-layer-probe" in src


def test_kill_switch_does_not_report_success(monkeypatch):
    """cron_heartbeat._classify records 'skipped' only when ok is not True, so
    returning {"ok": True, "skipped": ...} would make a DISABLED probe report
    success forever — the disarmed-verifier bug this module exists to prevent."""
    import inspect
    src = inspect.getsource(mlp.map_layer_probe)
    disable_branch = src.split("MAP_LAYER_PROBE_DISABLE")[1].split("result =")[0]
    assert "ok=True" not in disable_branch and '"ok": True' not in disable_branch, (
        "kill-switch branch must not claim ok=True — _classify would record it as 'ok'")


@pytest.mark.parametrize("status,body,coverage,where", [
    (404, "{}", "global", "us"),
    (503, "{}", "global", "us"),
    (0, "URLError: refused", "global", "us"),
    (200, '{"features": []}', "global", "us"),
    (200, '{"error":"upgrade_required"}', "global", "us"),
    (400, '{"error":"no state"}', "us", "non_us"),
])
def test_every_finding_carries_an_actionable_detail(monkeypatch, status, body,
                                                    coverage, where):
    """_classify builds its failure detail from error/skipped/reason, and the
    daily callout email prints it. A blank or contentless detail is a row nobody
    can action. Drives the REAL _check_one across every finding branch."""
    f = _run(monkeypatch, status, body, coverage=coverage, where=where)
    assert f is not None
    d = f["detail"]
    assert len(d.strip()) > 40, f"detail too thin to action: {d!r}"
    assert "Test layer" in d, "detail must name the affected map layer"
    assert "/x?" in d, "detail must name the endpoint probed"


def test_every_issue_string_is_a_bare_token():
    """_finding_type_of splits on the first ':' — an issue string containing a
    colon would be truncated into a different bucket."""
    import re
    src = inspect_source()
    issues = set(re.findall(r'"issue":\s*"([^"]+)"', src))
    assert issues, "no issue strings found — did the module change shape?"
    for i in issues:
        assert ":" not in i, f"issue {i!r} contains ':' and would be truncated"
        assert re.fullmatch(r"[a-z][a-z0-9_]*", i), f"issue {i!r} is not snake_case"


def test_every_issue_string_is_registered_as_an_error_class():
    """An unregistered issue persists and emails but is bucketed 'unknown' and
    dropped from actionable_now."""
    import re
    from routes import brain_error_classes as bec
    src = inspect_source()
    issues = set(re.findall(r'"issue":\s*"([^"]+)"', src))
    registered = {c.id for c in bec.REGISTRY}
    missing = issues - registered
    assert not missing, f"unregistered issue types would be bucketed 'unknown': {sorted(missing)}"


def inspect_source() -> str:
    import inspect
    return inspect.getsource(mlp)


# ── regression: the read cap truncated a valid 2.5 MB body ────────────
#
# First live run against production reported the (healthy, just-fixed) hazard
# layer as "body is not JSON". Cause was in the PROBE: a 200 KB read cap against
# a ~2.5 MB GDACS payload. The stubbed tests above could never catch it — they
# hand _check_one a body directly and never exercise the read.

class _FakeResp:
    """Minimal `requests` response. iter_content chunks like the real one, so a
    too-small _READ_CAP truncates here exactly as it did in production."""
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
    def iter_content(self, n=65536):
        for i in range(0, len(self._payload), n):
            yield self._payload[i:i + n]
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_fetch_does_not_truncate_a_hazard_sized_payload(monkeypatch):
    """THE REGRESSION, driven through the real _fetch and its read cap.

    Stubbing _fetch (as every test above does) cannot catch this: the bug was
    the read cap INSIDE _fetch. Here requests.get is stubbed instead, so the cap
    is exercised for real. With the old 200_000 cap this body comes back sliced
    mid-JSON and the probe reports a healthy endpoint as malformed."""
    big = json.dumps({"features": [{"i": i, "pad": "x" * 200} for i in range(10_000)]})
    assert len(big) > 2_000_000, "fixture is not representative of the real payload"
    monkeypatch.setattr(mlp.requests, "get",
                        lambda url, **kw: _FakeResp(200, big.encode()))
    status, body = mlp._fetch("/whatever")
    assert status == 200
    assert len(body) == len(big), (
        f"_fetch truncated {len(big)} bytes down to {len(body)} — the read cap regressed")
    assert json.loads(body)["features"], "truncated body would not parse"


def test_fetch_reports_a_redirect_rather_than_following_it(monkeypatch):
    """allow_redirects=False on purpose: a layer endpoint that starts redirecting
    is a finding. Following it silently is how the predecessor's substations
    probe reported 200 for a route that had moved."""
    seen = {}
    def fake_get(url, **kw):
        seen.update(kw)
        return _FakeResp(308, b"")
    monkeypatch.setattr(mlp.requests, "get", fake_get)
    status, _ = mlp._fetch("/moved")
    assert status == 308
    assert seen.get("allow_redirects") is False


def test_fetch_turns_a_transport_error_into_status_zero(monkeypatch):
    def boom(url, **kw):
        raise mlp.requests.exceptions.ConnectionError("refused")
    monkeypatch.setattr(mlp.requests, "get", boom)
    status, body = mlp._fetch("/dead")
    assert status == 0 and "ConnectionError" in body


def test_check_one_is_clean_on_a_hazard_sized_payload(monkeypatch):
    big = json.dumps({"features": [{"i": i, "pad": "x" * 200} for i in range(10_000)]})
    assert _run(monkeypatch, 200, big) is None


def test_truncated_body_is_reported_as_truncation_not_malformed(monkeypatch):
    """Truncation is a probe limit, not an endpoint defect. Conflating them
    sends someone to debug a healthy endpoint."""
    f = _run(monkeypatch, 200, "x" * mlp._READ_CAP)
    assert f is not None and f["issue"] == "map_layer_bad_shape"
    assert "read cap" in f["detail"], "truncation must be named as a probe limit"
    assert "PROBE limit" in f["detail"]


def test_read_cap_exceeds_the_largest_real_payload():
    """global-hazards measured ~2.5 MB on 2026-09-04."""
    assert mlp._READ_CAP > 4 * 1024 * 1024
