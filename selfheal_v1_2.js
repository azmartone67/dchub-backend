// ============================================================
// DC Hub Self-Healing Monitor v1.2
// ----------------------------------------------------------------
// Changes from v1.1:
//   • Root "/" handler now reports v1.2 (v1.1 file had v1.0 string left over).
//   • Canary set: 8 endpoints, all probed and confirmed reachable
//     in production (no more silent 404s in the canary list).
//       homepage, ai-page, api-health, api-version,
//       markets, stats, mcp-discovery, agents-registry
//     Dropped from v1.1 plan:
//       failover-status — endpoint returns 404 in prod
//       /api/v1/markets — wrong path (real one is /api/markets)
//   • Per-check timeout 6s; total budget guard 25s.
//   • Webhook guard preserved (logs to tail when ALERT_WEBHOOK_URL unset).
//   • Layer tag in dashboard chips so it's obvious which tier failed.
// ============================================================

const CONFIG = {
  checks: [
    { name: 'homepage',         url: 'https://dchub.cloud/',                        expect: 200, timeout: 7000, layer: 'frontend' },
    { name: 'ai-page',          url: 'https://dchub.cloud/ai',                      expect: 200, timeout: 7000, layer: 'frontend' },
    { name: 'api-health',       url: 'https://dchub.cloud/api/health',              expect: 200, timeout: 7000, layer: 'neon-direct' },
    { name: 'api-version',      url: 'https://dchub.cloud/api/v1/version',          expect: 200, timeout: 6000, layer: 'neon-direct' },
    { name: 'markets',          url: 'https://dchub.cloud/api/markets',             expect: 200, timeout: 7000, layer: 'neon-direct' },
    { name: 'stats',            url: 'https://dchub.cloud/api/stats',               expect: 200, timeout: 6000, layer: 'neon-direct' },
    { name: 'mcp-discovery',    url: 'https://dchub.cloud/.well-known/mcp.json',    expect: 200, timeout: 5000, layer: 'discovery' },
    { name: 'agents-registry',  url: 'https://dchub.cloud/api/agents/registry',     expect: 200, timeout: 6000, layer: 'worker' },
  ],

  failuresBeforeRollback: 3,
  failuresBeforeAlert:    1,
  rollbackCooldownMs:     30 * 60 * 1000,
  maxIncidentLog:         50,
  targetWorker:           'dchubapiproxy',
  CHECK_BUDGET_MS:        25_000,
};

export default {
  async scheduled(event, env, ctx) { ctx.waitUntil(runHealthChecks(env)); },
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === '/status')    return getStatusDashboard(env);
    if (url.pathname === '/check')     { await runHealthChecks(env); return json({ ran: true, ts: Date.now() }); }
    if (url.pathname === '/incidents') return json(await getIncidentLog(env));
    if (url.pathname === '/rollback' && request.method === 'POST')
      return json(await performRollback(env, 'manual-trigger'));
    return json({ service: 'DC Hub Self-Healing Monitor v1.2', endpoints: ['/status','/check','/incidents','/rollback'] });
  },
};

const json = (data, status = 200) => new Response(JSON.stringify(data, null, 2), {
  status, headers: { 'Content-Type': 'application/json' },
});

