#!/usr/bin/env python3
"""
fix-railway-p2.py — follow-up to fix-railway-p1.py (commit 319fc62).

After p1 landed, 2 of the 4 v1 aliases still 404ed in production:

  - /api/v1/grid/fuel-mix-live  → forwards to /api/grid/fuel-mix-live
      (line 4774 alias), which itself forwards to /api/grid/fuel-mix%s{qs}
      — the `%s` is a printf placeholder orphaned inside an f-string, a
      pre-existing typo. No real /api/grid/fuel-mix handler exists either
      (only a plan-gating manifest entry at line ~13470), so the whole
      chain resolves to 404.

  - /api/v1/energy/retail → my p1 alias forwards to /api/v1/energy/retail/rates,
      which is listed in the plan-gating manifest but has no @app.route
      handler. Also 404 → 404.

Neither underlying "real" handler exists. The p1 forward-aliases strategy
can't work when there's nothing to forward TO. Switch to Strategy B:

  (1) grid_fuel_mix_live_v1_alias — return 200 JSON stub pointing callers
      at the MCP tool (`get_fuel_mix`) which DOES have working data.
  (2) energy_retail_alias — forward to /api/v1/energy/summary instead,
      which at line ~14758 reads from `eia_retail_rates` and returns
      retail_rates data. That's the closest working analog.
  (3) Bonus: fix the pre-existing grid_fuel_mix_live_alias (line 4774)
      that has the `%s`-in-f-string typo — also return the same stub so
      /api/grid/fuel-mix-live stops 404ing for older non-v1 callers.

Idempotent; refuses to double-patch; py_compile self-checks.

Usage (from ~/workspace):
    python3 fix-railway-p2.py --check    # dry-run
    python3 fix-railway-p2.py            # apply + .bak-p2 backup
"""
from __future__ import annotations
import argparse, pathlib, subprocess, sys

MAIN_PY = "main.py"
PATCH_MARKER = "# [fix-railway-p2] stub body — no real handler exists"


# -----------------------------------------------------------------------------
# Patch 2a: pre-existing grid_fuel_mix_live_alias (line ~4774) — broken since
# day one. Its forward target /api/grid/fuel-mix%s{qs} is nonsensical.
# Replace body with a 200 JSON stub.
# -----------------------------------------------------------------------------
OLD_A = (
    "@app.route('/api/grid/fuel-mix-live', methods=['GET'])\n"
    "def grid_fuel_mix_live_alias():\n"
    "    from flask import make_response\n"
    "    # Forward directly instead of redirect (preserves X-Internal-Key header)\n"
    "    from werkzeug.test import EnvironBuilder\n"
    "    with app.test_request_context(f'/api/grid/fuel-mix%s{request.query_string.decode()}', headers=dict(request.headers)):\n"
    "        return app.full_dispatch_request()\n"
)
NEW_A = (
    "@app.route('/api/grid/fuel-mix-live', methods=['GET'])\n"
    "def grid_fuel_mix_live_alias():\n"
    "    " + PATCH_MARKER + "\n"
    "    # Previous body forwarded to '/api/grid/fuel-mix%s{qs}' — the `%s` was a\n"
    "    # printf placeholder orphaned inside an f-string. The target route\n"
    "    # '/api/grid/fuel-mix' is in the plan-gating manifest but has no\n"
    "    # @app.route handler, so the forward always resolved to 404.\n"
    "    # Returning a 200 stub silences log spam and tells callers where to go.\n"
    "    return jsonify({\n"
    "        \"success\": True,\n"
    "        \"deprecated\": True,\n"
    "        \"message\": \"Live fuel-mix REST endpoint not implemented. Use the MCP tool `get_fuel_mix` at https://dchub.cloud/mcp for live generation-source data.\",\n"
    "        \"mcp_tool\": \"get_fuel_mix\",\n"
    "        \"fuel_mix\": []\n"
    "    }), 200\n"
)


# -----------------------------------------------------------------------------
# Patch 2b: grid_fuel_mix_live_v1_alias — added in p1, forwards to the broken
# alias above. Replace its body with the same 200 stub.
# -----------------------------------------------------------------------------
OLD_B = (
    "@app.route('/api/v1/grid/fuel-mix-live', methods=['GET'])\n"
    "def grid_fuel_mix_live_v1_alias():\n"
    "    '''/api/v1/grid/fuel-mix-live -> /api/grid/fuel-mix-live'''\n"
    "    qs = request.query_string.decode()\n"
    "    sep = '?' if qs else ''\n"
    "    with app.test_request_context(f'/api/grid/fuel-mix-live{sep}{qs}', headers=dict(request.headers)):\n"
    "        return app.full_dispatch_request()\n"
)
NEW_B = (
    "@app.route('/api/v1/grid/fuel-mix-live', methods=['GET'])\n"
    "def grid_fuel_mix_live_v1_alias():\n"
    "    " + PATCH_MARKER + "\n"
    "    # p1 version forwarded to the broken /api/grid/fuel-mix-live alias;\n"
    "    # no real handler exists. Return same 200 stub to silence log spam.\n"
    "    return jsonify({\n"
    "        \"success\": True,\n"
    "        \"deprecated\": True,\n"
    "        \"message\": \"Live fuel-mix REST endpoint not implemented. Use the MCP tool `get_fuel_mix` at https://dchub.cloud/mcp for live generation-source data.\",\n"
    "        \"mcp_tool\": \"get_fuel_mix\",\n"
    "        \"fuel_mix\": []\n"
    "    }), 200\n"
)


