#!/usr/bin/env python3
"""A parser/evaluator for the subset of Cloudflare's expression language that
the dchub.cloud cache ruleset actually uses.

Why a parser and not a regex
----------------------------
The question this answers is "which rule wins for an ANONYMOUS request to path
P". Cache Rules are LAST-MATCH-WINS, so answering it needs real evaluation of
every rule, not a substring search for a prefix. A regex that looks for
`"/api/v1/foo"` inside the expressions cannot tell you that a LATER rule
re-cached the path, which is the exact mistake that let a keyed 1MB CSV be
served to an anonymous caller on 2026-09-06.

The grammar is small and closed. Measured over all 25 live rules, the only
operators in use are: starts_with, eq, contains, ends_with, lower, in, any,
wildcard; and the only fields are http.request.uri.path, http.cookie,
http.request.headers and http.request.uri.args.

★ THE ANONYMOUS MODEL. Evaluation is deliberately from one viewpoint: a request
carrying a path and NOTHING else. `http.cookie`, `http.request.headers[...]`
and `http.request.uri.args[...]` all evaluate empty, so a credential-keyed rule
correctly does NOT fire. That is the whole point — rule 24 protects callers who
send `x-api-key`, and this module exists to ask what happens to everyone else.

★ PARSE FAILURE IS NOT "NO MATCH". `ParseError` propagates so callers report
`unknown` rather than silently treating an unreadable rule as inapplicable.
Treating "I could not read this rule" as "this rule does not apply" would
manufacture coverage that does not exist.
"""
from __future__ import annotations

import re

__all__ = ["ParseError", "parse", "evaluate", "disposition"]


class ParseError(Exception):
    """The expression could not be parsed or evaluated. NEVER means 'no match'."""


_TOKEN = re.compile(
    r"""\s*(?:
        (?P<lparen>\()|(?P<rparen>\))|(?P<lbrace>\{)|(?P<rbrace>\})|(?P<comma>,)
      | (?P<rstr>r"(?:[^"\\]|\\.)*")
      | (?P<str>"(?:[^"\\]|\\.)*")
      | (?P<op>!=|==|>=|<=)
      | (?P<idx>\[\s*\*\s*\]|\[\s*"(?:[^"\\]|\\.)*"\s*\])
      | (?P<word>[A-Za-z_][A-Za-z0-9_.]*)
    )""",
    re.X,
)

_FUNCS = {"starts_with", "ends_with", "lower", "any", "concat"}
_INFIX = {"contains", "eq", "ne", "in", "wildcard", "matches"}
_PATH_FIELD = "http.request.uri.path"


def _lex(src: str) -> list[tuple[str, str]]:
    tokens, i = [], 0
    while i < len(src):
        m = _TOKEN.match(src, i)
        if not m:
            if src[i].isspace():
                i += 1
                continue
            raise ParseError(f"cannot tokenize at offset {i}: {src[i:i + 30]!r}")
        i = m.end()
        for kind, value in m.groupdict().items():
            if value is not None:
                tokens.append((kind, value))
                break
    return tokens


def _unquote(value: str) -> str:
    if value.startswith('r"'):
        value = value[1:]
    return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")


