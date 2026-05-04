#!/usr/bin/env python3
"""DC Hub ingestion watchdog — opens a GH issue when ingestion looks stale."""
import json, os, sys, urllib.request, datetime, subprocess

ENDPOINT = 'https://dchub.cloud/api/energy-discovery/status'
EXPECTED_NONZERO = [
    'total_substations', 'total_pipelines', 'total_power_plants',
    'total_wind_projects', 'total_gas_compressors',
    'total_gas_processings', 'total_transmissions',
]

def fetch():
    req = urllib.request.Request(
        f'{ENDPOINT}?_t={int(datetime.datetime.utcnow().timestamp())}',
        headers={'User-Agent':'dchub-watchdog/1.0','Cache-Control':'no-cache'})
    return json.loads(urllib.request.urlopen(req, timeout=15).read().decode('utf-8'))

def open_issue(title, body):
    if os.environ.get('GH_TOKEN'):
        try:
            subprocess.run(['gh','issue','create','--title',title,
                            '--body',body,'--label','watchdog'], check=True)
            return
        except Exception as e:
            print('gh issue create failed:', e)
    print('ISSUE-FALLBACK:'); print('TITLE:', title); print(body)

def main():
    try:
        d = fetch()
    except Exception as e:
        open_issue('[watchdog] /api/energy-discovery/status unreachable',
                   f'GET {ENDPOINT} raised: {e!r}')
        sys.exit(1)
    data = (d.get('data') or {}) if isinstance(d, dict) else {}
    problems = []
    if data.get('seed_data'):
        problems.append('seed_data is True')
    if not (data.get('recent_syncs') or []):
        problems.append('recent_syncs is empty')
    for k in EXPECTED_NONZERO:
        v = data.get(k)
        if v is None or v == 0:
            problems.append(f'{k} = {v}')
    if problems:
        body = '## Land-Power ingestion watchdog tripped\n\n'
        body += f'Endpoint: {ENDPOINT}\n\nFindings:\n'
        for p in problems: body += f'- {p}\n'
        body += '\n```\n' + json.dumps(data, indent=2)[:2000] + '\n```\n'
        open_issue('[watchdog] DC Hub ingestion appears stale', body)
        sys.exit(2)
    print('OK')

if __name__ == '__main__': main()
