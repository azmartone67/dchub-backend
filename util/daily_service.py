"""daily_service.py — canonical origin for the DC Hub Daily render service.

Single source of truth for "where does the daily FastAPI service live?".
Sibling of util/deals.py and util/capacity_pipeline.py, created for the same
reason: a one-line constant is not the hard part, keeping ONE of it is.

THE MIGRATION THAT MOTIVATED THIS (2026-08-07)
----------------------------------------------
The daily service used to run in the Railway project `heroic-reprieve`, whose
other two services were a frozen scheduler zombie and a monolith twin holding
the leader lock. Decommissioning that project meant moving the daily service
into `resourceful-essence` — which changes its hostname, because Railway
service domains are derived from the service name.

That one hostname change had to be made in EIGHT places in this repo:

    crawler_scheduler.py            hardcoded fallback + docstring
    routes/integrations_health.py   hardcoded fallback (under a THIRD env name)
    routes/agent_broadcast_loop.py  OWN_HOSTS tuple
    main.py                         CSP connect-src
    routes/dcpi.py                  CSP connect-src
    services/daily/app.py           the service's own self-reference
    .github/workflows/…             the cron that drives /refresh
    CONFIG_SNAPSHOT.md, railway-ui-checklist.md

...plus four files in the separate dchub-frontend repo. Two different env var
names (`DAILY_SVC_URL` in crawler_scheduler, `DAILY_BASE` in
integrations_health) had already drifted apart for the same value.

So: resolve it HERE, import it everywhere. The next move is one line.

Precedence is DAILY_SVC_URL → DAILY_BASE → baked default, so both historical
env names keep working and a service that sets only one of them is unaffected.
"""
import os

# The Railway service domain for `dchub-daily` in project resourceful-essence.
# Changing this line is the whole migration — every caller reads through
# daily_origin(). Keep the scheme, drop any trailing slash.
DAILY_ORIGIN_DEFAULT = "https://dchub-daily-production.up.railway.app"


def daily_origin() -> str:
    """Origin of the daily render service, no trailing slash.

    Reads DAILY_SVC_URL first, then the older DAILY_BASE alias, then the
    baked default. Never raises; always returns a usable absolute origin.
    """
    return (
        os.environ.get("DAILY_SVC_URL", "").strip()
        or os.environ.get("DAILY_BASE", "").strip()
        or DAILY_ORIGIN_DEFAULT
    ).rstrip("/")


def daily_host() -> str:
    """Bare hostname (no scheme) — for host allowlists like OWN_HOSTS."""
    return daily_origin().split("://", 1)[-1].split("/", 1)[0]
