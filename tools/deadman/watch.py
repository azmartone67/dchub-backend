#!/usr/bin/env python3
"""Off-worker dead-man watcher — the independent watcher the loop audit called for.

Runs on GitHub Actions (deadman-watch.yml), NOT the Railway APScheduler thread that
drives all ~75 in-worker jobs. A dead-man for the backend MUST live off the backend —
if the worker (or Railway) is wedged, this still fires.

Per run it:
  1. asks the GitHub Actions API for each critical WORKFLOW's last SUCCESSFUL run,
  2. BEATS the backend ingest_runs ledger with that last-success (durable record),
  3. computes overdue ITSELF (last success > 2x cadence) — self-sufficient, works even
     if the backend/ledger is unreachable,
  4. also pulls /api/v1/ops/deadman for backend-scheduler feeds that beat directly
     (jobs that aren't GitHub workflows), and
  5. opens/updates ONE dedup'd 'deadman' GH issue listing everything overdue.

"Cron green proves nothing" — this watches last-SUCCESS, not "a run exists".

Env: GH_REPO (owner/name), GH_TOKEN (gh auth), DCHUB_ADMIN_KEY (beat), API_BASE.
"""
import os
import json
import subprocess
import datetime
import urllib.request

REPO = os.environ.get("GH_REPO", "azmartone67/dchub-backend")
API = (os.environ.get("API_BASE", "https://dchub.cloud")).rstrip("/")
ADMIN = os.environ.get("DCHUB_ADMIN_KEY", "")
# Cloudflare bot-blocks the default Python-urllib UA (403) — identify ourselves.
UA = "dchub-deadman-watch/1.0 (+https://dchub.cloud/api/v1/ops/deadman)"

