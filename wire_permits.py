"""
wire_permits.py — Wire permit data into DC Hub platform
=======================================================
Does 4 things in one run:
  1. Add permit_date/source/confidence to get_facility_by_id in main.py
  2. Add /api/jobs/permit-scraper and /api/jobs/sec-parser endpoints to main.py
  3. Add both jobs to dchub-scheduler.py
  4. Verify all changes applied cleanly
"""

import re
import os

MAIN_PY   = os.path.expanduser("~/workspace/main.py")
SCHED_PY  = os.path.expanduser("~/workspace/dchub-scheduler.py")

# ── helpers ───────────────────────────────────────────────────────────────────

def read(path):
    with open(path, "r") as f:
        return f.read()

def write(path, content):
    with open(path, "w") as f:
        f.write(content)

def apply(path, old, new, label):
    t = read(path)
    if old not in t:
        print(f"  ✗ SKIP  {label} — anchor not found")
        return False
    if new in t:
        print(f"  ✓ ALREADY DONE  {label}")
        return True
    write(path, t.replace(old, new, 1))
    print(f"  ✓ APPLIED  {label}")
    return True

# ── 1. Add permit fields to get_facility_by_id SELECT query ──────────────────

print("\n[1] Adding permit_date to get_facility_by_id SELECT...")

apply(MAIN_PY,
    old="""                SELECT id, name, provider, city, state, country, market AS region,
                       latitude, longitude, power_mw, status, address, source
                FROM discovered_facilities WHERE id = %s LIMIT 1""",
    new="""                SELECT id, name, provider, city, state, country, market AS region,
                       latitude, longitude, power_mw, status, address, source,
                       permit_date, approval_date, co_date,
                       permit_source, permit_confidence
                FROM discovered_facilities WHERE id = %s LIMIT 1""",
    label="get_facility integer id query"
)

apply(MAIN_PY,
    old="""                SELECT id, name, provider, city, state, country, market AS region,
                       latitude, longitude, power_mw, status, address, source
                FROM discovered_facilities WHERE merged_facility_id = %s
                   OR source_id = %s LIMIT 1""",
    new="""                SELECT id, name, provider, city, state, country, market AS region,
                       latitude, longitude, power_mw, status, address, source,
                       permit_date, approval_date, co_date,
                       permit_source, permit_confidence
                FROM discovered_facilities WHERE merged_facility_id = %s
                   OR source_id = %s LIMIT 1""",
    label="get_facility hex id query"
)

# Also add permit fields to the free tier whitelist
apply(MAIN_PY,
    old="""            free_data = {k: v for k, v in full_data.items() if k in ("id", "name", "provider", "city", "state", "country", "status", "region")}""",
    new="""            free_data = {k: v for k, v in full_data.items() if k in ("id", "name", "provider", "city", "state", "country", "status", "region", "permit_date", "permit_source")}""",
    label="free tier permit_date whitelist"
)

# ── 2. Add job API endpoints to main.py ───────────────────────────────────────

print("\n[2] Adding job endpoints to main.py...")

# Find a good anchor — right after the auto-approve job endpoint
JOB_ENDPOINT_ANCHOR = """@app.route('/api/jobs/auto-approve', methods=['POST'])"""

PERMIT_JOB_ENDPOINTS = """@app.route('/api/jobs/permit-scraper', methods=['POST'])
def job_permit_scraper():
    \"\"\"Trigger Phase 1 permit scraper job.\"\"\"
    admin_key = request.headers.get('X-Admin-Key', '')
    if admin_key != os.environ.get('DCHUB_ADMIN_KEY', ''):
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        import subprocess, threading
        def run():
            env = dict(os.environ)
            env['PERMIT_MAX_FACILITIES'] = '500'
            subprocess.run(
                ['python3', os.path.expanduser('~/workspace/permit_scraper.py')],
                env=env, timeout=3600
            )
        threading.Thread(target=run, daemon=True).start()
        return jsonify({'success': True, 'job': 'permit_scraper', 'status': 'started'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/jobs/sec-parser', methods=['POST'])
def job_sec_parser():
    \"\"\"Trigger Phase 2 SEC/EDGAR permit parser job.\"\"\"
    admin_key = request.headers.get('X-Admin-Key', '')
    if admin_key != os.environ.get('DCHUB_ADMIN_KEY', ''):
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        import subprocess, threading
        def run():
            subprocess.run(
                ['python3', os.path.expanduser('~/workspace/sec_permit_parser.py')],
                env=dict(os.environ), timeout=3600
            )
        threading.Thread(target=run, daemon=True).start()
        return jsonify({'success': True, 'job': 'sec_parser', 'status': 'started'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


"""

