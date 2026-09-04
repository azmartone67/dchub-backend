"""Split a file-extension suffix off a slug BEFORE period-normalisation.

r-period-slug (2026-07-06) consolidates malformed period slugs — 'st.-louis' is
a soft-404 duplicate of the canonical 'st-louis', so /markets/st.-louis and
/dcpi/st.-louis 301 onto the real page. It did that with a blanket
``slug.replace(".", "")``.

That strips EVERY period, including the one that introduces a file extension.
A request for ``/dcpi/northern-virginia.json`` was normalised to
``northern-virginiajson`` and 301'd there — a URL that does not exist. The
caller got a redirect (which reads as "this moved, follow me") into a 404,
which is strictly worse than a plain 404 because it looks like it worked.
Measured 2026-09-03 on production, following the redirect: 60/60 sampled
``/dcpi/<slug>.json`` and 50/50 sampled ``/markets/<slug>.JSON`` ended in 404,
while the same slugs without a suffix were 200/200.

``.json`` on /markets is now bound by the ``/markets/<slug>.json`` twin route
(#3758) because Werkzeug ranks a rule with more static text higher — but ONLY
for a lowercase suffix. ``.JSON`` and every other extension still fall through
to the normaliser, and /dcpi has no twin at all.

The period this consolidation targets is the one inside a NAME ("St. Louis").
A period that introduces an extension is a different thing and must be split
off first. The suffix list is an explicit allowlist rather than a
"last dot wins" rule on purpose: ``washington-d.c`` would otherwise be read as
base ``washington-d`` + extension ``c``, and the real consolidation to
``washington-dc`` would break.
"""

# Extensions a caller plausibly appends to a page URL to ask for the same
# entity in another representation. Kept deliberately short — every entry is a
# suffix we would rather answer than silently fold into the slug.
KNOWN_SUFFIXES = (
    "json", "xml", "html", "htm", "txt", "csv", "md", "yaml", "yml", "rss",
    "atom", "pdf",
)


def split_suffix(slug):
    """Return ``(base, suffix)`` where suffix is a lowercase known extension.

    ``suffix`` is ``""`` when the slug carries no known extension, in which
    case ``base`` is the slug unchanged. Case-insensitive, so ``.JSON`` and
    ``.json`` split identically — the uppercase form is exactly what escaped
    the ``/markets/<slug>.json`` route and reached the normaliser.

    >>> split_suffix("northern-virginia.json")
    ('northern-virginia', 'json')
    >>> split_suffix("northern-virginia.JSON")
    ('northern-virginia', 'json')
    >>> split_suffix("st.-louis")          # the period this module protects
    ('st.-louis', '')
    >>> split_suffix("washington-d.c")     # not an extension — an initial
    ('washington-d.c', '')
    """
    s = slug or ""
    base, dot, ext = s.rpartition(".")
    if not dot:
        return s, ""
    if ext.lower() in KNOWN_SUFFIXES:
        return base, ext.lower()
    return s, ""


def normalize_periods(slug):
    """The r-period-slug consolidation, applied only to the NAME part.

    Returns ``(canonical_base, suffix)``. The caller decides what URL to build
    from them; this never returns a target itself, because /markets and /dcpi
    resolve a bare slug to different canonical pages.
    """
    base, ext = split_suffix(slug)
    return base.replace(".", ""), ext
