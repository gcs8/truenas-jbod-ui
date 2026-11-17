"""Contract of the order-independent placement index.

The index answers "which single observation claims this key?" with missing,
unique or ambiguous, and the answer never depends on the order observations
were added. Every identifier here is synthetic.
"""

from __future__ import annotations

import ast
import gc
import itertools
import unittest
import weakref
from pathlib import Path

from app.services.placement_index import (
    AMBIGUOUS,
    CLAIMANT_LIMIT,
    MISSING,
    UNIQUE,
    PlacementIndex,
    Resolution,
)

MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "services" / "placement_index.py"


def build(calls):
    index = PlacementIndex()
    for call in calls:
        index.add(*call)
    return index


def snapshot(index, scope, queries):
    return (
        index.keys(scope),
        index.unique(scope),
        tuple(index.resolve(scope, query) for query in queries),
    )


class _Token:
    """A claimant or value the index may only hold weakly for this test."""

    def __init__(self, name):
        self.name = name


class PlacementIndexBasicsTests(unittest.TestCase):
    def test_unclaimed_key_is_missing(self):
        index = PlacementIndex()
        self.assertEqual(index.resolve("enc-a", ["bay-0"]), Resolution(MISSING, None, 0, frozenset()))
        self.assertEqual(index.resolve("enc-a", []), Resolution(MISSING, None, 0, frozenset()))
        self.assertEqual(index.keys("enc-a"), frozenset())
        self.assertEqual(index.unique("enc-a"), {})

    def test_aliases_of_one_claimant_are_not_a_collision(self):
        index = build([
            ("enc-a", "sda", "disk-1", "record-1"),
            ("enc-a", "SANITIZED-S1", "disk-1", "record-1"),
            ("enc-a", "sas-alias-1", "disk-1", "record-1"),
        ])
        expected = Resolution(UNIQUE, "record-1", 1, frozenset({"disk-1"}))
        for key in ("sda", "SANITIZED-S1", "sas-alias-1"):
            self.assertEqual(index.resolve("enc-a", [key]), expected)
        self.assertEqual(index.resolve("enc-a", ["sda", "SANITIZED-S1", "sas-alias-1"]), expected)
        self.assertEqual(index.unique("enc-a"), {
            "sda": "record-1", "SANITIZED-S1": "record-1", "sas-alias-1": "record-1",
        })

    def test_repeated_add_of_the_same_claim_is_idempotent(self):
        index = build([("enc-a", "sda", "disk-1", "record-1")] * 3)
        self.assertEqual(index.resolve("enc-a", ["sda"]), Resolution(UNIQUE, "record-1", 1, frozenset({"disk-1"})))

    def test_two_claimants_on_one_key_are_ambiguous_and_carry_no_value(self):
        index = build([("enc-a", "bay-0", "disk-1", "record-1"), ("enc-a", "bay-0", "disk-2", "record-2")])
        self.assertEqual(
            index.resolve("enc-a", ["bay-0"]),
            Resolution(AMBIGUOUS, None, 2, frozenset({"disk-1", "disk-2"})),
        )
        self.assertEqual(index.unique("enc-a"), {})
        self.assertEqual(index.keys("enc-a"), frozenset({"bay-0"}))

    def test_equal_values_from_different_claimants_still_collide(self):
        # Two observations that happen to carry equal rows are still two rows.
        index = build([("enc-a", "bay-0", "disk-1", "same"), ("enc-a", "bay-0", "disk-2", "same")])
        self.assertEqual(index.resolve("enc-a", ["bay-0"]).state, AMBIGUOUS)

    def test_one_claimant_with_unequal_values_is_ambiguous(self):
        index = build([("enc-a", "sda", "disk-1", "record-1"), ("enc-a", "sda", "disk-1", "record-2")])
        self.assertEqual(index.resolve("enc-a", ["sda"]), Resolution(AMBIGUOUS, None, 1, frozenset({"disk-1"})))
        # Across keys too: the union sees one claimant naming two different rows.
        index = build([("enc-a", "sda", "disk-1", "record-1"), ("enc-a", "sdb", "disk-1", "record-2")])
        self.assertEqual(index.resolve("enc-a", ["sda"]).state, UNIQUE)
        self.assertEqual(index.resolve("enc-a", ["sda", "sdb"]), Resolution(AMBIGUOUS, None, 1, frozenset({"disk-1"})))

    def test_one_claimant_with_equal_but_distinct_value_objects_stays_unique(self):
        index = build([
            ("enc-a", "md0", "md0", {"name": "md0", "type": "raid1"}),
            ("enc-a", "md0", "md0", {"name": "md0", "type": "raid1"}),
        ])
        resolution = index.resolve("enc-a", ["md0"])
        self.assertEqual(resolution.state, UNIQUE)
        self.assertEqual(resolution.value, {"name": "md0", "type": "raid1"})

    def test_ambiguity_is_sticky_when_a_third_row_arrives(self):
        base = [("enc-a", "bay-0", "disk-1", "record-1"), ("enc-a", "bay-0", "disk-2", "record-2")]
        for third in (
            ("enc-a", "bay-0", "disk-1", "record-1"),
            ("enc-a", "bay-0", "disk-2", "record-2"),
            ("enc-a", "bay-0", "disk-3", "record-3"),
        ):
            with self.subTest(third=third):
                index = build([*base, third])
                resolution = index.resolve("enc-a", ["bay-0"])
                self.assertEqual(resolution.state, AMBIGUOUS)
                self.assertIsNone(resolution.value)
                self.assertEqual(resolution.claimant_count, len({base[0][2], base[1][2], third[2]}))

    def test_different_scopes_never_collide(self):
        index = build([
            ("controller-0", "bay-0", "disk-1", "record-1"),
            ("controller-1", "bay-0", "disk-2", "record-2"),
            (("node-a", "shelf-1"), "bay-0", "disk-3", "record-3"),
        ])
        self.assertEqual(index.resolve("controller-0", ["bay-0"]).value, "record-1")
        self.assertEqual(index.resolve("controller-1", ["bay-0"]).value, "record-2")
        self.assertEqual(index.resolve(("node-a", "shelf-1"), ["bay-0"]).value, "record-3")
        self.assertEqual(index.resolve(("node-b", "shelf-1"), ["bay-0"]).state, MISSING)
        self.assertEqual(index.resolve(None, ["bay-0"]).state, MISSING)

    def test_resolve_unions_every_key_instead_of_taking_the_first_hit(self):
        index = build([("enc-a", "sda", "disk-1", "record-1"), ("enc-a", "SANITIZED-S2", "disk-2", "record-2")])
        for keys in (["sda", "SANITIZED-S2"], ["SANITIZED-S2", "sda"], {"sda", "SANITIZED-S2"},
                     ("sda", "missing", "SANITIZED-S2"), (key for key in ["sda", "SANITIZED-S2"])):
            with self.subTest(keys=keys):
                self.assertEqual(
                    index.resolve("enc-a", keys),
                    Resolution(AMBIGUOUS, None, 2, frozenset({"disk-1", "disk-2"})),
                )
        self.assertEqual(index.resolve("enc-a", ["sda", "missing", "sda"]).value, "record-1")

    def test_ambiguous_key_is_never_turned_into_missing_or_unique_by_a_union(self):
        index = build([
            ("enc-a", "bay-0", "disk-1", "record-1"),
            ("enc-a", "bay-0", "disk-2", "record-2"),
            ("enc-a", "sda", "disk-1", "record-1"),
        ])
        self.assertEqual(index.resolve("enc-a", ["bay-0", "sda"]).state, AMBIGUOUS)
        self.assertEqual(index.resolve("enc-a", ["bay-0", "absent"]).state, AMBIGUOUS)

    def test_resolve_refuses_a_bare_string_for_keys(self):
        index = build([("enc-a", "s", "disk-1", "record-1")])
        for keys in ("sda", b"sda"):
            with self.subTest(keys=keys), self.assertRaises(TypeError):
                index.resolve("enc-a", keys)

    def test_rank_must_be_an_integer(self):
        index = PlacementIndex()
        for rank in (1.5, "1", None, True):
            with self.subTest(rank=rank), self.assertRaises(TypeError):
                index.add("enc-a", "sda", "disk-1", "record-1", rank=rank)
        self.assertEqual(index.keys("enc-a"), frozenset())


