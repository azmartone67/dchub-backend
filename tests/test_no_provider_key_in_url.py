"""A provider credential must never be interpolated into a URL query string.

★ WHY, measured 2026-09-06. ai_wars_automation._call_google built its request
as `...:generateContent?key={key}`. Google AI Studio accepts that form, so it
worked — and Cloudflare's AI Gateway records the full request PATH, so every
call wrote GOOGLE_AI_KEY into the gateway log in plaintext. Reading those logs
with a freshly-granted AI Gateway Read token showed the live key in 4 rows.
Query strings also reach proxy logs, browser history and Referer headers; the
gateway is simply where it was caught.

The header form (`x-goog-api-key`, `Authorization`, `X-API-Key`) is logged by
none of them, which is why this is a rule and not a preference.

SCOPE, deliberately narrow to stay actionable:
  · only EXTERNAL hosts — dchub.cloud's own `/upgrade?key={api_key}` hands a
    user their OWN key over TLS and is not this defect;
  · only formatted strings — f-strings, and %-formatting or str.format() on a
    literal — whose text ends in a secret-ish `?param=` right before a field.
    A hard-coded `?key=test-key` in a test is inert and is not flagged.
  · the host does not have to sit in the same literal: a base URL held in a
    constant, a variable, a parameter default or the left side of a `+` is
    followed (shape 4 below), and a base that cannot be followed is REPORTED,
    not assumed to be ours.
  · urlencode() dict building IS covered, by a second detector below — it
    was added after that gap hid three live sites from the first one.
  · requests(params=...) is covered by a third, including a dict picked by a
    conditional expression.
  · both of those also follow a key written into the dict after it was built
    (`params["api_key"] = KEY`), within one scope.
  · Anything not listed here is not seen.
"""
import ast
import collections
import functools
import pathlib
import re
import string

_ROOT = pathlib.Path(__file__).resolve().parents[1]

# The literal immediately BEFORE an interpolation, ending in a secret param.
_SECRET_PARAM = re.compile(
    r"[?&](key|api[-_]?key|access[-_]?token|auth[-_]?token|token|secret|"
    r"password|passwd|pwd|sig|signature)=\Z", re.I)

# Hosts we control: handing a user their own key over TLS is a different thing.
_OURS = ("dchub.cloud", "localhost", "127.0.0.1", "0.0.0.0", "railway.internal")

_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
              "site-packages", ".mypy_cache", ".pytest_cache", "build", "dist"}


def _is_external(url_head: str) -> bool:
    m = re.search(r"https?://([^/\s'\"]+)", url_head)
    if not m:
        return False                      # relative path — not an outbound URL
    host = m.group(1).lower()
    return not any(o in host for o in _OURS)


# A %-conversion: %s, %(name)s, %-10.3f ... and %% for a literal percent sign.
_PCT_FIELD = re.compile(
    r"%(?:\((?P<key>[^)]*)\))?[-#0 +]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[hlL]?"
    r"(?P<conv>[a-zA-Z%])")


def _format_parts(node):
    """A formatted string as a list of literal text (str) and fields (the
    field's expression, or None when it cannot be named): an f-string,
    `"literal" % args` or `"literal".format(...)`. None for any other node.
    """
    if isinstance(node, ast.JoinedStr):
        return [v.value if isinstance(v, ast.Constant) else
                getattr(v, "value", None) for v in node.values]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) \
            and isinstance(node.left, ast.Constant) \
            and isinstance(node.left.value, str):
        fmt, args = node.left.value, node.right
        named = ({k.value: v for k, v in zip(args.keys, args.values)
                  if isinstance(k, ast.Constant)}
                 if isinstance(args, ast.Dict) else {})
        positional = list(args.elts) if isinstance(args, ast.Tuple) else [args]
        parts, pos, i = [], 0, 0
        for m in _PCT_FIELD.finditer(fmt):
            parts.append(fmt[pos:m.start()])
            pos = m.end()
            if m.group("conv") == "%":
                parts.append("%")
            elif m.group("key") is not None:
                parts.append(named.get(m.group("key")))
            else:
                parts.append(positional[i] if i < len(positional) else None)
                i += 1
        parts.append(fmt[pos:])
        return parts
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "format" \
            and isinstance(node.func.value, ast.Constant) \
            and isinstance(node.func.value.value, str):
        try:
            fields = list(string.Formatter().parse(node.func.value.value))
        except ValueError:
            return None
        keywords = {k.arg: k.value for k in node.keywords if k.arg}
        parts, auto = [], 0
        for literal, field, _spec, _conv in fields:
            parts.append(literal)
            if field is None:
                continue
            name = re.split(r"[.\[]", field, maxsplit=1)[0]
            if name == "":
                parts.append(node.args[auto] if auto < len(node.args) else None)
                auto += 1
            elif name.isdigit():
                idx = int(name)
                parts.append(node.args[idx] if idx < len(node.args) else None)
            else:
                parts.append(keywords.get(name))
        return parts
    return None


def scan_source(src: str, label: str):
    """Return [(label, lineno, param)] for secrets interpolated into a URL."""
    hits = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return hits
    for node in ast.walk(tree):
        parts = _format_parts(node)
        if parts is None:
            continue
        head = ""
        for part in parts:
            if isinstance(part, str):
                head += part
                continue
            # a field: does the literal just before it name a secret?
            m = _SECRET_PARAM.search(head)
            if m and _is_external(head):
                hits.append((label, node.lineno, m.group(1)))
            head += "\x00"                # opaque placeholder, keeps offsets sane
    return hits


