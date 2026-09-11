"""scripts/apply_cf_cache_ruleset.py — the applier must refuse, and must verify
by reading BACK rather than by believing the write.

This is the only code in the repo that writes to the Cloudflare zone
configuration, so what is pinned here is mostly what it must NOT do: no adding,
no removing, no reordering, nothing outside the rule the operator named, and no
green verdict from a write it did not confirm on a fresh read.

★ Every "it did not write" assertion is paired with a control that DOES write
the same fixture. `writes == []` is trivially true for a broken applier, and an
assertion that cannot fail is not a test.
"""
from __future__ import annotations

import importlib.util
import json as _json
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "apply_cf_cache_ruleset.py"
WORKFLOW = REPO / ".github" / "workflows" / "cf-cache-rule-apply.yml"
REAL_CANON = REPO / "scripts" / "cf_cache_ruleset_canon.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


applier = _load("apply_cf_cache_ruleset", SCRIPT)
reader = _load("check_cf_cache_ruleset", REPO / "scripts" / "check_cf_cache_ruleset.py")


# ── fixtures ─────────────────────────────────────────────────────────────────
# Canon must clear check_canon_floor's MIN_CANON_RULES, so these are real-shaped
# rulesets rather than two-rule toys: a truncated fixture would exercise the
# floor instead of the planner.
def _rule(i: int, **over):
    rule = {
        "position": i,
        "id": f"{i:02d}" + "f" * 30,
        "description": f"rule {i}",
        "expression": f'starts_with(http.request.uri.path, "/p{i}/")',
        "action": "set_cache_settings",
        "action_parameters": {"cache": False},
        "enabled": True,
    }
    rule.update(over)
    return rule


def _canon(n: int = 25, **over):  # the live zone carries 25; index 23 is rule 24
    canon = {
        "zone_name": "dchub.cloud",
        "zone_id": "z" * 32,
        "ruleset_id": "r" * 32,
        "pinned_version": "42",
        "pinned_at": "2026-09-11T00:00:00Z",
        "rules": [_rule(i) for i in range(1, n + 1)],
    }
    canon.update(over)
    return canon


READ_KEY = "read-only-token"
WRITE_KEY = "read-write-token"
ENV_READ_ONLY = {"CF_CACHE_RULES_TOKEN": READ_KEY}
ENV_BOTH = {"CF_CACHE_RULES_TOKEN": READ_KEY, "CLOUDFLARE_API_TOKEN": WRITE_KEY}


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = _json.dumps(payload)

    def json(self):
        return self._payload


class FakeCF:
    """A Cloudflare that stores what it is told — unless asked to lie.

    `lies` makes PATCH answer 200 with the UPDATED ruleset in its body while
    leaving its stored state untouched. That is the shape of the failure this
    applier's read-back exists to catch, and the only fixture that can tell
    "verified by a fresh read" apart from "believed the write".
    """

    def __init__(self, rules, *, can_read=(READ_KEY,), can_write=(WRITE_KEY,),
                 version=42, lies=False, patch_status=200):
        self.rules = [dict(r) for r in rules]
        self.can_read = set(can_read)
        self.can_write = set(can_write)
        self.version = version
        self.lies = lies
        self.patch_status = patch_status
        self.reads = 0
        self.attempts: list[tuple[str, str]] = []   # (rule_id, token) — including refused
        self.writes: list[tuple[str, dict, str]] = []  # (rule_id, body, token) — accepted only

    @staticmethod
    def _token(headers):
        return (headers or {}).get("Authorization", "").replace("Bearer ", "", 1)

    def _payload(self, rules):
        return {"success": True,
                "result": {"rules": [dict(r) for r in rules], "version": str(self.version)}}

    def get(self, url, headers=None, timeout=None):
        if self._token(headers) not in self.can_read:
            return _Resp(403, {"success": False, "errors": [{"message": "read denied"}]})
        self.reads += 1
        return _Resp(200, self._payload(self.rules))

    def patch(self, url, headers=None, json=None, timeout=None):
        token, rule_id = self._token(headers), url.rsplit("/", 1)[-1]
        self.attempts.append((rule_id, token))
        if token not in self.can_write:
            return _Resp(403, {"success": False, "errors": [{"message": "write denied"}]})
        if self.patch_status != 200:
            return _Resp(self.patch_status,
                         {"success": False, "errors": [{"message": "expression rejected"}]})
        self.writes.append((rule_id, dict(json or {}), token))
        self.version += 1
        updated = []
        for rule in self.rules:
            copy = dict(rule)
            if copy["id"] == rule_id:
                copy.update(json or {})
                if not self.lies:
                    rule.update(json or {})
            updated.append(copy)
        return _Resp(200, self._payload(updated))


