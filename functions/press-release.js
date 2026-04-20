/**
 * Cloudflare Pages Function: /press-release
 * Bare /press-release was a dead URL showing "Press release not found".
 * This 301s at the edge BEFORE the static HTML is served.
 * Slug paths (/press-release/<slug>) fall through via context.next().
 */
export function onRequest(context) {
  const url = new URL(context.request.url);
  if (url.pathname === '/press-release' || url.pathname === '/press-release/') {
    return Response.redirect(new URL('/press', url.origin).toString(), 301);
  }
  return context.next();
}
