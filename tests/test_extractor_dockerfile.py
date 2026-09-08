"""The extractor cron builds from a Dockerfile, and that switch is scoped.

Railpack materialised the interpreter with mise at CONTAINER START, and twice —
2026-09-06 (8c643bec) and 2026-09-08 (7dfd3fe1) — a container started
mid-reinstall and ran the job against a bare interpreter. #4251 made that
survivable; the Dockerfile makes it unreachable. These guards pin the four
properties that make the switch correct, none of which a build log would show
until after a bad one shipped:

  1. SCOPED — the Dockerfile is NOT named `Dockerfile`. Railway: "Railway will
     always build with a Dockerfile if it finds one." dchub-backend (web) and
     dchub-worker build from the ROOT of this repo on RAILPACK, so a root-level
     `Dockerfile` would switch THEIR builder with no edit to their config. This
     is the guard with blast radius beyond the extractor.
  2. WIRED — railway-extractor.toml names the builder and a dockerfilePath that
     resolves to a file that exists. A typo here is a build that never runs.
  3. THE COPIED VENV IS VALID — both stages must use the SAME base image. The
     runtime stage copies /app/.venv, whose bin/python symlink and pyvenv.cfg
     point into the build stage's interpreter; that only resolves if the same
     interpreter is present in the runtime image, which is what sharing a base
     image guarantees. Different tags = a dangling symlink = exactly the
     bare-interpreter crash this change exists to end.
  4. ONE VENV PATH, DERIVED FROM BOTH SIDES — the path the Dockerfile creates
     and the path the start command probes are parsed out and compared, rather
     than each being checked against a restated literal. A hardcoded "/app/.venv"
     in this file would keep passing after either side moved.

No container runtime is needed (or available in CI): all four are static.
Every helper asserts it FOUND its target first — an empty parse satisfies
every "not in".
"""

import os
import re
import tomllib

ROOT = os.path.join(os.path.dirname(__file__), "..")
TOML = os.path.join(ROOT, "railway-extractor.toml")


def _cfg():
    with open(TOML, "rb") as f:
        return tomllib.load(f)


def _dockerfile_text():
    path = os.path.join(ROOT, _cfg()["build"]["dockerfilePath"])
    assert os.path.exists(path), f"dockerfilePath does not resolve: {path}"
    with open(path, encoding="utf-8") as f:
        return f.read()


def _code_lines(text):
    """Directive lines only — comments must not satisfy any assertion here."""
    out = [ln.strip() for ln in text.splitlines()
           if ln.strip() and not ln.strip().startswith("#")]
    assert out, "no directive lines parsed out of the Dockerfile"
    return out


def test_the_extractor_builds_from_a_dockerfile():
    build = _cfg().get("build", {})
    assert build.get("builder") == "DOCKERFILE", (
        "railway-extractor.toml no longer selects the DOCKERFILE builder — the "
        "service falls back to Railpack, which installs python at container "
        "start and reopens the 09-06/09-08 crash class")
    assert build.get("dockerfilePath"), (
        "builder is DOCKERFILE with no dockerfilePath: Railway would look for a "
        "root `Dockerfile`, which is the one name this change must not use")
    _dockerfile_text()  # asserts the path resolves


def test_no_root_dockerfile_hijacks_the_other_services():
    # dchub-backend (web) and dchub-worker both build from the root of this repo
    # on RAILPACK. Railway auto-detects a root `Dockerfile` and would switch them
    # with no edit to their own config.
    root_df = os.path.join(ROOT, "Dockerfile")
    assert not os.path.exists(root_df), (
        "a root-level `Dockerfile` exists. Railway builds with a Dockerfile "
        "whenever it finds one, so this silently switches the builder for "
        "dchub-backend (web) and dchub-worker too. Give it a suffixed name and "
        "point at it with dockerfilePath.")
    # And the extractor's own file must keep a name that is NOT auto-detected.
    assert os.path.basename(_cfg()["build"]["dockerfilePath"]) != "Dockerfile", (
        "the extractor's dockerfilePath is a bare `Dockerfile` — same hijack")


def test_both_stages_share_a_base_image_so_the_copied_venv_resolves():
    froms = [ln for ln in _code_lines(_dockerfile_text())
             if ln.upper().startswith("FROM ")]
    assert len(froms) >= 2, (
        f"expected a multi-stage build, parsed {len(froms)} FROM line(s)")
    images = {ln.split()[1] for ln in froms}
    assert len(images) == 1, (
        f"stages use different base images {sorted(images)}. The runtime stage "
        "copies /app/.venv, whose interpreter symlink points into the build "
        "stage's image — a different tag leaves it dangling, which IS the "
        "bare-interpreter failure this change exists to remove.")


def test_the_built_venv_is_the_venv_the_start_command_probes():
    text = _dockerfile_text()

    built = re.search(r"python\s+-m\s+venv\s+(\S+)", text)
    assert built, "no `python -m venv <path>` found in the Dockerfile"
    built = built.group(1)

    start = _cfg()["deploy"]["startCommand"]
    probed = re.search(r"\$\{VIRTUAL_ENV:-([^}]+)\}", start)
    assert probed, (
        "the start command no longer carries a ${VIRTUAL_ENV:-<path>} default; "
        "there is nothing left to compare the Dockerfile's venv path against")
    probed = probed.group(1)

    assert built == probed, (
        f"the Dockerfile builds its venv at {built!r} but the start command "
        f"probes {probed!r}. The probe would fail, and since #4251 removed the "
        "python3 fallback the cron now exits 1 rather than running bare.")

    # The runtime stage must actually carry that venv over.
    assert re.search(rf"COPY\s+--from=\S+\s+{re.escape(built)}\s", text), (
        f"no `COPY --from=<stage> {built}` in the runtime stage — the venv is "
        "built and then left behind in the build stage")


def test_the_base_image_matches_runtime_txt():
    with open(os.path.join(ROOT, "runtime.txt"), encoding="utf-8") as f:
        pinned = f.read().strip().replace("python-", "")
    assert pinned, "runtime.txt is empty"
    froms = [ln for ln in _code_lines(_dockerfile_text())
             if ln.upper().startswith("FROM ")]
    assert froms, "no FROM lines"
    image = froms[0].split()[1]
    assert pinned in image, (
        f"runtime.txt pins python {pinned} but the image is {image!r}. The "
        "switch is meant to change WHERE the interpreter comes from, not which.")