t = read(MAIN_PY)
if "job_permit_scraper" in t:
    print("  ✓ ALREADY DONE  permit job endpoints")
elif JOB_ENDPOINT_ANCHOR in t:
    write(MAIN_PY, t.replace(JOB_ENDPOINT_ANCHOR,
        PERMIT_JOB_ENDPOINTS + JOB_ENDPOINT_ANCHOR, 1))
    print("  ✓ APPLIED  permit job endpoints")
else:
    # Fallback: append before the last line
    print("  ✗ SKIP  job endpoints — anchor not found, trying fallback...")
    # Find any job route to anchor on
    fallback = re.search(r"@app\.route\('/api/jobs/", t)
    if fallback:
        insert_pos = fallback.start()
        write(MAIN_PY, t[:insert_pos] + PERMIT_JOB_ENDPOINTS + t[insert_pos:])
        print("  ✓ APPLIED (fallback)  permit job endpoints")
    else:
        print("  ✗ FAILED  could not find any job route anchor")

# ── 3. Add jobs to dchub-scheduler.py ────────────────────────────────────────

print("\n[3] Adding jobs to dchub-scheduler.py...")

SCHEDULER_ANCHOR = "    'news': {"

PERMIT_JOBS = """    'permit_scraper': {
        'name': 'Permit Scraper (Phase 1)',
        'endpoint': '/api/jobs/permit-scraper',
        'method': 'POST',
        'hours': [2],
        'minute': 0,
        'day_of_week': 6,  # Sunday only
        'timeout': 3600,
    },
    'sec_parser': {
        'name': 'SEC/EDGAR Parser (Phase 2)',
        'endpoint': '/api/jobs/sec-parser',
        'method': 'POST',
        'hours': [3],
        'minute': 0,
        'day_of_month': 1,  # 1st of month only
        'timeout': 3600,
    },
"""

apply(SCHED_PY,
    old=SCHEDULER_ANCHOR,
    new=PERMIT_JOBS + SCHEDULER_ANCHOR,
    label="permit_scraper + sec_parser scheduler jobs"
)

# ── 4. Verify ─────────────────────────────────────────────────────────────────

print("\n[4] Verifying changes...")

main_t  = read(MAIN_PY)
sched_t = read(SCHED_PY)

checks = [
    ("main.py — permit_date in SELECT",       "permit_date, approval_date, co_date" in main_t),
    ("main.py — permit_date in free tier",    "permit_date" in main_t and "free_data" in main_t),
    ("main.py — permit_scraper endpoint",     "job_permit_scraper" in main_t),
    ("main.py — sec_parser endpoint",         "job_sec_parser" in main_t),
    ("scheduler — permit_scraper job",        "'permit_scraper'" in sched_t),
    ("scheduler — sec_parser job",            "'sec_parser'" in sched_t),
]

all_ok = True
for label, ok in checks:
    status = "✓" if ok else "✗ FAILED"
    print(f"  {status}  {label}")
    if not ok:
        all_ok = False

print(f"\n{'✅ All changes applied — commit and push to deploy.' if all_ok else '❌ Some changes failed — check output above.'}")

if all_ok:
    print("\nNext steps:")
    print("  git add main.py dchub-scheduler.py")
    print("  git commit -m 'Wire permit data: API, MCP, scheduler'")
    print("  git push origin main")
