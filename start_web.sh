#!/usr/bin/env bash
# DC Hub web entrypoint.
#
# Why this exists: weasyprint (Market Brief + Site Report PDF export) needs the
# native Pango/Cairo/GLib shared libraries at runtime. Under Railway's NIXPACKS
# builder those libs live in the Nix store, but the runtime shell does NOT put
# their lib dirs on LD_LIBRARY_PATH, so ctypes can't load libgobject-2.0 and PDF
# rendering fails with "cannot load library 'libgobject-2.0-0'".
#
# Fix: discover every Nix-store lib dir at boot and prepend them to
# LD_LIBRARY_PATH (plus the Debian multiarch dir as a fallback). This is
# hash-agnostic and forward-compatible — it works wherever Nix places the libs,
# and is a no-op if /nix/store isn't present. Runs once per container start.
NIXLIBS="$(find /nix/store -maxdepth 2 -name lib -type d 2>/dev/null | tr '\n' ':')"
export LD_LIBRARY_PATH="${NIXLIBS}${LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}/usr/lib/x86_64-linux-gnu"

# Best-effort: launch the MCP sidecar (same as the prior start command).
# BUT NOT on the Render failover (2026-06-08): Render's free tier is 512MB, and
# running BOTH gunicorn (Flask, ~350MB) AND the Python MCP sidecar (~150MB) OOMs
# the container — the symptom is the endless restart/crash-loop where the
# STARTUP HEALTH CHECK never stabilizes and Render reports "No open HTTP ports
# detected on 0.0.0.0" → 502. Render is a READ failover; it does not serve /mcp
# (that's the separate Node MCP server on Railway), so the sidecar is dead weight
# here. Skip it on Render to fit in 512MB and let gunicorn bind cleanly.
if [ -z "$RENDER" ] && [ -z "$RENDER_SERVICE_ID" ]; then
  bash start_mcp.sh &
else
  echo "start_web: RENDER detected — skipping MCP sidecar to stay under the 512MB memory cap"
fi

# On Render's smaller box, fewer threads = lower memory + faster, cleaner boot.
_THREADS=16
if [ -n "$RENDER" ] || [ -n "$RENDER_SERVICE_ID" ]; then _THREADS=4; fi

exec gunicorn main:app --bind 0.0.0.0:"$PORT" \
  --workers 1 --threads "$_THREADS" --timeout 120 \
  --max-requests 1000 --max-requests-jitter 50
