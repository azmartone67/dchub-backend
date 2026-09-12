"""Every write to mcp_dev_keys.tier is classified, and the email ones are gated.

WHY THIS EXISTS. #4428 fixed "paid tier granted on an unverified email match" in
three statements: _inherit_paid_tier, admin_reconcile_keys, and the checkout
webhook's email match. It missed three more of exactly the same shape:

  * flask_mcp_endpoints.stripe_webhook_mcp — SELECTed the NEWEST active key on
    the buyer's address and lifted it to paid. Because the ordering is
    created_at DESC, a key bound to that address shortly before the customer
    paid took the grant deterministically.
  * stripe_metered._agentic_key_for_email — same SELECT, same newest-wins, and
    its caller writes tier='paid' to whatever it returns.
  * main.reconcile_mcp_tiers — the TWIN of admin_reconcile_keys, a second admin
    endpoint running the same users-to-keys email join. Gating one of a pair is
    not gating the rule.

They were missed because the search was "the statements I already knew about"
rather than "every statement that writes this column". So the guard is the
enumeration itself: find every tier write in the tree, and require each one to
be CLASSIFIED here by a human. A new one fails this test until someone decides
which kind it is — which is the decision that was skipped.

A class is about HOW THE TARGET ROWS ARE CHOSEN, because that is the whole
question. `email_match` means an address picked them, and an address is a
string anyone can type: those must carry the proof clause. `possession` means
the caller held the key itself. `admin` means a human named it. `downgrade`
never grants.

The pinned FLOOR is not decoration: a scan that finds nothing passes every
"all of them are fine" assertion vacuously.
"""
import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# A statement that writes mcp_dev_keys.tier. Both spellings: the full UPDATE and
# a bare `SET tier =` inside one (an ON CONFLICT ... DO UPDATE, say).
_TIER_WRITE = re.compile(
    r"(UPDATE\s+mcp_dev_keys\b(?:(?!;).)*?\bSET\b(?:(?!;).)*?\btier\b)|(\bSET\s+tier\s*=)",
    re.I | re.S)

EMAIL_MATCH = "email_match"      # rows chosen by an address — MUST carry the proof
POSSESSION = "possession"        # rows chosen by the key itself, or its hash
ADMIN = "admin"                  # a human operator named the key
DOWNGRADE = "downgrade"          # never grants a paid tier

# (file, enclosing function) -> the classes of its tier writes, in source order.
# `proof_in` names the function that must contain the clause, when the SELECT
# that chooses the rows lives somewhere other than the write.
REGISTER = {
    ("flask_mcp_endpoints.py", "_inherit_paid_tier"): [
        (EMAIL_MATCH, "the shared rule /keys/claim and /keys/identify both call"),
    ],
    ("flask_mcp_endpoints.py", "admin_reconcile_keys"): [
        (EMAIL_MATCH, "daily sweep, apply=1, joins users to keys on address"),
    ],
    ("flask_mcp_endpoints.py", "stripe_webhook_mcp"): [
        (EMAIL_MATCH, "adopts the newest active key on the buyer's address"),
    ],
    ("main.py", "handle_checkout_completed"): [
        (EMAIL_MATCH, "promotes active keys bound to the paying address"),
        (POSSESSION, "client_reference_id k-<sha256(api_key)>: the caller opened "
                     "the checkout HOLDING that key, so no address is involved"),
    ],
    ("main.py", "handle_subscription_deleted"): [
        (DOWNGRADE, "sets tier='free' when a subscription ends"),
    ],
    ("main.py", "reconcile_mcp_tiers"): [
        (EMAIL_MATCH, "the twin admin sweep; same users-to-keys join"),
    ],
    ("routes/stripe_metered.py", "handle_agentic_commerce_order"): [
        (EMAIL_MATCH, "writes to whatever _agentic_key_for_email resolved, and "
                      "that lookup is by address",
         "_agentic_key_for_email"),
    ],
    ("routes/stripe_metered.py", "link_metered_key"): [
        (ADMIN, "admin-gated; the api_key comes from the operator's own request body"),
    ],
    ("routes/pair_code.py", "redeem_pair_code"): [
        (POSSESSION, "matches left(encode(sha256(api_key),'hex'),32) — the key itself"),
    ],
    ("routes/team_accounts.py", "team_create"): [
        (POSSESSION, "ON CONFLICT on the shared_key this request just minted"),
    ],
    ("gen_dev_key.py", "cmd_upgrade"): [
        (ADMIN, "local CLI, operator passes the api_key"),
    ],
    ("gen_dev_key.py", "cmd_revoke"): [
        (DOWNGRADE, "local CLI revoke"),
    ],
    ("dchub-mcp-v2.1/gen_dev_key.py", "cmd_upgrade"): [
        (ADMIN, "vendored copy of the CLI"),
    ],
    ("dchub-mcp-v2.1/gen_dev_key.py", "cmd_revoke"): [
        (DOWNGRADE, "vendored copy of the CLI"),
    ],
}