class PlacementIndexRankTests(unittest.TestCase):
    def test_higher_rank_wins_in_both_orders(self):
        exact = ("sas", "sas-alias-1", "disk-1", "record-1", 1)
        shifted = ("sas", "sas-alias-1", "disk-2", "record-2", 0)
        for calls in ([exact, shifted], [shifted, exact]):
            with self.subTest(order=[call[2] for call in calls]):
                self.assertEqual(
                    build(calls).resolve("sas", ["sas-alias-1"]),
                    Resolution(UNIQUE, "record-1", 1, frozenset({"disk-1"})),
                )

    def test_exact_evidence_resolves_a_shifted_collision(self):
        calls = [
            ("sas", "sas-alias-2", "disk-1", "record-1", 0),
            ("sas", "sas-alias-2", "disk-3", "record-3", 0),
        ]
        self.assertEqual(build(calls).resolve("sas", ["sas-alias-2"]).state, AMBIGUOUS)
        calls.append(("sas", "sas-alias-2", "disk-2", "record-2", 1))
        for order in itertools.permutations(calls):
            self.assertEqual(build(order).resolve("sas", ["sas-alias-2"]).value, "record-2")

    def test_equal_top_ranks_tie_to_ambiguous_even_with_lower_rank_support(self):
        calls = [
            ("sas", "sas-alias-3", "disk-1", "record-1", 1),
            ("sas", "sas-alias-3", "disk-2", "record-2", 1),
            ("sas", "sas-alias-3", "disk-1", "record-1", 0),
        ]
        for order in itertools.permutations(calls):
            self.assertEqual(
                build(order).resolve("sas", ["sas-alias-3"]),
                Resolution(AMBIGUOUS, None, 2, frozenset({"disk-1", "disk-2"})),
            )

    def test_rank_competes_within_one_key_not_across_the_union(self):
        # One evidence tier per call: a lower-ranked claim on another key still counts.
        index = build([("sas", "alias-x", "disk-1", "record-1", 1), ("sas", "alias-y", "disk-2", "record-2", 0)])
        self.assertEqual(index.resolve("sas", ["alias-x"]).value, "record-1")
        self.assertEqual(index.resolve("sas", ["alias-x", "alias-y"]).state, AMBIGUOUS)

    def test_negative_ranks_order_like_integers(self):
        calls = [("lsblk", "wwn-1", "sda", "disk", 0), ("lsblk", "wwn-1", "sda1", "partition", -1)]
        for order in itertools.permutations(calls):
            self.assertEqual(build(order).resolve("lsblk", ["wwn-1"]).value, "disk")


