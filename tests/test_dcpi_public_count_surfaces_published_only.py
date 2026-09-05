"""r-one-dcpi-universe — the public surfaces that count, list or roll up
market_power_scores must read the PUBLISHED universe.

/dcpi and /api/v1/dcpi/scores serve rows with `published = true`. Four other
PUBLIC surfaces queried the same table with no such predicate, so each of them
counted the alias-twin rows r-twin-unpublish retires — rows deliberately kept so
direct links still resolve, because the flag is a VISIBILITY bit, not a delete:

    /api/v1/agent/index?domain=dcpi   coverage.dcpi_scored_markets
    /api/v1/alive, /alive             dcpi.markets_scored
    /api/v1/reports/monthly.json      markets_scored + the verdict split
    /api/v1/open-data/isos.csv        the whole per-ISO rollup
    /api/v1/open-data/dcpi-markets.csv  shipped the retired rows themselves

`util.dcpi_score_row.PUBLISHED_ONLY` is now the single spelling of that
predicate, and it lives beside MAY_PUBLISH — what earns the flag next to who
believes it.

These tests read the shipped source rather than importing main.py, per the house
rule. The invariant is deliberately EXCEPTION-FREE: every SELECT over the table
in a guarded module goes through the shared name. An allow-list is how the two
that were already right (`published = TRUE`, hand-written) would have taught the
next author that hand-writing it is fine.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PREDICATE_NAME = "PUBLISHED_ONLY"
PREDICATE_HOME = ROOT / "util" / "dcpi_score_row.py"
TABLE = "market_power_scores"

#: Public surfaces whose market counts/lists must match /dcpi. Add a module here
#: the moment it starts publishing a market tally.
GUARDED = (
    "routes/agent_index.py",
    "routes/alive.py",
    "routes/comprehensive_report.py",
    "routes/open_data_csv.py",
)


def _sql_texts(path):
    """(lineno, SQL text) for every SELECT over the scores table.

    ★ The SQL is reconstructed from the string node itself, with each f-string
    slot rendered as the SOURCE of the expression it interpolates — so
    `f"... AND {PUBLISHED_ONLY}"` yields the literal text `AND PUBLISHED_ONLY`.

    The first cut of this helper returned the unparsed ENCLOSING STATEMENT
    instead. That reads fine for the name check, but it silently DISARMED the
    paren-depth check below: unparsing wraps the SQL back inside its own call,
    so every character of the query sits at paren depth >= 1 and a bare
    top-level OR could never be seen. The mutation that strips the parentheses
    survived a full green run. Measure depth inside the SQL, never inside the
    code that carries it.

    Reconstructing rather than slicing raw source also means a COMMENT
    mentioning the table or the predicate can neither trip a check nor satisfy
    one.
    """
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    # ast.walk descends INTO an f-string, so each literal chunk of one is also
    # visited as a bare Constant — i.e. the same query a second time with its
    # interpolated slot missing. Left in, every f-string query reports itself as
    # unpredicated and the check is unusable.
    inside_fstring = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
                      for v in n.values}
    out = []
    for node in ast.walk(tree):
        if id(node) in inside_fstring:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                elif isinstance(v, ast.FormattedValue):
                    parts.append(ast.unparse(v.value))
            text = "".join(parts)
        else:
            continue
        if f"FROM {TABLE}" in " ".join(text.split()):
            out.append((getattr(node, "lineno", 0), text))
    return out


@pytest.mark.parametrize("path", GUARDED)
def test_guarded_module_actually_queries_the_table(path):
    """A module that stopped querying the table proves nothing by passing."""
    found = _sql_texts(path)
    assert found, (
        f"{path} no longer contains a SELECT over {TABLE}. If the query moved, "
        f"move this guard with it; if the surface is gone, drop it from GUARDED.")


@pytest.mark.parametrize("path", GUARDED)
def test_every_public_read_uses_the_shared_predicate(path):
    """The defect: a surface deciding for itself what a scored market is."""
    offenders = [ln for ln, sql in _sql_texts(path)
                 if PREDICATE_NAME not in sql]
    assert not offenders, (
        f"{path} queries {TABLE} without {PREDICATE_NAME} at line(s) "
        f"{offenders}. Every public count, list or rollup must read the same "
        f"published universe /dcpi serves — including a hand-written "
        f"`published = TRUE`, which is a second spelling waiting to drift.")


@pytest.mark.parametrize("path", GUARDED)
def test_an_or_beside_the_predicate_is_parenthesised(path):
    """AND binds tighter than OR — the trap this fix walked into.

    Appending `AND <predicate>` to `WHERE a = %s OR b = %s` filters only the
    second branch, and the query still runs, still returns a plausible number,
    and is wrong. Live proof at the time of writing: the region query for TX
    read 20 markets parenthesised and 21 unparenthesised, the extra row being a
    retired twin.
    """
    bad = []
    for ln, sql in _sql_texts(path):
        if PREDICATE_NAME not in sql:
            continue
        depth = 0
        for i, ch in enumerate(sql):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif sql[i:i + 4] == " OR " and depth == 0:
                bad.append(ln)
                break
    assert not bad, (
        f"{path} line(s) {bad}: an OR sits at paren depth 0 in a query that "
        f"ANDs on {PREDICATE_NAME}. Parenthesise the OR — AND binds tighter, "
        f"so only its right-hand branch is being filtered.")


def test_the_predicate_is_not_coalesced_to_true():
    """Bind the fence to WHY the number is right, not just to the plumbing.

    The column is nullable and its schema DEFAULT is false. Spelling the
    predicate `COALESCE(published, true) = true` inverts the table's own stated
    default and would count a row nobody ever published — and it would no
    longer be the predicate /dcpi and /api/v1/dcpi/scores use, which is the
    only reason these surfaces agree with them.
    """
    src = PREDICATE_HOME.read_text(encoding="utf-8")
    tree = ast.parse(src)
    value = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == PREDICATE_NAME
                        for t in node.targets)
                and isinstance(node.value, ast.Constant)):
            value = node.value.value
    assert value, f"{PREDICATE_NAME} is not a plain string constant in {PREDICATE_HOME.name}"
    normalized = " ".join(value.split()).lower()
    assert "coalesce" not in normalized, (
        f"{PREDICATE_NAME} coalesces the flag: {value!r}. The column defaults "
        f"to false; coalescing NULL to true inverts that and publishes rows "
        f"nobody released.")
    assert normalized.endswith("published = true"), (
        f"{PREDICATE_NAME} is {value!r}, which is no longer the predicate "
        f"/dcpi and /api/v1/dcpi/scores use.")


@pytest.mark.parametrize("path", GUARDED)
def test_sql_built_from_the_table_NAME_is_predicated_too(path):
    """★ The leg that the first version of this fence structurally could not have.

    _sql_texts finds queries that exist as SQL — a string containing
    `FROM market_power_scores`. routes/agent_index.py also builds a count from
    the table NAME at run time:

        for tbl, key, where in [("market_power_scores", "dcpi_scored_markets", ...)]:
            sql = f"SELECT COUNT(*) FROM {tbl}" + (f" WHERE {where}" if where else "")

    No literal ever contains `FROM market_power_scores`, so the AST scan sees
    nothing, greps see nothing, and the surface published the unfiltered count
    to AI agents through the whole of #3835 — which filtered every literal in
    the same file and reported itself complete. Verified live after that
    deploy: four surfaces read 327 and this one still read 334.

    So: wherever a guarded module names the table as DATA, the statement that
    does so must also carry the predicate.
    """
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and node.value == TABLE):
            continue
        stmt = node
        while stmt in parent and not isinstance(stmt, ast.stmt):
            stmt = parent[stmt]
        if PREDICATE_NAME not in ast.unparse(stmt):
            offenders.append(getattr(node, "lineno", 0))
    assert not offenders, (
        f"{path} line(s) {offenders}: the table is named as a STRING — SQL "
        f"built from it at run time is invisible to every check that looks for "
        f"`FROM {TABLE}`. The statement must carry {PREDICATE_NAME}.")