def _query_bound_urlencode_args(nodes, ctx):
    """Names passed to a urlencode() whose RESULT lands in a query string.

    ★ urlencode IS NOT A QUERY-STRING SIGNAL ON ITS OWN. An OAuth token
    exchange form-encodes client_secret/password into the request BODY, which
    is correct and must not be flagged — a first draft of this check reported
    routes/ercot_realtime.py and linkedin_poster.py as leaks. What makes it a
    leak is the result being concatenated into a URL, so that is what we look
    for: a "?" or "&" literal beside the call, either directly or through the
    variable the call was assigned to.

    `nodes` is what to read: ast.walk(tree) for the whole module, or
    _scope_nodes() for one scope; `ctx()` gives the module's _Ctx. A URL whose
    base _resolve() places on one of _OURS is not counted, whether the query
    is joined where the link is built or after it was assigned — the exemption
    scan_source() gives our own links. A base that cannot be followed is
    counted, as in shape 4.
    """
    def has_q(node):
        return any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                   and ("?" in n.value or "&" in n.value) for n in ast.walk(node))

    def encoded(node):
        # the names urlencode() is called on anywhere inside node
        out = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                fn = n.func
                if (isinstance(fn, ast.Name) and fn.id == "urlencode") or \
                   (isinstance(fn, ast.Attribute) and fn.attr == "urlencode"):
                    out.update(a.id for a in n.args[:1] if isinstance(a, ast.Name))
        return out

    def ours(node):
        return _where(_resolve(ctx(), node)) == "ours"

    direct, via_var, joined = set(), {}, []
    for node in nodes:
        # (a) urlencode() sitting inside a concat / f-string that has ? or &
        if isinstance(node, (ast.BinOp, ast.JoinedStr)) and has_q(node):
            joined.append(node)
        # (b) q = urlencode(params)   ... later   f"{base}?{q}"  — unless the
        #     value is already a whole link on our own host
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            names = encoded(node.value)
            if names and not (has_q(node.value) and ours(node.value)):
                via_var.setdefault(node.targets[0].id, set()).update(names)
    for node in joined:
        names = encoded(node)
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and n.id in via_var:
                names |= via_var[n.id]
        if names and not ours(node):
            direct |= names
    return direct


def scan_source_urlencode(src: str, label: str):
    """Credentials smuggled into a query string via a dict + urlencode().

    ★ THIS IS THE GAP THAT HID THREE SITES. The f-string detector walks
    JoinedStr only, so `params = {"api_key": KEY}; url = base + "?" +
    urlencode(params)` was invisible to it — and that shape was live in
    eia_retirements.py and both discovery scripts while the f-string list read
    as complete. A guard that reports a clean sweep over a subset of the shapes
    it claims to cover is worse than none, because it gets believed.

    A secret-named key counts only when its VALUE IS A VARIABLE (a literal is a
    test fixture) and the dict is urlencoded INTO A URL, not into a body.
    """
    hits = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return hits
    ctx = functools.cache(lambda: _Ctx(tree))
    # ★ A key written in AFTER the literal — `params = dict(params)` then
    # `params["api_key"] = KEY` — is in no dict literal, so the loop below
    # cannot see it. Followed within one scope, for the reason _scope_nodes
    # gives: a name as common as `params` collides across functions.
    for sc in _scopes(tree):
        nodes = _scope_nodes(sc)
        written = _secret_subscript_writes(nodes)
        if not written:
            continue
        for name in sorted(written.keys() & _query_bound_urlencode_args(nodes, ctx)):
            hits += [(label, ln, key) for ln, key in written[name]]
    wanted = _query_bound_urlencode_args(ast.walk(tree), ctx)
    if not wanted:
        return hits
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in wanted \
                and isinstance(node.value, ast.Dict):
            for k, v in zip(node.value.keys, node.value.values):
                if (isinstance(k, ast.Constant) and isinstance(k.value, str)
                        and _SECRET_PARAM.search("?" + k.value + "=")
                        and _is_variable(v)):
                    hits.append((label, k.lineno, k.value))
    return hits


# ── shape 3: requests(..., params={"api_key": KEY}) ─────────────────────────
# requests builds the query string from params=, so this leaks exactly like a
# hand-built URL — and neither detector above can see it. Found 2026-09-06,
# AFTER #4008 declared the sweep complete, which is the third time a "complete"
# list here turned out to cover only the shapes someone had thought of.
_HTTP_VERBS = {"get", "post", "put", "request", "patch", "delete"}

# Call sites where the provider genuinely offers no header auth. Each entry is
# a decision with evidence, not a shrug.
_NO_HEADER_AUTH = {
    # AbstractAPI ignores X-Api-Key entirely — with a bogus header it still
    # answers {"api_key": ["This is a required argument."]} (verified
    # 2026-09-06). Their design; the key must ride in the query.
    ("routes/signup_enrichment.py", "api_key"),
}


def _scope_nodes(root):
    """Walk one scope WITHOUT descending into nested function/class bodies.

    ★ THIS IS LOAD-BEARING. A plain ast.walk from the module makes every
    `params = {...}` in the file visible to every call in the file, and a name
    as common as `params` collides constantly: an early draft of this check
    reported enhancements/site_scoring.py:363 — a WattTime call that already
    uses a Bearer header — because a DIFFERENT function's params dict shared
    the name. test_a_dict_in_another_function_does_not_match pins it.
    """
    out = []
    stack = list(ast.iter_child_nodes(root))
    while stack:
        n = stack.pop()
        out.append(n)
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(n))
    return out


def _dicts(expr):
    """The dict literals an expression can evaluate to: the dict itself, or
    each branch of a conditional expression."""
    if isinstance(expr, ast.Dict):
        return [expr]
    if isinstance(expr, ast.IfExp):
        return _dicts(expr.body) + _dicts(expr.orelse)
    return []


def _scopes(tree):
    """The module and every function in it, each to be read with _scope_nodes."""
    return [tree] + [n for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _is_variable(value):
    """A value computed at run time. A literal is a test fixture."""
    if isinstance(value, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) for v in value.values)
    return not isinstance(value, ast.Constant)


def _secret_subscript_writes(nodes):
    """name -> [(line, key)] for each `name["api_key"] = <variable>` among
    nodes: a credential written into a dict after the dict was built."""
    out = {}
    for n in nodes:
        if not isinstance(n, ast.Assign) or not _is_variable(n.value):
            continue
        for t in n.targets:
            if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                    and isinstance(t.slice, ast.Constant)
                    and isinstance(t.slice.value, str)
                    and _SECRET_PARAM.search("?" + t.slice.value + "=")):
                out.setdefault(t.value.id, []).append((n.lineno, t.slice.value))
    return out


