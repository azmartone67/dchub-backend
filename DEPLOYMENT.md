# Deployment Topology — READ FIRST

**This is the source of truth for how dchub.cloud deploys. Future agents must follow this.**

## Two GitHub repos, easy to confuse

| What | GitHub repo | Local checkout | Deploys to |
|---|---|---|---|
| **Frontend** (HTML/JS/CSS, the live site) | `azmartone67/dchub-frontend` | `/tmp/dchub-frontend-real/` (clone) | Cloudflare Pages → `dchub.cloud` |
| **Backend** (Flask/main.py, MCP, scripts) | `azmartone67/dchub-backend` | `~/workspace/` | Railway (`dchub-backend-production`) |

## The trap

`~/workspace/dchub-frontend/` is a **tracked subfolder of the BACKEND repo**, NOT a clone of the frontend repo. Editing files there and pushing pushes to `dchub-backend.git` — Cloudflare Pages never sees them. The site does not change.

## Correct frontend workflow

```bash
# One-time clone of the real frontend repo
cd /tmp && git clone https://github.com/azmartone67/dchub-frontend.git dchub-frontend-real

# For each frontend change:
cd /tmp/dchub-frontend-real
# ... edit files ...
git add <files>
git -c user.email="azmartone@gmail.com" -c user.name="Jonathan Martone" \
    commit -m "fix(scope): one-line summary"
git push origin main
# Cloudflare Pages auto-builds in ~60-90s
```

## How to verify a frontend change is live

```bash
# Compare local file size vs live (live should match local within ~30s)
echo "Local: $(wc -c < /tmp/dchub-frontend-real/js/land-power-app.js)"
echo "Live:  $(curl -sSI "https://dchub.cloud/js/land-power-app.js?cb=$(date +%s)" | grep -i content-length | awk '{print $2}' | tr -d '\r')"

# Or grep for a unique string in your patch:
curl -sS "https://dchub.cloud/js/land-power-app.js?cb=$(date +%s)" | grep -c "<unique-string-from-your-patch>"
# Should return 1
```

If live still shows the old size after 2 minutes:
- Open https://dash.cloudflare.com/?to=/:account/pages → click `dchub-frontend` → Deployments
- Confirm your commit appears as latest. If it shows `Failed`, read the build log.

## Files frequently confused

These exist in **both** repos but only one matters for the live site:

| File | Authoritative location | Effect of editing the wrong one |
|---|---|---|
| `js/land-power-app.js` | `dchub-frontend` repo | No live change |
| `land-power-map.html` | `dchub-frontend` repo | No live change |
| `_worker.js` | `dchub-frontend` repo | No live change |
| `_headers` (CSP) | `dchub-frontend` repo | No live change |
| `functions/press-release.js` | `dchub-frontend` repo | No live change |
| `main.py` | `dchub-backend` (`~/workspace`) | Push to backend repo, deploys via Railway |
| `worker.js` (the Cloudflare Worker `dchubapiproxy`) | `~/workspace/worker.js` | Deployed via `bash deploy-v47-mega.sh` (manual, not GitHub-triggered) |

## Backend workflow (unchanged)

`~/workspace/` is the dchub-backend repo. Edit, commit, push to `azmartone67/dchub-backend`. Railway auto-deploys `main.py` etc.

The Cloudflare Worker `dchubapiproxy` (`~/workspace/worker.js`) is **NOT auto-deployed** by either repo. Deploy it explicitly:
```bash
cd ~/workspace && bash deploy-v47-mega.sh
```

## TL;DR for agents

- Frontend change? `cd /tmp/dchub-frontend-real`, edit, commit, push.
- Backend change? `cd ~/workspace`, edit, commit, push.
- Never edit `~/workspace/dchub-frontend/` and expect it to deploy. It won't.