class _Parser:
    def __init__(self, tokens):
        self.tokens, self.i = tokens, 0

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else (None, None)

    def next(self):
        token = self.peek()
        self.i += 1
        return token

    def expect(self, kind):
        got_kind, got_value = self.next()
        if got_kind != kind:
            raise ParseError(f"expected {kind}, got {got_kind}:{got_value!r}")
        return got_value

    def or_expr(self):
        nodes = [self.and_expr()]
        while self.peek() == ("word", "or"):
            self.next()
            nodes.append(self.and_expr())
        return ("or", nodes) if len(nodes) > 1 else nodes[0]

    def and_expr(self):
        nodes = [self.unary()]
        while self.peek() == ("word", "and"):
            self.next()
            nodes.append(self.unary())
        return ("and", nodes) if len(nodes) > 1 else nodes[0]

    def unary(self):
        kind, value = self.peek()
        if (kind, value) == ("word", "not"):
            self.next()
            return ("not", self.unary())
        if kind == "lparen":
            self.next()
            node = self.or_expr()
            self.expect("rparen")
            return node
        return self.predicate()

    def predicate(self):
        kind, value = self.next()
        if kind != "word":
            raise ParseError(f"expected a field or function, got {kind}:{value!r}")

        if self.peek()[0] == "lparen" and value in _FUNCS:
            self.next()
            args = [self.or_expr() if value == "any" else self.operand()]
            while self.peek()[0] == "comma":
                self.next()
                args.append(self.operand())
            self.expect("rparen")
            node = (value, args)
        else:
            node = ("field", value)

        while self.peek()[0] == "idx":
            self.next()
            node = ("index", node)

        kind, value = self.peek()
        if kind == "word" and value in _INFIX:
            self.next()
            if value == "in":
                self.expect("lbrace")
                items = []
                while self.peek()[0] in ("str", "rstr"):
                    items.append(_unquote(self.next()[1]))
                self.expect("rbrace")
                return ("in", node, items)
            literal_kind, literal = self.next()
            if literal_kind not in ("str", "rstr"):
                raise ParseError(f"{value} needs a string literal, got {literal_kind}")
            return (value, node, _unquote(literal))
        if kind == "op":
            self.next()
            self.next()
            return ("cmp", node)
        return node

    def operand(self):
        kind, value = self.peek()
        if kind in ("str", "rstr"):
            self.next()
            return ("lit", _unquote(value))
        return self.predicate()


def parse(expression: str):
    parser = _Parser(_lex(expression))
    node = parser.or_expr()
    if parser.peek()[0] is not None:
        raise ParseError(f"trailing tokens from offset {parser.i}")
    return node


def evaluate(node, path: str):
    """Evaluate one parsed expression for an ANONYMOUS request to `path`."""
    kind = node[0]
    if kind == "or":
        return any(evaluate(child, path) for child in node[1])
    if kind == "and":
        return all(evaluate(child, path) for child in node[1])
    if kind == "not":
        return not evaluate(node[1], path)
    if kind == "field":
        # Every field other than the path is ABSENT for an anonymous caller.
        return path if node[1] == _PATH_FIELD else ""
    if kind == "lit":
        return node[1]
    if kind == "index":
        return ""          # headers["x-api-key"][*] / args["api_key"][*]
    if kind == "any":
        return False       # any(<absent array> != "")
    if kind == "cmp":
        return False
    if kind == "lower":
        return str(evaluate(node[1][0], path)).lower()
    if kind == "starts_with":
        return str(evaluate(node[1][0], path)).startswith(str(evaluate(node[1][1], path)))
    if kind == "ends_with":
        return str(evaluate(node[1][0], path)).endswith(str(evaluate(node[1][1], path)))
    if kind == "contains":
        return node[2] in str(evaluate(node[1], path))
    if kind == "eq":
        return str(evaluate(node[1], path)) == node[2]
    if kind == "ne":
        return str(evaluate(node[1], path)) != node[2]
    if kind == "in":
        return str(evaluate(node[1], path)) in node[2]
    if kind == "wildcard":
        pattern = "^" + ".*".join(re.escape(part) for part in node[2].split("*")) + "$"
        return re.match(pattern, str(evaluate(node[1], path))) is not None
    raise ParseError(f"cannot evaluate node kind {kind!r}")


def disposition(rules: list[dict], path: str) -> tuple[str, dict | None, str | None]:
    """What the edge does with an ANONYMOUS GET of `path`.

    Returns (verdict, winning_rule, error). verdict is one of:
      bypass   — a cache:false rule wins       (safe for tier-varying content)
      cached   — a cache:true rule wins        (anon response cached, URL-keyed)
      no-rule  — nothing matches               (Cloudflare defaults apply)
      unknown  — an expression would not parse (NEVER treated as 'no match')

    ★ LAST MATCH WINS. The loop deliberately does not break on the first hit.
    """
    winner = None
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        try:
            if evaluate(parse(rule["expression"]), path):
                winner = rule
        except ParseError as exc:
            return "unknown", rule, str(exc)
    if winner is None:
        return "no-rule", None, None
    cache = (winner.get("action_parameters") or {}).get("cache")
    if cache is False:
        return "bypass", winner, None
    if cache is True:
        return "cached", winner, None
    return "unknown", winner, "rule has no explicit cache setting"
