"""The card photo library must not ship a third party's screen (2026-09-05).

`data/images.json` carried `dc-cooling-1` — filed under "cooling", tagged
`infrastructure` — pointing at Unsplash photo-1600267165477-6d4cc741b379. That
photograph is a person looking at an **Umbraco CMS redirect manager on a Windows
laptop**: another company's host name (`cms-exfo.azurewebsites.net`) is legible
in the address bar, their brand is in the sidebar, and 4,418 rows of their URLs
are on screen.

It was not an edge case — it was the DEFAULT for the homepage. ImageMatcher
scores +2 per tag found as a substring of the title+content. For the live
homepage title ("The neutral data layer for data-center infrastructure …") it
scored 3 on the single generic tag `infrastructure` while every other entry
scored 1 (the style bonus alone). So dchub.cloud's OG card — what LinkedIn,
Slack and X render for our own front page — was a stranger's CMS.

Two call sites had to change, which is why a one-line JSON deletion would not
have held: `ImageMatcher.load_images()` falls back to `_get_default_images()`
whenever `data/images.json` is missing, and then WRITES IT BACK. Railway's
filesystem is ephemeral, so the deleted entry would have regenerated itself.
"""
import json
import os
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Unsplash photo IDs that must never appear in this repo again, and why.
DENYLISTED_PHOTOS = {
    "photo-1600267165477-6d4cc741b379":
        "a third party's Umbraco CMS admin panel, their domain legible on screen",
}


def _tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=REPO,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"git ls-files failed: {out.stderr}"
    files = [f for f in out.stdout.splitlines() if f]
    assert len(files) > 100, f"suspiciously few tracked files ({len(files)}) — bad scan"
    return files


@pytest.mark.parametrize("photo_id,reason", sorted(DENYLISTED_PHOTOS.items()))
def test_denylisted_photo_appears_nowhere_in_the_repo(photo_id, reason):
    """Scans every TRACKED file, so re-adding it through data/images.json,
    _get_default_images(), a fixture or a doc all fail the same way."""
    hits = []
    for rel in _tracked_files():
        path = os.path.join(REPO, rel)
        try:
            with open(path, "rb") as fh:
                if photo_id.encode() in fh.read():
                    hits.append(rel)
        except (OSError, IsADirectoryError):
            continue
    # This test file names the ID on purpose — it is the denylist.
    hits = [h for h in hits if h != "tests/test_image_library_denylist.py"]
    assert not hits, (
        f"{photo_id} is back in {hits} — it is {reason}. "
        f"Do not publish it under the DC Hub brand.")


def test_the_library_and_its_regenerating_defaults_agree():
    """load_images() rewrites data/images.json from _get_default_images() when
    the file is absent (Railway's FS is ephemeral). A URL purged from one and
    not the other comes back on the next cold start."""
    from services.image_matcher import ImageMatcher

    disk = {i.get("url") for i in json.load(
        open(os.path.join(REPO, "data", "images.json")))}
    defaults = {i.get("url") for i in ImageMatcher._get_default_images(None)}
    for photo_id in DENYLISTED_PHOTOS:
        assert not any(photo_id in (u or "") for u in disk), \
            f"{photo_id} still in data/images.json"
        assert not any(photo_id in (u or "") for u in defaults), \
            f"{photo_id} still in _get_default_images() — it will regenerate"


def test_every_library_entry_still_resolves_to_a_photo():
    """Deleting an entry must not leave a hole the matcher can select."""
    lib = json.load(open(os.path.join(REPO, "data", "images.json")))
    assert lib, "the library must not be empty — match() falls back to images[0]"
    for img in lib:
        assert (img.get("url") or "").startswith("https://images.unsplash.com/"), \
            f"{img.get('id')} has no usable url: {img.get('url')!r}"
        assert img.get("id") and img.get("category") and img.get("tags"), \
            f"incomplete library entry: {img}"


def test_the_homepage_no_longer_matches_the_removed_photo():
    """End-to-end on the REAL live homepage title — the exact input that
    selected the CMS screenshot in production on 2026-09-05."""
    from services.image_matcher import ImageMatcher

    m = ImageMatcher()
    title = "The neutral data layer for data-center infrastructure"
    blob = f"{title} 20,500+ facilities · 170+ countries · cited by Claude & Cursor"
    picked = (m.match(title, blob) or {}).get("image") or {}
    for photo_id, reason in DENYLISTED_PHOTOS.items():
        assert photo_id not in (picked.get("url") or ""), (
            f"the homepage card still resolves to {picked.get('id')} — {reason}")
    assert picked.get("url"), "homepage matched nothing at all"
