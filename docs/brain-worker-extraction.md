# Brain → Dedicated Worker Extraction (peak-CPU / RSS isolation)

**Status:** Design / proposal — not yet implemented
**Branch:** `perf/brain-worker-extraction`
**Author:** drafted via Claude Code (Railway agent session, 2026-06-27)

## Problem

`dchub-backend` runs `gunicorn --workers 1 --threads 8` across **2 Railway replicas**
behind Cloudflare. All heavy autonomous-brain work is **leader-gated** (advisory
lock, `is_current_leader()`) so it runs on exactly one replica — the **leader**.

That means the leader replica carries *both* its share of request traffic *and*
the entire brain workload, including Claude (Anthropic) synthesis calls:

- **L8 Orchestrator** (`/api/v1/brain/orchestrator/refresh`) — ~56s Claude call,
  fires 4×/day (03/09/15/21 :45 UTC) via the external `dchub-scheduler.py`.
- L2 narrative refresh, L11 QA agent (currently DISABLED for this exact reason),
  L14 causal, self-heal, etc.

History (from in-code comments):
- main.py:888-896 — a 56s L8 Claude call + brain in-process caches pushed RSS
  over the 2048MB health threshold → watchdog `SIGTERM` restart. Threshold bumped
  to 3072MB as a stopgap.
- dchub-scheduler.py:511-514 — L8 endpoint **already made fire-and-forget**
  (returns 202, Claude call on a background thread) + an **L20 RSS durability
  guard** that refuses new calls when memory is high. The acute *request-blocking*
  crash-loop is closed.
- routes/brain_layer11_qa_agent.py / scheduler:527-530 — L11 QA was disabled with
  the note: *"likely requires moving QA probes to a separate worker."*

So the acute pain is mitigated, but the brain still **executes in-process on the
web replica**, contributing the periodic CPU spike (observed 96% peak on the
single worker) and sustained RSS pressure. The team's own roadmap (the L11 note)
already points at the fix: **run the brain on a dedicated worker, not the web
process.**

## Goal

Web replicas serve HTTP only. A dedicated **worker service** (same image/repo,
different entrypoint + env) owns all brain/scheduler execution. Request-serving
CPU/RSS is fully isolated from 56s Claude calls and brain caches.

Leader-election already exists and is DB-backed (pg advisory lock), so a separate
service participates in the same singleton election with no new coordination
primitive.

## Approach A — env-split (RECOMMENDED, minimal code)

Reuse the existing image. Add one Railway service pointed at the same repo.

1. **New service** `dchub-brain-worker` in project `resourceful-essence`
   (same repo `azmartone67/dchub-backend`, same env group).
2. **Worker start command** — runs the app context + brain, no public HTTP behind
   the LB. Two sub-options:
   - *A1 (simplest):* keep gunicorn but **don't attach a domain**; set env so this
     instance is the brain host. The web service keeps its domain.
   - *A2 (cleaner):* a thin entrypoint `brain_worker.py` that imports the app
     (to get DB pool + brain modules) and blocks on the brain loop without
     binding gunicorn. More work; do later.
   Start with **A1**.
3. **Env flips:**
   | Var | web service (×2 replicas) | brain-worker |
   |---|---|---|
   | `AUTONOMOUS_BRAIN_ENABLED` | `false` | `true` |
   | `BRAIN_HOST` (new flag) | unset/`false` | `true` |
   | `DATABASE_URL` / Neon | same | same (shared lock) |
4. **Gate the in-process brain + scheduler-driven layers on `BRAIN_HOST`** so the
   web replicas no longer start the brain thread (main.py:8818-8828) *or* execute
   the L8/L2/L14 orchestrator endpoints. Today those endpoints run wherever the
   external scheduler POSTs. So also:
5. **Repoint `dchub-scheduler.py`** brain endpoints (`/api/v1/brain/*`) at the
   brain-worker's internal URL instead of the public web URL. (Railway private
   networking — worker is reachable at its `*.railway.internal` host.)

### Why this is low-risk
- No new queue/broker. Leader-election unchanged (same advisory lock).
- Web behavior for end users is unchanged (it already returns 202 for L8).
- Fully reversible: flip `AUTONOMOUS_BRAIN_ENABLED` back to `true` on web and
  delete the worker service.

## Approach B — queue-based (defer)

Web enqueues brain jobs to Redis (already have a `Redis` service in
`grand-energy`); worker consumes. More moving parts, only needed if we want
multiple brain workers or durable job retries. Not warranted now.

## Exact change points (Approach A1)

- `main.py:5236-5251` — introduce `BRAIN_HOST = os.environ.get('BRAIN_HOST')=='true'`;
  gate `ENABLE_BACKGROUND_SCHEDULERS` / brain-thread start on it.
- `main.py:8804-8831` — brain thread start: require `BRAIN_HOST` (not just
  `AUTONOMOUS_BRAIN_ENABLED`).
- `routes/brain_layer8_orchestrator.py` (+ L2/L14 endpoints) — add a guard that
  refuses to *execute* (202 no-op) unless `BRAIN_HOST`, so a stray POST to a web
  replica can't run a 56s call there.
- `dchub-scheduler.py:500-525` — base URL for `/api/v1/brain/*` entries →
  brain-worker internal host.

## Verification plan
1. Deploy worker with `BRAIN_HOST=true`, web with `AUTONOMOUS_BRAIN_ENABLED=false`.
2. Confirm exactly ONE brain leader (logs: `👑 promoted to LEADER` on worker only).
3. Watch a full L8 cycle (next :45 UTC slot): Claude call CPU/RSS appears on
   **worker** metrics, not web.
4. Web replica `service_metrics`: peak CPU should drop materially; RSS flattens.
5. Rollback = flip envs back; no data migration.

## Rollout order (no surprises)
1. Create worker service, **brain still enabled on web** (worker idle/follower).
2. Verify worker can acquire leadership when web brain is off.
3. Flip web `AUTONOMOUS_BRAIN_ENABLED=false` in a low-traffic window.
4. Repoint scheduler. Watch 24h, compare peak before/after.

## Open questions for owner
- OK to add a 3rd always-on service (cost) vs. reusing `heroic-reprieve/worker`?
- Any brain layer that *must* run co-located with web (none found, but confirm)?
- Keep web at `--threads 8` (already shipped) or revisit `--workers 2` once brain
  load is off the web process?
