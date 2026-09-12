"""Guard: a server-rendered, tier-gated page can heal a lapsed access JWT.

2026-09-12. A Pro customer was shown "PRO unlocks all sections" over Power &
Grid, Pipeline and Operator Footprint on /markets/midland-tx/brief, and
"Download PDF" failed three times. Reproduced from the outside, both symptoms
came from ONE cause -- an expired 7-day access JWT:

    arm                          render       brief.pdf
    anonymous                    anonymous    402 pdf_requires_pro
    EXPIRED access JWT           anonymous    402 pdf_requires_pro   <-- the bug
    keyed paid seat              paid         200, 78,305 B, %PDF-

The renderer resolves the tier from the dchub_token cookie before a byte is
written, so the paywall is baked into the HTML and brief.pdf -- gated by the
same _caller_tier() -- answers 402. dchub-nav.js keeps the optimistic Pro chip
on a 401 from /api/auth/me because, in its own words, "a lapsed access JWT that
the platform refresh (api-base) heals moments later". These four pages never
loaded api-base, so nothing healed and the contradiction was permanent.

These tests pin the fix:
  1. every tier-gated brief emits /js/dchub-api-base.js BEFORE /js/dchub-nav.js
     (both are `defer`, so document order is execution order -- api-base must
     patch window.fetch first to see nav's /api/auth/me 401);
  2. a NON-pro render also emits the one-shot dchub:token-refreshed reload,
     because refreshing a token cannot repaint sections the origin never sent;
  3. a PRO render does NOT emit that reload (nothing to heal, and no loop);
  4. the whole class is covered, over a pinned floor -- any future page that
     renders the paywall must carry the heal too.

Renders through the real _render_html of each module; no fixture mirrors the
markup. routes/*_brief.py import without dragging in main.py (checked).
"""

import importlib
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

NAV_TAG = '<script src="/js/dchub-nav.js" defer></script>'
API_BASE_TAG = '<script src="/js/dchub-api-base.js" defer></script>'
REFRESH_EVENT = "dchub:token-refreshed"
RELOAD_CALL = "location.reload()"
PAYWALL_CARD = "PRO unlocks"

# The tier-gated, server-rendered briefs. Adding a paywalled page here without
# the heal is the defect this file exists to stop -- see test_the_whole_class.
GATED_BRIEF_MODULES = [
    "routes.market_brief",
    "routes.hyperscaler_brief",
    "routes.operator_brief",
    "routes.state_brief",
]

# A rendered brief is a full HTML page; anything near-empty means the render
# failed into a stub and every substring assertion below would be vacuous.
MIN_RENDER_BYTES = 4000


def _render(mod_name: str, is_pro: bool) -> str:
    """Drive the module's real _render_html, filling whatever keys it asks for.

    The four briefs carry different payload shapes; rather than freeze four
    fixtures that would drift from the renderers, seed the keys they share and
    let KeyError name the rest. Bounded, and the result is floor-checked by the
    caller, so a render that collapses cannot pass as a render that worked.
    """
    mod = importlib.import_module(mod_name)
    brief = {
        "slug": "midland-tx", "market": "Midland, TX", "state": "TX",
        "tier": "PRO" if is_pro else "FREE", "is_pro": is_pro,
        "hero": {"market": "Midland, TX", "state": "TX", "country": "US"},
        "outlook": {"teaser": "teaser", "summary": "summary"},
        "live_as_of": {"iso": "2026-09-12T00:00:00"},
    }
    last = None
    for _ in range(40):
        try:
            return mod._render_html(brief)
        except KeyError as exc:            # name the missing key and retry
            key = exc.args[0]
            if not isinstance(key, str) or key in brief:
                raise
            brief[key] = {}
            last = key
    raise AssertionError(f"{mod_name}: _render_html still incomplete at {last!r}")


