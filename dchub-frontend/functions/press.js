export async function onRequest(ctx){
  const url = new URL(ctx.request.url);
  const parts = url.pathname.replace(/^\/+|\/+$/g,'').split('/');
  const slug = parts[1];
  return Response.redirect(new URL(slug?`/press-release/${slug}`:'/press-release', url.origin).toString(), 301);
}