# The registry: workflow file -> max hours expected between SUCCESSFUL runs.
# Alarm fires at 2x this. Keep in rough sync with each workflow's own cron; generous
# so a single missed run is not an alarm, but a stalled loop is. Source of truth for
# "what loops must keep succeeding".
WORKFLOWS = {
    # high-frequency telemetry (cron minutes/20min) — a couple hours of silence is fine
    "iso-data-pull.yml": 3,
    # ★2026-08-10: eia-pricing / iso-lmp / news-ner removed from the
    # conclusion-based watcher for the SAME reason osm-crawl was on 2026-08-08
    # (SH52-002, B2) — one-direction masking. Each of these producers now
    # computes its own status (success / no_new_data / error), and this
    # watcher's 2h conclusion beat was overwriting it with a bare "success"
    # carrying no rows_inserted. Measured 2026-08-10: all three read
    # note="beat by deadman-watch (GH Actions API)" on the live board, and
    # news-ner-discovery beat an HONEST no_new_data (HTTP 200) that was clobbered
    # back to success within the cycle — so its 5-run zero streak never cleared
    # and a healthy feed stayed red. The producer is now the single writer; the
    # ledger fold (block 4) still covers staleness. Each producer declares the
    # cadence this registry had for it, so no alert threshold moves.
    # ★2026-08-19: iso-queue-ingest removed for the SAME reason, and it is the
    # first case where the masking would have run in the OTHER direction. #2931
    # replaced that workflow's hardcoded `status:"success"` beat with the
    # OBSERVED outcome (error when the aggregate ingest did not run, and
    # rows_inserted omitted rather than a false 0). This watcher's 2h
    # conclusion beat would have overwritten every one of those honest errors
    # with a bare success within the cycle — turning a fix for a false green
    # back into a false green, quietly. The producer is now the single writer
    # and declares cadence_hours:30, the value this registry had, so no alert
    # threshold moves. tests/test_alarm_reachability.py fences both halves.
    # daily loops
    # ★2026-08-08 (audit SH52-002, B2): osm-crawl removed from the
    # conclusion-based watcher — its producer writes an HONEST error beat on
    # zero-fetch, and the 2h conclusion-writer here was OVERWRITING that error
    # with success (one-direction masking). The ledger fold (block 4) still
    # covers the feed; the producer is now the single writer.
    "infra-growth-tracker.yml": 30,
    # the other watcher (watch the watchers) — every 6h
    "dchub-ingestion-watchdog.yml": 10,
    # weekly infra refreshes
    "gas-pipeline-ingest.yml": 190,
    "transmission-ingest.yml": 190,
    "power-plants-ingest.yml": 190,
    "generator-inventory-ingest.yml": 190,
    "fcc-fiber-refresh.yml": 200,
    # monthly
    "planned-generators-ingest.yml": 780,
    "gem-refresh.yml": 780,
    # audit-named blind spots (2026-07-19): Neon DR, billing reconciliation, sitemap
    # regen, DR-restore proof, failover canary — loops whose silent death is expensive.
    "backup-neon-r2.yml": 30,          # daily 08:00 — Neon PITR/pg_dump -> R2
    "billing-reconcile-daily.yml": 30, # daily 08:23 — invoices_paid_count vs Stripe
    "seo-sitemap-and-warm.yml": 30,    # daily 07:40 — sitemap re-crawl + narrative warm
    "restore-test.yml": 190,           # weekly Mon — prove the Neon backup restores
    "failover-canary.yml": 190,        # weekly Mon — Railway->Render->KV failover path
    # ★2026-08-09 anti-drift blind-spot closure: watch the DETECTORS themselves.
    # qa-superuser is the platform's best detector (outside-in, real-envelope);
    # it was unwatched, so its silent death would blind the whole see→fix loop.
    "qa-superuser.yml": 10,            # every 4h — overdue after ~2.5 cycles
    # off-board growth/conversion/media loops (2026-07-20 loop-coverage additions).
    # OBSERVABILITY ONLY: this watches each workflow's last SUCCESS — it does not arm
    # any send. Those loops stay dry-run/gated by their own Railway env flags.
    # (Only scheduled workflows are registered — manual/workflow_dispatch-only ones
    #  like audit-conversions.yml and the datacentermap-* runners are deliberately
    #  omitted: with no cron they would recede past cadence and false-alarm forever.)
    "activation-nudge-daily.yml": 30,   # daily 15:43 UTC — activation / first-query nudge
    "funnel-autotune.yml": 30,          # daily 16:00 UTC — trial unbound-cap auto-tune
    "churn-watcher.yml": 190,           # weekly Mon 14:27 UTC — at-risk churn sweep
    "facility-snapshot-daily.yml": 30,  # daily 05:19 UTC — competitor-gap facility snapshot
    # ★2026-08-14 (pm-brief Lens A1): the PM chase loop itself — nobody chased
    # the PM. A dead cron (GH 60-day auto-disable, deleted secret) alarmed
    # nothing while GET /pm-brief kept serving the newest-ever row as
    # "latest". This watches the WORKFLOW's last success; run_collection()
    # separately beats feed pm-brief-collection (producer beat, ledger fold)
    # so the cron dying and the collection silently no-oping alarm on
    # independent paths.
    "pm-brief-daily.yml": 30,           # daily 10:23 UTC — PM brief board collection
    "media-organism-tick.yml": 3,       # hourly :22 — media organism heartbeat
    # brain cadence (2026-07-25 brain-ascension #28): the registry covered
    # ingest/growth/media loops but NOT ONE brain-* workflow — if every brain
    # loop stopped succeeding, nothing alarmed. These are the liveness spine;
    # cadences are 2-4x each workflow's own cron so one missed run never alarms.
    "cron-heartbeat.yml": 3,            # every 5 min — drives the 93-job dispatcher
    "brain-autonomy.yml": 3,            # every 30 min — core autonomy tick
    "brain-autopilot.yml": 3,           # :15,:45 — 5-min-MTTR remediation
    "brain-verify.yml": 3,              # :10,:30,:50 — outcome verification
    "brain-master-tick.yml": 8,         # every 2h — master orchestrator tick
    "brain-model-reachability.yml": 8,  # every 2h — model roster probe (Fable gate)
    "brain-layer5.yml": 20,             # every 6h — codegen proposals
    "brain-inspector.yml": 20,          # every 6h — inspector pass
    "brain-reasoning-layers.yml": 20,   # every 6h — L7 evolving detectors
    "brain-self-direct.yml": 16,        # every 4h — self-directed work queue
    "brain-mirror.yml": 30,             # daily 09:30 — honest layer-grading reflection
    "brain-stuck-drain.yml": 30,        # daily 08:20 — stuck-findings drain
    "strategic-briefing-weekly.yml": 190,   # Mon 14:20 — L6 synthesis + digest
    "brain-lifecycle-curator.yml": 190,     # Mon 07:37 — L23 moat curator
    # ★2026-08-30 batch-3 coverage sweep. All four were SCHEDULED, doing real
    # work against production, and invisible to this board. mcp-facts-export had
    # failed 60 consecutive runs — 17 days — with nobody looking; the other three
    # surfaced only because the sweep cross-checked every scheduled workflow
    # against the Actions API. Cadence = cron interval x1.5 (one missed fire plus
    # slack); all four clear _assert_watch_margin (overdue >= 3.0h).
    "mcp-facts-export.yml": 36,          # daily 05:17
    "ai-surface-partner-sync.yml": 36,   # daily 15:23
    "mcp-registry-watch.yml": 252,       # weekly Mon 14:00
    "needs-decision-digest.yml": 252,    # weekly Mon 14:11
    # ★2026-08-30 batch-3 TRIAGE. The 65 ledgered producers were classified
    # against live evidence — the deadman board, the Actions API, and each
    # workflow's own cron — not by eye. These 40 are recurring producers whose
    # failure would be silent: nothing else alarms on them and they are not on
    # the board by any other route. Cadence = cron interval x1.5 (one missed
    # fire plus slack); every one clears _assert_watch_margin.
    # Three are ALREADY below 80% success and will paint the board red on the
    # watcher's next pass. That is the correct outcome, not a regression:
    # ingestion-integrity-tick (12/19), monthly-trend-cron (1/3),
    # tool-tuner-reseed (10/13).
    "actuation-shell-investigate.yml": 252,         # every 168h
    "agent-digest-weekly.yml": 252,                 # every 168h
    "ai-adoption-master-tick.yml": 9,               # every 6h
    "ai-citation-probe.yml": 36,                    # every 24h
    "brain-autonomy-daily.yml": 36,                 # every 24h
    "dcpi-weekly-digest.yml": 252,                  # every 168h
    "deploy-integrity.yml": 6,                      # every 4h
    "detector-scout-daily.yml": 36,                 # every 24h
    "digest-daily.yml": 252,                        # every 168h
    "founder-briefing.yml": 36,                     # every 24h
    "indexnow-dcpi-daily.yml": 36,                  # every 24h
    "indexnow.yml": 9,                              # every 6h
    "ingestion-integrity-tick.yml": 36,             # every 24h
    "lp-alerts-nightly.yml": 36,                    # every 24h
    "mcp-conversion-outreach.yml": 252,             # every 168h
    "mcp-identity-backfill.yml": 9,                 # every 6h
    "mcp-outreach.yml": 36,                         # every 24h
    "media-citations-testimonials.yml": 126,        # every 84h
    "media-comment-dm-chain.yml": 18,               # every 12h
    "monthly-trend-cron.yml": 36,                   # every 24h
    "newsroom-auto.yml": 4.5,                       # every 3h
    "outreach-daily.yml": 36,                       # every 24h
    "paid-account-health-daily.yml": 36,            # every 24h
    "paid-intent-digest-weekly.yml": 252,           # every 168h
    "pockets-weekly-digest.yml": 252,               # every 168h
    "press-scan-daily.yml": 36,                     # every 24h
    "sentinel-master-tick.yml": 6,                  # every 4h
    "sitemap-snapshot.yml": 6,                      # every 4h
    "slug-freeze-daily.yml": 9,                     # every 6h
    "sponsor-crawl-snapshot-daily.yml": 36,         # every 24h
    "sponsor-crawl-snapshot.yml": 36,               # every 24h
    "testimonials-seed.yml": 36,                    # every 24h
    "tool-tuner-reseed.yml": 252,                   # every 168h
    "upgrade-nudge-weekly.yml": 252,                # every 168h
    "upgrade-nudge.yml": 252,                       # every 168h
    "visitor-intel-backfill.yml": 36,               # every 24h
    "visual-sentinel.yml": 9,                       # every 6h
    "white-glove-propagate-backstop.yml": 36,       # every 24h
    "white-glove-tick-daily.yml": 36,               # every 24h
    "winback-weekly.yml": 252,                      # every 168h
}