def scan_source_params(src: str, label: str):
    """Credentials handed to requests(params=...), which become a query string."""
    hits = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return hits
    for sc in _scopes(tree):
        nodes = _scope_nodes(sc)
        # ★ A key written in after the literal was built, or into a dict that
        # arrived as an argument — `if key: params["api_key"] = key` — is in
        # no literal `local` holds.
        written = _secret_subscript_writes(nodes)
        local = {n.targets[0].id: n.value for n in nodes
                 if isinstance(n, ast.Assign) and len(n.targets) == 1
                 and isinstance(n.targets[0], ast.Name)
                 and _dicts(n.value)}
        for n in nodes:
            if not isinstance(n, ast.Call):
                continue
            fn = n.func
            verb = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if verb not in _HTTP_VERBS:
                continue
            for kw in n.keywords:
                if kw.arg != "params":
                    continue
                # ★ A conditional expression picks between dicts, and both
                # count: `{"api_key": K, ...} if K else {...}` passed the
                # Dict-only version of this check.
                dicts = _dicts(kw.value) or \
                    _dicts(local.get(getattr(kw.value, "id", "")))
                for d in dicts:
                    for k, v in zip(d.keys, d.values):
                        if (isinstance(k, ast.Constant) and isinstance(k.value, str)
                                and _SECRET_PARAM.search("?" + k.value + "=")
                                and _is_variable(v)):
                            hits.append((label, n.lineno, k.value))
                for _ln, key in written.get(getattr(kw.value, "id", ""), ()):
                    hits.append((label, n.lineno, key))
    return hits


# ── shape 4: the host is somewhere else ─────────────────────────────────────
# scan_source() reads a URL only when its https://host sits in the SAME
# literal as the secret parameter, so a URL built like these was never
# examined at all:
#
#     f"{EIA_V2_BASE}/{dataset}/data/?api_key={key}"   host in a constant
#     f"{base_url}?api_key={self.eia_api_key}"         host in a variable
#     config["url"] + f"?key={api_key}"                host left of a `+`
#     "%s&api_key=%s" % (_EIA_HH_URL, key)             host in a %-field
#
# The base is followed through assignments, parameter defaults,
# os.environ.get(NAME, default), `or`, `+` and the str methods that keep a
# string's start, then placed: an external host, one of _OURS, a relative
# path — or UNRESOLVED. Unresolved is reported exactly like external: a base
# this reading cannot follow is where every site above sat, and calling it
# ours would rebuild the blind spot one level up.
_SCHEME = re.compile(r"https?://", re.I)
# Where a URL starts inside prose: after whitespace, a quote, `<` or `(`.
_URL_BREAK = re.compile(r"[\s'\"<(]")
# The places that fail the build, and how a mixed reading ranks (worst first).
_REPORTED = ("external", "unresolved")
_RANK = ("external", "unresolved", "ours", "relative")
# str methods that leave the start of a string — and so its host — alone.
_KEEPS_START = {"strip", "rstrip", "replace", "lower", "removesuffix", "format"}


def _hostless_candidates(parts):
    """Yield (param, token, at_start, value) for each secret ?param= directly
    before a field, when the URL's own text carries no scheme.

    `token` is the run of parts that URL occupies up to the parameter — it
    starts after the last break in the literal text — and `at_start` says
    whether it runs back to the start of the string, i.e. whether the base may
    lie outside the string altogether. `value` is the field's expression.
    """
    head, token, at_start = "", [], True
    for part in parts:
        if isinstance(part, str):
            head += part
            breaks = list(_URL_BREAK.finditer(part))
            if breaks:
                token, at_start = [part[breaks[-1].end():]], False
            else:
                token.append(part)
            continue
        m = _SECRET_PARAM.search(head)
        if m and not _SCHEME.search("".join(p for p in token if isinstance(p, str))):
            yield m.group(1), list(token), at_start, part
        head += "\x00"
        token.append(part)


class _Ctx:
    """Parent links for one parsed module — built only once a candidate needs
    them, which almost no file does."""

    def __init__(self, tree):
        self.tree = tree
        self.parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node

    def chain(self, node):
        """The nodes enclosing `node`, innermost first."""
        node = self.parents.get(node)
        while node is not None:
            yield node
            node = self.parents.get(node)

    def scopes(self, node):
        """Where a name used at `node` is looked up: the enclosing functions,
        innermost first, then the module. Class bodies are skipped, as Python
        skips them for a name used inside a method."""
        return [n for n in self.chain(node)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda))] + [self.tree]

    def qualname(self, node):
        names = [n.name for n in self.chain(node)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef))]
        return ".".join(reversed(names)) or "<module>"


def _bindings(ctx, scope, name):
    """What `name` is bound to directly in `scope`: expressions, and None for
    a binding whose value cannot be read (a parameter with no default, a loop
    or `with` target, an import, a `global`). An empty list means `scope` does
    not bind the name at all."""
    found = []
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        a = scope.args
        positional = a.posonlyargs + a.args
        defaults = [None] * (len(positional) - len(a.defaults)) + list(a.defaults)
        for arg, default in (list(zip(positional, defaults))
                             + list(zip(a.kwonlyargs, a.kw_defaults))):
            if arg.arg == name:
                found.append(default)
        for arg in (a.vararg, a.kwarg):
            if arg is not None and arg.arg == name:
                found.append(None)
    for n in _scope_nodes(scope):
        if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Store):
            parent = ctx.parents.get(n)
            if isinstance(parent, ast.Assign) and n in parent.targets:
                found.append(parent.value)
            elif isinstance(parent, ast.AnnAssign) and parent.value is not None:
                found.append(parent.value)
            else:
                found.append(None)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and n.name == name:
            found.append(None)
        elif isinstance(n, ast.alias) and \
                (n.asname or n.name).split(".")[0] == name:
            found.append(None)
        elif isinstance(n, (ast.Global, ast.Nonlocal)) and name in n.names:
            found.append(None)
        elif isinstance(n, ast.ExceptHandler) and n.name == name:
            found.append(None)
    return found


def _dotted(expr):
    names = []
    while isinstance(expr, ast.Attribute):
        names.append(expr.attr)
        expr = expr.value
    if isinstance(expr, ast.Name):
        names.append(expr.id)
    return ".".join(reversed(names))


