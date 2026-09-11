"""An in-memory facility world for the served-slug tests. NO DB, NO NETWORK.

The profile route decides where /facilities/<slug> lands by asking three
questions: which row does the slug fetch (_fetch_facility_by_slug), which
keeper does that row's duplicate_of_id name (_canonical_twin_row), and where
does the alias table send a slug that fetched nothing (resolve_alias). This
module answers them from lists of fixture rows and wires the answers in under
BOTH of the route's resolvers:

  install_batch(...)     facility_profile_page.served_slugs, through its three
                         batch lookups — what every list emitter calls;
  install_per_slug(...)  facility_profile_page.resolve_final_slug, through the
                         per-request lookups the page itself runs.

Everything above those seams — both walks, and every redirect decision in
_twin_redirect_target — is the real code. Nothing in this file decides a
redirect.

★ UNAMBIGUOUS FIXTURES ONLY. The page picks among rows sharing a frozen slug or
  a hash8 tail with an ORDER BY this file does not implement, so the world
  REFUSES (AssertionError) any question with more than one candidate rather
  than guess. The ordering, the lookup that raises live and the real parameter
  coercions are tests/test_served_slugs_sql_parity.py's job, against Postgres.
★ No more capable than the page: `facilities` is searched by frozen slug only
  when the world is told that table has is_duplicate (live it does not, and
  the page's lookup on it raises), and a `facilities` row found by hash8 comes
  back with is_duplicate and duplicate_of_id NULL, as the page selects them.
★ install_batch makes every per-request lookup RAISE and hands served_slugs a
  connection that refuses every statement, both recorded in `refused`: the
  batch can reach this world through its own three lookups and nothing else.
"""
import sys
import types

from routes.facility_slug import stable_hash8


class _RefusingCursor:
    def __init__(self, world):
        self._world = world

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())[:160]
        self._world.refused.append("statement: " + text)
        raise AssertionError("a statement reached the database behind the "
                             "served-slug world: " + text)

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        pass


class _RefusingConnection:
    def __init__(self, world):
        self._world = world

    def cursor(self, *_a, **_k):
        return _RefusingCursor(self._world)

    def rollback(self):
        pass

    def close(self):
        self._world.closed += 1


