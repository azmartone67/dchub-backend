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
# r-rolesplit (2026-07-02): the sidecar serves MCP traffic — web-only.
# The DCHUB_ROLE=worker service binds gunicorn purely for its healthcheck
# and gets no public traffic, so the sidecar is dead weight there.
if [ "$DCHUB_ROLE" = "worker" ]; then
  echo "start_web: DCHUB_ROLE=worker — skipping MCP sidecar (web role serves MCP)"
elif [ -z "$RENDER" ] && [ -z "$RENDER_SERVICE_ID" ]; then
  bash start_mcp.sh &
else
  echo "start_web: RENDER detected — skipping MCP sidecar to stay under the 512MB memory cap"
fi

# On Render's smaller box, fewer threads = lower memory + faster, cleaner boot.
_THREADS=16
if [ -n "$RENDER" ] || [ -n "$RENDER_SERVICE_ID" ]; then _THREADS=4; fi

# r82 PERF: --max-requests 1000 → 20000. At current volume 1000 recycled the
# worker every few MINUTES, which wiped every in-process cache (grid intel,
# surface_brain, dcpi/markets memos), re-armed the 180s staggered startup +
# 90s brain delay, and churned the leader lock — the "load-bearing" latency
# +self-probe-storm cause. RSS is provably FLAT at ~441MB (5 samples) with a
# 60s gc.collect+malloc_trim loop + 3-tier guard (1500/1800/2200MB) already
# doing real leak defense, and the host has huge headroom — so 1000 was ~20×
# over-cautious. 20000 keeps a slow safety-net recycle (every several hours)
# without the storm. (preload + 2 workers is a separate follow-up PR — needs
# the post-fork thread-start fix; NOT a config-only change.)
# r-workerproxy (2026-07-02): bind the IPv6 wildcard, not 0.0.0.0.
# Railway private networking is IPv6-only — the web→worker job proxy
# (DCHUB_WORKER_INTERNAL_URL → dchub-worker.railway.internal) can only
# reach the worker if gunicorn listens on ::. Linux dual-stack
# (bindv6only=0) means :: still accepts the IPv4/public-edge traffic.
exec gunicorn main:app --bind "[::]:$PORT" \
  --workers 1 --threads "$_THREADS" --timeout 120 \
  --max-requests 20000 --max-requests-jitter 1000
