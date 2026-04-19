# DC Hub — QA Harness (v7.9.10)

Two Python programs that work together to stop the bug families from the
April-14 2026 QA pass from recurring.

| File             | When it runs  | What it checks                                  |
| ---------------- | ------------- | ----------------------------------------------- |
| `qa/squasher.py` | **pre-deploy** | Static source tree — dead URLs, nav drift, chart date bugs, redirect-shadowing, stale market slugs. Stdlib only. |
| `qa/smoke.py`    | **post-deploy** | Hits the live site over HTTP — CORS preflight, API shape, `/markets/` routes, MCP tools, etc. Requires `requests`. |

Both exit `0` on success and `1` on failure, so they slot directly into
Cloudflare Pages build commands, GitHub Actions, or a cron.

---

## Quick start

```bash
# Before you push
python qa/squasher.py                      # scan cwd
python qa/squasher.py --json               # machine-readable
python qa/squasher.py --fix                # safe auto-fixes (R1 only)
python qa/squasher.py --strict             # warnings also fail

# After deploy
pip install requests
python qa/smoke.py                         # default: https://dchub.cloud
python qa/smoke.py --base https://staging.dchub.cloud
python qa/smoke.py --only S1 S3 S6         # run a subset
python qa/smoke.py --json                  # CI-friendly output
```

---

## Coverage matrix

| Bug family (Apr-14 2026)                                      | Squasher rule | Smoke test |
| ------------------------------------------------------------- | :-----------: | :--------: |
| Assets page — dead `web-production-*` Railway URL + CORS      | R1            | S1, S7     |
| Capacity pipeline — chart collapses to `Date.now()` bucket    | R2            | S2         |
| AI Integrations — nav sends traffic to `/ai`                  | R3            | S3         |
| Press releases — hits Railway first, CORS preflight fails     | R4            | S4         |
| Testimonials — auth-sync 503 noise / fallback not painted     | —             | S5         |
| `/markets` static directory shadowed by a 301 redirect        | R5            | S6         |
| `markets/index.html` missing city links                       | R6            | —          |
| Front-page banner references a city that was never created    | R7            | —          |
| MCP `tools/list` not returning full catalog                   | —             | S8         |
| `/.well-known/mcp/server-card.json` regression                | —             | S9         |

---

## Wiring it in

### Cloudflare Pages build command

```bash
python qa/squasher.py && npm run build
```

### GitHub Actions (post-deploy smoke)

```yaml
- name: Post-deploy smoke
  run: |
    pip install requests
    python qa/smoke.py --base https://dchub.cloud --json > smoke.json
- uses: actions/upload-artifact@v4
  with: { name: smoke-report, path: smoke.json }
```

### Cron (monitoring, every 15 min)

```cron
*/15 * * * *  cd /srv/dchub && python3 qa/smoke.py --json >> /var/log/dchub-smoke.log
```

---

## Adding a new rule

1. Write a `rule_<name>(root, report)` function in `squasher.py` that
   appends `Finding(...)` entries.
2. Add it to the `RULES = [...]` list at the bottom.
3. Pair it with a smoke test (`s<N>_<name>(base) -> Result`) in `smoke.py`
   and register it in `TESTS`.
4. Document the pair in the coverage matrix above.

Keep squasher rules **fast and stdlib-only** so they run on any build
image without setup. Keep smoke tests **idempotent** (no writes) so they
can run against production without side effects.

---

## R2 pattern — how to fix chart `Date.now()` fallbacks

Bad (v7.9.9 `capacity-pipeline.html`):

```js
const date = new Date(item.announcement_date || item.created_at || Date.now());
```

Good (v7.9.10 pattern):

```js
function pickDate(item) {
    const candidates = [item.announcement_date, item.announced_date,
                        item.expected_online, item.expected_completion,
                        item.online_date, item.commissioning_date,
                        item.updated_at, item.created_at];
    for (const c of candidates) {
        if (!c) continue;
        const d = new Date(c);
        if (!isNaN(d.getTime()) && d.getFullYear() > 2000) return d;
    }
    return null;  // caller drops or buckets as "TBD"
}
```

The squasher's R2 rule flags the bad pattern with a regex; once you
switch to `pickDate`, it passes.