NOW = datetime.datetime.now(datetime.timezone.utc)
ISSUE_LABEL = "deadman"


def gh_json(args, retries=3):
    """Run a `gh` command returning JSON, retrying transient failures (GitHub 5xx).

    Returns the parsed JSON, or None if it genuinely could not be fetched — callers
    MUST treat None as "unknown / watcher blind", never as "the loop is dead".
    """
    last = ""
    for attempt in range(retries):
        try:
            out = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=60)
        except Exception as e:  # noqa: BLE001
            last = str(e)
        else:
            if out.returncode == 0:
                try:
                    return json.loads(out.stdout or "null")
                except Exception:  # noqa: BLE001
                    return None
            last = out.stderr.strip()[:160]
        # transient GitHub outage (503/timeout) — brief backoff then retry
        if attempt < retries - 1:
            try:
                import time
                time.sleep(3 * (attempt + 1))
            except Exception:  # noqa: BLE001
                pass
    print(f"gh failed after {retries}x {args[:3]}: {last}")
    return None


def last_success(workflow):
    """Return (iso_ts, status_note) for the workflow's most recent successful run.

    The status must describe CURRENT health, not merely "a success exists
    somewhere in the last 20 runs". 2026-07-25: this beat status='success'
    for eia-pricing-ingest while its two most recent runs had just failed,
    overwriting the producer's own status='error' and silently un-redding a
    live-failing loop on the board. The timestamp still reports the genuine
    last success (that is what cadence is measured against) — but a currently
    failing loop now carries a status OUTSIDE _OK_STATUS so it stays visible.
    """
    d = gh_json(
        ["run", "list", "--repo", REPO, "--workflow", workflow, "--limit", "20",
         "--json", "conclusion,updatedAt,status"]
    )
    if d is None:
        return None, "actions-api-error"
    if not d:
        return None, "never-run"
    # Newest COMPLETED run decides current health; an in-flight run is not a
    # failure, so skip entries with no conclusion yet.
    newest_done = next((r for r in d if r.get("conclusion")), None)
    currently_failing = bool(
        newest_done and newest_done.get("conclusion") != "success")
    for r in d:
        if r.get("conclusion") == "success":
            ts = r.get("updatedAt")
            if currently_failing:
                return ts, ("latest-run-failed (latest="
                            + str(newest_done.get("conclusion")) + ")")
            return ts, "success"
    # runs exist but none of the recent ones succeeded
    latest = d[0]
    return None, f"no-recent-success (latest={latest.get('conclusion') or latest.get('status')})"