class PlacementIndexOrderIndependenceTests(unittest.TestCase):
    """Every permutation of small inputs, not one reversal."""

    def assert_permutation_invariant(self, calls, scope, queries):
        expected = None
        count = 0
        for order in itertools.permutations(calls):
            count += 1
            result = snapshot(build(order), scope, queries)
            if expected is None:
                expected = result
            else:
                self.assertEqual(result, expected, order)
        self.assertGreater(count, 1)
        return expected

    def test_collision_aliases_ranks_and_scopes_in_every_order(self):
        calls = [
            ("enc-a", "bay-0", "disk-1", "record-1"),
            ("enc-a", "sda", "disk-1", "record-1"),
            ("enc-a", "bay-0", "disk-2", "record-2"),
            ("enc-a", "sdb", "disk-2", "record-2"),
            ("enc-a", "alias-x", "disk-3", "record-3", 1),
            ("enc-a", "alias-x", "disk-1", "record-1", 0),
            ("enc-b", "bay-0", "disk-4", "record-4"),
        ]
        queries = [["bay-0"], ["sda"], ["sdb"], ["alias-x"], ["sda", "sdb"], ["sda", "alias-x"], ["absent"]]
        keys, unique, resolutions = self.assert_permutation_invariant(calls, "enc-a", queries)
        self.assertEqual(keys, frozenset({"bay-0", "sda", "sdb", "alias-x"}))
        self.assertEqual(unique, {"sda": "record-1", "sdb": "record-2", "alias-x": "record-3"})
        self.assertEqual([r.state for r in resolutions], [AMBIGUOUS, UNIQUE, UNIQUE, UNIQUE, AMBIGUOUS, AMBIGUOUS, MISSING])
        self.assert_permutation_invariant(calls, "enc-b", [["bay-0"]])

    def test_three_way_collision_and_same_claimant_conflict_in_every_order(self):
        calls = [
            ("pool", "sda", "pool-a/leaf-0", "member-a"),
            ("pool", "sda", "pool-b/leaf-0", "member-b"),
            ("pool", "sda", "pool-c/leaf-0", "member-c"),
            ("pool", "guid-1", "pool-a/leaf-0", "member-a"),
            ("pool", "sdz", "pool-d/leaf-0", "member-d"),
            ("pool", "sdz", "pool-d/leaf-0", "member-e"),
        ]
        _, unique, resolutions = self.assert_permutation_invariant(
            calls, "pool", [["sda"], ["guid-1"], ["sdz"], ["guid-1", "sdz"]],
        )
        self.assertEqual(unique, {"guid-1": "member-a"})
        self.assertEqual(
            [(r.state, r.claimant_count) for r in resolutions],
            [(AMBIGUOUS, 3), (UNIQUE, 1), (AMBIGUOUS, 1), (AMBIGUOUS, 2)],
        )

    def test_saturated_counts_are_order_independent(self):
        calls = [("enc-a", "bay-0", f"disk-{n}", f"record-{n}") for n in range(CLAIMANT_LIMIT + 1)]
        calls.append(("enc-a", "bay-0", "disk-0", "record-0"))
        first = build(calls).resolve("enc-a", ["bay-0"])
        for shift in range(len(calls)):
            rotated = calls[shift:] + calls[:shift]
            self.assertEqual(build(rotated).resolve("enc-a", ["bay-0"]), first)
            self.assertEqual(build(reversed(rotated)).resolve("enc-a", ["bay-0"]), first)
        self.assertEqual(first, Resolution(AMBIGUOUS, None, CLAIMANT_LIMIT + 1, None))