class World:
    def __init__(self, discovered=(), facilities=(), aliases=None,
                 facilities_has_is_duplicate=False):
        self.discovered = [dict(r) for r in discovered]
        self.facilities = [dict(r) for r in facilities]
        self.aliases = dict(aliases or {})
        self.facilities_has_is_duplicate = facilities_has_is_duplicate
        # batch lookups that would have issued a statement, per lookup
        self.rounds = {"page_rows": 0, "keeper_rows": 0, "alias_targets": 0}
        self.refused = []
        self.closed = 0

    @staticmethod
    def _one(hits, what):
        assert len(hits) == 1, (
            f"ambiguous fixture: {len(hits)} {what}. The page ORDERs these and "
            "this world does not — put the case in "
            "tests/test_served_slugs_sql_parity.py.")
        return dict(hits[0])

    # ── the page's three questions ──────────────────────────────────────────
    def page_row(self, slug):
        """The row /facilities/<slug> fetches, or None."""
        parts = str(slug).rsplit("-", 1)
        if len(parts) != 2 or len(parts[1]) != 8:
            return None
        by_slug = [("discovered_facilities", self.discovered)]
        if self.facilities_has_is_duplicate:
            by_slug.append(("facilities", self.facilities))
        for table, rows in by_slug:
            hits = [r for r in rows if r.get("canonical_slug") == slug]
            if hits:
                return dict(self._one(hits, f"{table} rows wear {slug}"),
                            _src_table=table)
        for table, rows in (("discovered_facilities", self.discovered),
                            ("facilities", self.facilities)):
            hits = [r for r in rows
                    if stable_hash8(r.get("provider"), r.get("name")) == parts[1]]
            if hits:
                row = dict(self._one(hits, f"{table} rows hash to {parts[1]}"),
                           _src_table=table)
                if table == "facilities":
                    row.update(is_duplicate=None, duplicate_of_id=None)
                return row
        return None

    def keeper(self, dup):
        """The keeper row a duplicate_of_id names, or None."""
        if dup is None or isinstance(dup, bool):
            return None
        try:
            key = int(str(dup).strip())
        except ValueError:
            return None
        hits = [r for r in self.discovered if r.get("id") == key]
        if not hits:
            return None
        k = self._one(hits, f"discovered rows with id {key}")
        if (k.get("is_duplicate") or 0) != 0 or not k.get("canonical_slug"):
            return None
        return {"canonical_slug": str(k["canonical_slug"]),
                "address": k.get("address"),
                "latitude": k.get("latitude"), "longitude": k.get("longitude"),
                "duplicate_of_id": k.get("duplicate_of_id"),
                "slug_rows": sum(1 for r in self.discovered
                                 if r.get("canonical_slug") == k["canonical_slug"])}

    def alias(self, slug):
        """Where the alias table sends `slug`, or None."""
        key = (slug[:-5] if slug.endswith(".html") else slug).split("/")[0].strip()
        target = self.aliases.get(key)
        return target if target and target != key else None

    # ── wiring ──────────────────────────────────────────────────────────────
    def install_batch(self, monkeypatch, fpp):
        """served_slugs reads this world through its three batch lookups only."""
        main = types.ModuleType("main")
        main.get_read_db = lambda: _RefusingConnection(self)
        monkeypatch.setitem(sys.modules, "main", main)
        monkeypatch.setattr(fpp, "_canonical_slug_tables",
                            lambda _conn, _cur: {"discovered_facilities",
                                                 "facilities"})

        def page_rows(_conn, _cur, slugs, _has_canon):
            asked = [s for s in slugs
                     if len(s.rsplit("-", 1)) == 2 and len(s.rsplit("-", 1)[1]) == 8]
            if asked:
                self.rounds["page_rows"] += 1
            found = {}
            for s in asked:
                row = self.page_row(s)
                if row is not None:
                    found[s] = row
            return found

        def keeper_rows(_cur, dups):
            keys = {fpp._twin_key(d) for d in dups} - {None}
            if keys:
                self.rounds["keeper_rows"] += 1
            keepers = {}
            for k in keys:
                row = self.keeper(k)
                if row is not None:
                    keepers[k] = row
            return keepers

        def alias_targets(_cur, slugs):
            slugs = [s for s in slugs if s]
            if slugs:
                self.rounds["alias_targets"] += 1
            return {s: t for s in slugs for t in [self.alias(s)] if t}

        monkeypatch.setattr(fpp, "_batch_page_rows", page_rows)
        monkeypatch.setattr(fpp, "_batch_keeper_rows", keeper_rows)
        monkeypatch.setattr(fpp, "_batch_alias_targets", alias_targets)

        def refuse(name):
            def _refused(*_a, **_k):
                self.refused.append(f"per-request lookup: {name}()")
                raise AssertionError(f"served_slugs called {name}(), the "
                                     "per-request lookup")
            return _refused

        import routes.facility_slug_freeze as fsf
        for name in ("_fetch_facility_by_slug", "_canonical_twin_row",
                     "_resolve_legacy_slug"):
            monkeypatch.setattr(fpp, name, refuse(name))
        monkeypatch.setattr(fsf, "resolve_alias", refuse("resolve_alias"))
        return self

    def install_per_slug(self, monkeypatch, fpp):
        """resolve_final_slug reads this world through the page's own lookups."""
        import routes.facility_slug_freeze as fsf
        monkeypatch.setattr(fpp, "_fetch_facility_by_slug", self.page_row)
        monkeypatch.setattr(fpp, "_canonical_twin_row", self.keeper)
        monkeypatch.setattr(fsf, "resolve_alias", self.alias)
        # the one step served_slugs does not take (its module comment says why)
        monkeypatch.setattr(fpp, "_resolve_legacy_slug", lambda _s: None)
        return self
