"""media_post_quality.py — the defects a reader sees, measured (2026-08-24).

WHY THIS EXISTS. `media_editorial_gate.editorial_gate()` has run a 14-day
cooldown + an LLM editorial review since 2026-07-19 — but its ONLY callers are
`dcpi_auto_press` and `marketing_engine`. The module that actually publishes the
LinkedIn quad references it ZERO times, so nothing has ever judged the OUTPUT of
the 4-posts-a-day lane.

I read the 15 posts that actually shipped (`/api/v1/linkedin-quad/status` now
returns `post_text`, per #3105) and counted:

    truncated mid-sentence                       3 of 15   (detector is conservative;
                                                            a 4th ends "Source: D")
    opened a paragraph "The second-order read"  13 of 15   (87%)
    the same 8-word phrase twice in ONE post     2 of 15
    facility counts claimed across the set       SEVEN different numbers
                                                 (18,300 / 18,400×7 / 18,500 /
                                                  18,644 + raw-record 26,228 /
                                                  26,327 / 26,334)

Two of those are unambiguous BREAKAGE and belong in the publish gate. One is a
house-style problem that a gate must NOT touch: blocking 87% of posts would
silence the feed, which is the failure this whole program has been digging out
of. It belongs in the composer's prompt as a ban list — see `overused_openers`.

★ THE SPLIT IS THE POINT. Block what is broken; steer what is repetitive.
  A quality gate that turns a stylistic tic into silence is a worse bug than
  the tic.

Pure + stdlib-only: no DB, no network, no Flask. Every function is total and
returns "" / [] rather than raising, so a caller can never be dark-held by it.
"""
from __future__ import annotations

import re

# Lines that are furniture, not prose — a post legitimately ends on any of them.
_FURNITURE = re.compile(
    r"^\s*(?:#|→|—|\(DC Hub data\b|Source\s*:|Try it\s*:|Full index|Closed transactions|https?://)",
    re.I)
# Terminal punctuation a finished sentence may end on.
_TERMINAL = ('.', '!', '?', '"', '”', ':', ')', '…')

_ENDS_URL = re.compile(r"https?://\S+\s*$")

_SHINGLE_N = 8


def _lines(text: str) -> list:
    return [l.strip() for l in (text or "").split("\n") if l.strip()]


# Function words a sentence never ENDS on. A final line dangling on one of
# these is cut; a line ending on a content word is a headline.
_DANGLING = {
    "a", "an", "and", "or", "but", "the", "of", "in", "on", "at", "to", "for",
    "with", "by", "from", "as", "into", "onto", "than", "that", "which", "who",
    "is", "are", "was", "were", "be", "been", "has", "have", "had", "will",
    "its", "their", "our", "your", "his", "her", "this", "these", "those",
    "over", "under", "across", "between", "about", "after", "before", "while",
    "when", "where", "how", "not", "no", "if", "so", "per", "via", "up", "out",
}