def _resolve(ctx, expr, depth=0):
    """The statically readable start of a string expression, with "\\x00"
    standing for an unreadable tail — or None when not even the start can be
    read."""
    if expr is None or depth > 8:
        return None
    if isinstance(expr, ast.Constant):
        return expr.value if isinstance(expr.value, str) else None
    parts = _format_parts(expr)
    if parts is not None:
        out = ""
        for part in parts:
            if isinstance(part, str):
                out += part
                continue
            value = _resolve(ctx, part, depth + 1)
            if value is None and not out:
                return None
            out += "\x00" if value is None else value
        return out
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left = _resolve(ctx, expr.left, depth + 1)
        if left is None:
            return None
        right = _resolve(ctx, expr.right, depth + 1)
        return left + ("\x00" if right is None else right)
    if isinstance(expr, ast.Name):
        for scope in ctx.scopes(expr):
            bound = _bindings(ctx, scope, expr.id)
            if bound:
                return _agree([_resolve(ctx, b, depth + 1) for b in bound])
        return None
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name) \
            and expr.value.id == "self":
        cls = next((n for n in ctx.chain(expr) if isinstance(n, ast.ClassDef)), None)
        bound = [] if cls is None else [
            n.value for n in ast.walk(cls) if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Attribute)
            and t.attr == expr.attr and isinstance(t.value, ast.Name)
            and t.value.id == "self"]
        return _agree([_resolve(ctx, b, depth + 1) for b in bound])
    if isinstance(expr, ast.Call):
        fn = _dotted(expr.func)
        if fn.endswith("environ.get") or fn.split(".")[-1] == "getenv":
            default = expr.args[1] if len(expr.args) > 1 else next(
                (k.value for k in expr.keywords if k.arg == "default"), None)
            return _resolve(ctx, default, depth + 1)
        if isinstance(expr.func, ast.Attribute) and expr.func.attr in _KEEPS_START:
            return _resolve(ctx, expr.func.value, depth + 1)
        return None
    if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.Or):
        return _agree([v for v in (_resolve(ctx, x, depth + 1)
                                   for x in expr.values) if v is not None])
    if isinstance(expr, ast.IfExp):
        return _agree([_resolve(ctx, expr.body, depth + 1),
                       _resolve(ctx, expr.orelse, depth + 1)])
    return None


def _where(value):
    """external / ours / relative / unresolved, for a resolved base."""
    if value is None:
        return "unresolved"
    value = value.lstrip()
    m = re.match(r"(?:https?:)?//([^/?#\s\x00]*)", value, re.I)
    if m:
        host = m.group(1).lower()
        if not host:
            return "unresolved"
        return "ours" if any(o in host for o in _OURS) else "external"
    return "relative" if value.startswith("/") else "unresolved"


def _agree(values):
    """One reading when every value can be read and all land in the same
    place; None when any cannot, or when they disagree."""
    if not values or any(v is None for v in values):
        return None
    return values[0] if len({_where(v) for v in values}) == 1 else None


def _outer_bases(ctx, node):
    """For a string that starts at its query ("?key=..."): the expressions its
    base is joined from — the left side of `base + <string>`, or of `base + q`
    when the string was assigned to `q` first."""
    parent = ctx.parents.get(node)
    if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Add) \
            and parent.right is node:
        return [parent.left]
    if isinstance(parent, ast.Assign) and len(parent.targets) == 1 \
            and isinstance(parent.targets[0], ast.Name):
        name = parent.targets[0].id
        return [n.left for n in _scope_nodes(ctx.scopes(parent)[0])
                if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add)
                and isinstance(n.right, ast.Name) and n.right.id == name]
    return []


def _place(ctx, node, token, at_start):
    """Where the URL a candidate belongs to points."""
    parts = [p for p in token if p != ""]
    if not parts:
        return "unresolved"
    first = parts[0]
    if not isinstance(first, str):
        return _where(_resolve(ctx, first))
    if first.startswith("/") and not first.startswith("//"):
        return "relative"
    if first[0] in "?&" and at_start:
        places = {_where(_resolve(ctx, b)) for b in _outer_bases(ctx, node)}
        return min(places, key=_RANK.index) if places else "unresolved"
    return "unresolved"


def _is_literal(ctx, expr, depth=0):
    """True when the value is text written in the source — a fixture — rather
    than a credential read at run time. Every binding of a name must be one."""
    if expr is None or depth > 8:
        return False
    if isinstance(expr, ast.Constant):
        return isinstance(expr.value, str)
    if isinstance(expr, ast.Name):
        for scope in ctx.scopes(expr):
            bound = _bindings(ctx, scope, expr.id)
            if bound:
                return all(_is_literal(ctx, b, depth + 1) for b in bound)
    return False


def scan_source_hostless(src: str, label: str, tree=None):
    """[(label, lineno, param, function, place)] for every secret ?param= in a
    formatted string whose own text carries no scheme.

    place: external, unresolved, ours, relative — or fixture, when the secret
    field's value is a string written in the source. The repo rule fails on
    _REPORTED."""
    found = []
    if tree is None:
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return found
    ctx = None
    for node in ast.walk(tree):
        parts = _format_parts(node)
        if parts is None:
            continue
        for param, token, at_start, value in _hostless_candidates(parts):
            if ctx is None:
                ctx = _Ctx(tree)
            place = _place(ctx, node, token, at_start)
            if place in _REPORTED and _is_literal(ctx, value):
                place = "fixture"
            found.append((label, node.lineno, param, ctx.qualname(node), place))
    return found


def _hit_is_ai_host(hit) -> bool:
    """Re-read the flagged line to decide whether it targets an AI provider."""
    f, ln, _param = hit
    try:
        lines = (_ROOT / f).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    window = " ".join(lines[max(0, ln - 1):ln + 6])
    return any(h in window for h in _AI_HOSTS)


def _python_files():
    for p in _ROOT.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        yield p


@functools.lru_cache(maxsize=1)   # every repo test below shares one walk
def scan_repo():
    hits, enc, prm, hostless, files, fstrings = [], [], [], [], 0, 0
    for p in _python_files():
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files += 1
        rel = str(p.relative_to(_ROOT))
        try:
            tree = ast.parse(src)
        except SyntaxError:
            tree = None
        if tree is not None:
            fstrings += sum(1 for n in ast.walk(tree)
                            if isinstance(n, ast.JoinedStr))
            hostless += scan_source_hostless(src, rel, tree=tree)
        hits += scan_source(src, rel)
        enc += scan_source_urlencode(src, rel)
        prm += scan_source_params(src, rel)
    return tuple(hits), files, fstrings, tuple(enc), tuple(prm), tuple(hostless)


# ── the checker must be able to SEE a violation ──────────────────────────────
_BAD = (
    "url = f'https://gateway.ai.cloudflare.com/v1/a/b/google-ai-studio"
    "/v1beta/models/x:generateContent?key={key}'\n")
_OURS_OK = "url = f'https://dchub.cloud/upgrade?key={api_key}'\n"
_HEADER_OK = (
    "r = requests.post('https://api.example.com/v1/x',\n"
    "                  headers={'x-goog-api-key': key})\n")
