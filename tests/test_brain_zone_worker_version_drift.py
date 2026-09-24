"""check_zone_worker_version_drift watches the OTHER worker, on the only path
that reports its version.

WHY THIS EXISTS. dchub.cloud is served by TWO workers that stamp the SAME header
name with INDEPENDENT version numbers. Measured 2026-09-16, one second apart:

    /mcp                  x-dc-worker-version: 4.9.70-capacity-search      (zone)
    /api/v1/dcpi/scores   x-dc-worker-version: 5.0.0-listing-slug-…       (Pages)

check_worker_version_drift() reads dchub-frontend/_worker.js and probes an
/api/v1/* path. That pair is self-consistent — and blind to dchub-backend's own
worker.js by construction. Nothing watched it, and it deploys by a HUMAN PASTE
into the Cloudflare dashboard, which is the deploy mechanism most able to drift:
a paste with no commit leaves production running code no commit contains, and a
commit with no paste leaves a merged change unshipped. Both are silent.

★ The probe path is the whole ballgame. Point this detector at an /api/* URL and
it reads the Pages worker's version, compares it to the zone worker's source,
and reports a mismatch that no paste can ever fix — a detector that is wrong
forever and looks like it is working. test_probe_reads_mcp_not_an_api_path pins
it, and is the single most load-bearing assertion in this file.
"""
import os
import pytest

pytest.importorskip("flask")

import routes.brain_consistency_radar as R

_SRC = "/* banner */\nconst WORKER_VERSION = '4.9.71-capacity-source-on-mcp-get';\n"


def _install(monkeypatch, tmp_path, *, source=_SRC, headers=None):
    """Point the detector at a temp worker.js and a fake probe."""
    if source is not None:
        f = tmp_path / "worker.js"
        f.write_text(source, encoding="utf-8")
        monkeypatch.setattr(R, "_ZONE_WORKER_SOURCE_PATH", str(f))
    else:
        monkeypatch.setattr(R, "_ZONE_WORKER_SOURCE_PATH",
                            str(tmp_path / "absent.js"))
    seen = []

    def fake(url, timeout=8):
        seen.append(url)
        return ("", headers) if headers is not None else (None, None)

    monkeypatch.setattr(R, "_http_get", fake)
    return seen


def _only(findings):
    assert len(findings) == 1, findings
    return findings[0]


# ── the wiring that makes the comparison mean anything ──────────────
def test_probe_reads_mcp_not_an_api_path():
    """An /api/* probe reads the OTHER worker. See the module docstring."""
    assert R._ZONE_WORKER_PROBE_URL.endswith("/mcp"), R._ZONE_WORKER_PROBE_URL
    assert "/api/" not in R._ZONE_WORKER_PROBE_URL, (
        "/api/* is served by the frontend Pages worker; its version header "
        "would never match this worker's source, and no paste could fix it."
    )


def test_probe_is_cache_busted(monkeypatch, tmp_path):
    """A header read off a cached response is not a reading of production."""
    seen = _install(monkeypatch, tmp_path,
                    headers={"x-dc-worker-version": "4.9.71-capacity-source-on-mcp-get"})
    R.check_zone_worker_version_drift()
    assert seen and "?_=" in seen[0], seen


def test_source_is_the_worker_this_repo_owns():
    """Local file, not raw.githubusercontent.com: no token, and no 404 that
    could mean four different things."""
    assert R._ZONE_WORKER_SOURCE_PATH.endswith("/worker.js")
    assert "raw.githubusercontent.com" not in R._ZONE_WORKER_SOURCE_PATH
    assert os.path.exists(R._ZONE_WORKER_SOURCE_PATH), (
        "worker.js should sit beside this repo's routes/ package"
    )


def test_the_real_repo_file_parses():
    """The regex must match the file as it actually is, not a fixture."""
    findings = []
    with open(R._ZONE_WORKER_SOURCE_PATH, encoding="utf-8") as fh:
        import re
        m = re.search(r"^const\s+WORKER_VERSION\s*=\s*['\"]([\w\d\.\-]+)['\"]",
                      fh.read(), re.M)
    assert m, "WORKER_VERSION not found in the real worker.js"
    assert not findings


