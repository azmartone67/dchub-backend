"""Registry acquisition (2026-07-27) — pins the two-question verdict.

Maintenance keeps listings from rotting; only acquisition grows presence,
and directories are ~84% of new agent arrivals. The risk in an acquisition
loop is the mirror of the maintenance bug we just fixed: instead of calling
an unread listing healthy, it would call an unread directory "absent" and
generate busywork — or worse, assert we should submit somewhere that does
not exist.

So the classifier answers two questions in order, and the first gates the
second: is the directory real, and only then, are we on it.

CI-SAFETY: pure classifier, no network, no DB.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def ra():
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import registry_acquisition as m
    return m


# ── question 1 gates question 2 ──────────────────────────────────────

def test_dead_directory_never_becomes_a_submission(ra):
    # The seed list is a GUESS. A candidate that does not resolve must drop
    # out, not become "we should submit here".
    v = ra.classify_candidate(404, 200, "<html>whatever</html>")
    assert v["verdict"] == "dead_directory"


def test_unreachable_home_is_unverified_not_absent(ra):
    v = ra.classify_candidate(None, 200, "<html>no dchub here</html>")
    assert v["verdict"] == "unverified"


# ── question 2, answered only honestly ───────────────────────────────

def test_unreadable_probe_is_unknown_not_absent(ra):
    # The mirror of the maintenance bug: a 403/429 on the probe means we do
    # not know, and must not manufacture a submission task.
    for status in (403, 429, 401, None):
        v = ra.classify_candidate(200, status, "")
        assert v["verdict"] == "unverified", status
        assert "unknown" in v["reason"].lower() or "could not" in v["reason"].lower()


def test_broken_probe_url_is_not_our_absence(ra):
    # A 404 on the probe means the probe URL is wrong — our own bug, not
    # evidence about the directory's contents.
    v = ra.classify_candidate(200, 404, "")
    assert v["verdict"] == "unverified"
    assert "probe is wrong" in v["reason"]


def test_present_when_identity_found(ra):
    v = ra.classify_candidate(200, 200, "<html>… DC Hub — dchub.cloud …</html>")
    assert v["verdict"] == "present"


def test_absent_only_when_live_and_readable_and_missing(ra):
    v = ra.classify_candidate(200, 200, "<html>a list of other servers</html>")
    assert v["verdict"] == "absent"
    assert "submittable" in v["reason"]


def test_empty_probe_body_is_unknown(ra):
    v = ra.classify_candidate(200, 200, "   ")
    assert v["verdict"] == "unverified"


# ── the loop must not pretend to submit ──────────────────────────────

def test_does_not_auto_submit(ra):
    """The speculative registry webhooks were deleted 2026-07-17 after every
    POST 404'd. Almost every directory needs a manual form or a GitHub PR, so
    this produces a reviewed QUEUE. A module that pretends to submit is worse
    than one that admits it cannot."""
    src = _read(os.path.join("routes", "registry_acquisition.py"))
    code = "\n".join(l for l in src.split("\n")
                     if not l.lstrip().startswith("#"))
    for banned in ("requests.post", ".post(", 'method="POST"'):
        assert banned not in code, banned


# ── wiring ───────────────────────────────────────────────────────────

def test_registered_and_cron_ticked():
    assert "register_blueprint(registry_acquisition_bp)" in _read("main.py")
    cron = _read(os.path.join("routes", "cron_heartbeat.py"))
    assert "/api/v1/admin/registry-acquisition/scan" in cron
    assert "REGISTRY_ACQUISITION_DISABLE" in cron


def test_lane_is_advisory_not_critical():
    # Deliberate: a non-empty queue is work-to-do, not breakage. Only
    # registry_truth (broken / long-unread listings) is a RED.
    src = _read(os.path.join("routes", "seven_levers_master_shell.py"))
    i = src.index("def _lane_registry_acquisition")
    body = src[i:src.index("# ── lane 2", i)]
    assert "critical=True" not in body
    assert "critical=False" in body


def test_shell_read_path_does_no_outbound_http():
    src = _read(os.path.join("routes", "registry_acquisition.py"))
    i = src.index("def read_queue")
    body = src[i:src.index("@registry_acquisition_bp", i)]
    for banned in ("requests.", "_fetch(", "urlopen"):
        assert banned not in body, banned
