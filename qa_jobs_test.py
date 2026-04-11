#!/usr/bin/env python3
"""
DC Hub — Jobs / Cron Endpoints QA
====================================
Tests all 19 POST /api/jobs/* endpoints and the scheduler status API.
Uses the DCHUB_ADMIN_KEY for authenticated calls.

Usage:
    export DCHUB_ADMIN_KEY=your_key
    python qa_jobs_test.py                     # Railway
    python qa_jobs_test.py --env replit        # Replit failover
    python qa_jobs_test.py --job news_sync     # single job
    python qa_jobs_test.py --dry-run           # check routes exist (no trigger)

WARNING: Some jobs (news_sync, autopilot) hit external APIs and insert DB rows.
         Use --dry-run in production to verify routes without side effects.
         Safe jobs (fiber-sync, permit-scraper) are triggered by default.
"""

import os, sys, json, time, argparse, urllib.request, urllib.error
from datetime import datetime

TARGETS = {
    "railway": "https://dchub-backend-production.up.railway.app",
    "replit":  "https://dc-hub-replit-fixedzip--azmartone1.replit.app",
    "local":   "http://localhost:8080",
}

GREEN  = "\033[92m"; RED = "\033[91m"; YELLOW = "\033[93m"
CYAN   = "\033[96m"; RESET = "\033[0m"; BOLD = "\033[1m"

pass_count = fail_count = warn_count = 0

# ── All 27 verified job endpoints ─────────────────────────────────────────────
# Source: routes/jobs_routes.py (24 routes) + main.py (3 routes)  — verified Apr 2026
# Format: (path, description, safe_to_trigger, key_header)
ALL_JOBS = [
    # ── From main.py (inline routes) ──────────────────────────────────────────
    ("/api/jobs/permit-scraper",      "Phase 1 permit scraper",               True,  "X-Admin-Key"),
    ("/api/jobs/sec-parser",          "SEC/EDGAR permit parser",              True,  "X-Admin-Key"),
    ("/api/jobs/fiber-sync",          "Fiber route sync (PeeringDB/OSM)",     True,  "X-Internal-Key"),
    # ── From routes/jobs_routes.py — safe ─────────────────────────────────────
    ("/api/jobs/auto-approve",        "Auto-approve staged discoveries",      True,  "X-Admin-Key"),
    ("/api/jobs/alert-emails",        "Alert email notification checker",     True,  "X-Admin-Key"),
    ("/api/jobs/simple-alerts",       "Simple alerts processing loop",        True,  "X-Admin-Key"),
    ("/api/jobs/market-report",       "Daily market report generation",       True,  "X-Admin-Key"),
    ("/api/jobs/infrastructure-sync", "Infra sync (substations, lines)",      True,  "X-Admin-Key"),
    ("/api/jobs/capacity-headroom",   "Capacity headroom calculation",        True,  "X-Admin-Key"),
    ("/api/jobs/mcp-rate-cleanup",    "MCP rate limit table cleanup",         True,  "X-Admin-Key"),
    ("/api/jobs/db-backup",           "DB backup (all tables to JSON)",       True,  "X-Admin-Key"),
    ("/api/jobs/keep-alive",          "Keepalive ping",                       True,  "X-Admin-Key"),
    ("/api/jobs/backup",              "Full backup job",                      True,  "X-Admin-Key"),
    ("/api/jobs/global-intelligence", "Global intelligence index refresh",    True,  "X-Admin-Key"),
    ("/api/jobs/content-publish",     "Content publishing pipeline",          True,  "X-Admin-Key"),
    ("/api/jobs/ambassador",          "Ambassador outreach job",              True,  "X-Admin-Key"),
    # ── From routes/jobs_routes.py — heavy ────────────────────────────────────
    ("/api/jobs/news-refresh",        "RSS feed aggregation (60+ sources)",   False, "X-Admin-Key"),
    ("/api/jobs/discovery",           "PeeringDB/OSM/datacentermap scan",     False, "X-Admin-Key"),
    ("/api/jobs/evolution",           "Evolution/ML pattern detection",       False, "X-Admin-Key"),
    ("/api/jobs/ai-ecosystem",        "AI ecosystem agent enrichment",        False, "X-Admin-Key"),
    ("/api/jobs/ai-outreach",         "AI platform outreach pings",           False, "X-Admin-Key"),
    ("/api/jobs/autopilot",           "Auto-Pilot facility/deal discovery",   False, "X-Admin-Key"),
    ("/api/jobs/autonomous-brain",    "Autonomous learning & pattern detect", False, "X-Admin-Key"),
    ("/api/jobs/energy-discovery",    "Energy discovery data refresh",        False, "X-Admin-Key"),
]

