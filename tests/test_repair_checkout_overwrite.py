"""scripts/repair_checkout_overwrite.py (backend#5555 one-account repair).

The decision logic is pure and tested here; the script's DB/Stripe I/O is not
run in CI.
"""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "repair_checkout_overwrite", ROOT / "scripts" / "repair_checkout_overwrite.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

STALE = "cus_VK46QB7qWNwWX5"
ROW = ("admin001", "pro", "pro", STALE, "active")


def test_repoints_to_the_newest_other_live_customer():
    fix = r.plan_repair(ROW, STALE, [("cus_new", 200), ("cus_old", 100)], "admin")
    assert fix == {"id": "admin001", "stripe_customer_id": "cus_new", "role": "admin"}


def test_no_other_live_customer_clears_the_column():
    assert r.plan_repair(ROW, STALE, [])["stripe_customer_id"] is None


def test_role_is_left_alone_unless_named():
    assert r.plan_repair(ROW, STALE, [])["role"] is None


def test_noop_when_the_row_no_longer_points_at_the_stale_customer():
    assert r.plan_repair(("admin001", "pro", "admin", "cus_other", "active"), STALE, []) is None
    assert r.plan_repair(None, STALE, []) is None


class _Obj(dict):
    pass


class _List:
    def __init__(self, data):
        self.data = data

    def auto_paging_iter(self):
        return iter(self.data)


class _FakeStripe:
    class Customer:
        @staticmethod
        def list(email=None, limit=None):
            return _List([_Obj(id=STALE), _Obj(id="cus_live"), _Obj(id="cus_dead")])

    class Subscription:
        @staticmethod
        def list(customer=None, status=None, limit=None):
            subs = {STALE: [_Obj(status="trialing", created=300)],
                    "cus_live": [_Obj(status="active", created=100),
                                 _Obj(status="canceled", created=250)],
                    "cus_dead": [_Obj(status="canceled", created=200)]}
            return _List(subs[customer])


def test_live_customers_excludes_stale_and_dead():
    assert r.live_customers("x@y.z", STALE, stripe_mod=_FakeStripe) == [("cus_live", 100)]


def test_dry_run_is_the_default():
    src = (ROOT / "scripts" / "repair_checkout_overwrite.py").read_text(encoding="utf-8")
    assert 'ap.add_argument("--apply", action="store_true")' in src
    assert "SET TRANSACTION READ ONLY" in src
    assert "AND stripe_customer_id = %s" in src