def beat(feed, last_run_iso, status, cadence_h):
    """Record the last-success into the durable backend ledger (best effort)."""
    if not ADMIN:
        return
    body = json.dumps({
        "feed": feed,
        "last_run": last_run_iso,
        "status": status,
        "cadence_hours": cadence_h,
        "note": "beat by deadman-watch (GH Actions API)",
    }).encode()
    req = urllib.request.Request(
        f"{API}/api/v1/admin/ingest-runs/beat", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Admin-Key": ADMIN, "User-Agent": UA},
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # noqa: BLE001
        print(f"beat {feed} failed (non-fatal): {e}")


def age_hours(iso_ts):
    try:
        t = datetime.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
    return (NOW - t).total_seconds() / 3600.0


# How often deadman-watch.yml itself is scheduled. MUST be kept in sync with the
# cron in that workflow — _assert_watch_margin() below is what makes the coupling
# visible instead of silent.
WATCH_INTERVAL_H = 2.0
# A feed is overdue at 2x its cadence. If the watcher runs less often than that, it
# CANNOT keep the feed green: the feed ages past the threshold between two beats and
# the board reports a dead loop that is in fact running fine. Demand real headroom,
# not just "less than" — GitHub cron drift of 25-35 min is routine.
WATCH_MARGIN = 1.5



# ★ EVERY scheduled workflow that touches production must be either WATCHED
# (in WORKFLOWS above) or listed HERE with a reason. Absence is not a decision:
# the board reported "150 tracked, 0 overdue" — which reads as full coverage —
# while 67 scheduled, production-touching workflows sat outside its view and two
# of them were failing. The deadman was not lying. It was not looking.
#
# ★ 67 IS A LOWER BOUND. It counts workflows whose YAML names an API endpoint;
# one that shells into Python which then calls the API is not detected. Stated
# rather than rounded off, because the honest number here is "at least 67".
#
# ★ THE BACKLOG RATCHETS DOWN, NEVER UP. tests/test_deadman_coverage.py pins
# the untriaged count so a new unwatched producer cannot join silently — the
# way all 67 of these did. Draining an entry means either watching it or
# replacing "untriaged" with a real reason.
NOT_WATCHED = {
    # ── Already on the board by another route. Each beats ITSELF from its own
    #    workflow; verified against the live /api/v1/ops/deadman feed list, not
    #    inferred from the code that sends the beat.
    "agent-pay-shell-tick.yml": "already on the board as feed 'agent-pay-shell-daily' (self-beat)",
    "daily-infra-sync.yml": "already on the board as feed 'daily-infra-sync' (self-beat)",
    "eia-pricing-ingest.yml": "already on the board as feed 'eia-pricing-ingest' (self-beat)",
    "iso-lmp-ingest.yml": "already on the board as feed 'iso-lmp-ingest' (self-beat)",
    "iso-queue-ingest.yml": "already on the board as feed 'iso-queue-ingest' (self-beat)",
    "kill-switch-probe.yml": "already on the board as feed 'kill-switch-probe' (self-beat)",
    "news-ner-discovery.yml": "already on the board as feed 'news-ner-discovery' (self-beat)",
    "osm-crawl.yml": "already on the board as feed 'osm-crawl' (self-beat)",
    "story-debt-author.yml": "already on the board as feed 'story-debt-author' (self-beat)",

    # ── Too frequent for THIS watcher, which is a structural fact and not a
    #    judgement call. deadman-watch runs every 2h and a feed goes overdue at
    #    2x cadence, so anything under a 1.5h cadence would FALSE-RED on ordinary
    #    cron drift — the exact 2026-07-30 incident _assert_watch_margin records.
    #    To be tracked, these must beat themselves (see iso-lmp-ingest.yml).
    "auto-rollback.yml": "runs every 0.08h — below the 1.5h floor this watcher can hold; must self-beat",
    "data-pulse.yml": "runs every 0.25h — below the 1.5h floor this watcher can hold; must self-beat",
    "dchub-self-healing.yml": "runs every 1h — below the 1.5h floor this watcher can hold; must self-beat",
    "evolve-cron.yml": "runs every 1h — below the 1.5h floor this watcher can hold; must self-beat",
    "external-watchdog.yml": "runs every 0.08h — below the 1.5h floor this watcher can hold; must self-beat",
    "failover-warm.yml": "runs every 0.33h — below the 1.5h floor this watcher can hold; must self-beat",
    "health-check.yml": "runs every 0.08h — below the 1.5h floor this watcher can hold; must self-beat",
    "heartbeat-auto.yml": "runs every 0.25h — below the 1.5h floor this watcher can hold; must self-beat",
    "regression-canary.yml": "runs every 1h — below the 1.5h floor this watcher can hold; must self-beat",
    "render-build-monitor.yml": "runs every 0.25h — below the 1.5h floor this watcher can hold; must self-beat",
    "site-qa.yml": "runs every 0.25h — below the 1.5h floor this watcher can hold; must self-beat",
    "uptime-probe.yml": "runs every 0.08h — below the 1.5h floor this watcher can hold; must self-beat",

    # ── Failure is already visible: these file a GitHub issue when they fail,
    #    so a silent death is not the failure mode the board defends against.
    "agent-pay-signal-watch.yml": "files a GitHub issue on failure — visible without the board",
    "anomaly-digest.yml": "files a GitHub issue on failure — visible without the board",
    "data-sync.yml": "files a GitHub issue on failure — visible without the board",
    "dchub-osm-refresh.yml": "files a GitHub issue on failure — visible without the board",
}

# The ratchet. Lower it as entries are drained; the fence fails if it grows.
MAX_UNTRIAGED = 0

def _assert_watch_margin():
    """Report any feed this watcher structurally cannot keep green.

    2026-07-30: the watcher ran every 6h while seven feeds were registered at
    cadence 3h — overdue at exactly 6h. Zero margin, so ordinary cron drift flipped
    all seven red simultaneously (07-28 and again 07-30) while every underlying
    workflow was succeeding minutes earlier. Six hours of false RED on the board that
    exists to tell us what is really dead.

    Warn-only ON PURPOSE: a hard exit here would take the watcher down, and a blind
    watcher is strictly worse than a noisy one. The 2026-07-19 rule stands — watcher
    problems must never masquerade as dead loops.
    """
    floor = WATCH_INTERVAL_H * WATCH_MARGIN
    bad = {wf: cad for wf, cad in WORKFLOWS.items() if (2.0 * cad) < floor}
    if bad:
        print(f"::error::deadman-watch runs every {WATCH_INTERVAL_H}h but "
              f"{len(bad)} feed(s) go overdue sooner than {floor}h — they will "
              f"FALSE-RED on ordinary cron drift: "
              + ", ".join(f"{wf}(cad {cad}h -> overdue {2*cad}h)"
                          for wf, cad in sorted(bad.items())))
        print("::error::fix: raise the cadence, run this watcher more often, or have "
              "that loop beat itself from its own workflow (see iso-lmp-ingest.yml)")
    else:
        tightest = min(WORKFLOWS.values()) if WORKFLOWS else None
        if tightest is not None:
            print(f"watch margin OK: interval {WATCH_INTERVAL_H}h vs tightest "
                  f"overdue threshold {2*tightest}h")
    return bad


def main():
    _assert_watch_margin()
    overdue = []          # (feed, reason, cadence) — a loop genuinely stopped succeeding
    blind = []            # feeds the Actions API could not report on (watcher blind)
    seen_feeds = set()

    # 1-3: workflow crons via the Actions API (self-sufficient — no backend needed)
    for wf, cad in WORKFLOWS.items():
        feed = wf[:-4] if wf.endswith(".yml") else wf
        seen_feeds.add(feed)
        ts, status = last_success(wf)
        # An API error is watcher-blindness, NOT a dead loop — never alarm on it, and
        # do not poison the ledger with a false "down" beat.
        if status == "actions-api-error":
            blind.append(feed)
            print(f"blind    {feed}: actions-api unavailable")
            continue
        beat(feed, ts, status, cad)
        if ts is None:
            overdue.append((feed, status, cad))
            print(f"OVERDUE  {feed}: {status}")
            continue
        ah = age_hours(ts)
        if ah is None:
            overdue.append((feed, f"unparseable last-success ts {ts}", cad))
        elif ah > 2 * cad:
            overdue.append((feed, f"last success {ah:.0f}h ago (>2x cadence {cad}h)", cad))
            print(f"OVERDUE  {feed}: {ah:.0f}h > {2*cad}h")
        else:
            print(f"ok       {feed}: {ah:.0f}h (<= {2*cad}h)")

    # If the Actions API was down for MOST feeds it is a GitHub outage, not a fleet of
    # dead loops — stay quiet (the next run re-checks) rather than fire a phantom alarm.
    if blind and len(blind) > len(WORKFLOWS) / 2:
        print(f"\nDEADMAN: GitHub Actions API degraded ({len(blind)}/{len(WORKFLOWS)} "
              f"unreadable) — skipping this cycle to avoid a false alarm")
        return

    # 4: backend-scheduler feeds that beat the ledger directly (not GitHub workflows)
    try:
        greq = urllib.request.Request(
            f"{API}/api/v1/ops/deadman", headers={"User-Agent": UA})
        with urllib.request.urlopen(greq, timeout=20) as r:
            d = json.loads(r.read())
        for rec in d.get("overdue", []):
            feed = rec.get("feed")
            if feed and feed not in seen_feeds:
                overdue.append((feed, "; ".join(rec.get("reasons", [])) or "overdue",
                                rec.get("cadence_hours")))
                print(f"OVERDUE  {feed} (ledger): {rec.get('reasons')}")
        print(f"ledger: tracked={d.get('tracked')} any_overdue={d.get('any_overdue')}")
    except Exception as e:  # noqa: BLE001
        print(f"ledger read failed (non-fatal): {e}")

    # ★2026-08-09 who-watches-the-watcher: deadman-watch cannot detect its OWN
    # death (a check only runs when the watcher runs). So it writes a liveness
    # beat to the ledger every run, and shell #52 — which runs on the Railway
    # APScheduler, a DIFFERENT scheduler from this GitHub-Actions job — asserts
    # that beat is fresh. deadman-watch already watches shell #52's
    # audit-closure beat via the ledger fold, so the two independent schedulers
    # now watch each OTHER: neither can go silently dark. Non-fatal.
    # ★2026-08-21: this block sat AFTER the `if not overdue: return` below, so
    # on every healthy run — the common case — main() returned before beating.
    # The public board read deadman-watch OVERDUE (last beat 13:05, 5.7h old)
    # while this job had succeeded at 14:57 and 16:56, and audit-closure lane A
    # failed on "the deadman watcher is itself alive". The self-beat is the one
    # thing that must happen regardless of the verdict, so it runs here, after
    # the ledger fold and BEFORE any early return.
    try:
        beat("deadman-watch", NOW.isoformat(), "success", WATCH_INTERVAL_H)
        print("self-beat written (deadman-watch alive)")
    except Exception as e:  # noqa: BLE001
        print(f"self-beat failed (non-fatal): {e}")

    # 5: alarm — one dedup'd issue
    blind_note = f" ({len(blind)} unreadable this cycle: {', '.join(blind)})" if blind else ""
    if not overdue:
        print(f"\nDEADMAN ✓ all {len(seen_feeds) - len(blind)} readable loops within "
              f"cadence{blind_note}")
        return

    if os.environ.get("DEADMAN_DRY_RUN"):
        print(f"\nDRY_RUN: would alarm on {len(overdue)} loop(s); no issue opened")
        return

    title = f"[deadman] {len(overdue)} loop(s) not succeeding within cadence"
    lines = [
        "The off-worker dead-man watcher found loops whose **last SUCCESSFUL run** is "
        "overdue (>2x cadence), reporting failure, or emitting future-dated content.",
        "",
        "| feed | issue |",
        "| --- | --- |",
    ]
    for feed, reason, _cad in sorted(overdue):
        lines.append(f"| `{feed}` | {reason} |")
    lines += [
        "",
        f"_Checked {len(seen_feeds)} GitHub-cron loops + the backend ingest_runs ledger."
        + (f" {len(blind)} were unreadable this cycle (Actions API): "
           f"{', '.join(blind)}._" if blind else "_"),
        f"_Generated {NOW.isoformat()} by deadman-watch.yml — {API}/api/v1/ops/deadman_",
    ]
    body = "\n".join(lines)
    print("\n" + body)

    existing = gh_json(
        ["issue", "list", "--repo", REPO, "--state", "open", "--label", ISSUE_LABEL,
         "--json", "number,title"]
    )
    if existing:
        num = existing[0]["number"]
        subprocess.run(
            ["gh", "issue", "comment", str(num), "--repo", REPO, "--body", body],
            check=False,
        )
        print(f"updated existing deadman issue #{num}")
    else:
        subprocess.run(
            ["gh", "issue", "create", "--repo", REPO, "--title", title,
             "--body", body, "--label", ISSUE_LABEL],
            check=False,
        )
        print("opened new deadman issue")

    triage_red_feeds(overdue)


# ── per-feed triage (2026-08-07, audit SH52-008 / LC5 minimal closure) ──
#
# The single dedup'd issue above accumulated 62 append-comments over 18 days
# while its red feeds sat unfixed 5-11 days each — a monologue, not a work
# queue. This routes each PERSISTENTLY red feed into its own triage issue
# (one per feed, deduped by title, capped per run) and closes it when the
# feed goes green — detection becomes work items with individual state.
# L8 rule intact: issues only, never an auto-executed fix.

TRIAGE_LABEL = "deadman-triage"
TRIAGE_MAX_NEW_PER_RUN = 5


def triage_red_feeds(overdue):
    """Open one triage issue per overdue feed; close triage issues whose
    feed has recovered. Fail-soft: triage must never fail the watch run."""
    try:
        red = {feed for feed, _r, _c in overdue}
        open_triage = gh_json(
            ["issue", "list", "--repo", REPO, "--state", "open",
             "--label", TRIAGE_LABEL, "--json", "number,title"]) or []
        by_feed = {}
        for it in open_triage:
            t = it.get("title") or ""
            if t.startswith("[deadman-triage] "):
                by_feed[t[len("[deadman-triage] "):].strip()] = it["number"]

        # Close recovered feeds' triage issues — red→green is the loop
        # actually closing, and the auto-close is what keeps this from
        # becoming a second graveyard.
        for feed, num in by_feed.items():
            if feed not in red:
                subprocess.run(
                    ["gh", "issue", "close", str(num), "--repo", REPO,
                     "--comment", "Feed is green on the deadman board — "
                     "auto-closed by the triage router."], check=False)
                print(f"triage: closed #{num} ({feed} recovered)")

        opened = 0
        for feed, reason, cad in sorted(overdue):
            if feed in by_feed or opened >= TRIAGE_MAX_NEW_PER_RUN:
                continue
            body = "\n".join([
                f"The deadman board reports `{feed}` overdue: {reason}",
                f"(cadence: {cad}h)",
                "",
                "This is a WORK ITEM, not a notification: diagnose the "
                "producer, land the fix, and this issue auto-closes when "
                "the feed goes green. The aggregate [deadman] issue is the "
                "log; this is the queue.",
                "",
                f"_Filed {NOW.isoformat()} by deadman-watch.yml triage "
                f"router — {API}/api/v1/ops/deadman_",
            ])
            subprocess.run(
                ["gh", "issue", "create", "--repo", REPO,
                 "--title", f"[deadman-triage] {feed}",
                 "--body", body, "--label", TRIAGE_LABEL], check=False)
            opened += 1
            print(f"triage: opened work item for {feed}")
        if opened >= TRIAGE_MAX_NEW_PER_RUN:
            print(f"triage: cap reached ({TRIAGE_MAX_NEW_PER_RUN}/run) — "
                  f"remaining reds queue for the next cycle")
    except Exception as e:  # noqa: BLE001
        print(f"triage router failed (non-fatal): {e}")



if __name__ == "__main__":
    main()
