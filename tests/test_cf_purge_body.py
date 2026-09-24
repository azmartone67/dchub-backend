"""scripts/cf_purge_body.py + .github/workflows/cf-purge.yml.

NO NETWORK. Cloudflare's purge_cache takes ONE kind of target per request;
the dispatch form has three inputs, so the body builder must refuse zero or
several, and each kind has its own shape (absolute URL / host+path, no scheme /
bare host). prefixes and hosts were added 2026-09-24, after single-file purges
of api.dchub.cloud/api/v1/stats — public URL and Railway fetch URL — returned
success without evicting the object.
"""
import importlib.util
import json
import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import cf_purge_body as B  # noqa: E402

WF = ROOT / ".github" / "workflows" / "cf-purge.yml"
RAILWAY = "dchub-backend-production.up.railway.app"


@pytest.mark.parametrize("kw,body", [
    ({"urls": "https://dchub.cloud/a https://dchub.cloud/b"},
     {"files": ["https://dchub.cloud/a", "https://dchub.cloud/b"]}),
    ({"prefixes": f"{RAILWAY}/api/v1/stats"}, {"prefixes": [f"{RAILWAY}/api/v1/stats"]}),
    ({"hosts": RAILWAY}, {"hosts": [RAILWAY]}),
    ({"hosts": f"  {RAILWAY}  ", "urls": "   "}, {"hosts": [RAILWAY]}),  # blank != set
])
def test_each_kind_builds_its_cloudflare_key(kw, body):
    assert B.build(**kw) == body


@pytest.mark.parametrize("kw", [
    {},
    {"urls": "https://dchub.cloud/a", "prefixes": f"{RAILWAY}/api"},
    {"prefixes": f"{RAILWAY}/api", "hosts": RAILWAY},
    {"urls": "https://dchub.cloud/a", "hosts": RAILWAY},
])
def test_exactly_one_kind(kw):
    with pytest.raises(ValueError, match="exactly ONE"):
        B.build(**kw)


@pytest.mark.parametrize("kw", [
    {"urls": "dchub.cloud/a"},                          # url without scheme
    {"prefixes": f"https://{RAILWAY}/api/v1/stats"},    # prefix WITH scheme
    {"prefixes": RAILWAY},                              # prefix with no path
    {"hosts": f"https://{RAILWAY}"},                    # host with scheme
    {"hosts": f"{RAILWAY}/api"},                        # host with path
])
def test_malformed_targets_are_refused(kw):
    with pytest.raises(ValueError):
        B.build(**kw)


def test_main_writes_the_body_or_emits_an_error_annotation(tmp_path, monkeypatch, capsys):
    out = tmp_path / "b.json"
    monkeypatch.setenv("PREFIXES", f"{RAILWAY}/api/v1/stats")
    monkeypatch.delenv("URLS", raising=False)
    monkeypatch.delenv("HOSTS", raising=False)
    assert B.main([str(out)]) == 0
    assert json.loads(out.read_text()) == {"prefixes": [f"{RAILWAY}/api/v1/stats"]}
    monkeypatch.setenv("URLS", "https://dchub.cloud/a")
    assert B.main([str(tmp_path / "c.json")]) == 1
    assert "::error::" in capsys.readouterr().out, "stdout is where the runner reads ::error::"
    assert not (tmp_path / "c.json").exists()


# ── workflow wiring ──────────────────────────────────────────────────

def _wf():
    d = yaml.safe_load(WF.read_text())
    return d, (d.get("on") or d.get(True))


def test_every_input_defaults_empty():
    """Dispatch fills unspecified inputs with their defaults; a non-empty
    default would ride along with every other kind and trip the one-kind rule."""
    _, on = _wf()
    inputs = on["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"urls", "prefixes", "hosts"}
    for name, spec in inputs.items():
        assert spec.get("default", "") == "" and spec.get("required") is False, name


def test_step_builds_the_body_with_the_script_and_checks_the_status():
    d, _ = _wf()
    step = next(s for s in d["jobs"]["purge"]["steps"] if "run" in s)
    for var, inp in (("URLS", "urls"), ("PREFIXES", "prefixes"), ("HOSTS", "hosts")):
        assert step["env"][var] == "${{ github.event.inputs.%s }}" % inp, var
    assert "scripts/cf_purge_body.py" in step["run"]
    assert "--data @/tmp/purge-body.json" in step["run"]
    assert any(s.get("uses", "").startswith("actions/checkout") for s in d["jobs"]["purge"]["steps"]), (
        "the body script lives in the repo; without a checkout it is not there")
    # Judged by the curl ratchet's OWN detector, not a copy of its rules.
    spec = importlib.util.spec_from_file_location(
        "curl_guard", ROOT / "tests" / "test_workflow_curl_guard.py")
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    assert not [s for f, s in g._survey() if "cf-purge.yml" in str(f)], (
        "cf-purge.yml has an unguarded curl step again")
