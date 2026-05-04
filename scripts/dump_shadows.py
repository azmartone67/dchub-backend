#!/usr/bin/env python3
"""Phase 30B — dump shadowed routes to docs/SHADOWED-ROUTES.md.

Hits /api/v1/observability/route-audit on the live site, formats the
shadowed_routes list as markdown, and writes it to docs/. Intended to
run on a weekly cron via .github/workflows/weekly-shadow-audit.yml.
"""
import json, sys, pathlib, datetime, urllib.request

URL = 'https://dchub.cloud/api/v1/observability/route-audit'

def main():
    try:
        with urllib.request.urlopen(URL, timeout=20) as r:
            payload = json.loads(r.read().decode('utf-8'))
    except Exception as e:
        print(f"ERROR fetching route-audit: {e}")
        sys.exit(1)

    data = payload.get('data', {})
    shadows = data.get('shadowed_routes', [])
    total = data.get('total_routes', '?')
    asof = data.get('as_of', datetime.datetime.utcnow().isoformat() + 'Z')

    out = [f'# Shadowed Routes Inventory',
           '',
           f'_Generated: {asof}_',
           f'_Total routes: {total}_',
           f'_Shadowed routes: {len(shadows)}_',
           '',
           'A "shadowed route" is a URL path registered in two or more places.',
           'Flask uses the FIRST registration; the others are dead code that',
           'creates ambiguity and can mask bugs (Phase 20 lost a week to one).',
           '',
           '## Inventory',
           '']

    if not shadows:
        out.append('_No shadows detected — clean ship._')
    else:
        for s in sorted(shadows, key=lambda x: x.get('path', '')):
            path = s.get('path', '?')
            methods = ', '.join(s.get('methods', []))
            endpoints = s.get('endpoints', [])
            out.append(f'### `{path}` ({methods})')
            out.append('')
            for ep in endpoints:
                out.append(f'- `{ep}`')
            out.append('')
            out.append('**Recommended action:** identify which file each')
            out.append('endpoint lives in via `grep -rn "def {endpoint}" .`,')
            out.append('keep the canonical one, comment out or delete the others.')
            out.append('')

    pathlib.Path('docs').mkdir(exist_ok=True)
    pathlib.Path('docs/SHADOWED-ROUTES.md').write_text('\n'.join(out))
    print(f"wrote docs/SHADOWED-ROUTES.md with {len(shadows)} shadow(s)")


if __name__ == '__main__':
    main()