class PlacementIndexScaleTests(unittest.TestCase):
    def test_a_thousand_disks_behind_one_expander_are_all_admitted(self):
        index = PlacementIndex()
        for bay in range(1000):
            index.add("expander-0", ("bay", bay), f"disk-{bay}", f"record-{bay}")
            index.add("expander-0", f"SANITIZED-S{bay:04d}", f"disk-{bay}", f"record-{bay}")
        unique = index.unique("expander-0")
        self.assertEqual(len(unique), 2000)
        self.assertEqual(len(index.keys("expander-0")), 2000)
        self.assertEqual(unique[("bay", 999)], "record-999")
        self.assertEqual(index.resolve("expander-0", [("bay", 0), "SANITIZED-S0000"]).value, "record-0")

    def test_a_thousand_claimants_on_one_key_keep_a_bounded_sample(self):
        index = PlacementIndex()

        def add_claims():
            tokens = [(_Token(f"disk-{n}"), _Token(f"record-{n}")) for n in range(1000)]
            for claimant, value in tokens:
                index.add("expander-0", ("bay", 0), claimant, value)
            return [weakref.ref(token) for pair in tokens for token in pair]

        refs = add_claims()
        gc.collect()
        self.assertLessEqual(sum(ref() is not None for ref in refs), 2 * CLAIMANT_LIMIT)
        self.assertEqual(
            index.resolve("expander-0", [("bay", 0)]),
            Resolution(AMBIGUOUS, None, CLAIMANT_LIMIT + 1, None),
        )
        self.assertEqual(index.keys("expander-0"), frozenset({("bay", 0)}))

    def test_claimant_count_is_exact_up_to_the_limit(self):
        for size in range(1, CLAIMANT_LIMIT + 1):
            with self.subTest(size=size):
                index = build([("enc-a", "bay-0", f"disk-{n}", f"record-{n}") for n in range(size)])
                resolution = index.resolve("enc-a", ["bay-0"])
                self.assertEqual(resolution.claimant_count, size)
                self.assertEqual(resolution.claimants, frozenset(f"disk-{n}" for n in range(size)))
        # A union past the limit saturates too.
        index = build([("enc-a", f"key-{n}", f"disk-{n}", n) for n in range(CLAIMANT_LIMIT + 1)])
        union = index.resolve("enc-a", [f"key-{n}" for n in range(CLAIMANT_LIMIT + 1)])
        self.assertEqual(union, Resolution(AMBIGUOUS, None, CLAIMANT_LIMIT + 1, None))


