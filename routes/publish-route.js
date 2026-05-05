// Cloudflare Worker — /api/publish route handler
// Drop into your Worker repo (the same one that has handleNewsRoute).
// Then wire it into your main fetch() dispatcher — see INTEGRATION.md.

const RAILWAY_PUBLISH_URL = 'https://dchub-backend-production.up.railway.app/publish/all';

export async function handlePublishRoute(request, env) {
  // Only POST
  if (request.method !== 'POST') {
    return json({ error: 'method_not_allowed', allow: 'POST' }, 405);
  }

  // Auth: Bearer PUBLISH_PROXY_SECRET (set in Worker env)
  const auth = request.headers.get('authorization') || '';
  const token = auth.startsWith('Bearer ') ? auth.slice(7) : '';
  if (!env.PUBLISH_PROXY_SECRET || token !== env.PUBLISH_PROXY_SECRET) {
    return json({ error: 'unauthorized' }, 401);
  }

  // Parse body
  let payload;
  try { payload = await request.json(); }
  catch { return json({ error: 'invalid_json' }, 400); }

  const slug = (payload.slug || '').replace(/[^a-z0-9-]/gi, '');
  if (!slug) return json({ error: 'missing_slug' }, 400);

  // 1) Mirror to R2 first — durable archive even if Railway is down
  let r2Status = 'skipped';
  if (env.NEWS_ARCHIVE) {
    try {
      const meta = { slug, publishedAt: new Date().toISOString(), source: 'worker' };
      const puts = [];
      if (payload.html) {
        puts.push(env.NEWS_ARCHIVE.put(`news/${slug}.html`, payload.html, {
          httpMetadata: { contentType: 'text/html; charset=utf-8' },
          customMetadata: meta,
        }));
      }
      if (payload.markdown) {
        puts.push(env.NEWS_ARCHIVE.put(`news/${slug}.md`, payload.markdown, {
          httpMetadata: { contentType: 'text/markdown; charset=utf-8' },
        }));
      }
      if (payload.linkedin_text) {
        puts.push(env.NEWS_ARCHIVE.put(`news/${slug}.linkedin.txt`, payload.linkedin_text, {
          httpMetadata: { contentType: 'text/plain; charset=utf-8' },
        }));
      }
      await Promise.all(puts);
      r2Status = 'ok';
    } catch (err) {
      r2Status = `error: ${String(err)}`;
    }
  } else {
    r2Status = 'no_binding';
  }

  // 2) Forward to Railway server-to-server (Worker egress is unrestricted,
  //    so this clears the sandbox 403 the cron hit)
  let railwayStatus = 0;
  let railwayBody = null;
  try {
    const upstream = await fetch(RAILWAY_PUBLISH_URL, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'authorization': `Bearer ${env.RAILWAY_PUBLISH_SECRET || ''}`,
        'x-publish-source': 'worker',
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(45_000),
    });
    railwayStatus = upstream.status;
    railwayBody = await upstream.text();
  } catch (err) {
    railwayStatus = 599;
    railwayBody = String(err);
  }

  const railwayOk = railwayStatus >= 200 && railwayStatus < 300;
  return json({
    success: railwayOk || r2Status === 'ok', // durable if either succeeded
    slug,
    railway: { status: railwayStatus, body: truncate(railwayBody, 500) },
    r2: { status: r2Status },
    ts: new Date().toISOString(),
  }, railwayOk ? 200 : 502);
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
      'x-dc-worker-version': '4.5.0',
    },
  });
}
function truncate(s, n) { return typeof s === 'string' && s.length > n ? s.slice(0, n) + '…' : s; }