_LITERAL_OK = "url = f'https://api.example.com/v1/{thing}?key=not-a-real-secret'\n"
_BAD_PCT = "url = 'https://api.example.com/v1/x?key=%s' % key\n"
_BAD_FMT = "url = 'https://api.example.com/v1/x?api_key={}'.format(key)\n"


def test_the_checker_flags_the_shape_that_actually_shipped():
    """Positive control. Without this, an over-narrow regex would make every
    assertion below vacuously green."""
    hits = scan_source(_BAD, "<synthetic>")
    assert len(hits) == 1 and hits[0][2].lower() == "key", hits


def test_the_checker_does_not_flag_our_own_upgrade_link():
    assert scan_source(_OURS_OK, "<synthetic>") == []


def test_the_checker_does_not_flag_a_header_or_an_inert_literal():
    assert scan_source(_HEADER_OK, "<synthetic>") == []
    assert scan_source(_LITERAL_OK, "<synthetic>") == []


def test_the_checker_reads_percent_and_format_strings_too():
    assert [h[2] for h in scan_source(_BAD_PCT, "<synthetic>")] == ["key"]
    assert [h[2] for h in scan_source(_BAD_FMT, "<synthetic>")] == ["api_key"]


# ── the repo itself ──────────────────────────────────────────────────────────
# ★ A REPO-WIDE SCAN THAT FINDS NOTHING IS VACUOUSLY GREEN. If a bad glob, a
# moved test root or an exclude-list edit made this examine zero files, every
# assertion below would still pass. Floors sit at ~80% of the counts MEASURED
# 2026-09-06 (2,445 files, 28,669 f-strings) so ordinary churn does not trip
# them, but a scan that has stopped seeing the tree does.
_MIN_FILES = 1950
_MIN_FSTRINGS = 22900

# ── absolute rule vs. ratchet ────────────────────────────────────────────────
# AI providers are an ABSOLUTE ban: this is the surface where the leak was
# actually observed, into logs WE own and retain (Cloudflare AI Gateway keeps
# up to 10M rows, readable by any AI-Gateway-Read token).
_AI_HOSTS = ("generativelanguage.googleapis.com", "gateway.ai.cloudflare.com",
             "api.anthropic.com", "api.openai.com", "api.x.ai",
             "api.mistral.ai", "api.cohere.ai", "api.groq.com")

# Everything else was a RATCHET while the api.eia.gov sites were converted.
# All 15 are DONE (2026-09-06) — 12 found by the f-string detector plus 3
# the urlencode detector found once it existed. Each sends X-Api-Key now,
# verified live against api.eia.gov:
#   no key -> API_KEY_MISSING ; bogus header -> API_KEY_INVALID
# so the list is empty and the rule is absolute everywhere. Re-populating this
# is a deliberate act that has to be argued for in a PR, which is the point.
_KNOWN_DEBT: dict[tuple[str, str], int] = {}


def test_the_scan_actually_reads_the_repo():
    """Floors, so a broken glob cannot make the rules below vacuously green."""
    _, files, fstrings, _, _, _ = scan_repo()
    assert files >= _MIN_FILES, (
        f"scanned only {files} python files (floor {_MIN_FILES}) — this guard "
        "is not reading the repo any more, so its green means nothing")
    assert fstrings >= _MIN_FSTRINGS, (
        f"parsed only {fstrings} f-strings (floor {_MIN_FSTRINGS}) — the walk "
        "is not reaching the code it claims to check")


def test_no_ai_provider_key_is_ever_in_a_url():
    """ABSOLUTE. This is the surface the leak was observed on."""
    hits, _, _, _, _, _ = scan_repo()
    bad = [h for h in hits if _hit_is_ai_host(h)]
    assert not bad, (
        "AI provider credential in a URL query string — it will be written "
        "verbatim into the AI Gateway log:\n" + "\n".join(
            f"  {f}:{ln}  ?{p}=<interpolated>" for f, ln, p in bad))


def test_no_new_credential_in_url_outside_the_known_debt():
    """RATCHET. A new site fails — including a new one in a file already on
    the list, which a name-only allowlist would have waved through."""
    hits, _, _, _, _, _ = scan_repo()
    seen = collections.Counter((f, p) for f, _ln, p in hits)
    extra = []
    for key, n in seen.items():
        allowed = _KNOWN_DEBT.get(key, 0)
        if n > allowed:
            extra.append(f"  {key[0]}  ?{key[1]}=  ({n} occurrences, "
                         f"{allowed} recorded)")
    assert not extra, (
        "NEW credential-in-URL site (put the key in a header instead):\n"
        + "\n".join(extra))


def test_the_known_debt_list_does_not_rot():
    """If a listed site is fixed, its entry must be updated or removed —
    otherwise the list slowly stops describing anything and the ratchet
    loosens for free."""
    hits, _, _, _, _, _ = scan_repo()
    seen = collections.Counter((f, p) for f, _ln, p in hits)
    stale = [f"  {k[0]}  ?{k[1]}=  (recorded {n}, now {seen.get(k, 0)})"
             for k, n in _KNOWN_DEBT.items() if seen.get(k, 0) < n]
    assert not stale, (
        "recorded as known debt but no longer present that many times — "
        "tighten _KNOWN_DEBT:\n" + "\n".join(stale))


_BAD_ENC = (
    "from urllib.parse import urlencode\n"
    "params = {'api_key': EIA_API_KEY, 'frequency': 'monthly'}\n"
    "url = base + '?' + urlencode(params)\n")
_ENC_LITERAL_OK = (
    "from urllib.parse import urlencode\n"
    "params = {'api_key': 'test-fixture-key'}\n"
    "url = base + '?' + urlencode(params)\n")
_ENC_NO_URLENCODE_OK = "payload = {'api_key': EIA_API_KEY}\nrequests.post(u, json=payload)\n"


def test_the_urlencode_detector_sees_the_shape_that_hid_three_sites():
    hits = scan_source_urlencode(_BAD_ENC, "<synthetic>")
    assert len(hits) == 1 and hits[0][2] == "api_key", hits


def test_the_urlencode_detector_ignores_fixtures_and_json_bodies():
    assert scan_source_urlencode(_ENC_LITERAL_OK, "<synthetic>") == []
    assert scan_source_urlencode(_ENC_NO_URLENCODE_OK, "<synthetic>") == []