def truncation_reason(text: str) -> str:
    """Non-empty reason if the post reads as CUT OFF mid-thought.

    Measured cases this must catch, verbatim from the live feed:
        "…and it is not cosmetic. Double-counted facilities in"   (08-24 ledger)
        "Source: D"                                               (08-21 deal)
        "The second-order read: headroom"                         (08-19 build)
        "(DC Hub data · Aug 19, 2026"                             (unclosed)

    ★★★ AND THE CASE IT MUST NOT CATCH — measured in PRODUCTION 2026-08-25,
    hours after the first version shipped. `/api/v1/media/self-critique`
    showed two real posts blocked as "broken copy":

        "…'s Cold-Load Corridor Now Competitive with Texas and Oklahoma"
        "Real-Time Infrastructure Intelligence Reaching Mainstream AI"

    Both are HEADLINES. A headline has no terminal punctuation by convention,
    so "ends without a full stop" flagged perfectly good copy and silenced two
    slots. That is strictly worse than the truncation it was written to stop:
    a truncated post is embarrassing, a false positive is silence — the exact
    failure this whole program spent August digging out of.

    So the bar is no longer "no terminal punctuation". It is a POSITIVE signal
    of a cut:
      · the line dangles on a function word ("… facilities in")  — _DANGLING;
      · a colon with almost nothing after it ("… read: headroom");
      · a severed label ("Source: D");
      · an unclosed bracket.
    A line ending on a content word is treated as a headline and passes.
    """
    lines = _lines(text)
    if not lines:
        return ""

    # An unclosed bracket is a cut wherever it lands, furniture included.
    for l in lines:
        if l.count("(") > l.count(")"):
            return f"unclosed '(' : {l[-60:]!r}"

    # A label whose value was severed ("Source: D").
    for l in lines:
        m = re.match(r"^\s*(?:Source|Try it|Full index|Closed transactions)\s*:\s*(.*)$",
                     l, re.I)
        if m and 0 < len(m.group(1).strip()) < 12:
            return f"label with a severed value: {l[-60:]!r}"

    prose = [l for l in lines
             if not _FURNITURE.match(l) and not _ENDS_URL.search(l)]
    if not prose:
        return ""
    last = prose[-1]
    if last.endswith(_TERMINAL):
        return ""

    words = re.findall(r"[A-Za-z0-9'’-]+", last)
    if not words:
        return ""

    # ★ Dangling function word — the signature of a real cut.
    if words[-1].lower().strip("'’-") in _DANGLING:
        return f"final prose line dangles on '{words[-1]}': {last[-60:]!r}"

    # ★ A colon that introduces almost nothing ("The second-order read: headroom").
    if ":" in last:
        tail = last.rsplit(":", 1)[1].strip()
        if 0 < len(re.findall(r"[A-Za-z0-9'’-]+", tail)) <= 2:
            return f"colon introduces a fragment: {last[-60:]!r}"

    return ""   # ends on a content word — a headline, not a cut


def intra_post_repeat(text: str, n: int = _SHINGLE_N) -> str:
    """Non-empty reason if one post says the same n-word phrase twice.

    This is the glued-openings bug: the 08-21 telemetry post announced Japan /
    South Korea / Brazil in its first paragraph and announced it AGAIN in its
    third, with a stray lowercase fragment wedged between them. Three drafts
    concatenated into one post.
    """
    words = re.findall(r"[A-Za-z0-9']+", (text or "").lower())
    if len(words) < n * 2:
        return ""
    seen: dict = {}
    for i in range(len(words) - n + 1):
        sh = " ".join(words[i:i + n])
        if sh in seen:
            return f"the same {n}-word phrase appears twice: {sh!r}"
        seen[sh] = i
    return ""


def opener_phrases(text: str, n: int = 4) -> list:
    """The first n words of each AUTHORED paragraph, normalized.

    ★ Furniture paragraphs are skipped. Including them made the boilerplate
      footer ("Source: DC Hub, the live…") outrank the real tic in the overuse
      ranking — the ban list would have told the writer to stop using its own
      required attribution line.
    """
    out = []
    for para in re.split(r"\n\s*\n", text or ""):
        para = para.strip()
        if not para or _FURNITURE.match(para):
            continue
        words = re.findall(r"[A-Za-z0-9']+", para.lower())
        if len(words) >= n:
            out.append(" ".join(words[:n]))
    return out


def overused_openers(texts: list, min_count: int = 3, n: int = 4) -> list:
    """Paragraph openers used at least `min_count` times across recent posts.

    ★ FEEDS THE COMPOSER, NEVER THE GATE. "The second-order read" opened a
    paragraph in 13 of 15 published posts. Blocking that is blocking the feed;
    telling the writer "you have used this opener 13 times, use another" is
    what an editor would actually do.
    """
    counts: dict = {}
    for t in (texts or []):
        for op in set(opener_phrases(t, n=n)):   # once per post, not per repeat
            counts[op] = counts.get(op, 0) + 1
    return sorted([p for p, c in counts.items() if c >= min_count],
                  key=lambda p: (-counts[p], p))


def ban_list_block(texts: list, min_count: int = 3) -> str:
    """The composer-prompt fragment. Empty string when nothing is overused, so
    it can be concatenated unconditionally."""
    banned = overused_openers(texts, min_count=min_count)
    if not banned:
        return ""
    lines = "\n".join(f'  - "{p}…"' for p in banned[:6])
    return ("\nOPENERS YOU HAVE OVERUSED — do not start any paragraph with these, "
            "and do not paraphrase them into the same rhythm:\n" + lines +
            "\nVary how you introduce the second-order point. An analyst who "
            "opens the same way every time reads as a template.\n")
