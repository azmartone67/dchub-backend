# DEPLOYMENT LOCK — DC Hub Nexus

**Last verified:** Feb 19, 2026
**Auth flow tested:** login → dashboard → Land & Power → evaluate ✅

---

## Critical Configurations — Do NOT Change Without Full Auth Flow Testing

### 1. CORS Credentialed Prefixes (main.py ~line 1530)

`CREDENTIALED_PREFIXES` must include:
- `/api/v1/land-power/`
- `/api/land-power/`

These match the **Cloudflare Worker v3.1** `TRANSPARENT_PROXY_PATHS`. If these paths change in `main.py`, the Cloudflare Worker must be updated to match.

### 2. JWT Decoder Injection (main.py ~line 15237)

```python
app.config['DECODE_JWT_FUNC'] = decode_jwt
```

This line **must run BEFORE** `register_usage_routes(app)`. The Land & Power usage limiter depends on this config value to decode JWT tokens. If it is `None`, all Land & Power requests return 401.

### 3. Land & Power Usage Limiter (land_power_usage_limiter.py)

Uses `app.config.get('DECODE_JWT_FUNC')` to decode JWT tokens. If this returns `None` (because the config was not set or was set after route registration), all Land & Power requests fail with 401 Unauthorized.

### 4. Health Watchdog (health_watchdog.py)

- **Memory restart threshold:** 450MB RSS (triggers auto-restart after 3 consecutive failures)
- **Port 5000 stuck process cleanup:** Kills orphaned processes on port 5000 before restart
- **Log rotation:** Truncates `.log` files exceeding 50MB (~hourly checks)
- **Stale news cleanup:** Purges `news_articles` older than 90 days (~every 6 hours)

### 5. Discovery Schedulers (scheduled_discovery.py)

- **7 jobs** staggered 30 minutes apart
- No two discovery jobs should overlap
- All schedulers run as background threads

### 6. News Engine (news_engine.py)

- **Savepoint-based article inserts:** Uses PostgreSQL savepoints to prevent transaction poisoning on duplicate/constraint violations
- **90-day retention:** Both `news_articles` and `announcements` tables purge records older than 90 days

---

## External Dependencies

### Cloudflare Worker v3.1 (dchub.cloud)

The Cloudflare Worker proxies these paths as **transparent proxy** with full header forwarding (Authorization, cookies):
- `/api/auth/`
- `/api/stripe/`
- `/api/v1/land-power/`
- `/api/land-power/`

**If CORS paths in main.py change, the Worker must be updated to match.**

Location: Cloudflare Dashboard → Workers

### Production Deployment URL

```
dc-hub-replit-fixedzip--azmartone1.replit.app
```

Confirmed **stable across redeploys** (Feb 2026). URL does not change when the app is republished.

---

## Change Checklist

Before modifying any of the above configurations:

1. [ ] Read this document fully
2. [ ] Test the complete auth flow: login → dashboard → Land & Power → evaluate
3. [ ] Verify CORS prefixes match Cloudflare Worker TRANSPARENT_PROXY_PATHS
4. [ ] Confirm `DECODE_JWT_FUNC` is set before usage route registration
5. [ ] Check health watchdog is running and memory is under 450MB
6. [ ] Verify news engine savepoints are functioning (no "current transaction is aborted" errors)
7. [ ] Update this document with new verification date