# ── a key written into the dict after it was built ──────────────────────────
_BAD_ENC_WRITTEN = (
    "def _request(path, params):\n"
    "    params = dict(params)\n"
    "    params['api_key'] = _api_key()\n"
    "    qs = urllib.parse.urlencode(params)\n"
    "    return urllib.request.urlopen(API_BASE + path + '?' + qs)\n")
_BAD_ENC_WRITTEN_HOST = (
    "def link(key):\n"
    "    params = {'source': 'cta'}\n"
    "    if key:\n"
    "        params['key'] = key\n"
    "    return f'https://api.example.com/v1/opt-in?{urlencode(params)}'\n")
_ENC_WRITTEN_OURS_OK = _BAD_ENC_WRITTEN_HOST.replace("api.example.com", "dchub.cloud")
_ENC_WRITTEN_FIXTURE_OK = _BAD_ENC_WRITTEN.replace("_api_key()", "'literal-fixture'")
_ENC_WRITTEN_OURS_ASSIGNED_OK = (
    "def cta(key):\n"
    "    params = {'source': 'cta'}\n"
    "    if key:\n"
    "        params['key'] = key\n"
    "    link = f'https://dchub.cloud/opt-in?{urlencode(params)}'\n"
    "    return 'Power user? Confirm here: ' + link\n")
_ENC_WRITTEN_OTHER_SCOPE_OK = (
    "def token(key):\n"
    "    params = {}\n"
    "    params['api_key'] = key\n"
    "    return requests.post(TOKEN_URL, data=urlencode(params))\n"
    "def search(term):\n"
    "    params = {'q': term}\n"
    "    return urlopen(BASE + '?' + urlencode(params))\n")


def test_the_urlencode_detector_follows_a_key_written_after_the_literal():
    assert [h[1:] for h in scan_source_urlencode(_BAD_ENC_WRITTEN, "<s>")] == [(3, "api_key")]
    assert [h[1:] for h in scan_source_urlencode(_BAD_ENC_WRITTEN_HOST, "<s>")] == [(4, "key")]


def test_the_urlencode_detector_skips_a_written_key_in_our_link_a_fixture_or_another_scope():
    assert _ENC_WRITTEN_OURS_OK != _BAD_ENC_WRITTEN_HOST
    assert _ENC_WRITTEN_FIXTURE_OK != _BAD_ENC_WRITTEN
    assert scan_source_urlencode(_ENC_WRITTEN_OURS_OK, "<s>") == []
    assert scan_source_urlencode(_ENC_WRITTEN_OURS_ASSIGNED_OK, "<s>") == []
    moved = _ENC_WRITTEN_OURS_ASSIGNED_OK.replace("dchub.cloud", "api.example.com")
    assert [h[1:] for h in scan_source_urlencode(moved, "<s>")] == [(4, "key")]
    assert scan_source_urlencode(_ENC_WRITTEN_FIXTURE_OK, "<s>") == []
    assert scan_source_urlencode(_ENC_WRITTEN_OTHER_SCOPE_OK, "<s>") == []