class PlacementIndexExistingRefusalTests(unittest.TestCase):
    """The refusals callers already make can be expressed with the index."""

    def test_bare_bay_number_reported_by_two_controllers_places_no_drive(self):
        calls = [("bmc-bay", 0, "drive-a", "row-a"), ("bmc-bay", 0, "drive-b", "row-b"), ("bmc-bay", 1, "drive-c", "row-c")]
        for order in itertools.permutations(calls):
            index = build(order)
            self.assertEqual(index.resolve("bmc-bay", [0]).state, AMBIGUOUS)
            self.assertEqual(index.unique("bmc-bay"), {1: "row-c"})
            self.assertEqual(sorted(index.keys("bmc-bay")), [0, 1])

    def test_controller_qualified_keys_survive_a_shared_bare_key(self):
        index = build([
            ("esxi", "13:0", "disk-1", "record-1"), ("esxi", "c0/e13/s0", "disk-1", "record-1"),
            ("esxi", "13:0", "disk-2", "record-2"), ("esxi", "c1/e13/s0", "disk-2", "record-2"),
        ])
        self.assertEqual(index.unique("esxi"), {"c0/e13/s0": "record-1", "c1/e13/s0": "record-2"})

    def test_serial_must_be_unique_on_both_sides_of_a_two_population_match(self):
        bmc = [("bmc", "SANITIZED-S1", "bmc-a", "bmc-row-a"), ("bmc", "SANITIZED-S1", "bmc-b", "bmc-row-b"),
               ("bmc", "SANITIZED-S2", "bmc-c", "bmc-row-c")]
        host = [("host", "SANITIZED-S1", "host-a", "host-row-a"), ("host", "SANITIZED-S2", "host-c", "host-row-c"),
                ("host", "SANITIZED-S2", "host-d", "host-row-d")]
        index = build([*bmc, *host])
        for serial in ("SANITIZED-S1", "SANITIZED-S2"):
            both = (index.resolve("bmc", [serial]).state, index.resolve("host", [serial]).state)
            self.assertNotEqual(both, (UNIQUE, UNIQUE))
        control = build([bmc[2], host[1]])
        self.assertEqual(control.resolve("bmc", ["SANITIZED-S2"]).value, "bmc-row-c")
        self.assertEqual(control.resolve("host", ["SANITIZED-S2"]).value, "host-row-c")

    def test_one_geom_listed_under_two_names_is_one_claimant(self):
        geom = {"name": "multipath/disk1"}
        index = build([("geom", "da0", id(geom), geom), ("geom", "da0", id(geom), geom), ("geom", "da1", id(geom), geom)])
        self.assertEqual(index.resolve("geom", ["da0", "da1"]).value, geom)


class PlacementIndexModuleShapeTests(unittest.TestCase):
    def test_module_is_small_pure_and_standard_library_only(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertLess(len(source.splitlines()), 200)
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                imported.add(node.module)
        self.assertLessEqual(imported, {"__future__", "collections.abc", "dataclasses", "typing"})
        called = {node.func.id for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertFalse(called & {"open", "print", "exec", "eval", "__import__"})


if __name__ == "__main__":
    unittest.main()
