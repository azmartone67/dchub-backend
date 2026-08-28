"""What a sponsor may send us, checked at INTAKE (2026-08-28).

WHY THIS EXISTS. `hero_html` was TEXT NOT NULL validated by exactly one rule —
non-empty after .strip(). No length cap, no tag allowlist, no image or link
policy. So the honest answer to a prospect's first question, "what do I send
you?", was "email us some HTML and we will paste it". There was no spec sheet
because there was nothing to write one against.

★★★ THE SPEC IS DERIVED FROM THE SURFACES, NOT FROM TASTE. A sponsorship
    renders through FOUR paths in routes/sponsor_render.py, and only one of
    them keeps markup:

      sponsor_module_html   HTML page      hero_html interpolated RAW
      sponsor_block_text    plain text     _plain() strips every tag
      sponsor_block_payload JSON           _plain() strips every tag
      sponsor_block_html    root domain    _plain() then html.escape()

    Three of four DELETE markup. The root domain — the most-cited surface, and
    the whole basis of Product 2 — is one of the three. So a creative built out
    of markup arrives as bare text everywhere it matters most, and an image tag
    reaches ONE surface out of four and ZERO of the AI-cited ones. The spec we
    publish therefore says: send PROSE. The allowlist below exists to keep the
    one HTML surface safe, not to invite anyone to design in it.

★★★ VALIDATE AT THE POST, NEVER IN THE RENDERER. sponsor_render is fail-soft by
    construction: every failure path returns ''. A check placed there would
    SILENTLY DROP a paying sponsor's block off a live page instead of rejecting
    a bad submission at the door. Rejection belongs where a human is waiting
    for an answer.

★ THE ERROR STRINGS ARE THE SPEC. A rejection has to tell the sender what to
  change, because it is the first place most senders will actually read the
  rules. Do not shorten these into codes.

★ THE SPONSOR GETS EXACTLY ONE LINK, AND WE RENDER IT. `<a>` is not on the
  allowlist: an anchor inside hero_html would appear on the HTML surface
  WITHOUT rel="sponsored nofollow", outside the click counter, pointing
  anywhere. The CTA we render from link_url carries the right rel and is the
  only link an advertiser is buying.
"""
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

# ── the published numbers ────────────────────────────────────────────
# Measured against the surfaces above, not chosen for roundness. 400 characters
# of plain text is roughly 60 words: two or three sentences, which is what
# survives into an llms.txt block and a JSON envelope without crowding the page
# it sits on. The raw cap is the same budget plus room for light markup.
MAX_PLAIN_CHARS = 400
MAX_RAW_CHARS = 1000
MAX_SPONSOR_NAME_CHARS = 60
MAX_LINK_CHARS = 500

# Inline emphasis and a line break. Everything structural is unnecessary for
# two sentences and only exists to be abused on the one surface that keeps it.
ALLOWED_TAGS = ("b", "strong", "i", "em", "br")
VOID_TAGS = ("br",)

# No attributes at all on allowed tags. `style` lets a creative restyle the
# page around it, `class` lets it impersonate our own components, and `on*` is
# script. There is no attribute an emphasis tag needs.
ALLOW_ATTRIBUTES = False

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SCHEME_RE = re.compile(r"(?i)\b(?:javascript|vbscript|data)\s*:")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def plain_text(fragment: str) -> str:
    """The fragment as it reaches the three non-HTML surfaces.

    ★ Must stay behaviourally identical to routes.sponsor_render._plain, which
    is what actually ships. If the two drift, we cap a length nobody sees.
    tests/test_sponsor_creative_intake.py fences that equivalence.
    """
    import html as _html
    if not fragment:
        return ""
    return _WS_RE.sub(" ", _html.unescape(_TAG_RE.sub(" ", fragment))).strip()