def _function_source(rel, name):
    src = (_ROOT / rel).read_text(encoding="utf-8")
    fns = [n for n in ast.walk(ast.parse(src))
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    assert len(fns) == 1, (rel, name, len(fns))
    return ast.get_source_segment(src, fns[0]), fns[0]


_OPTIN_LINK = "https://dchub.cloud/api/v1/opt-in/request?"


def test_our_own_opt_in_link_carries_no_key_and_the_detector_still_sees_it():
    """_optin_cta_block used to write the caller's key into this link on our
    own host, the by-design site this control was built on. Since 2026-09-24
    it writes none (nothing read it). Control, on the real function: it reads
    clean, and the same function with the key write restored AND the link
    moved to another host is reported, so the clean read is not blindness."""
    seg, fn = _function_source("mcp_gatekeeper.py", "_optin_cta_block")
    written = _secret_subscript_writes(_scope_nodes(fn))
    assert [k for w in written.values() for _ln, k in w] == [], written
    assert scan_source_urlencode(seg, "mcp_gatekeeper.py") == []
    assert seg.count(_OPTIN_LINK) == 1
    anchor = '    params = {"source": "paywall_optin_cta", "tool": tool_name}\n'
    assert seg.count(anchor) == 1, "anchor moved; update the control"
    moved = (seg.replace(anchor, anchor + '    params["key"] = api_key\n')
             .replace(_OPTIN_LINK, "https://api.example.com/v1/opt-in/request?"))
    assert [h[2] for h in scan_source_urlencode(moved, "<moved>")] == ["key"]


def test_no_credential_reaches_a_query_string_via_urlencode():
    _, _, _, enc, _, _ = scan_repo()
    assert not enc, "credential urlencoded into a query string:\n" + "\n".join(
        f"  {f}:{ln}  {param}" for f, ln, param in enc)


_BAD_PARAMS = ("r = requests.get(url, params={'api_key': EIA_API_KEY}, timeout=5)\n")
_BAD_PARAMS_VAR = ("p = {'api_key': KEY, 'x': 1}\n"
                   "r = requests.get(url, params=p, timeout=5)\n")
_BAD_PARAMS_IFEXP = ("p = {'api_key': KEY, 'length': 1} if KEY else {'length': 1}\n"
                     "r = session.get(url, params=p, timeout=5)\n")
_BAD_PARAMS_IFEXP_INLINE = (
    "r = requests.get(url, params={'api_key': KEY} if KEY else {}, timeout=5)\n")
_PARAMS_OK = ("r = requests.get(url, params={'state': st}, "
              "headers={'X-Api-Key': KEY}, timeout=5)\n")
_PARAMS_FIXTURE_OK = "r = requests.get(url, params={'api_key': 'literal-fixture'})\n"
_SCOPE_COLLISION = (
    "def a(KEY):\n"
    "    params = {'api_key': KEY}\n"
    "    return requests.get(u, params=params, headers={})\n"
    "def b(lat):\n"
    "    params = {'latitude': lat}\n"
    "    return requests.get(u2, params=params, headers={'Authorization': 'Bearer x'})\n")


def test_the_params_detector_sees_both_inline_and_variable_dicts():
    assert len(scan_source_params(_BAD_PARAMS, "<s>")) == 1
    assert len(scan_source_params(_BAD_PARAMS_VAR, "<s>")) == 1


def test_the_params_detector_sees_a_dict_picked_by_a_conditional():
    assert len(scan_source_params(_BAD_PARAMS_IFEXP, "<s>")) == 1
    assert len(scan_source_params(_BAD_PARAMS_IFEXP_INLINE, "<s>")) == 1


def test_the_params_detector_ignores_headers_and_fixtures():
    assert scan_source_params(_PARAMS_OK, "<s>") == []
    assert scan_source_params(_PARAMS_FIXTURE_OK, "<s>") == []


def test_a_dict_in_another_function_does_not_match():
    """★ The scope bug, pinned. Both functions call their local `params`; only
    the one that actually carries a credential may be reported. An ast.walk
    from the module makes this report TWO."""
    hits = scan_source_params(_SCOPE_COLLISION, "<s>")
    assert len(hits) == 1, hits


_BAD_PARAMS_WRITTEN = (
    "def demand(rto):\n"
    "    key = os.environ.get('EIA_API_KEY', '')\n"
    "    params = {'frequency': 'hourly', 'length': 48}\n"
    "    if key: params['api_key'] = key\n"
    "    return _rq.get('https://api.eia.gov/v2/x', params=params, headers=H)\n")
_BAD_PARAMS_WRITTEN_ARG = (
    "def make_request(endpoint, params=None):\n"
    "    if params is None:\n"
    "        params = {}\n"
    "    params['api_key'] = EIA_API_KEY\n"
    "    return requests.get(BASE + endpoint, params=params, timeout=30)\n")
_PARAMS_WRITTEN_OK = (
    "def demand(rto):\n"
    "    key = os.environ.get('EIA_API_KEY', '')\n"
    "    params = {'frequency': 'hourly'}\n"
    "    params['length'] = 48\n"
    "    h = {**H, 'X-Api-Key': key} if key else H\n"
    "    return _rq.get('https://api.eia.gov/v2/x', params=params, headers=h)\n")
_PARAMS_WRITTEN_FIXTURE_OK = (
    "def demand(rto):\n"
    "    params = {'frequency': 'hourly'}\n"
    "    params['api_key'] = 'literal-fixture'\n"
    "    return requests.get(u, params=params)\n")
_PARAMS_WRITTEN_OTHER_SCOPE_OK = (
    "def a(KEY):\n"
    "    params = {}\n"
    "    params['api_key'] = KEY\n"
    "    return requests.post(u, json=params)\n"
    "def b(lat):\n"
    "    params = {'latitude': lat}\n"
    "    return requests.get(u2, params=params)\n")


def test_the_params_detector_follows_a_key_written_after_the_literal():
    """Both read clean from the literal alone: a key added under a condition,
    and a key added to a dict that arrived as an argument."""
    assert [h[1:] for h in scan_source_params(_BAD_PARAMS_WRITTEN, "<s>")] == [(5, "api_key")]
    assert [h[1:] for h in scan_source_params(_BAD_PARAMS_WRITTEN_ARG, "<s>")] == [(5, "api_key")]


def test_the_params_detector_skips_a_header_a_plain_field_a_fixture_or_another_scope():
    assert scan_source_params(_PARAMS_WRITTEN_OK, "<s>") == []
    assert scan_source_params(_PARAMS_WRITTEN_FIXTURE_OK, "<s>") == []
    assert scan_source_params(_PARAMS_WRITTEN_OTHER_SCOPE_OK, "<s>") == []


_BAD_PARAMS_CALL = "r = requests.get(url, params={'api_key': _api_key()}, timeout=5)\n"
_BAD_ENC_CALL = (
    "from urllib.parse import urlencode\n"
    "params = {'api_key': os.environ.get('EIA_API_KEY'), 'frequency': 'monthly'}\n"
    "url = base + '?' + urlencode(params)\n")


def test_a_key_computed_by_a_call_is_a_credential_not_a_fixture():
    """Only a literal is a fixture: a key read through a call counts in a dict
    literal exactly as it does when written in afterwards."""
    assert [h[2] for h in scan_source_params(_BAD_PARAMS_CALL, "<s>")] == ["api_key"]
    assert [h[2] for h in scan_source_urlencode(_BAD_ENC_CALL, "<s>")] == ["api_key"]


def test_no_credential_reaches_a_query_string_via_requests_params():
    _, _, _, _, prm, _ = scan_repo()
    unexpected = [h for h in prm if (h[0], h[2]) not in _NO_HEADER_AUTH]
    assert not unexpected, (
        "credential handed to requests(params=...), which makes it a query "
        "string:\n" + "\n".join(f"  {f}:{ln}  params[{p}]"
                                 for f, ln, p in unexpected))


# ── shape 4 controls: one per shape, each must be SEEN ───────────────────────
_HL_CONSTANT = (
    "import os\n"
    "EIA_V2_BASE = 'https://api.eia.gov/v2/electricity/rto'\n"
    "def url(dataset, respondent):\n"
    "    key = os.environ.get('EIA_API_KEY', '')\n"
    "    return f'{EIA_V2_BASE}/{dataset}/data/?api_key={key}"
    "&facets[respondent][]={respondent}'\n")
_HL_LOCAL = (
    "class Discovery:\n"
    "    def catalog(self):\n"
    "        base_url = 'https://api.eia.gov/v2/'\n"
    "        return self.session.get(f'{base_url}?api_key={self.eia_api_key}')\n")
_HL_CONCAT = (
    "def call(config, api_key):\n"
    "    return config['url'] + f'?key={api_key}'\n")
_HL_ASSIGNED = (
    "import os\n"
    "KEY = os.environ.get('EIA_API_KEY')\n"
    "BASE = 'https://api.eia.gov/v2/electricity/operating-generator-capacity/data/'\n"
    "def page(offset):\n"
    "    params = (f'?api_key={KEY}' f'&offset={offset}')\n"
    "    url = BASE + params\n"
    "    return url\n")
_HL_PERCENT = (
    "HH = 'https://api.eia.gov/v2/natural-gas/pri/fut/data/?frequency=daily'\n"
    "def hh(key):\n"
    "    return '%s&api_key=%s' % (HH, key)\n")
_HL_FORMAT = (
    "BASE = 'https://generativelanguage.googleapis.com/v1beta/models/x:generateContent'\n"
    "def g(key):\n"
    "    return '{}?key={}'.format(BASE, key)\n")
_HL_OURS = (
    "import os\n"
    "SITE = os.environ.get('DCHUB_SITE', 'https://dchub.cloud')\n"
    "_PUBLIC = (os.environ.get('DCHUB_PUBLIC_BASE_URL') or 'https://dchub.cloud').rstrip('/')\n"
    "def links(e, tok, sig, base='https://dchub.cloud'):\n"
    "    return [f'{SITE}/api/v1/opt-in/confirm?email={e}&token={tok}',\n"
    "            f'{_PUBLIC}/api/v1/admin/feedback/1/approve?token={tok}',\n"
    "            f'{base}/api/v1/site-report?lat=1&sig={sig}',\n"
    "            f'/api/v1/listings/x/leads?token={tok}']\n")
_HL_PARAM = "def link(base, tok):\n    return f'{base}/x?token={tok}'\n"
_HL_DISAGREE = (
    "def link(prod, tok):\n"
    "    base = 'https://dchub.cloud'\n"
    "    if prod:\n"
    "        base = 'https://api.example.com'\n"
    "    return f'{base}/x?token={tok}'\n")
_HL_FIXTURE = (
    "SECRET = 'not-a-real-key'\n"
    "def get(url):\n"
    "    raise OSError(f'502 for url: {url}?api_key={SECRET}&lat=1')\n")
_HL_ENV = (
    "import os\n"
    "KEY = os.environ.get('EIA_API_KEY', 'placeholder')\n"
    "def get(url):\n"
    "    return f'{url}?api_key={KEY}'\n")


def _placed(src):
    return [(h[2], h[4]) for h in scan_source_hostless(src, "<s>")]


def test_hostless_a_base_in_a_module_constant_is_followed():
    assert _placed(_HL_CONSTANT) == [("api_key", "external")]


def test_hostless_a_base_in_a_local_variable_is_followed():
    assert _placed(_HL_LOCAL) == [("api_key", "external")]


def test_hostless_a_query_appended_to_an_unreadable_base_is_reported():
    """config["url"] + f"?key=..." — nothing here says where it goes, and that
    must fail rather than pass."""
    assert _placed(_HL_CONCAT) == [("key", "unresolved")]


def test_hostless_a_query_built_first_and_joined_later_is_followed():
    assert _placed(_HL_ASSIGNED) == [("api_key", "external")]


def test_hostless_percent_formatting_is_read():
    assert _placed(_HL_PERCENT) == [("api_key", "external")]


def test_hostless_str_format_is_read():
    assert _placed(_HL_FORMAT) == [("key", "external")]


def test_hostless_our_own_links_are_found_and_not_reported():
    """A negative control with teeth: four candidates are FOUND and placed as
    ours or relative, so an empty result cannot pass for a clean one."""
    assert sorted(pl for _p, pl in _placed(_HL_OURS)) == \
        ["ours", "ours", "ours", "relative"]


def test_hostless_a_base_that_cannot_be_followed_is_not_assumed_ours():
    assert _placed(_HL_PARAM) == [("token", "unresolved")]
    assert _placed(_HL_DISAGREE) == [("token", "unresolved")]


def test_hostless_a_literal_secret_is_a_fixture_an_environment_read_is_not():
    assert _placed(_HL_FIXTURE) == [("api_key", "fixture")]
    assert _placed(_HL_ENV) == [("api_key", "unresolved")]


# Host-less sites whose base cannot be followed and that are correct as they
# stand, keyed by (file, function, param) -> count. A decision with evidence
# per entry, like _NO_HEADER_AUTH above.
_HOSTLESS_BY_DESIGN = {
    # The /me diagnostics report which channel the caller's own key arrived
    # on, as masked text ("?api_key=" + _mask(key)) in the JSON response. It
    # is text for a reader, not a URL anything requests.
    ("dchub_me.py", "_observed_header", "api_key"): 1,
}

# Floors for the reading itself, at ~80% of the counts MEASURED 2026-09-13
# once the host-less provider sites sent their keys as headers: 17 candidates,
# 9 of them resolved to our own host. A reading that stopped finding
# candidates, or stopped following our own base constants, trips these.
_MIN_HOSTLESS = 13
_MIN_HOSTLESS_OURS = 7


def test_the_hostless_reading_actually_reads_the_repo():
    _, _, _, _, _, hostless = scan_repo()
    ours = [h for h in hostless if h[4] == "ours"]
    assert len(hostless) >= _MIN_HOSTLESS, (
        f"the host-less reading found {len(hostless)} candidates (floor "
        f"{_MIN_HOSTLESS}) — it has stopped seeing the links it exists to judge")
    assert len(ours) >= _MIN_HOSTLESS_OURS, (
        f"only {len(ours)} links resolved to our own host (floor "
        f"{_MIN_HOSTLESS_OURS}) — the reading no longer follows the constants "
        "those links are built from")


def test_no_credential_reaches_a_url_whose_host_is_elsewhere():
    """ABSOLUTE, like the rules above: a URL whose base resolves to an
    external host — or cannot be followed at all — must not carry a key."""
    _, _, _, _, _, hostless = scan_repo()
    reported = [h for h in hostless if h[4] in _REPORTED]
    seen = collections.Counter((f, fn, p) for f, _ln, p, fn, _pl in reported)
    bad = [h for h in reported if seen[(h[0], h[3], h[2])]
           > _HOSTLESS_BY_DESIGN.get((h[0], h[3], h[2]), 0)]
    assert not bad, (
        "credential in the query string of a URL built from a base held "
        "elsewhere — send it in a header instead. If the host is ours, build "
        "the link from a module constant this reading can follow; a base it "
        "cannot follow is reported, not assumed to be ours:\n" + "\n".join(
            f"  {f}:{ln}  {fn}  ?{p}=  ({pl})" for f, ln, p, fn, pl in bad))


def test_the_by_design_hostless_list_does_not_rot():
    _, _, _, _, _, hostless = scan_repo()
    seen = collections.Counter((f, fn, p) for f, _ln, p, fn, pl in hostless
                               if pl in _REPORTED)
    stale = [f"  {k[0]}  {k[1]}  ?{k[2]}=  (recorded {n}, now {seen.get(k, 0)})"
             for k, n in _HOSTLESS_BY_DESIGN.items() if seen.get(k, 0) < n]
    assert not stale, (
        "recorded as by design but no longer present that many times — "
        "tighten _HOSTLESS_BY_DESIGN:\n" + "\n".join(stale))
