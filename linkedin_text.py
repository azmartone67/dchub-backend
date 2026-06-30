"""LinkedIn "little" commentary text format — escape reserved characters.

The /rest/posts `commentary` field uses LinkedIn's "little text format", whose
reserved characters MUST be backslash-escaped. If they aren't, LinkedIn
TRUNCATES the post at the first unescaped reserved character. An unescaped '('
silently cut ~1 in 5 DC Hub posts at their first parenthetical — the "Guam "
("Guam (GPA)…" → "Guam ") and "…28.6 (CAUTION)…" → "…28.6 " incidents.

Reserved set (per the little-text-format grammar):
    \\  |  {  }  @  [  ]  (  )  <  >  #  *  _  ~
"All reserved characters need to be escaped with a backslash, even if those
characters are not used in one of the supported elements or templates."

This helper escapes LITERAL reserved characters while PRESERVING the things that
legitimately contain them:
  • mentions   @[Name](urn:li:organization:123)   — little-format mention syntax
  • hashtags   #word                              — rendered as a clickable tag
  • URLs       https://…                          — may contain _ ~ ( ) etc.

NOTE: this is ONLY for the modern /rest/posts `commentary` field. The legacy
/v2/ugcPosts `shareCommentary.text` field is PLAIN text — do NOT escape it.
"""
import re

# Order of protection matters: mentions before URLs before hashtags so a URL
# inside a mention token isn't double-stashed.
_MENTION_RE = re.compile(r'@\[[^\]]+\]\(urn:li:[^)]+\)')
_URL_RE = re.compile(r'https?://[^\s]+')
_HASHTAG_RE = re.compile(r'(?<!\w)[#＃]\w+')
# Backslash is escaped FIRST and separately, so it is not in this set.
_RESERVED = '|{}@[]()<>#*_~'
_PH = '\x00%d\x00'  # placeholder for stashed tokens (no reserved chars inside)


def escape_li_commentary(text):
    """Return `text` safe to send as a /rest/posts `commentary` value.

    Idempotency caveat: NOT idempotent (escaping twice double-escapes), so call
    exactly once, at the publish boundary, on raw human/LLM text. Store the raw
    text elsewhere; only the outgoing payload should be escaped."""
    if not text or not isinstance(text, str):
        return text
    stash = []

    def _hold(m):
        stash.append(m.group(0))
        return _PH % (len(stash) - 1)

    t = _MENTION_RE.sub(_hold, text)
    t = _URL_RE.sub(_hold, t)
    t = _HASHTAG_RE.sub(_hold, t)
    # Escape literal backslashes first, then every other reserved char.
    t = t.replace('\\', '\\\\')
    for ch in _RESERVED:
        t = t.replace(ch, '\\' + ch)
    # Restore protected tokens verbatim (they are valid little-format already).
    for i, tok in enumerate(stash):
        t = t.replace(_PH % i, tok)
    return t


if __name__ == '__main__':  # quick self-test: python3 linkedin_text.py
    cases = [
        ("Guam (GPA) just shifted to AVOID on the DC Hub Power Index.",
         "Guam \\(GPA\\) just shifted to AVOID on the DC Hub Power Index."),
        ("Abilene held flat at a DCPI score of 28.6 (CAUTION), context matters.",
         "Abilene held flat at a DCPI score of 28.6 \\(CAUTION\\), context matters."),
        ("Read more at https://dchub.cloud/dcpi/guam #datacenter #DCPI",
         "Read more at https://dchub.cloud/dcpi/guam #datacenter #DCPI"),
        ("Thanks @[Data Center Hub](urn:li:organization:110894959) (really)",
         "Thanks @[Data Center Hub](urn:li:organization:110894959) \\(really\\)"),
        ("C# and a path a_b and <tag> and [note]",
         "C\\# and a path a\\_b and \\<tag\\> and \\[note\\]"),
    ]
    ok = True
    for src, want in cases:
        got = escape_li_commentary(src)
        flag = 'OK ' if got == want else 'FAIL'
        if got != want:
            ok = False
        print(f"{flag} {src!r}\n     -> {got!r}")
    print("ALL PASS" if ok else "SOME FAILED")