class _TagWalker(HTMLParser):
    """Collects tag names, attributes and nesting. Never rewrites anything."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bad_tags, self.bad_attrs, self.stack, self.unclosed = [], [], [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.bad_tags.append(tag)
            return
        if attrs and not ALLOW_ATTRIBUTES:
            self.bad_attrs.extend(f"{tag}[{a}]" for a, _ in attrs)
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.bad_tags.append(tag)
        elif attrs and not ALLOW_ATTRIBUTES:
            self.bad_attrs.extend(f"{tag}[{a}]" for a, _ in attrs)

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if tag not in ALLOWED_TAGS:
            self.bad_tags.append(tag)
            return
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.unclosed.append(tag)


def _check_hero(hero, errors):
    if len(hero) > MAX_RAW_CHARS:
        errors.append(
            f"hero_html is {len(hero)} characters; the limit is {MAX_RAW_CHARS} "
            f"including any markup.")
    if _CONTROL_RE.search(hero):
        errors.append("hero_html contains control characters. Send plain text.")
    if _SCHEME_RE.search(hero):
        errors.append(
            "hero_html contains a javascript:, vbscript: or data: URL. Links "
            "belong in link_url, which we render ourselves.")

    walker = _TagWalker()
    try:
        walker.feed(hero)
        walker.close()
    except Exception:
        errors.append("hero_html could not be parsed as HTML. Send plain text.")
        return

    if walker.bad_tags:
        uniq = sorted(set(walker.bad_tags))
        extra = ""
        if "a" in uniq:
            extra = (" Your sponsorship includes exactly one link — send it as "
                     "link_url and we render it with rel=\"sponsored nofollow\" "
                     "and click tracking.")
        if any(t in uniq for t in ("img", "picture", "svg", "video")):
            extra += (" Images are not accepted: three of the four surfaces a "
                      "sponsorship renders on are plain text or JSON, including "
                      "the root domain that AI engines cite, so an image would "
                      "reach one surface out of four and none of the cited ones.")
        errors.append(
            f"hero_html uses tag(s) that are not allowed: {', '.join(uniq)}. "
            f"Allowed: {', '.join(ALLOWED_TAGS)}.{extra}")
    if walker.bad_attrs:
        errors.append(
            f"hero_html puts attributes on a tag: {', '.join(sorted(set(walker.bad_attrs)))}. "
            f"Allowed tags take no attributes at all.")
    if walker.stack or walker.unclosed:
        errors.append(
            "hero_html has unbalanced tags "
            f"({', '.join(sorted(set(walker.stack + walker.unclosed)))}). An "
            "unclosed tag reformats the page below your placement.")

    plain = plain_text(hero)
    if not plain:
        errors.append(
            "hero_html has no readable text once markup is removed. Three of "
            "the four surfaces a sponsorship renders on strip markup, so a "
            "creative made only of tags is blank on all of them.")
    elif len(plain) > MAX_PLAIN_CHARS:
        errors.append(
            f"hero_html is {len(plain)} characters of readable text; the limit "
            f"is {MAX_PLAIN_CHARS}. That is roughly two to three sentences — "
            f"what fits an llms.txt block without crowding the page.")


def _check_link(link, errors):
    if len(link) > MAX_LINK_CHARS:
        errors.append(f"link_url is longer than {MAX_LINK_CHARS} characters.")
        return
    try:
        parts = urlsplit(link)
    except Exception:
        errors.append("link_url is not a URL we can parse.")
        return
    if parts.scheme.lower() != "https":
        errors.append(
            "link_url must be an https:// URL. We render it as the click "
            "destination on our own domain, so it cannot downgrade the "
            "connection of someone who arrived over https.")
        return
    if not parts.netloc:
        errors.append("link_url has no host.")
        return
    if "@" in parts.netloc:
        errors.append(
            "link_url embeds credentials before the host, which reads as a "
            "phishing link. Send the plain destination URL.")
    if _CONTROL_RE.search(link) or any(c.isspace() for c in link):
        errors.append("link_url contains whitespace or control characters.")


def _check_name(name, errors):
    if len(name) > MAX_SPONSOR_NAME_CHARS:
        errors.append(
            f"sponsor_name is {len(name)} characters; the limit is "
            f"{MAX_SPONSOR_NAME_CHARS}. It is rendered inline in a label and "
            f"in a call to action.")
    if "<" in name or ">" in name:
        errors.append(
            "sponsor_name must be plain text with no markup. It is escaped on "
            "every surface, so tags would appear literally.")
    if "\n" in name or "\r" in name or _CONTROL_RE.search(name):
        errors.append("sponsor_name must be a single line of plain text.")


def validate_creative(payload) -> dict:
    """Check one submitted creative against the published spec.

    Returns {"ok", "errors", "plain_text", "plain_chars"}. Errors are written
    for the person who has to fix them; they are returned verbatim by the POST
    and are the closest thing to a spec most senders will read.

    Required fields being absent is NOT this function's business — the route
    already rejects that — but they are checked here too so the validator can
    be run standalone against a row.
    """
    p = payload or {}
    errors: list = []

    name = (p.get("sponsor_name") or "").strip()
    hero = (p.get("hero_html") or "").strip()
    link = (p.get("link_url") or "").strip()

    if not name:
        errors.append("sponsor_name is required.")
    else:
        _check_name(name, errors)
    if not hero:
        errors.append("hero_html is required — it is the sponsored message.")
    else:
        _check_hero(hero, errors)
    if not link:
        errors.append("link_url is required — it is the one link you are buying.")
    else:
        _check_link(link, errors)

    email = (p.get("sponsor_email") or "").strip()
    if email and not _EMAIL_RE.match(email):
        errors.append("sponsor_email is not a valid address.")

    week_of = p.get("week_of")
    if week_of not in (None, "") and not _DATE_RE.match(str(week_of)):
        errors.append("week_of must be a date in YYYY-MM-DD form.")

    price = p.get("price_cents")
    if price not in (None, ""):
        try:
            if int(price) < 0:
                raise ValueError
        except Exception:
            errors.append("price_cents must be a non-negative whole number of cents.")

    plain = plain_text(hero)
    return {"ok": not errors, "errors": errors,
            "plain_text": plain, "plain_chars": len(plain)}


def spec() -> dict:
    """The published creative spec, generated from the constants above.

    One source of truth: the numbers a prospect is told are the numbers the
    POST enforces, because both read these names. A spec sheet maintained by
    hand drifts from the validator on its first edit.
    """
    return {
        "what_to_send": {
            "sponsor_name": {
                "type": "plain text, one line",
                "max_chars": MAX_SPONSOR_NAME_CHARS,
                "notes": "Rendered in the 'Sponsored by' label and the call to action.",
            },
            "hero_html": {
                "type": "prose; light inline markup optional",
                "max_readable_chars": MAX_PLAIN_CHARS,
                "max_chars_including_markup": MAX_RAW_CHARS,
                "allowed_tags": list(ALLOWED_TAGS),
                "attributes_allowed": ALLOW_ATTRIBUTES,
                "notes": (
                    "Write it as prose. Three of the four surfaces a "
                    "sponsorship renders on — plain-text llms.txt, the JSON "
                    "envelope, and the root domain AI engines cite — strip "
                    "markup entirely, so anything built out of tags arrives as "
                    "bare text there."),
            },
            "link_url": {
                "type": "https URL",
                "max_chars": MAX_LINK_CHARS,
                "notes": (
                    "One link per placement. We render it with "
                    "rel=\"sponsored nofollow\" through a click-tracked "
                    "redirect, which is where your click count comes from."),
            },
            "sponsor_email": {"type": "email address", "required": False},
            "week_of": {"type": "date, YYYY-MM-DD", "required": False},
        },
        "not_accepted": [
            "Images, video, SVG, or any embedded media — they reach one of the "
            "four surfaces and none of the AI-cited ones.",
            "Links inside the message. You get one link, and we render it.",
            "Scripts, iframes, forms, styles, or class names.",
            "Attributes of any kind on the permitted tags.",
        ],
        "how_it_is_labelled": (
            "Every placement is labelled as a paid placement in the SOURCE "
            "TEXT, as a sentence rather than a CSS class, so the label survives "
            "an AI engine stripping the markup. The label is not optional and "
            "is not conditional on tier or price."),
        "limits_are_enforced_at_submission": True,
    }