def _run(tmp_path, monkeypatch, canon, cf, *, env=None, argv=()):
    path = tmp_path / "canon.json"
    path.write_text(_json.dumps(canon))
    monkeypatch.setattr(applier, "CANON_PATH", path)
    args = applier.build_parser().parse_args(list(argv))
    lines: list[str] = []
    code = applier.run(cf, args, log=lines.append, environ=dict(env or ENV_BOTH))
    return code, "\n".join(lines)


def _live(canon, mutate=None):
    """The zone as the API returns it, optionally mutated away from canon."""
    rules = [dict(r) for r in canon["rules"]]
    if mutate:
        mutate(rules)
        for position, rule in enumerate(rules, start=1):
            rule["position"] = position
    return rules


def _bend(index, field, value):
    def mutate(rules):
        rules[index][field] = value
    return mutate


# ── planning: what may be applied ────────────────────────────────────────────
def test_a_zone_that_already_matches_plans_nothing():
    canon = _canon()
    changes, refusals = applier.plan_changes(canon, _live(canon))
    assert (changes, refusals) == ([], [])


def test_a_changed_expression_is_planned_with_both_values():
    canon = _canon()
    live = _live(canon, _bend(23, "expression", 'starts_with(http.request.uri.path, "/api/")'))
    changes, refusals = applier.plan_changes(canon, live)
    assert refusals == []
    assert [c["id"] for c in changes] == [canon["rules"][23]["id"]]
    before, after = changes[0]["fields"]["expression"]
    assert before == 'starts_with(http.request.uri.path, "/api/")'
    assert after == canon["rules"][23]["expression"]


@pytest.mark.parametrize(
    "name,mutate",
    [
        ("a rule added in the dashboard", lambda rules: rules.append(_rule(99))),
        ("a rule canon pins but the zone lacks", lambda rules: rules.pop(5)),
        ("the same rules in a different order",
         lambda rules: rules.insert(0, rules.pop(20))),
    ],
)
def test_structural_differences_refuse(name, mutate):
    """Add, delete and reorder all change which rule WINS — Cache Rules are
    last-match-wins — so none of them is a one-click edit."""
    canon = _canon()
    _, refusals = applier.plan_changes(canon, _live(canon, mutate))
    assert refusals, f"{name} was not refused"


def test_expect_refuses_a_rule_it_was_not_pointed_at():
    canon = _canon()

    def mutate(rules):
        rules[23]["expression"] = "changed-24"
        rules[9]["expression"] = "changed-10"

    live = _live(canon, mutate)
    expect = canon["rules"][23]["id"][:8]
    _, refusals = applier.plan_changes(canon, live, expect=expect)
    assert refusals and "also differ" in refusals[0]

    # control: with only the named rule differing, the same prefix applies.
    live_one = _live(canon, _bend(23, "expression", "changed-24"))
    changes, refusals = applier.plan_changes(canon, live_one, expect=expect)
    assert refusals == [] and len(changes) == 1


def test_more_differences_than_the_cap_refuse():
    canon = _canon()

    def mutate(rules):
        for index in range(4):
            rules[index]["expression"] = f"changed-{index}"

    live = _live(canon, mutate)
    _, refusals = applier.plan_changes(canon, live, cap=3)
    assert refusals and "above the cap" in refusals[0]
    # control: the same four differences pass when the operator raises the cap.
    changes, refusals = applier.plan_changes(canon, live, cap=4)
    assert refusals == [] and len(changes) == 4


@pytest.mark.parametrize(
    "mutate",
    [
        None,
        _bend(3, "expression", "changed"),
        _bend(3, "enabled", False),
        _bend(3, "action", "set_config"),
        _bend(3, "action_parameters", {"cache": True}),
        _bend(3, "description", "quietly reworded"),
        lambda rules: rules.append(_rule(99)),
        lambda rules: rules.pop(2),
        lambda rules: rules.insert(0, rules.pop(20)),
    ],
)
def test_the_planner_and_the_read_guard_agree_on_whether_the_zone_differs(mutate):
    """Two independent implementations of the same comparison. The guard that
    ALARMS and the tool that FIXES must not disagree about what a match is —
    if they drift, one of them starts calling a drifted zone clean."""
    canon = _canon()
    live = _live(canon, mutate)
    changes, refusals = applier.plan_changes(canon, live, cap=99)
    findings = reader.diff_ruleset(canon, live)
    assert bool(changes or refusals) == bool(findings)


# ── writing: exit codes, and never a green verdict it did not earn ───────────
def test_without_apply_nothing_is_written(tmp_path, monkeypatch):
    canon = _canon()
    cf = FakeCF(_live(canon, _bend(23, "expression", "drifted")))
    code, log = _run(tmp_path, monkeypatch, canon, cf)
    assert code == applier.EXIT_OK
    assert cf.attempts == [] and "plan only" in log

    # ★ CONTROL: the same fixture with --apply DOES write. Without this, the
    # assertion above passes just as well for an applier that can never write.
    cf2 = FakeCF(_live(canon, _bend(23, "expression", "drifted")))
    code2, _ = _run(tmp_path, monkeypatch, canon, cf2, argv=["--apply"])
    assert code2 == applier.EXIT_OK and len(cf2.writes) == 1


