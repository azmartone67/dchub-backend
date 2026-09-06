"""Find positional row access inside a RealDictCursor scope.

★ 2026-09-05 — THIS SCANNER'S FIRST VERSION WAS WRONG IN BOTH DIRECTIONS, and
that is worth recording because the output LOOKED authoritative:

    18 sites flagged  ->  ALL 18 were false positives (a second, PLAIN cursor
                          in the same function; positional access correct)
     7 real bugs      ->  ZERO of them flagged

The blind spot was shape, not scope: it only matched subscripts on a BOUND NAME
(`for r in cur.fetchall()`), and every real defect used the commoner idiom —
`cur.fetchone()[0]`, or `(cur.fetchone() or [None])[0]` where a non-empty dict
is truthy so the fallback never substitutes. Both are now matched.

★ IT IS STILL NOT AUTHORITATIVE, and must not be treated as one. Two of the
seven real bugs open their cursor OUTSIDE the flagged function (ai_wars._init_
tables) or receive it as a PARAMETER (outcome_verifier._latest_obs), which
function-scoped analysis cannot follow. Use this to LOCATE candidates; a human
or an agent must then trace each row back to the cursor that produced it. A hit
is a question, not a verdict.
"""
_ORIGINAL_DOC = """Find positional row access inside a RealDictCursor scope.

The bug: a RealDictRow is a dict subclass, so row[0] is KeyError(0), not the
first column. Detection is scoped to the FUNCTION that opens the cursor, and
only flags subscripts on names that demonstrably hold a row.
"""
import ast, glob, os, sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else "."

def opens_realdict(fn):
    for n in ast.walk(fn):
        if isinstance(n, ast.keyword) and n.arg == "cursor_factory":
            if "RealDict" in ast.dump(n.value):
                return True
    return False

def row_names(fn):
    """Names bound to a row: `for r in cur.fetchall()` / `x = cur.fetchone()`."""
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.For) and isinstance(n.iter, ast.Call):
            f = n.iter.func
            if isinstance(f, ast.Attribute) and f.attr in ("fetchall", "fetchmany"):
                if isinstance(n.target, ast.Name):
                    out.add(n.target.id)
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for gen in n.generators:
                if isinstance(gen.iter, ast.Call):
                    f = gen.iter.func
                    if isinstance(f, ast.Attribute) and f.attr in ("fetchall", "fetchmany"):
                        if isinstance(gen.target, ast.Name):
                            out.add(gen.target.id)
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
            f = n.value.func
            if isinstance(f, ast.Attribute) and f.attr == "fetchone":
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        out.add(t.id)
    return out

hits = []
for path in sorted(glob.glob(os.path.join(ROOT, "routes", "*.py")) +
                   glob.glob(os.path.join(ROOT, "*.py")) +
                   glob.glob(os.path.join(ROOT, "util", "*.py"))):
    try:
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
    except Exception:
        continue
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not opens_realdict(fn):
            continue
        names = row_names(fn)
        if not names:
            continue
        for n in ast.walk(fn):
            if not isinstance(n, ast.Subscript):
                continue
            idx = n.slice
            if not (isinstance(idx, ast.Constant) and isinstance(idx.value, int)):
                continue
            v = n.value
            # (a) subscript on a bound row name
            if isinstance(v, ast.Name) and v.id in names:
                hits.append((os.path.relpath(path, ROOT), n.lineno, fn.name,
                             f"{v.id}[{idx.value}]")); continue
            # (b) ★ THE SHAPE THE FIRST SCAN MISSED: subscript directly on the
            #     CALL -- cur.fetchone()[0] -- by far the commonest idiom.
            if isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) \
               and v.func.attr in ("fetchone", "fetchall", "fetchmany"):
                hits.append((os.path.relpath(path, ROOT), n.lineno, fn.name,
                             f"cur.{v.func.attr}()[{idx.value}]")); continue
            # (c) the `or [default]` idiom wrapping a fetch:
            #     (cur.fetchone() or [None])[0] -- a non-empty dict is TRUTHY,
            #     so the default never substitutes and [0] is a key lookup.
            if isinstance(v, ast.BoolOp) and isinstance(v.op, ast.Or):
                for val in v.values:
                    if isinstance(val, ast.Call) and isinstance(val.func, ast.Attribute) \
                       and val.func.attr in ("fetchone", "fetchall"):
                        hits.append((os.path.relpath(path, ROOT), n.lineno, fn.name,
                                     f"(cur.{val.func.attr}() or ...)[{idx.value}]")); break

print(f"functions scanned in files under routes/, util/, root")
print(f"POSITIONAL ACCESS ON A REALDICT ROW: {len(hits)} site(s)\n")
for f, ln, fn, expr in hits:
    print(f"  {f}:{ln}  {fn}()  ->  {expr}")

