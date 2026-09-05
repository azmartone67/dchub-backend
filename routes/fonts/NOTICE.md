# Bundled typefaces

The card renderer must not depend on host fonts — Railway's Nixpacks image has
none at the system paths, which is the 2026-06-06 "empty cards" bug
(`ImageFont.load_default()` ignores the requested size). Everything `og_cards.py`
draws with is bundled here.

## Brand faces — these are what dchub.cloud itself serves

| File | Family | Licence |
|---|---|---|
| `InstrumentSans-{Bold,SemiBold,Medium,Regular}.ttf` | Instrument Sans | SIL OFL 1.1 — Copyright 2022 The Instrument Sans Project Authors, https://github.com/Instrument/instrument-sans |
| `JetBrainsMono-{Bold,Regular}.ttf` | JetBrains Mono | SIL OFL 1.1 — Copyright 2020 The JetBrains Mono Project Authors, https://github.com/JetBrains/JetBrainsMono |

The site's stylesheet declares exactly these two families:

    --font  'Instrument Sans', -apple-system, BlinkMacSystemFont, sans-serif
    --mono  'JetBrains Mono', 'SF Mono', monospace

Both are SIL Open Font License 1.1, which permits redistribution in this form.
Full licence text: https://openfontlicense.org

## Fallback faces

`DejaVuSans*.ttf` are kept as the last resort before `load_default()`. They were
the ONLY bundled faces until 2026-09-05, which is why every card rendered in
DejaVu Sans Mono while the website rendered in Instrument Sans — the generator
could not set the brand face because the brand face was not present.
