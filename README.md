# DC Hub — Utility Scripts

Diagnostic and maintenance scripts. Not deployed — run manually from your terminal or Railway/Replit shell.

## Files

### `ai_tracking_diagnostic.py`
Full diagnostic of AI tracking data across all Neon tables. Shows lifetime totals, today vs yesterday, pre-fix vs post-fix comparison, MCP traffic, and last 24h activity.

**Run from:** Railway shell or Replit shell  
**Requires:** `NEON_DATABASE_URL` or `DATABASE_URL` env var  
```bash
python3 scripts/ai_tracking_diagnostic.py
```

### `check_ai_tracking.sh`
Quick health check hitting live API endpoints. Checks tracking stats, MCP endpoint, discovery files (llms.txt, ai-plugin.json, AGENTS.md), and API health.

**Run from:** Any terminal (Mac, Linux, etc.)  
**Requires:** `curl` and `python3`  
```bash
bash scripts/check_ai_tracking.sh
```

## Notes
- `ai_tracking_diagnostic.py` has a `FIX_DATE` variable at the top — update it to match your last deployment date for accurate pre/post comparisons
- Both scripts are read-only — they don't modify any data