async function runHealthChecks(env) {
  const start = Date.now();
  const results = [];
  for (const check of CONFIG.checks) {
    if (Date.now() - start > CONFIG.CHECK_BUDGET_MS) {
      results.push({ name: check.name, url: check.url, passed: false, status: null, error: 'budget-exceeded' });
      continue;
    }
    results.push(await runSingleCheck(check));
  }
  const allPassed   = results.every(r => r.passed);
  const failedChecks = results.filter(r => !r.passed);
  const state = await getState(env);
  const previouslyHealthy = state.consecutiveFailures === 0;

  if (allPassed) {
    if (!previouslyHealthy) {
      await logIncident(env, { type: 'recovery', timestamp: new Date().toISOString(), checks: results });
      if (state.consecutiveFailures >= CONFIG.failuresBeforeAlert) {
        await sendAlert(env, { status: 'RECOVERED', message: `DC Hub recovered after ${state.consecutiveFailures} failures.`, checks: results });
      }
    }
    state.consecutiveFailures = 0;
    state.lastCheckStatus = 'healthy';
    state.lastCheckTime = new Date().toISOString();
    state.lastCheckResults = results;
    await saveState(env, state);
    return;
  }

  state.consecutiveFailures = (state.consecutiveFailures || 0) + 1;
  state.lastCheckStatus = 'unhealthy';
  state.lastCheckTime = new Date().toISOString();
  state.lastCheckResults = results;
  console.error(`[selfheal] ${state.consecutiveFailures} consecutive failures: ` + failedChecks.map(f => `${f.name}=${f.status||f.error}`).join(', '));

  await logIncident(env, {
    type: 'failure', timestamp: new Date().toISOString(),
    consecutiveFailures: state.consecutiveFailures,
    failedChecks: failedChecks.map(f => ({ name: f.name, layer: CONFIG.checks.find(c=>c.name===f.name)?.layer, status: f.status, error: f.error })),
  });

  if (state.consecutiveFailures >= CONFIG.failuresBeforeAlert) {
    await sendAlert(env, {
      status: 'DOWN',
      message: `DC Hub DOWN — ${state.consecutiveFailures} consecutive failures.`,
      failedChecks: failedChecks.map(f => `${f.name}: ${f.status||f.error}`),
      threshold: `Auto-rollback at ${CONFIG.failuresBeforeRollback}`,
    });
  }

  if (state.consecutiveFailures >= CONFIG.failuresBeforeRollback) {
    const lastRb = state.lastRollbackTime ? new Date(state.lastRollbackTime).getTime() : 0;
    if (Date.now() - lastRb > CONFIG.rollbackCooldownMs) {
      const rb = await performRollback(env, 'auto-threshold');
      state.lastRollbackTime = new Date().toISOString();
      state.lastRollbackResult = rb;
      await sendAlert(env, { status: 'ROLLBACK', message: `Auto-rollback after ${state.consecutiveFailures} failures.`, rollbackResult: rb });
    }
  }
  await saveState(env, state);
}

async function runSingleCheck(check) {
  const t0 = Date.now();
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), check.timeout);
    const resp = await fetch(check.url + (check.url.includes('?')?'&':'?') + '_cb=' + Date.now(), {
      signal: ctrl.signal,
      headers: { 'User-Agent': 'DCHub-SelfHeal/1.2', 'Cache-Control': 'no-cache' },
      redirect: 'follow',
    });
    clearTimeout(timer);
    const responseTime = Date.now() - t0;
    return { name: check.name, url: check.url, status: resp.status, passed: resp.status === check.expect, responseTime, layer: check.layer };
  } catch (e) {
    return { name: check.name, url: check.url, status: null, passed: false, responseTime: Date.now() - t0, error: e.message || 'unknown', layer: check.layer };
  }
}

async function performRollback(env, trigger) {
  if (!env.CF_API_TOKEN || !env.CF_ACCOUNT_ID) return { success: false, error: 'CF_API_TOKEN or CF_ACCOUNT_ID missing' };
  try {
    const dlist = await fetch(`https://api.cloudflare.com/client/v4/accounts/${env.CF_ACCOUNT_ID}/workers/scripts/${CONFIG.targetWorker}/deployments`,
      { headers: { Authorization: `Bearer ${env.CF_API_TOKEN}` } }).then(r => r.json());
    const items = dlist.result?.items || [];
    if (items.length < 2) return { success: false, error: 'no previous deployment' };
    const cur = items[0], prev = items[1];
    const rb = await fetch(`https://api.cloudflare.com/client/v4/accounts/${env.CF_ACCOUNT_ID}/workers/deployments/by-script/${CONFIG.targetWorker}/detail/${prev.id}/rollback`,
      { method: 'POST', headers: { Authorization: `Bearer ${env.CF_API_TOKEN}`, 'Content-Type': 'application/json' }, body: JSON.stringify({ message: `selfheal:${trigger}` }) });
    const result = { success: rb.ok, trigger, ts: new Date().toISOString(), from: cur.id, to: prev.id };
    await logIncident(env, { type: 'rollback', ...result });
    return result;
  } catch (e) { return { success: false, error: e.message, trigger }; }
}

async function getState(env) {
  if (!env.HEAL_STATE) return { consecutiveFailures: 0 };
  return (await env.HEAL_STATE.get('monitor-state', 'json').catch(() => null)) || { consecutiveFailures: 0 };
}
async function saveState(env, state) {
  if (env.HEAL_STATE) await env.HEAL_STATE.put('monitor-state', JSON.stringify(state));
}
async function getIncidentLog(env) {
  if (!env.HEAL_STATE) return [];
  return (await env.HEAL_STATE.get('incident-log', 'json').catch(() => null)) || [];
}
async function logIncident(env, incident) {
  if (!env.HEAL_STATE) return;
  const log = await getIncidentLog(env);
  log.unshift(incident);
  if (log.length > CONFIG.maxIncidentLog) log.length = CONFIG.maxIncidentLog;
  await env.HEAL_STATE.put('incident-log', JSON.stringify(log));
}

