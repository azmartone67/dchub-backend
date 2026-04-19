/**
 * Cloudflare Worker — pretty URLs + daily cron trigger for the DC Hub daily renderer.
 *
 * Routes (fetch):
 *   /daily/today.png              -> today's hero image (rotating theme, square)
 *   /daily/{date}/{theme}_{size}  -> specific variant
 *   /daily/latest.json            -> metadata manifest for today
 *
 * Cron trigger (scheduled):
 *   Runs the schedule defined in wrangler.toml -> [triggers] -> crons
 *   Hits Railway's POST /refresh, which regenerates today's 9 PNGs + autoposts.
 *
 * Bindings (wrangler.toml):
 *   DAILY_BUCKET   R2 bucket
 *   DAILY_ORIGIN   Railway HTTPS URL
 *   REFRESH_SECRET Secret matching Railway's REFRESH_SECRET env var
 */

export default {
  async scheduled(event, env, ctx) {
    // Fire-and-forget — Railway will process in the background.
    const res = await fetch(`${env.DAILY_ORIGIN}/refresh`, {
      method: "POST",
      headers: {
        "X-Refresh-Secret": env.REFRESH_SECRET ?? "",
        "User-Agent": "dchub-daily-cron/1.0",
      },
    });
    console.log("cron refresh status=", res.status);
  },

  async fetch(req, env) {
    const url = new URL(req.url);
    const p = url.pathname;

    // serve today's default share image — rotate themes daily
    if (p === "/daily/today.png") {
      const today = new Date();
      const date = today.toISOString().slice(0, 10);
      const theme = ["a", "b", "c"][daysSince(today) % 3];
      return fromR2(env, `${date}/${theme}_square.png`);
    }

    // /daily/YYYY-MM-DD/a_portrait.png
    const m = p.match(/^\/daily\/(\d{4}-\d{2}-\d{2})\/([abc])_(portrait|square|story)\.png$/);
    if (m) {
      const [, date, theme, size] = m;
      return fromR2(env, `${date}/${theme}_${size}.png`);
    }

    // manifest proxy to Railway
    if (p === "/daily/latest.json") {
      const origin = env.DAILY_ORIGIN;
      const r = await fetch(`${origin}/snapshot`, {
        cf: { cacheTtl: 300, cacheEverything: true },
      });
      return new Response(await r.text(), {
        status: r.status,
        headers: {
          "content-type": "application/json",
          "cache-control": "public, max-age=300",
        },
      });
    }

    return new Response("not found", { status: 404 });
  },
};

function daysSince(d) {
  // days since epoch (matches Python's date.toordinal() % 3)
  return Math.floor(d.getTime() / 86400000);
}

async function fromR2(env, key) {
  const obj = await env.DAILY_BUCKET.get(key);
  if (!obj) return new Response("not found", { status: 404 });
  return new Response(obj.body, {
    headers: {
      "content-type": "image/png",
      "cache-control": "public, max-age=86400, immutable",
      "etag": obj.httpEtag,
    },
  });
}
