"""Canonical Gemini / Google AI Studio key resolver.

Since 2026-07-10 the Railway GEMINI_API_KEY value carries a paste
artifact — the literal var name appended to an otherwise-valid key
("AQ.…yJggQGEMINI_API_KEY"). Every consumer that read the var raw
(citation tracker, citation scraper, model relations) got HTTP 400s and
silently skipped the Gemini lane, flatlining the Gemini share-of-voice
series. The stripped key was verified working against
generativelanguage.googleapis.com on 2026-07-17, as is GOOGLE_AI_KEY
(classic AIza… AI Studio key on the same project).

One resolver, used by every Gemini caller: sanitize the pasted suffix,
then fall back across the known var names. Never raises.
"""
import os

# Var names an operator has historically used for this key, in preference
# order. testimonial_probe discovered the same drift independently and
# carries its own inline chain — keep this list a superset of that one.
_VAR_CHAIN = ("GEMINI_API_KEY", "GOOGLE_AI_API_KEY", "GOOGLE_AI_KEY",
              "GOOGLE_API_KEY")


def gemini_api_key() -> str:
    """Best usable Gemini API key, or '' if none is configured."""
    for env in _VAR_CHAIN:
        k = (os.environ.get(env) or "").strip()
        if not k:
            continue
        # Strip an accidentally-pasted trailing var NAME (the 07-10 artifact).
        for suffix in _VAR_CHAIN:
            if k.endswith(suffix) and len(k) > len(suffix):
                k = k[: -len(suffix)].strip()
        if k:
            return k
    return ""


# Model churn has now broken Gemini probes THREE ways (gemini-1.5-flash
# retired → 404; gemini-2.5-flash gated off for this project → 404;
# gemini-2.0-flash daily quota → 429). The -latest aliases exist exactly to
# absorb retirement churn; keep 2.0-flash as the historic middle hop while
# it still serves, and the lite alias as the cheap last resort.
GEMINI_MODEL_CHAIN = ("gemini-flash-latest", "gemini-2.0-flash",
                      "gemini-flash-lite-latest")


def gemini_generate(prompt_text: str, timeout: int = 30) -> tuple:
    """One generateContent call, walking GEMINI_MODEL_CHAIN past 404/429.

    Returns (text, err) — err is None on success, else 'no_key' or the
    LAST hop's failure ('http_429', 'exc:…'). Shared by the citation
    tracker + scraper so the next model retirement is a one-line fix here
    instead of another silent per-caller flatline.
    """
    import json
    import urllib.request
    key = gemini_api_key()
    if not key:
        return "", "no_key"
    err = "no_model_attempted"
    for model in GEMINI_MODEL_CHAIN:
        try:
            req = urllib.request.Request(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent",
                data=json.dumps({"contents": [{"parts": [
                    {"text": prompt_text}]}]}).encode(),
                # key in a HEADER, never the query string — a query key is
                # written verbatim into gateway/proxy logs (see
                # tests/test_no_provider_key_in_url.py).
                headers={"Content-Type": "application/json",
                         "User-Agent": "dchub-brain/1.0",
                         "x-goog-api-key": key})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            text = ""
            for cand in (data.get("candidates") or []):
                for part in ((cand.get("content") or {}).get("parts") or []):
                    text += " " + (part.get("text") or "")
            if text.strip():
                return text.strip(), None
            err = "no_candidates"
        except Exception as e:
            # urllib.error.HTTPError carries .code; anything else is exc:…
            code = getattr(e, "code", None)
            err = f"http_{code}" if code else f"exc:{str(e)[:60]}"
            # 404 (retired model) / 429 (quota) → try the next hop; anything
            # else (network, parse) also just walks on — the chain is short.
    return "", err
