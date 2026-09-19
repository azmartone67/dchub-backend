"""No Unsplash under the suite: hand the card renderer a photo, not a socket.

routes/og_cards._library_bg() downloads a curated Unsplash photo over HTTP and
returns None on any problem, so the renderer falls back to a flat navy canvas.
Under the suite that download is dead weight: measured 2026-09-13, ten test ids
across three files reached images.unsplash.com on every run, and every one of
them passed with the network refused — because the refusal IS the fallback.

So the tests were already exercising the no-photo path; the only thing the
fetch added was a third-party request per card render and a suite whose speed
depended on Unsplash. This hands them a deterministic photo instead, which
removes the request AND puts the compositing path back under test, where a
refused socket had quietly taken it out.

★ The stub returns a NEW image per call. _library_bg's contract is that callers
own what they get — it hands out `.copy()` of its cache for exactly that reason
— and a renderer that draws onto a shared instance would otherwise accumulate
every previous card's typography.
"""


def stub_library_photo(monkeypatch, colour=(18, 28, 51)):
    """Point og_cards._library_bg at a synthetic canvas. Returns the module."""
    from PIL import Image
    from routes import og_cards

    def _fake_library_bg(pr):
        return Image.new("RGB", (og_cards.W, og_cards.H), colour)

    monkeypatch.setattr(og_cards, "_library_bg", _fake_library_bg)
    return og_cards