async function sendAlert(env, payload) {
  if (!env.ALERT_WEBHOOK_URL) {
    console.log('[selfheal alert — no webhook]', JSON.stringify(payload));
    return;
  }
  try {
    const isSlack = env.ALERT_WEBHOOK_URL.includes('hooks.slack.com');
    const isDiscord = env.ALERT_WEBHOOK_URL.includes('discord.com/api/webhooks');
    let body;
    if (isSlack) {
      const emoji = payload.status === 'RECOVERED' ? ':white_check_mark:' : payload.status === 'ROLLBACK' ? ':arrows_counterclockwise:' : ':rotating_light:';
      body = JSON.stringify({ text: `${emoji} DC Hub ${payload.status}\n${payload.message}`,
        blocks: [
          { type: 'section', text: { type: 'mrkdwn', text: `${emoji} *DC Hub ${payload.status}*\n${payload.message}` } },
          ...(payload.failedChecks ? [{ type: 'section', text: { type: 'mrkdwn', text: '*Failed:* ' + payload.failedChecks.map(c=>`\n• ${c}`).join('') } }] : []),
        ] });
    } else if (isDiscord) {
      const color = payload.status === 'RECOVERED' ? 0x00ff00 : payload.status === 'ROLLBACK' ? 0xffaa00 : 0xff0000;
      body = JSON.stringify({ embeds: [{ title: `DC Hub ${payload.status}`, description: payload.message, color, timestamp: new Date().toISOString() }] });
    } else {
      body = JSON.stringify({ subject: `DC Hub ${payload.status}`, ...payload, timestamp: new Date().toISOString() });
    }
    await fetch(env.ALERT_WEBHOOK_URL, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
  } catch (e) { console.error('[selfheal] alert delivery failed:', e.message); }
}

async function getStatusDashboard(env) {
  const state = await getState(env);
  const incidents = (await getIncidentLog(env)).slice(0, 10);
  const html = `<!DOCTYPE html><html><head><meta charset="UTF-8"><title>DC Hub Self-Heal v1.2</title>
<style>body{font-family:-apple-system,sans-serif;background:#0a0e17;color:#e0e0e0;padding:2rem;line-height:1.5}
.wrap{max-width:980px;margin:auto}h1{color:#00d4aa}.card{background:#141b2d;border:1px solid #1e293b;border-radius:10px;padding:1.2rem;margin-bottom:1rem}
.h{border-left:4px solid #00d4aa}.u{border-left:4px solid #ff4444}
.checks{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:.5rem;margin-top:1rem}
.chk{background:#1a2235;padding:.7rem;border-radius:6px;font-size:.9rem}
.lay{font-size:.7rem;color:#64748b;text-transform:uppercase;letter-spacing:.5px;margin-top:2px}
.bad{color:#ff4444}.ok{color:#00d4aa}</style></head>
<body><div class="wrap">
<h1>DC Hub Self-Heal — v1.2</h1><p>Auto health monitoring + rollback for dchub.cloud</p>
<div class="card ${state.lastCheckStatus==='healthy'?'h':'u'}">
<h2>Status: ${state.lastCheckStatus||'unknown'}</h2>
<div>Consecutive failures: <b>${state.consecutiveFailures||0}</b> · Last check: ${state.lastCheckTime||'never'}</div>
<div class="checks">
${(state.lastCheckResults||[]).map(r=>`<div class="chk"><b>${r.passed?'<span class="ok">✔</span>':'<span class="bad">✗</span>'} ${r.name}</b><br><div class="lay">${r.layer||'?'} · ${r.status??r.error??'?'} · ${r.responseTime}ms</div></div>`).join('')}
</div></div>
<div class="card"><h2>Recent incidents (10)</h2>
${incidents.length?incidents.map(i=>`<div style="padding:.5rem;border-bottom:1px solid #1e293b"><b>${i.type}</b> · ${i.timestamp} ${i.failedChecks?'<br><span class="bad">'+i.failedChecks.map(f=>f.name).join(', ')+'</span>':''}</div>`).join(''):'<div style="color:#64748b">none</div>'}
</div></div></body></html>`;
  return new Response(html, { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
}