# ── the comparison ──────────────────────────────────────────────────
def test_matching_versions_file_nothing(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path,
             headers={"x-dc-worker-version": "4.9.71-capacity-source-on-mcp-get"})
    assert R.check_zone_worker_version_drift() == []


def test_deployed_ahead_means_pasted_but_never_committed(monkeypatch, tmp_path):
    """The 2026-09-16 case: live 4.9.70, repo file read as 4.9.68."""
    _install(monkeypatch, tmp_path,
             source="const WORKER_VERSION = '4.9.68-capacity-terms';\n",
             headers={"x-dc-worker-version": "4.9.70-capacity-search"})
    f = _only(R.check_zone_worker_version_drift())
    assert f["issue"] == "zone_worker_deployed_ahead_of_repo"
    assert f["expected"] == "4.9.68-capacity-terms"
    assert f["deployed"] == "4.9.70-capacity-search"
    assert "revert" in f["detail"].lower(), (
        "the finding must warn that editing the repo copy and pasting it "
        "DISCARDS what was only ever pasted — that is the damage this "
        "direction of drift actually causes."
    )


def test_repo_ahead_means_committed_but_never_pasted(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path,
             headers={"x-dc-worker-version": "4.9.70-capacity-search"})
    f = _only(R.check_zone_worker_version_drift())
    assert f["issue"] == "zone_worker_commit_not_pasted"
    assert "paste" in f["detail"].lower()
    # Since 2026-09-23 a merge CAN ship worker.js. The finding must send the
    # reader to that workflow and the switch that arms it, not only to a paste
    # (tests/test_deploy_zone_worker.py pins that both exist as named).
    assert R._ZONE_WORKER_DEPLOY_WORKFLOW in f["detail"]
    assert R._ZONE_WORKER_ARM_VAR in f["detail"]
    assert "dry_run=false" in f["detail"]
    assert "no merge performs" not in f["detail"], "no longer true"


def test_the_two_directions_are_different_findings(monkeypatch, tmp_path):
    """They need different actions, so they must not share an issue name."""
    _install(monkeypatch, tmp_path,
             source="const WORKER_VERSION = '4.9.68-x';\n",
             headers={"x-dc-worker-version": "4.9.70-y"})
    ahead = _only(R.check_zone_worker_version_drift())["issue"]
    _install(monkeypatch, tmp_path,
             source="const WORKER_VERSION = '4.9.72-x';\n",
             headers={"x-dc-worker-version": "4.9.70-y"})
    behind = _only(R.check_zone_worker_version_drift())["issue"]
    assert ahead != behind, (ahead, behind)


def test_same_core_different_suffix_is_its_own_finding(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path,
             headers={"x-dc-worker-version": "4.9.71-something-else"})
    assert _only(R.check_zone_worker_version_drift())["issue"] == \
        "zone_worker_version_suffix_mismatch"


# ── refusing to guess ───────────────────────────────────────────────
def test_probe_failure_is_never_a_drift_finding(monkeypatch, tmp_path):
    """A transient failure reaching our own edge says nothing about drift."""
    _install(monkeypatch, tmp_path, headers=None)
    assert R.check_zone_worker_version_drift() == []


def test_missing_header_is_reported_as_itself(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, headers={"content-type": "application/json"})
    assert _only(R.check_zone_worker_version_drift())["issue"] == \
        "zone_worker_version_header_missing"


def test_unreadable_source_is_reported_as_itself(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, source=None,
             headers={"x-dc-worker-version": "4.9.71-capacity-source-on-mcp-get"})
    assert _only(R.check_zone_worker_version_drift())["issue"] == \
        "zone_worker_source_unreadable"


def test_missing_constant_is_reported_as_itself(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, source="/* no version here */\n",
             headers={"x-dc-worker-version": "4.9.71-capacity-source-on-mcp-get"})
    assert _only(R.check_zone_worker_version_drift())["issue"] == \
        "zone_worker_version_constant_not_found"


# ── it has to actually run ──────────────────────────────────────────
def test_detector_is_registered():
    """An unregistered detector is a function nobody calls."""
    import inspect
    src = inspect.getsource(R)
    assert src.count("check_zone_worker_version_drift") >= 2, (
        "defined but never referenced — it must appear in the radar's "
        "detector list, not only in its own def"
    )