# Pinned. Raise it deliberately when a new tier write is added AND classified.
FLOOR = 15


def _scan():
    """Every tier write in the tree, as {(relpath, funcname): [lineno, ...]}."""
    hits = {}
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith((".git/", "tests/", "node_modules/", "venv/", ".venv/")):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        stack = []

        class V(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()
            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Constant(self, node):
                if (isinstance(node.value, str)
                        and "mcp_dev_keys" in node.value.lower()
                        and _TIER_WRITE.search(node.value)):
                    hits.setdefault(
                        (rel, stack[-1] if stack else "<module>"), []
                    ).append(node.lineno)

        V().visit(tree)
    return hits


def _func_src(rel, func):
    """One function's source, with any module-level string constant it names
    REPLACED BY THAT CONSTANT'S VALUE.

    The clause may legitimately live in a shared constant — one spelling of a
    security predicate is better than five. But accepting the mere NAME would
    make this guard checkable by writing `_VERIFIED_BINDING_SQL` anywhere,
    whatever the constant now holds. So resolve the value and look for the real
    clause in it: the guard follows the derivation, not the reference.
    """
    src = (ROOT / rel).read_text(encoding="utf-8")
    tree = ast.parse(src)
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    consts[t.id] = node.value.value
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == func):
            body = ast.get_source_segment(src, node) or ""
            for name in {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}:
                if name in consts:
                    body += "\n# resolved %s = %r" % (name, consts[name])
            return body
    raise AssertionError(f"{rel}: no function named {func}")


SCAN = _scan()


def test_the_scan_still_finds_the_writes_it_is_meant_to_guard():
    """A scan that can find nothing passes everything below for free."""
    total = sum(len(v) for v in SCAN.values())
    assert total >= FLOOR, (
        f"found {total} tier writes, floor is {FLOOR}. Either the regex or the "
        f"AST walk stopped matching — every assertion in this file is vacuous "
        f"until that is fixed. Found: {sorted(SCAN)}")


def test_every_tier_write_is_classified():
    unknown = {k: v for k, v in SCAN.items() if k not in REGISTER}
    assert not unknown, (
        "a new write to mcp_dev_keys.tier is not classified:\n  "
        + "\n  ".join(f"{f}:{lns} in {fn}()" for (f, fn), lns in sorted(unknown.items()))
        + "\n\nDecide how its target rows are chosen. If an ADDRESS chooses them "
          "it is email_match and needs the proof clause — that is the defect "
          "#4428 fixed in three places and missed in three more.")


def test_the_register_has_no_stale_entries():
    """An entry for a write that no longer exists hides a real one behind it."""
    gone = sorted(set(REGISTER) - set(SCAN))
    assert not gone, f"register names writes that are no longer there: {gone}"


def test_each_function_has_the_number_of_writes_the_register_claims():
    """Keying by function alone would let a SECOND write appear inside an
    already-classified function and inherit its classification."""
    for key, entries in sorted(REGISTER.items()):
        assert len(SCAN.get(key, [])) == len(entries), (
            f"{key[0]}:{key[1]}() has {len(SCAN.get(key, []))} tier writes but "
            f"the register classifies {len(entries)}. A new one inside an "
            f"already-classified function is exactly how a grant slips in.")


@pytest.mark.parametrize("key", sorted(k for k, v in REGISTER.items()
                                       if any(e[0] == EMAIL_MATCH for e in v)))
def test_an_email_matched_grant_carries_the_proof_clause(key):
    rel, func = key
    for entry in REGISTER[key]:
        if entry[0] != EMAIL_MATCH:
            continue
        proof_in = entry[2] if len(entry) > 2 else func
        src = _func_src(rel, proof_in)
        assert "email_verified_for" in src, (
            f"{rel}:{proof_in}() chooses rows by address and grants a tier, but "
            f"carries no email_verified_for clause. An address is a string "
            f"anyone can type; without the clause this grants a paying "
            f"customer's tier to whoever typed theirs.")