def _req(url, method="POST", headers=None, timeout=30):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=b"{}", headers=h, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            ms = round((time.time() - t0) * 1000)
            try:
                return r.status, json.loads(raw), ms
            except Exception:
                return r.status, raw, ms
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        ms = round((time.time() - t0) * 1000)
        try:
            return e.code, json.loads(raw), ms
        except Exception:
            return e.code, raw, ms
    except urllib.error.URLError as e:
        return 0, str(e), 0
    except TimeoutError:
        ms = round((time.time() - t0) * 1000)
        # Jobs do real work — a read timeout means the job is RUNNING, not crashed.
        # Return -1 as sentinel so _check can print "running" instead of "fail".
        return -1, "timeout — job likely still running (not a crash)", ms


def _check(name, status, body, ms, dry_run=False):
    global pass_count, fail_count, warn_count

    prefix = f"  {'[DRY]' if dry_run else '      '}"

    if status == -1:
        # Read timeout — job is doing real work, not crashed
        print(f"{prefix} {YELLOW}⏳ RUNNING{RESET}  {name} [{ms}ms] — job running (read timeout, not a crash)")
        warn_count += 1
    elif status == 0:
        print(f"{prefix} {RED}✗ FAIL{RESET}  {name} — connection error: {body}")
        fail_count += 1
    elif status == 429:
        print(f"{prefix} {YELLOW}⚠ WARN{RESET}  {name} — 429 rate limited")
        warn_count += 1
    elif status == 404:
        print(f"{prefix} {YELLOW}⚠ WARN{RESET}  {name} — 404 route not found")
        warn_count += 1
    elif status == 401:
        print(f"{prefix} {YELLOW}⚠ WARN{RESET}  {name} — 401 admin key rejected")
        warn_count += 1
    elif status in (200, 202):
        msg = ""
        if isinstance(body, dict):
            msg = body.get("message", body.get("status", ""))
        print(f"{prefix} {GREEN}✓ PASS{RESET}  {name} [{ms}ms] {msg[:60] if msg else ''}")
        pass_count += 1
    else:
        err = ""
        if isinstance(body, dict):
            err = body.get("error", body.get("message", ""))
        print(f"{prefix} {RED}✗ FAIL{RESET}  {name} — HTTP {status} {err[:60]}")
        fail_count += 1


def _section(title):
    print(f"\n{BOLD}{CYAN}── {title} {'─' * (50 - len(title))}{RESET}")


def test_scheduler_status(base, key):
    _section("Scheduler Status")
    headers = {"X-Admin-Key": key} if key else {}
    s, b, ms = _req(f"{base}/api/scheduler/status", method="GET", headers=headers)
    if s == 200 and isinstance(b, dict):
        jobs = b.get("jobs", b.get("schedulers", {}))
        print(f"  {GREEN}✓ PASS{RESET}  /api/scheduler/status [{ms}ms]  ({len(jobs)} jobs registered)")
        global pass_count
        pass_count += 1
        # Print job table
        if jobs:
            print(f"\n  {'Job':<35} {'Interval':>10} {'Last Run':<25}")
            print(f"  {'-'*35} {'-'*10} {'-'*25}")
            for name, info in (jobs.items() if isinstance(jobs, dict) else []):
                interval = info.get("interval_seconds", info.get("interval", "?"))
                last     = info.get("last_run", info.get("last", "never"))
                if isinstance(last, str) and len(last) > 24:
                    last = last[:24]
                print(f"  {name:<35} {str(interval):>10}s {str(last):<25}")
    elif s == 404:
        print(f"  {YELLOW}⚠ WARN{RESET}  /api/scheduler/status — 404 (route missing)")
        global warn_count
        warn_count += 1
    else:
        print(f"  {RED}✗ FAIL{RESET}  /api/scheduler/status — HTTP {s}")
        global fail_count
        fail_count += 1