@pytest.mark.parametrize("mod_name", GATED_BRIEF_MODULES)
@pytest.mark.parametrize("is_pro", [False, True])
def test_api_base_loads_before_nav(mod_name, is_pro):
    """The self-heal script is on the page, and ahead of the nav script."""
    html = _render(mod_name, is_pro)
    assert len(html) >= MIN_RENDER_BYTES, (
        f"{mod_name}: render is {len(html)} B -- too small to be the page; "
        "the substring checks below would be vacuous"
    )
    assert NAV_TAG in html, f"{mod_name}: no nav tag -- this is not the brief page"
    assert API_BASE_TAG in html, (
        f"{mod_name} (is_pro={is_pro}): does not load /js/dchub-api-base.js, so a "
        "lapsed access JWT can never be refreshed and a paying seat is shown the upsell"
    )
    assert html.index(API_BASE_TAG) < html.index(NAV_TAG), (
        f"{mod_name}: api-base must precede the nav script -- both are `defer`, so "
        "loading it after nav means it never wraps nav's /api/auth/me call"
    )


@pytest.mark.parametrize("mod_name", GATED_BRIEF_MODULES)
def test_non_pro_render_reloads_once_on_refresh(mod_name):
    """The paywall is server-rendered, so healing the token must re-fetch."""
    html = _render(mod_name, is_pro=False)
    assert REFRESH_EVENT in html, (
        f"{mod_name}: a paywalled render does not listen for {REFRESH_EVENT}, so a "
        "seat whose token refreshes keeps looking at the pre-refresh paywall"
    )
    assert RELOAD_CALL in html, f"{mod_name}: listens for the refresh but never reloads"
    assert "sessionStorage" in html, (
        f"{mod_name}: the reload is not latched -- a free plan whose refresh "
        "succeeds would reload forever"
    )


@pytest.mark.parametrize("mod_name", GATED_BRIEF_MODULES)
def test_pro_render_does_not_reload(mod_name):
    """Nothing to heal on a paid render; no reload may be armed."""
    html = _render(mod_name, is_pro=True)
    assert RELOAD_CALL not in html, (
        f"{mod_name}: a PRO render arms a reload it does not need"
    )


def test_the_whole_class_carries_the_heal():
    """Every server-rendered page that shows the paywall must carry the heal.

    Floored and closed on both sides. The scan walks the repo -- a scan that can
    match nothing passes for free -- and it also requires every page it finds to
    be REGISTERED in GATED_BRIEF_MODULES, so a new paywalled brief cannot satisfy
    this one check and skip the render tests above, which are what actually pin
    the load order. The floor is the four briefs known to render the card on
    2026-09-12; it may grow, never shrink.
    """
    found, offenders = {}, []
    for path in sorted((REPO_ROOT / "routes").glob("*.py")):
        src = path.read_text(encoding="utf-8", errors="replace")
        if NAV_TAG not in src or PAYWALL_CARD not in src:
            continue
        found[f"routes.{path.stem}"] = path.name
        # The heal is either emitted inline or delegated to the shared helper.
        if "dchub-api-base.js" not in src and "paid_seat_heal_html" not in src:
            offenders.append(path.name)

    assert len(found) >= len(GATED_BRIEF_MODULES), (
        f"scan matched only {len(found)} paywalled renderer(s) ({sorted(found)}) -- "
        f"below the floor of {len(GATED_BRIEF_MODULES)}. The markup moved and this "
        "guard went blind."
    )
    assert not offenders, (
        "these renderers show the paywall but cannot heal a lapsed access token: "
        f"{offenders}. Emit routes._paid_seat_heal.paid_seat_heal_html(is_pro) "
        "above the nav script."
    )
    unregistered = sorted(set(found) - set(GATED_BRIEF_MODULES))
    assert not unregistered, (
        f"paywalled renderer(s) {unregistered} are not in GATED_BRIEF_MODULES, so "
        "the load-order tests above never run against them. Add them."
    )
