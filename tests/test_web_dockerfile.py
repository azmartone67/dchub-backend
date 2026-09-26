"""Guards for Dockerfile.web — the builder for dchub-backend (web) and dchub-worker.

The web and worker services build from Dockerfile.web, not Railpack, since the
worker crashlooped twice (#3222, #5544) on a venv whose python symlink pointed
at a patch version the runtime image did not ship. That choice lived in
railway.toml until 2026-09-26 and now lives in .railway/railway.ts (Railway IaC;
the root railway.toml / railway.json are gone). These pin the properties
that make that failure impossible, and the things start_web.sh / start_mcp.sh
need from the image. All static: CI builds the image separately
(.github/workflows/web-image-build.yml).
"""
import os
import re

from tests._railway_iac import ROOT, service_block as _service_block, string_field as _field

SERVICES = ("dchub-backend", "dchub-worker")


def _cfg():
    """The web service's build/deploy settings, shaped like the old toml."""
    return {"build": {"builder": _field("dchub-backend", "builder"),
                      "dockerfilePath": _field("dchub-backend", "dockerfilePath")},
            "deploy": {"startCommand": _field("dchub-backend", "start")}}


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def _dockerfile_path():
    return _cfg().get("build", {}).get("dockerfilePath", "")


def _dockerfile_text():
    path = os.path.join(ROOT, _dockerfile_path())
    assert os.path.isfile(path), f"dockerfilePath does not resolve: {path}"
    with open(path, encoding="utf-8") as f:
        return f.read()


def _code_lines(text):
    """Directive lines only — comments must not satisfy any assertion here."""
    out = [ln.strip() for ln in text.splitlines()
           if ln.strip() and not ln.strip().startswith("#")]
    assert out, "no directive lines parsed out of the Dockerfile"
    return out


def _froms():
    froms = [ln for ln in _code_lines(_dockerfile_text())
             if ln.upper().startswith("FROM ")]
    assert froms, "no FROM lines"
    return froms


def test_web_and_worker_build_from_a_suffixed_dockerfile():
    for svc in SERVICES:
        assert _field(svc, "builder") == "DOCKERFILE", (
            f"{svc} no longer selects DOCKERFILE in .railway/railway.ts — it falls "
            "back to Railpack and the venv/runtime python drift (#3222, #5544) "
            "returns")
        assert _field(svc, "dockerfilePath") == _dockerfile_path(), (
            f"{svc} builds a different Dockerfile than dchub-backend; both run "
            "the same image and start script")
    assert os.path.basename(_dockerfile_path()) not in ("", "Dockerfile"), (
        "dockerfilePath must name a suffixed file; a bare root `Dockerfile` is "
        "auto-detected by every service that builds from this root")
    _dockerfile_text()


def test_both_stages_share_a_base_image_so_the_copied_venv_resolves():
    froms = _froms()
    assert len(froms) >= 2, f"expected a multi-stage build, got {froms}"
    images = {ln.split()[1] for ln in froms}
    assert len(images) == 1, (
        f"stages use different base images {sorted(images)}; the copied venv's "
        "python symlink would point at an interpreter the runtime stage lacks — "
        "the exact failure this builder exists to remove")


def test_the_base_image_matches_runtime_txt():
    pinned = _read("runtime.txt").strip().replace("python-", "")
    assert re.fullmatch(r"\d+\.\d+\.\d+", pinned), f"runtime.txt: {pinned!r}"
    for ln in _froms():
        image = ln.split()[1]
        assert image.startswith(f"python:{pinned}-"), (
            f"runtime.txt pins {pinned} but a stage uses {image!r}")


def test_the_venv_is_where_start_web_probes_and_is_carried_over():
    text = _dockerfile_text()
    built = re.search(r"python\s+-m\s+venv\s+(\S+)", text)
    assert built, "no `python -m venv <path>` in the Dockerfile"
    built = built.group(1)

    probed = re.search(r'^_VENV_PY="([^"]+)/bin/python"', _read("start_web.sh"),
                       re.M)
    assert probed, "start_web.sh no longer sets _VENV_PY=\"<venv>/bin/python\""
    assert built == probed.group(1), (
        f"Dockerfile builds the venv at {built!r}; start_web.sh probes "
        f"{probed.group(1)!r}")
    assert re.search(rf"COPY\s+--from=\S+\s+{re.escape(built)}\s", text), (
        f"no `COPY --from=<stage> {built}` — the venv stays in the build stage")
    env = " ".join(ln for ln in _code_lines(text) if ln.upper().startswith("ENV")
                   or ln.startswith("PATH="))
    assert f"{built}/bin" in env, (
        "the runtime PATH does not include the venv's bin; start_web.sh execs "
        "a bare `gunicorn`")


def test_the_runtime_stage_carries_what_the_start_scripts_call():
    lines = _code_lines(_dockerfile_text())
    last_from = max(i for i, ln in enumerate(lines)
                    if ln.upper().startswith("FROM "))
    runtime = " ".join(lines[last_from:])
    mcp = _read("start_mcp.sh")
    # binary -> the apt package that provides it on Debian
    needed = {"curl": "curl", "pkill": "procps", "fuser": "psmisc"}
    for binary, pkg in needed.items():
        if re.search(rf"^\s*[^#\n]*\b{binary}\b", mcp, re.M):
            assert re.search(rf"\b{pkg}\b", runtime), (
                f"start_mcp.sh calls `{binary}` but the runtime stage does not "
                f"install `{pkg}`")
    assert "libpango-1.0-0" in runtime, (
        "weasyprint's pango libs are missing from the runtime stage; the PDF "
        "exports fail to dlopen them")


def test_the_start_command_is_start_web():
    for svc in SERVICES:
        assert _field(svc, "start") == "bash start_web.sh", svc
    assert 'CMD ["bash", "start_web.sh"]' in _code_lines(_dockerfile_text())


def test_web_runs_at_least_two_replicas():
    # r-2026-07-15: a single web replica was an outage.
    m = re.search(r'\breplicas\s*:\s*\{\s*"us-west2"\s*:\s*(\d+)',
                  _service_block("dchub-backend"))
    assert m and int(m.group(1)) >= 2, "web must run >= 2 replicas"


def test_no_root_config_as_code_file_overrides_iac():
    # A root railway.toml/railway.json overrides these IaC settings on every
    # web/worker deploy until 2026-12-01, and is what a service with no Config
    # File setting falls back to (dchub-daily redeployed as a web-app clone on
    # 2026-09-25). Its re-appearance is a silent override, so it fails here.
    for name in ("railway.toml", "railway.json",
                 "services/daily/railway.json", "services/daily/railway.toml",
                 "railway-extractor.toml"):
        assert not os.path.exists(os.path.join(ROOT, name)), (
            f"{name} is back; it overrides .railway/railway.ts")