def test_the_patch_body_carries_every_field_this_tool_may_change(tmp_path, monkeypatch):
    canon = _canon()
    cf = FakeCF(_live(canon, _bend(23, "expression", "drifted")))
    code, _ = _run(tmp_path, monkeypatch, canon, cf, argv=["--apply"])
    assert code == applier.EXIT_OK
    rule_id, body, token = cf.writes[0]
    assert rule_id == canon["rules"][23]["id"]
    assert set(body) == set(applier.PATCHED_FIELDS)
    assert body["expression"] == canon["rules"][23]["expression"]
    assert token == WRITE_KEY


def test_a_refusal_writes_nothing_even_with_apply(tmp_path, monkeypatch):
    canon = _canon()
    cf = FakeCF(_live(canon, lambda rules: rules.append(_rule(99))))
    code, log = _run(tmp_path, monkeypatch, canon, cf, argv=["--apply"])
    assert code == applier.EXIT_REFUSED
    assert cf.attempts == [] and "NOTHING was written" in log


def test_every_token_is_tried_and_the_writer_is_named(tmp_path, monkeypatch):
    """The token that can READ cache rules is not necessarily the one that can
    EDIT them, and which repo secret can write is not knowable from here."""
    canon = _canon()
    cf = FakeCF(_live(canon, _bend(23, "expression", "drifted")),
                can_read=(READ_KEY,), can_write=(WRITE_KEY,))
    code, log = _run(tmp_path, monkeypatch, canon, cf, argv=["--apply"])
    assert code == applier.EXIT_OK
    assert [token for _, token in cf.attempts] == [READ_KEY, WRITE_KEY]
    assert "CLOUDFLARE_API_TOKEN" in log


def test_no_credential_can_write_exits_cannot_not_ok(tmp_path, monkeypatch):
    canon = _canon()
    cf = FakeCF(_live(canon, _bend(23, "expression", "drifted")), can_write=())
    code, log = _run(tmp_path, monkeypatch, canon, cf, argv=["--apply"])
    assert code == applier.EXIT_CANNOT
    assert code != applier.EXIT_OK
    assert "CANNOT WRITE" in log
    for name in ("CF_CACHE_RULES_TOKEN", "CLOUDFLARE_API_TOKEN"):
        assert name in log, f"the message does not say {name} was tried"
    assert cf.writes == []


def test_a_rejected_body_stops_instead_of_trying_the_next_key(tmp_path, monkeypatch):
    """A 400 means Cloudflare read the request and refused the CONTENT. Trying
    another credential would only bury that behind a permissions story."""
    canon = _canon()
    cf = FakeCF(_live(canon, _bend(23, "expression", "drifted")),
                can_write=(READ_KEY, WRITE_KEY), patch_status=400)
    code, log = _run(tmp_path, monkeypatch, canon, cf, argv=["--apply"])
    assert code == applier.EXIT_CANNOT
    assert len(cf.attempts) == 1, "a rejected body must not fall through to the next token"
    assert "no other token was tried" in log


def test_a_write_that_did_not_take_is_caught_by_the_read_back(tmp_path, monkeypatch):
    """The PATCH answers 200 AND returns the updated rule in its body, while the
    zone keeps the old one. Believing the write reports success here."""
    canon = _canon()
    cf = FakeCF(_live(canon, _bend(23, "expression", "drifted")), lies=True)
    code, log = _run(tmp_path, monkeypatch, canon, cf, argv=["--apply"])
    assert code == applier.EXIT_VERIFY_FAILED
    assert "VERIFICATION FAILED" in log
    assert cf.reads >= 2, "the verdict was reached without a second, fresh read"


def test_an_unreadable_zone_is_not_a_pass(tmp_path, monkeypatch):
    canon = _canon()
    cf = FakeCF(_live(canon), can_read=())
    code, log = _run(tmp_path, monkeypatch, canon, cf, argv=["--apply"])
    assert code == applier.EXIT_CANNOT
    assert "COULD NOT LOOK" in log and cf.attempts == []


def test_an_already_matching_zone_is_a_no_op(tmp_path, monkeypatch):
    canon = _canon()
    cf = FakeCF(_live(canon))
    code, log = _run(tmp_path, monkeypatch, canon, cf, argv=["--apply"])
    assert code == applier.EXIT_OK
    assert cf.attempts == [] and "already matches" in log


