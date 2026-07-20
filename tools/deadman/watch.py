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

# The registry: workflow file -> max hours expected between SUCCESSFUL runs.
# Alarm fires at 2x this. Keep in rough sync with each workflow's own cron; generous
# so a single missed run is not an alarm, but a stalled loop is. Source of truth for
# "what loops must keep succeeding".
WORKFLOWS = {
    # high-frequency telemetry (cron minutes/20min) — a couple hours of silence is fine
    "iso-data-pull.yml": 3,
    "iso-lmp-ingest.yml": 3,
    # daily loops
    "iso-queue-ingest.yml": 30,
    "eia-pricing-ingest.yml": 30,
    "osm-crawl.yml": 30,
    "news-ner-discovery.yml": 30,
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
}

NOW = datetime.datetime.now(datetime.timezone.utc)
ISSUE_LABEL = "deadman"


def gh_json(args):
    try:
        out = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        print(f"gh error {args}: {e}")
        return None
    if out.returncode != 0:
        print(f"gh nonzero {args}: {out.stderr.strip()[:200]}")
        return None
    try:
        return json.loads(out.stdout or "null")
    except Exception:  # noqa: BLE001
        return None


def last_success(workflow):
    """Return (iso_ts, status_note) for the workflow's most recent successful run."""
    d = gh_json(
        ["run", "list", "--repo", REPO, "--workflow", workflow, "--limit", "20",
         "--json", "conclusion,updatedAt,status"]
    )
    if d is None:
        return None, "actions-api-error"
    if not d:
        return None, "never-run"
    for r in d:
        if r.get("conclusion") == "success":
            return r.get("updatedAt"), "success"
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
        headers={"Content-Type": "application/json", "X-Admin-Key": ADMIN},
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


def main():
    overdue = []          # (feed, reason, cadence)
    seen_feeds = set()

    # 1-3: workflow crons via the Actions API (self-sufficient — no backend needed)
    for wf, cad in WORKFLOWS.items():
        feed = wf[:-4] if wf.endswith(".yml") else wf
        seen_feeds.add(feed)
        ts, status = last_success(wf)
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

    # 4: backend-scheduler feeds that beat the ledger directly (not GitHub workflows)
    try:
        with urllib.request.urlopen(f"{API}/api/v1/ops/deadman", timeout=20) as r:
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

    # 5: alarm — one dedup'd issue
    if not overdue:
        print(f"\nDEADMAN ✓ all {len(seen_feeds)} watched loops within cadence")
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
        f"_Checked {len(seen_feeds)} GitHub-cron loops + the backend ingest_runs ledger._",
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


if __name__ == "__main__":
    main()
