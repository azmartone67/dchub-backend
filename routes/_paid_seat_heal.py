"""The lapsed-access-token heal for server-rendered, tier-gated pages.

2026-09-12. A Pro customer opened /markets/midland-tx/brief and was shown
"PRO unlocks all sections" over Power & Grid, Pipeline and Operator Footprint,
while the header still carried the Pro chip and their avatar. "Download PDF"
failed three times: "Couldn't download - Something went wrong."

Both symptoms are ONE root cause. The access JWT (7 days) had expired:

  1. dchub-nav.js bridges localStorage's dchub_token into a first-party cookie
     on every load, expired or not.
  2. The page is rendered by the ORIGIN, which resolves the tier from that
     cookie before a byte is written (routes/tier_gate._resolve_caller_tier:
     an expired JWT raises, is swallowed, and falls through to FREE).
  3. So the paywall is baked into the HTML, and GET /markets/<slug>/brief.pdf
     -- gated by the same _caller_tier() -- answers 402 pdf_requires_pro. The
     browser reports that as a failed download, once per click.
  4. The header keeps saying Pro because dchub-nav.js reads localStorage and
     never checks exp; on a 401 from /api/auth/me it deliberately KEEPS the
     optimistic cached tier, with the comment "a lapsed access JWT that the
     platform refresh (api-base) heals moments later".

That comment names a dependency these pages did not satisfy: the brief
templates emitted /js/dchub-nav.js and nothing else, so dchub-api-base.js --
which owns the 401/402 -> /api/auth/refresh -> retry path against the 90-day
httpOnly dchub_refresh cookie -- was never on the page. Nothing could heal.

What this module emits:

  * /js/dchub-api-base.js, BEFORE the nav script. Both are `defer`, so they run
    in document order: api-base patches window.fetch first and is therefore
    underneath nav's own wrapper, which is what lets it see nav's
    /api/auth/me 401 and refresh.
  * On a NON-pro render only, a one-shot reload keyed to dchub:token-refreshed.
    This is required because the paywall is server-rendered: refreshing the
    token does not repaint sections that were never sent. The reload is latched
    in sessionStorage per path, so a genuinely free or logged-out visitor --
    whose refresh also succeeds, or whose refresh 401s and never fires this
    event at all -- cannot loop. A pro render CLEARS the latch, so a second
    lapse later in the same tab still heals.

Why the heal is not done on the server: POST /api/auth/refresh ROTATES the
refresh token and treats a second presentation of a rotated token as theft --
it revokes the user's whole chain (routes/auth_routes.refresh_token). A render
that silently spent the refresh cookie would race the page's own single-flight
refresh and log the customer out. The exchange belongs in the one place that
serialises it: window.DCHUB_refreshToken in dchub-api-base.js.

Cache note: the canon bypasses /api/auth* ("No cache auth"), so the refresh
response's Set-Cookie reaches the browser, and rule #18 bypasses /markets/*
for requests carrying dchub_token/dchub_refresh, so the reload is served a
fresh origin render rather than a stored anonymous copy.
"""

# Loaded before the nav script; see module docstring for why the order matters.
API_BASE_TAG = '<script src="/js/dchub-api-base.js" defer></script>'

_LATCH = "'dchub_seat_reheal:' + location.pathname"

_RELOAD_ONCE = (
    "<script>(function(){var k=" + _LATCH + ";"
    "window.addEventListener('dchub:token-refreshed',function(){"
    "try{if(sessionStorage.getItem(k))return;sessionStorage.setItem(k,'1');}catch(e){return;}"
    "location.reload();});})();</script>"
)

_CLEAR_LATCH = (
    "<script>(function(){try{sessionStorage.removeItem(" + _LATCH + ");}catch(e){}})();</script>"
)


def paid_seat_heal_html(is_pro) -> str:
    """Markup a tier-gated, server-rendered page must emit ABOVE its
    /js/dchub-nav.js tag so a paying seat with a lapsed access JWT is not shown
    the upsell.

    `is_pro` is the tier the ORIGIN resolved for THIS render -- not what the
    browser believes. A falsey value means this HTML carries the paywall, which
    is the only case that needs the reload.
    """
    return API_BASE_TAG + (_CLEAR_LATCH if is_pro else _RELOAD_ONCE)
