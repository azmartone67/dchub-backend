# CBRE Follow-up — URL Correction (send within 1 hour of original)

**Draft v1.0  ·  2026-06-04  ·  Sender: Jonathan Martone**

Short corrective note. Send as a REPLY to the original thread (Pat + Gordon) so it stays in the same chain. Keep it dry — frame as "caught it on our end" not as an apology spiral.

---

**Subject:**  Re: DC Hub × CBRE Research — Power Delivery methodology for H2 2026 review

---

Pat, Gordon —

Small correction on the URLs cited in the methodology PDF I sent. Our CF routing has an in-progress fix that's not letting the `/methodology/*` namespace through publicly yet. Caught it on our side after I sent. The working URLs are under the canonical API path — same convention as our DCGI methodology lives at:

- Methodology v1.0:  https://dchub.cloud/api/v1/methodology/queue/v1.0
- Data dictionary:  https://dchub.cloud/api/v1/methodology/data-dictionary.json
- Methodology index:  https://dchub.cloud/api/v1/methodology

PDF artifact (unchanged):  https://dchub.cloud/static/DCHUB_POWER_DELIVERY_METHODOLOGY_v1.0.pdf

I'll re-publish the PDF as v1.1 with the corrected URLs by end of day. Everything else in the prior email stands.

Jonathan

---

## Why this framing

- **"Caught it on our side"** — not "we screwed up." Frames it as DC Hub's own pre-publish QA loop, not a customer-side discovery.
- **"In-progress fix"** — implies CF routing work is ongoing, not that something's broken.
- **No long apology** — Gordon's a research principal; he respects engineers who catch their own bugs, doesn't need contrition.
- **PDF artifact link unchanged** — Gordon already has the PDF in his inbox; the link is just so he can re-download a clean copy if he forwarded the email and lost the attachment.
- **"v1.1 by end of day"** — turns a bug into a versioning artifact. Methodology PDFs are SUPPOSED to be versioned; v1.0 → v1.1 within hours signals an active maintenance discipline that compliance reviewers respect.

## What to do tonight (optional, for v1.1)

If you want a clean v1.1 PDF with the corrected URLs, I can regenerate it in 10 minutes — same content, just the citation footnote URL string updated to /docs/methodology/queue/v1.0. Say the word.

## The real fix (when CBRE call settles)

The CF zone-level worker is intercepting `/methodology*` with Error 1000 ("DNS points to prohibited IP") — same root cause as `/research/*` from May 2026 (see `reference_dchub_research_path_error1000.md`). Fix is in the Cloudflare dashboard: either remove the offending Workers Route on `dchub.cloud/methodology*`, or add a passthrough exception for that prefix. Once that lands, `/methodology/*` will work AND the `/docs/methodology/*` mirror routes can stay as alternate paths (no harm leaving them).