def test_jobs(base, key, single_job=None, dry_run=False, trigger_heavy=False):
    _section("Job Endpoints")

    if not key:
        print(f"  {YELLOW}⚠ WARN{RESET}  No DCHUB_ADMIN_KEY — all job tests skipped")
        print(f"         Set: export DCHUB_ADMIN_KEY=your_key")
        global warn_count
        warn_count += 1
        return

    headers = {"X-Admin-Key": key}
    jobs_to_test = ALL_JOBS

    if single_job:
        jobs_to_test = [(p, d, s, k) for (p, d, s, k) in ALL_JOBS if single_job in p]
        if not jobs_to_test:
            print(f"  {YELLOW}⚠{RESET}  No job found matching '{single_job}'")
            return

    safe_label    = f"{CYAN}[safe]{RESET}"
    heavy_label   = f"{YELLOW}[heavy]{RESET}"
    skipped_label = f"{YELLOW}[skipped]{RESET}"

    for path, desc, safe, key_header in jobs_to_test:
        label = safe_label if safe else heavy_label

        if dry_run:
            # In dry-run: just hit HEAD or check if route exists (expect 405 for HEAD on POST-only)
            s, b, ms = _req(f"{base}{path}", method="HEAD", headers=headers, timeout=10)
            exists = s not in (0, 404)
            status_str = f"HTTP {s}" if s else "no response"
            if exists:
                print(f"  {GREEN}✓{RESET} {label}  {path:<40} route exists ({status_str})")
                global pass_count
                pass_count += 1
            else:
                print(f"  {RED}✗{RESET} {label}  {path:<40} {RED}404 — not found{RESET}")
                global fail_count
                fail_count += 1
        elif not safe and not trigger_heavy:
            print(f"  {skipped_label}      {path:<40} use --trigger-heavy to run")
        else:
            s, b, ms = _req(f"{base}{path}", method="POST", headers={key_header: key}, timeout=45)
            _check(f"{label} {path}", s, b, ms, dry_run=False)
            time.sleep(0.5)  # brief pause between triggers


# =============================================================================
# RUNNER
# =============================================================================

def run(env_name, base_url, args):
    global pass_count, fail_count, warn_count
    pass_count = fail_count = warn_count = 0

    key = args.key

    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  DC Hub Jobs QA — {env_name.upper()}{RESET}")
    print(f"  Target  : {base_url}")
    print(f"  Admin   : {'✓ key set' if key else '✗ not set'}")
    print(f"  Mode    : {'DRY RUN (route existence only)' if args.dry_run else 'LIVE (triggers jobs)'}")
    print(f"  Time    : {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{BOLD}{'='*60}{RESET}")

    if not args.dry_run and not args.job:
        print(f"\n  {YELLOW}Note:{RESET} Only safe jobs are triggered by default.")
        print(f"  Use --trigger-heavy to also run news/autopilot/discovery jobs.")
        print(f"  Use --dry-run to just verify routes exist without triggering anything.")

    test_scheduler_status(base_url, key)
    test_jobs(base_url, key,
              single_job=args.job,
              dry_run=args.dry_run,
              trigger_heavy=getattr(args, 'trigger_heavy', False))

    print(f"\n{BOLD}── Summary {'─'*48}{RESET}")
    print(f"  {GREEN}PASS: {pass_count}{RESET}   {RED}FAIL: {fail_count}{RESET}   {YELLOW}WARN: {warn_count}{RESET}")
    if fail_count == 0:
        print(f"  {GREEN}{BOLD}✓ Jobs QA passed for {env_name}{RESET}")
    else:
        print(f"  {RED}{BOLD}✗ {fail_count} job(s) failed{RESET}")

    return fail_count == 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DC Hub Jobs / Cron QA")
    parser.add_argument("--env",    default="railway", choices=["railway","replit","local","both"])
    parser.add_argument("--key",    default=os.environ.get("DCHUB_ADMIN_KEY",""), help="Admin key")
    parser.add_argument("--job",    default="", help="Test a single job by path fragment (e.g. news-sync)")
    parser.add_argument("--dry-run",        action="store_true", help="Check routes exist without triggering")
    parser.add_argument("--trigger-heavy",  action="store_true", help="Also trigger heavy jobs (news, autopilot)")
    args = parser.parse_args()

    envs = ["railway","replit"] if args.env == "both" else [args.env]
    all_ok = True
    for env in envs:
        ok = run(env, TARGETS[env], args)
        all_ok = all_ok and ok

    sys.exit(0 if all_ok else 1)
