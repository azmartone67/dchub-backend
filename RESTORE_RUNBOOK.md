# DC Hub — Database Backup & Restore Runbook

_Last updated: 2026-08-08. Read this **before** an incident, not during one._

## What protects the database (defense in depth)

| Tier | Mechanism | Protects against | RPO | Where |
|---|---|---|---|---|
| 1 | **Neon PITR** (built-in history) | Bad migration, `DROP TABLE`, app bug | seconds | Neon console |
| 2 | **Nightly `pg_dump` → R2** | Neon losing data / Neon-the-company vanishing | ≤ 24h | Cloudflare R2 `dchub-backups`, 30-day retention |
| 3 | **Weekly restore test** (CI) | A backup that silently can't be restored | — | GitHub Actions `restore-test.yml` |

There is **no live standby** — recovery (not instant failover) is the deliberate posture.
**RPO ≤ 24h, RTO ~30–60 min** (time to provision a fresh Neon DB + restore the dump).
If that RTO/RPO ever becomes unacceptable, the next step is a warm standby in a second
Neon region — see "Future: standby" at the bottom.

> 📉 **RPO history (SH52-090):** the R2 dump cadence was deliberately widened from
> **every 6h to nightly (≤ 24h)** to cut backup cost/churn. That trade is accepted, but
> note the consequence: **Tier 2 (R2) is the _only_ off-Neon copy of the data.** Tier 1
> (PITR) lives inside Neon and Tier 3 only re-reads the same R2 dump, so if Neon-the-company
> vanishes, the most you can lose is one night's writes — and there is no second independent
> copy behind it. If that exposure ever stops being acceptable, tighten this cadence (back
> toward 6h) or stand up the second-region standby _before_ leaning harder on R2 alone.

> ⚠️ **Single-point-of-failure note:** all of {2 Railway services + 1 Render service}
> point at one Neon project. If the logical DBs `dchub` and `dchub_daily` live on the
> **same** Neon project, that project is a shared SPOF — confirm in the Neon console.

---

## A. Restore for real (Neon is gone / data is corrupted)

The dumps are plain SQL made with `pg_dump --clean --if-exists --no-owner --no-acl`.

> 🛑 **NEVER restore a `--clean` dump onto the live prod DB.** It runs `DROP ... IF EXISTS`
> first and will wipe whatever it connects to. Always restore into a **fresh, empty** DB,
> verify it, then cut traffic over to it.

1. **Provision a new empty Postgres 18 DB** (new Neon project, or any PG18 host).
   Copy its connection string → call it `NEW_DB_URL`.

2. **Get the latest dump from R2** (creds are on Railway / in GitHub Actions secrets):
   ```bash
   pip install boto3
   python - <<'PY'
   import os, boto3
   s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT_URL"],
       aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
       aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")
   objs = s3.list_objects_v2(Bucket="dchub-backups", Prefix="neon_backup_")["Contents"]
   latest = max(objs, key=lambda o: o["LastModified"])
   print("restoring:", latest["Key"], latest["LastModified"])
   s3.download_file("dchub-backups", latest["Key"], "backup.sql.gz")
   PY
   ```

3. **Restore** (needs `postgresql-client-18` — older clients refuse PG18 dumps):
   ```bash
   gunzip -c backup.sql.gz | psql "$NEW_DB_URL" -v ON_ERROR_STOP=0 2> restore.err
   grep -iE "ERROR|FATAL" restore.err | sort | uniq -c | sort -rn   # review noise
   ```
   Extension/role errors (`vector`, `postgis`, etc. not installed) are usually benign —
   install the missing extension on the new DB and re-run only if a real table failed.

4. **Verify before cutting over:**
   ```bash
   RESTORE_TARGET_URL="$NEW_DB_URL" SOURCE_DATABASE_URL="" python restore_verify.py
   ```

5. **Cut traffic over** — update `DATABASE_URL` (and `NEON_DATABASE_URL`) on **all three**
   services, then redeploy / restart each:
   - Railway `resourceful-essence` (main Flask backend)
   - Railway `heroic-reprieve` (DC Hub Daily / FastAPI)
   - Render (failover origin)
   Also update the same secrets in GitHub Actions and on Cloudflare so backups keep running.

### Prefer PITR for "oops" recovery
For accidental deletes / bad migrations where Neon itself is healthy, **don't** use the R2
dump — use Neon's point-in-time restore (console → project → **Restore** / branch to a
timestamp just before the mistake). It's near-zero RPO and far faster than a full dump restore.

---

## B. Max out Neon PITR — _the one manual step_  ⬅️ DO THIS

PITR retention is a console/API setting and depends on plan. Set it to the max your plan allows:

- Neon console → your project → **Settings → Storage / History retention**.
- Set **History retention** to the plan max: Launch = **7 days**, Scale/Business = **30 days**.
- (Free is 24h — if you're on Free, this alone is a reason to move to Launch.)

This makes "someone dropped a table at 2pm" a 2-minute restore instead of a 24h-stale dump.

---

## C. The automated restore test (how it stays honest)

`.github/workflows/restore-test.yml` runs **Mondays 11:00 UTC** and on-demand:
downloads the latest R2 dump → restores into an ephemeral `postgres:18` container →
runs `restore_verify.py` (asserts table count + non-zero rows + every prod table present).
It **cannot** touch prod (prod is read-only, table-names only).

Run it manually anytime:
```bash
gh workflow run restore-test.yml
gh run watch "$(gh run list --workflow=restore-test.yml --limit 1 --json databaseId -q '.[0].databaseId')"
```
A red run = the backup is **not** restorable — treat as a sev incident, not a flaky test.

---

## Future: standby (only if RTO ~30–60 min stops being acceptable)

1. Second Neon **project in a different region** (or a different provider for true
   vendor independence: Railway PG / Supabase / RDS — all PG18).
2. Keep in sync by **logical replication** (`CREATE PUBLICATION` on primary →
   `CREATE SUBSCRIPTION` on standby) for ~seconds RPO, **or** by restoring the R2 dump
   into it every few hours for a simpler "warm" standby (~hours RPO, no DDL-drift headaches).
3. Failover = promote standby + flip `DATABASE_URL` on all three services.

Gotchas if you go the replication route: logical replication does **not** carry DDL
(schema changes must be applied to both) or advance sequences on the subscriber
(reset them on promotion).
