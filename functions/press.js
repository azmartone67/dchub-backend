/**
 * Cloudflare Pages Function: /press + /press/<slug>
 * 301 → canonical /press-release + /press-release/<slug>.
 */
export function onRequest(context) {
  const url = new URL(context.request.url);
  const p = url.pathname;
  if (p === '/press' || p === '/press/') {
    return Response.redirect(new URL('/press-release', url.origin).toString(), 301);
  }
  const m = p.match(/^\/press\/([^/]+)\/?$/);
  if (m) {
    return Response.redirect(new URL('/press-release/' + m[1], url.origin).toString(), 301);
  }
  return context.next();
}
