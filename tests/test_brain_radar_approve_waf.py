"""check_approve_directive_waf_blocked — the detector for the 2026-09-04 miss.

The innovation dashboard's approve button posted the item's own "decision for
human" back as the drafting directive, in the clear. The brain writes those
decisions with the shell command quoted in backticks, and Cloudflare's managed
WAF reads backticks + URL as command injection: 403, HTML block page, BEFORE
Railway. No brain_approvals row, no log line, nothing for the redrive to find,
and the page printed the literal word "error".

The fixtures below are the LIVE MEASUREMENTS taken while diagnosing it
(2026-09-04, POST to dchub.cloud vs the Railway origin, identical body):

    `curl -i https://dchub.cloud/js/dchub-nav.js` (in prose)   edge 403
    `curl https://dchub.cloud/js/dchub-nav.js`                 edge 403
    the same text with the backticks removed                   edge 400
    `curl -i`            (backticks, command, no URL)          edge 400
    `https://dchub.cloud/js/dchub-nav.js` (backticks, no cmd)  edge 400
    `gh workflow run press-rss.yml`         (no URL)           edge 400
    base64 of the 403 text                                     edge 400

400 is the app's own reply (the probes used an invalid `kind`), i.e. the
request got through. So the detector must fire on the first two and stay
silent on the rest — a detector that flags everything is as useless as one
that flags nothing.
"""
import pytest

radar = pytest.importorskip("routes.brain_consistency_radar")


BLOCKED_AT_THE_EDGE = [
    "Run `curl -i https://dchub.cloud/js/dchub-nav.js` and, if it returns 404, "
    "restore that asset/route first.",
    "`curl https://dchub.cloud/js/dchub-nav.js`",
    "In the dchub-frontend repo run `gh workflow run press-rss.yml`, wait for "
    "it to complete, then `curl -i https://dchub.cloud/press-release/x`",
]

REACHED_THE_APP = [
    "Run curl -i https://dchub.cloud/js/dchub-nav.js and, if it returns 404, "
    "restore that asset/route first.",
    "Run `curl -i`",
    "Run `https://dchub.cloud/js/dchub-nav.js`",
    "In the dchub-frontend repo run `gh workflow run press-rss.yml`, wait for "
    "it to complete.",
    "Open routes/iso_orchestrator.py and check whether that ingest job is alive.",
    "Choose one: (A) revive the site-baseline cron, or (B) retire it.",
]


@pytest.mark.parametrize("text", BLOCKED_AT_THE_EDGE)
def test_signature_matches_what_the_edge_actually_blocked(text):
    assert radar._WAF_BACKTICK_CMD.search(text), \
        f"edge returned 403 for this text; the detector must flag it: {text!r}"


@pytest.mark.parametrize("text", REACHED_THE_APP)
def test_signature_is_silent_on_what_the_edge_let_through(text):
    assert not radar._WAF_BACKTICK_CMD.search(text), \
        f"edge let this through (400 from the app); flagging it is a false alarm: {text!r}"


def test_transport_half_fires_when_the_directive_goes_out_in_the_clear(monkeypatch):
    """Half 1 of the invariant: the approve POST must not carry operator text
    where a WAF can match it. Simulate the pre-fix page and require a finding."""
    import builtins
    real_open = builtins.open

    def _fake_open(path, *a, **k):
        if str(path).endswith("brain_innovation_dashboard.py"):
            import io
            return io.BytesIO(b"fetch('/api/v1/brain/innovation/approve', "
                              b"{body: JSON.stringify({directive:directive})})")
        return real_open(path, *a, **k)
    monkeypatch.setattr(builtins, "open", _fake_open)
    monkeypatch.setattr(radar, "_WAF_BACKTICK_CMD", radar._WAF_BACKTICK_CMD)
    out = radar.check_approve_directive_waf_blocked()
    issues = {f.get("issue") for f in out}
    assert "approve_directive_sent_in_the_clear" in issues


def test_transport_half_is_silent_on_the_shipped_page():
    """And stays silent against the real file as shipped — otherwise the
    detector would cry wolf on every sweep forever."""
    out = radar.check_approve_directive_waf_blocked()
    issues = {f.get("issue") for f in out}
    assert "approve_directive_sent_in_the_clear" not in issues


def test_detector_is_registered_in_the_sweep():
    """A check that is not in scan_all's tuple never runs. Located with ast so
    a name in a comment does not count."""
    import ast
    import inspect
    rule = pytest.importorskip("util.brain_detector_rule")
    src = inspect.getsource(radar)
    names = rule.registered_detectors(src) if hasattr(
        rule, "registered_detectors") else None
    if names is None:
        tree = ast.parse(src)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "scan_all":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
    assert "check_approve_directive_waf_blocked" in names
