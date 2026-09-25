// Railway Infrastructure as Code for dchub-backend's services.
//
// Replaces Config as Code (railway.toml, railway-extractor.toml,
// services/daily/railway.json), which Railway stops reading on 2026-12-01.
// Evaluated by the Railway CLI (`railway config plan` / `apply`), NOT at deploy
// time: merging a change here does nothing until it is applied.
//
// Migrated so far: dchub-daily. Still on Config as Code: dchub-backend (web),
// dchub-worker, desirable-playfulness (extractor). Each moves here in its own PR.
//
// ★ partial: this repo owns ONLY the services it declares. Project
// resourceful-essence also holds dchub-mcp-server (owned by partial
// "dchub-mcp-server" in that repo), render-pdf, function-bun and
// dchub-mpp-sidecar. Without a named partial an apply from here would delete them.
//
// ★ env: every variable is preserve() (value stays in Railway, never in git).
// A service() block with no env entry for a variable PLANS TO DELETE IT, and a
// missing `source` plans to disconnect the repo. Add new variables here as
// preserve() when you create them in Railway, or the next apply removes them.
import { defineRailway, github, preserve, project, service } from "railway/iac";

export const partial = "dchub-backend";

export default defineRailway(() => {
  // dchub-daily — values from services/daily/railway.json (in effect via the
  // service's Railway Config File setting), except `start`: the Dockerfile CMD,
  // which the dashboard already mirrors. The json's older copy lacked `exec`
  // and `--log-level info` (info is uvicorn's default; exec only changes signal
  // delivery). Deploys only when services/daily/** changes (watchPatterns).
  const dchubDaily = service("dchub-daily", {
    source: github("azmartone67/dchub-backend", { checkSuites: false, rootDirectory: "services/daily" }),
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile", watchPatterns: ["services/daily/**"] },
    start: "sh -c 'exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --log-level info'",
    healthcheck: "/health",
    healthcheckTimeout: 120,
    replicas: { "us-west2": 1 },
    deploy: { restartPolicyType: "ON_FAILURE", restartPolicyMaxRetries: 3 },
    env: {
      AUTOPOST_ENABLED: preserve(),
      AUTOPOST_SIZE: preserve(),
      AUTOPOST_THEME: preserve(),
      DAILY_RENDER_ORIGIN: preserve(),
      DATABASE_URL: preserve(),
      DCHUB_API_BASE: preserve(),
      DCHUB_API_KEY: preserve(),
      DRY_RUN: preserve(),
      R2_ACCESS_KEY_ID: preserve(),
      R2_ACCOUNT_ID: preserve(),
      R2_BUCKET: preserve(),
      R2_PUBLIC_BASE: preserve(),
      R2_SECRET_ACCESS_KEY: preserve(),
      REFRESH_SECRET: preserve(),
    },
  });

  return project("resourceful-essence", {
    resources: [dchubDaily],
  });
});
