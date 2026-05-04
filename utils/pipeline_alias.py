"""Phase 32B — pipeline alias normalization.

The MCP tool get_pipeline accepts a query string. Free-text inputs like
"amazon", "google", "microsoft" should resolve to canonical pipeline
records. Without this, the selfheal probe with "amazon" returns empty.
"""

ALIAS_MAP = {
    "amazon":       ["AWS", "Amazon", "AMAZON", "amazon-web-services"],
    "aws":          ["AWS", "Amazon"],
    "google":       ["GCP", "Google", "GOOGLE", "google-cloud"],
    "gcp":          ["GCP", "Google"],
    "microsoft":    ["Azure", "Microsoft", "MICROSOFT", "msft"],
    "azure":        ["Azure", "Microsoft"],
    "msft":         ["Azure", "Microsoft"],
    "meta":         ["Meta", "Facebook", "META", "FB"],
    "facebook":     ["Meta", "Facebook"],
    "fb":           ["Meta", "Facebook"],
    "apple":        ["Apple", "AAPL"],
    "oracle":       ["Oracle", "ORCL"],
    "tesla":        ["Tesla", "TSLA"],
    "openai":       ["OpenAI", "Microsoft"],
    "x":            ["X", "Twitter"],
    "twitter":      ["X", "Twitter"],
    "tiktok":       ["TikTok", "ByteDance"],
    "bytedance":    ["TikTok", "ByteDance"],
}


def expand_query(q):
    """Return a list of canonical names to search for, given any input.

    `expand_query("amazon")` -> ["AWS", "Amazon", "AMAZON", ...]
    `expand_query("AWS")`    -> ["AWS"] (case-preserved if not in alias map)
    """
    if not q:
        return []
    norm = q.strip().lower()
    if norm in ALIAS_MAP:
        return ALIAS_MAP[norm]
    return [q.strip()]


def matches_any(value, query):
    """Case-insensitive substring match against any expansion of query."""
    if not value:
        return False
    val_lower = str(value).lower()
    for cand in expand_query(query):
        if cand.lower() in val_lower:
            return True
    return False