def test_a_truncated_canon_refuses_before_it_reads_the_zone(tmp_path, monkeypatch):
    """An emptied or half-written canon must never be able to rewrite a zone."""
    cf = FakeCF(_live(_canon()))
    code, log = _run(tmp_path, monkeypatch, _canon(n=3), cf, argv=["--apply"])
    assert code == applier.EXIT_REFUSED
    assert "below the floor" in log and cf.reads == 0


def test_canon_without_a_zone_id_refuses(tmp_path, monkeypatch):
    canon = _canon()
    canon["zone_id"] = ""
    cf = FakeCF(_live(canon))
    code, _ = _run(tmp_path, monkeypatch, canon, cf, env={}, argv=["--apply"])
    assert code == applier.EXIT_REFUSED and cf.reads == 0


def test_tokens_are_tried_in_intent_order_and_deduplicated_by_value():
    same = "one-secret-two-names"
    pairs = applier.resolve_tokens(
        {"CF_TOKEN": same, "CLOUDFLARE_API_TOKEN": same, "CF_CACHE_RULES_TOKEN": "first"})
    assert [name for name, _ in pairs] == ["CF_CACHE_RULES_TOKEN", "CLOUDFLARE_API_TOKEN"]
    assert applier.resolve_tokens({}) == []


def test_no_flag_is_accepted_by_an_abbreviation():
    """argparse accepts unambiguous prefixes by default, so `--app` would mean
    `--apply` — a typo in the workflow would silently WRITE on the plan step."""
    with pytest.raises(SystemExit):
        applier.build_parser().parse_args(["--app"])


# ── the lane ─────────────────────────────────────────────────────────────────
def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers():
    """YAML 1.1 reads a bare `on:` key as the boolean True, so a workflow's
    trigger block arrives under True, not 'on'."""
    workflow = _workflow()
    return workflow[True] if True in workflow else workflow["on"]


def _steps():
    return _workflow()["jobs"]["apply"]["steps"]


def _code(step):
    return str(step.get("run") or "")


def _index_of(predicate, why):
    for index, step in enumerate(_steps()):
        if predicate(step):
            return index
    raise AssertionError(f"no step in cf-cache-rule-apply.yml {why}")


def test_the_lane_is_manual_only():
    """No schedule, no push. The only workflow that writes zone config does not
    get to run because something merged."""
    assert set(_triggers()) == {"workflow_dispatch"}


def test_the_apply_step_runs_the_applier_with_apply_and_expect():
    index = _index_of(lambda s: "apply_cf_cache_ruleset.py --apply" in _code(s),
                      "runs the applier with --apply")
    step = _steps()[index]
    assert '--expect "$EXPECT"' in _code(step) and '--cap "$CAP"' in _code(step)
    for name in ("CF_CACHE_RULES_TOKEN", "CLOUDFLARE_API_TOKEN", "CF_TOKEN"):
        assert name in (step.get("env") or {}), f"the apply step cannot try {name}"


def test_the_plan_step_runs_first_and_does_not_write():
    plan = _index_of(lambda s: "apply_cf_cache_ruleset.py" in _code(s)
                     and "--apply" not in _code(s), "shows the plan without writing")
    apply_at = _index_of(lambda s: "--apply" in _code(s), "applies")
    assert plan < apply_at


def test_the_expected_rule_prefix_names_a_rule_that_exists_in_canon():
    """A typo'd default would refuse every run forever while looking careful."""
    default = _triggers()["workflow_dispatch"]["inputs"]["expect"]["default"]
    ids = [r["id"] for r in _json.loads(REAL_CANON.read_text())["rules"]]
    assert [i for i in ids if i.startswith(default)], \
        f"expect default {default!r} matches no rule id in the pinned canon"


def test_the_paid_seat_smoke_verifies_after_the_apply():
    """The oracle for 'the leak is closed' is a paying seat reading through the
    edge, not the ruleset diff the applier just made agree with itself."""
    apply_at = _index_of(lambda s: "--apply" in _code(s), "applies")
    verify_at = _index_of(lambda s: "smoke_pro_seat_brief.py" in _code(s),
                          "verifies from a paid seat")
    assert apply_at < verify_at
    verify = _steps()[verify_at]
    assert "if" not in verify, "the verify step must inherit success() — not run after a failed apply"
    assert "DCHUB_API_KEY" in (verify.get("env") or {})


def test_the_purge_derives_its_urls_from_the_coverage_checker():
    """A hand-typed URL list drifts away from the surface the checker covers."""
    purge_at = _index_of(lambda s: "purge_cache" in _code(s), "purges the stored copies")
    code = _code(_steps()[purge_at])
    assert "PROBE_PATHS" in code and "check_credential_channel_coverage.py" in code
    assert "https://dchub.cloud/markets" not in code, "the URL list is typed, not derived"
