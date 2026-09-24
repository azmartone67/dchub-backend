"""A status-0 registry probe must say why, and get one retry.

2026-09-24: /api/v1/brain/mcp-registries showed modelcontextprotocol_registry
verdict=fetch_error http_status=0, and the /ai Registry Standing score fell
5/8 -> 4/8. From a Mac the same API answered 200 in 0.35-0.49s (3 of 3) and
listed cloud.dchub/mcp-server. The cause could not be read: _fetch returns
(0, "_fetch_error: ...") and _probe_official_registry returned (0, "") on a
parse failure, and _probe_all stored neither. No network: _fetch is stubbed.
"""
import routes.mcp_registry_watch as w


def _run(monkeypatch, fetch):
    monkeypatch.setattr(w, "_fetch", fetch)
    monkeypatch.setattr(w, "_copy_canon", lambda: ({}, None))
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    return w._probe_all()["modelcontextprotocol_registry"]


def test_fetch_error_text_is_kept_and_retried_once(monkeypatch):
    calls = []

    def fetch(url, timeout=15):
        calls.append(url)
        return 0, "_fetch_error: <urlopen error [Errno -3] Temporary failure in name resolution>", url

    row = _run(monkeypatch, fetch)
    assert row["verdict"] == "fetch_error"
    assert row["http_status"] == 0
    assert "name resolution" in row["error"]
    assert row["attempts"] == 2
    assert sum(1 for u in calls if u == w._OFFICIAL_REGISTRY_API) == 2


def test_a_transient_first_failure_recovers_on_the_retry(monkeypatch):
    n = {"official": 0}

    def fetch(url, timeout=15):
        if url == w._OFFICIAL_REGISTRY_API:
            n["official"] += 1
            if n["official"] == 1:
                return 0, "_fetch_error: timed out", url
            return 200, '{"servers":[{"server":{"name":"cloud.dchub/mcp-server"}}]}', url
        return 200, "", url

    row = _run(monkeypatch, fetch)
    assert row["verdict"] == "present"
    assert row["error"] is None
    assert row["attempts"] == 2


def test_a_parse_failure_names_itself_and_the_body_size(monkeypatch):
    def fetch(url, timeout=15):
        return 200, '{"servers": [ {"server": ', url    # truncated JSON

    row = _run(monkeypatch, fetch)
    assert row["verdict"] == "fetch_error"
    assert row["error"].startswith("_parse_error: JSONDecodeError")
    assert "(body 25 B)" in row["error"]


def test_control_a_healthy_probe_carries_no_error_and_one_attempt(monkeypatch):
    def fetch(url, timeout=15):
        return 200, '{"servers":[{"server":{"name":"cloud.dchub/mcp-server"}}]}', url

    row = _run(monkeypatch, fetch)
    assert row["verdict"] == "present"
    assert row["error"] is None
    assert row["attempts"] == 1