# -----------------------------------------------------------------------------
# Patch 2c: energy_retail_alias — forwards to /api/v1/energy/retail/rates
# which has no handler. Re-target to /api/v1/energy/summary (line ~14758),
# cf_stub_energy_discovery, which returns eia_retail_rates data.
# -----------------------------------------------------------------------------
OLD_C = (
    "@app.route('/api/v1/energy/retail', methods=['GET'])\n"
    "def energy_retail_alias():\n"
    "    '''/api/v1/energy/retail -> /api/v1/energy/retail/rates'''\n"
    "    qs = request.query_string.decode()\n"
    "    sep = '?' if qs else ''\n"
    "    with app.test_request_context(f'/api/v1/energy/retail/rates{sep}{qs}', headers=dict(request.headers)):\n"
    "        return app.full_dispatch_request()\n"
)
NEW_C = (
    "@app.route('/api/v1/energy/retail', methods=['GET'])\n"
    "def energy_retail_alias():\n"
    "    '''/api/v1/energy/retail -> /api/v1/energy/summary (returns eia_retail_rates data)'''\n"
    "    # [fix-railway-p2] redirected from ghost /api/v1/energy/retail/rates\n"
    "    # to real handler cf_stub_energy_discovery at /api/v1/energy/summary.\n"
    "    qs = request.query_string.decode()\n"
    "    sep = '?' if qs else ''\n"
    "    with app.test_request_context(f'/api/v1/energy/summary{sep}{qs}', headers=dict(request.headers)):\n"
    "        return app.full_dispatch_request()\n"
)


MAIN_PATCHES = [
    ("pre-existing grid_fuel_mix_live_alias → 200 stub",   OLD_A, NEW_A, 1),
    ("grid_fuel_mix_live_v1_alias         → 200 stub",     OLD_B, NEW_B, 1),
    ("energy_retail_alias  → /api/v1/energy/summary",      OLD_C, NEW_C, 1),
]


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def die(msg: str, code: int = 1) -> None:
    sys.stderr.write(f"ERROR: {msg}\n"); sys.exit(code)

def read_file(p: str) -> str:
    pp = pathlib.Path(p)
    if not pp.is_file(): die(f"file not found: {p}")
    return pp.read_text(encoding="utf-8")

def write_file(p: str, data: str) -> None:
    pp = pathlib.Path(p)
    if pp.is_file():
        bak = pp.with_suffix(pp.suffix + ".bak-p2")
        if not bak.exists():
            bak.write_text(pp.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"  backup: {bak.name}")
    pp.write_text(data, encoding="utf-8")


def apply_patches(src: str, dry: bool) -> tuple[str, list[str]]:
    if src.count(PATCH_MARKER) >= 2:
        die(f"{MAIN_PY}: already patched (p2 markers present). Refusing to double-patch.")
    changes = []
    for label, old, new, expected in MAIN_PATCHES:
        count = src.count(old)
        if count != expected:
            die(f"{MAIN_PY}: patch '{label}' anchor count={count}, expected {expected}. "
                f"Likely the p1 state has drifted or been re-edited.")
        if not dry:
            src = src.replace(old, new)
        changes.append(f"  ✓ {label} (×{expected})")
    return src, changes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="dry-run")
    args = ap.parse_args()
    dry = args.check

    src = read_file(MAIN_PY)
    print(f"== {MAIN_PY} ({len(src.splitlines())} lines) ==")
    new_src, changes = apply_patches(src, dry)
    for c in changes: print(c)

    if not dry:
        write_file(MAIN_PY, new_src)
        print(f"  wrote: {MAIN_PY} ({len(new_src.splitlines())} lines)")
        print("\n== Python syntax self-check ==")
        r = subprocess.run([sys.executable, "-m", "py_compile", MAIN_PY],
                           capture_output=True, text=True)
        if r.returncode == 0:
            print(f"  ✓ {MAIN_PY}")
        else:
            print(f"  ✗ {MAIN_PY}:\n{r.stderr}")
            die(f"syntax check failed — restore {MAIN_PY}.bak-p2 and inspect")

    print("\n== Next steps ==")
    print("  1) git diff main.py")
    print("  2) git add main.py")
    print("  3) git commit -m 'fix(railway): stub fuel-mix-live + retarget energy/retail alias'")
    print("  4) git push")
    print("  5) after deploy (~90s):")
    print("     curl -s  https://dchub-backend-production.up.railway.app/api/v1/grid/fuel-mix-live | head -c 200 ; echo")
    print("     curl -sI https://dchub-backend-production.up.railway.app/api/v1/energy/retail | head -3")


if __name__ == "__main__":
    main()
