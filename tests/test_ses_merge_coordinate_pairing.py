"""SES merging keeps each bay's enclosure path and element as one coordinate (#1319).

Two SES paths can report the same physical bay. When the second path carries
stronger slot-number evidence, the merge adopts its element number. The
enclosure path must come from that same record: an element number from one
path paired with the device of another names a coordinate that neither path
reported, and on the retained path it can be another bay. These tests build
synthetic shelves only; no host is contacted and no locate action runs.
"""

from __future__ import annotations

import copy
import unittest

from app.services.inventory import InventoryService
from app.services.parsers import (
    ParsedSSHData,
    SESMapEnclosure,
    SESMapSlot,
    _merge_ses_enclosures,
    _merge_ses_slot_evidence,
    build_slot_candidates_from_ses_enclosures,
)

SHELF_ID = "synthetic-shelf-a"
WEAK_PATH = "/dev/sg1"
STRONG_PATH = "/dev/sg2"


def _slot(bay: int, element: int, path: str, source: str) -> SESMapSlot:
    slot = SESMapSlot(
        slot_number=bay,
        element_id=element,
        ses_device=path,
        slot_number_source=source,
        description=f"Slot {bay:02d}",
        status="OK",
        present=True,
        presence_source="sg_ses",
    )
    slot.control_targets = [
        {
            "ses_device": path,
            "ses_element_id": element,
            "ses_slot_number": None if source == "ses_element_id_fallback" else bay,
        }
    ]
    return slot


def _weak_path_enclosure() -> SESMapEnclosure:
    """The first path numbers bays by element order: element N is bay N."""

    return SESMapEnclosure(
        ses_device=WEAK_PATH,
        ses_devices=[WEAK_PATH],
        enclosure_id=SHELF_ID,
        slots={
            bay: _slot(bay, bay, WEAK_PATH, "ses_element_id_fallback")
            for bay in (3, 4)
        },
    )


def _strong_path_enclosure() -> SESMapEnclosure:
    """The second path reports device slot numbers; its elements start at 1."""

    return SESMapEnclosure(
        ses_device=STRONG_PATH,
        ses_devices=[STRONG_PATH],
        enclosure_id=SHELF_ID,
        slots={
            bay: _slot(bay, bay + 1, STRONG_PATH, "ses_device_slot_number")
            for bay in (3, 4)
        },
    )


# Every coordinate a source path reported, as (path, element).
REPORTED_COORDINATES = {
    (WEAK_PATH, 3), (WEAK_PATH, 4), (STRONG_PATH, 4), (STRONG_PATH, 5),
}


def _coordinate(slot: SESMapSlot) -> tuple[str | None, int | None]:
    return slot.ses_device, slot.element_id


def _target_coordinates(targets: list[dict]) -> set[tuple[str | None, int | None]]:
    return {(target.get("ses_device"), target.get("ses_element_id")) for target in targets}


def _bay_coordinates(enclosures: list[SESMapEnclosure]) -> dict[int, tuple[str | None, int | None]]:
    (enclosure,) = enclosures
    return {bay: _coordinate(slot) for bay, slot in sorted(enclosure.slots.items())}


class SlotEvidenceCoordinatePairingTests(unittest.TestCase):
    def test_stronger_record_supplies_path_and_element_together(self) -> None:
        existing = _slot(3, 3, WEAK_PATH, "ses_element_id_fallback")
        _merge_ses_slot_evidence(existing, _slot(3, 4, STRONG_PATH, "ses_device_slot_number"))
        self.assertEqual(existing.slot_number_source, "ses_device_slot_number")
        self.assertEqual(_coordinate(existing), (STRONG_PATH, 4))

    def test_weaker_record_leaves_the_existing_coordinate_whole(self) -> None:
        existing = _slot(3, 4, STRONG_PATH, "ses_device_slot_number")
        _merge_ses_slot_evidence(existing, _slot(3, 3, WEAK_PATH, "ses_element_id_fallback"))
        self.assertEqual(_coordinate(existing), (STRONG_PATH, 4))

    def test_missing_element_is_filled_with_the_incoming_path(self) -> None:
        existing = _slot(3, 3, WEAK_PATH, "ses_description")
        existing.element_id = None
        _merge_ses_slot_evidence(existing, _slot(3, 4, STRONG_PATH, "ses_description"))
        self.assertEqual(_coordinate(existing), (STRONG_PATH, 4))

    def test_missing_path_is_not_filled_beside_an_existing_element(self) -> None:
        existing = _slot(3, 3, WEAK_PATH, "ses_description")
        existing.ses_device = None
        _merge_ses_slot_evidence(existing, _slot(3, 4, STRONG_PATH, "ses_description"))
        self.assertEqual(_coordinate(existing), (None, 3))


class EnclosureMergeCoordinateTests(unittest.TestCase):
    def test_merged_bays_carry_only_reported_coordinates(self) -> None:
        merged = _merge_ses_enclosures([_weak_path_enclosure(), _strong_path_enclosure()])
        coordinates = _bay_coordinates(merged)
        self.assertEqual(coordinates, {3: (STRONG_PATH, 4), 4: (STRONG_PATH, 5)})
        for bay, slot in merged[0].slots.items():
            with self.subTest(bay=bay):
                self.assertLessEqual(_target_coordinates(slot.control_targets), REPORTED_COORDINATES)

    def test_remerging_a_merged_enclosure_is_idempotent(self) -> None:
        once = _merge_ses_enclosures([_weak_path_enclosure(), _strong_path_enclosure()])
        twice = _merge_ses_enclosures(copy.deepcopy(once))
        self.assertEqual(sorted(twice[0].slots), [3, 4])
        self.assertEqual(_bay_coordinates(twice), _bay_coordinates(once))
        for bay in (3, 4):
            with self.subTest(bay=bay):
                self.assertEqual(
                    _target_coordinates(twice[0].slots[bay].control_targets),
                    _target_coordinates(once[0].slots[bay].control_targets),
                )

    def test_reapplying_a_path_to_the_merged_enclosure_keeps_bays_apart(self) -> None:
        # A later overlay pass merges the merged shelf with a fresh read of a
        # path it already holds. A mixed coordinate would match another bay's
        # element on that path and fold that bay's evidence into this one.
        once = _merge_ses_enclosures([_weak_path_enclosure(), _strong_path_enclosure()])
        weak = _weak_path_enclosure()
        weak.slots[4].serial = "SERIAL-BAY-4"
        weak.slots[3].serial = "SERIAL-BAY-3"
        again = _merge_ses_enclosures([*copy.deepcopy(once), weak])
        self.assertEqual(sorted(again[0].slots), [3, 4])
        self.assertEqual(_bay_coordinates(again), _bay_coordinates(once))
        self.assertEqual(again[0].slots[3].serial, "SERIAL-BAY-3")
        self.assertEqual(again[0].slots[4].serial, "SERIAL-BAY-4")
        for bay in (3, 4):
            with self.subTest(bay=bay):
                self.assertEqual(
                    _target_coordinates(again[0].slots[bay].control_targets),
                    _target_coordinates(once[0].slots[bay].control_targets),
                )

    def test_path_order_does_not_change_bays_or_coordinates(self) -> None:
        forward = _merge_ses_enclosures([_weak_path_enclosure(), _strong_path_enclosure()])
        reverse = _merge_ses_enclosures([_strong_path_enclosure(), _weak_path_enclosure()])
        self.assertEqual(_bay_coordinates(forward), _bay_coordinates(reverse))
        for bay in (3, 4):
            with self.subTest(bay=bay):
                self.assertEqual(
                    _target_coordinates(forward[0].slots[bay].control_targets),
                    _target_coordinates(reverse[0].slots[bay].control_targets),
                )


class CandidateAndLocateTargetTests(unittest.TestCase):
    def _candidates(self, enclosures: list[SESMapEnclosure]) -> dict[int, dict]:
        candidates, _meta = build_slot_candidates_from_ses_enclosures(
            enclosures, 8, None, SHELF_ID,
        )
        return candidates

    def _bay_targets(self, candidates: dict[int, dict]) -> dict[int, set]:
        return {
            index: _target_coordinates(candidate["ses_targets"])
            for index, candidate in candidates.items()
        }

    def test_selected_shelf_candidates_never_offer_a_mixed_locate_target(self) -> None:
        candidates = self._candidates([_weak_path_enclosure(), _strong_path_enclosure()])
        self.assertEqual(len(candidates), 2)
        for index, candidate in candidates.items():
            with self.subTest(index=index):
                self.assertIn(
                    (candidate["ses_device"], candidate["ses_element_id"]),
                    REPORTED_COORDINATES,
                )
                self.assertLessEqual(_target_coordinates(candidate["ses_targets"]), REPORTED_COORDINATES)
        # Each bay's targets name only that bay on either path, so no two bays
        # share a coordinate.
        targets = list(self._bay_targets(candidates).values())
        self.assertFalse(targets[0] & targets[1])

    def test_candidates_are_stable_across_order_and_remerge(self) -> None:
        forward = self._candidates([_weak_path_enclosure(), _strong_path_enclosure()])
        reverse = self._candidates([_strong_path_enclosure(), _weak_path_enclosure()])
        remerged = self._candidates(
            _merge_ses_enclosures(_merge_ses_enclosures([_weak_path_enclosure(), _strong_path_enclosure()]))
        )
        expected = self._bay_targets(forward)
        self.assertEqual(self._bay_targets(reverse), expected)
        self.assertEqual(self._bay_targets(remerged), expected)

    def test_host_overlay_merge_keeps_locate_targets_paired(self) -> None:
        base = ParsedSSHData(ses_enclosures=[_weak_path_enclosure()])
        overlay = ParsedSSHData(ses_enclosures=[_strong_path_enclosure()])
        merged = InventoryService._merge_ses_overlay_data(base, overlay)
        # A later overlay pass merges the already merged enclosure again.
        again = InventoryService._merge_ses_overlay_data(merged, ParsedSSHData())
        for parsed in (merged, again):
            candidates = self._candidates(parsed.ses_enclosures)
            self.assertEqual(len(candidates), 2)
            for index, candidate in candidates.items():
                with self.subTest(index=index):
                    self.assertLessEqual(
                        _target_coordinates(candidate["ses_targets"]), REPORTED_COORDINATES,
                    )


def _pathless(slot: SESMapSlot) -> SESMapSlot:
    """The same evidence with no device path on the slot or its targets."""

    slot.ses_device = None
    slot.control_targets = []
    return slot


class MissingPathCandidateTests(unittest.TestCase):
    """A record that reported an element but no path never borrows one.

    The candidate builder falls back to the enclosure's path for a slot with
    none of its own. After a merge, that path can belong to another record,
    so the fallback would offer a coordinate neither input reported.
    """

    def _targets(self, enclosures: list[SESMapEnclosure], **kwargs) -> set:
        candidates, meta = build_slot_candidates_from_ses_enclosures(
            enclosures, 8, None, SHELF_ID, **kwargs,
        )
        targets = set()
        for candidate in candidates.values():
            targets |= _target_coordinates(candidate["ses_targets"])
            if candidate["ses_device"] is not None:
                targets.add((candidate["ses_device"], candidate["ses_element_id"]))
        for element in meta.get("unmapped_ses_elements", []):
            targets |= _target_coordinates(element["ses_targets"])
            if element["ses_device"] is not None:
                targets.add((element["ses_device"], element["ses_element_id"]))
        return targets

    def _weak_bay(self) -> SESMapEnclosure:
        return SESMapEnclosure(
            ses_device=WEAK_PATH,
            enclosure_id=SHELF_ID,
            slots={3: _slot(3, 3, WEAK_PATH, "ses_element_id_fallback")},
        )

    def _pathless_strong_bay(self, record_path: str | None = None) -> SESMapEnclosure:
        return SESMapEnclosure(
            ses_device=record_path,
            enclosure_id=SHELF_ID,
            slots={3: _pathless(_slot(3, 4, STRONG_PATH, "ses_device_slot_number"))},
        )

    def test_pathless_stronger_element_never_takes_another_record_path(self) -> None:
        for order in ("weak-first", "strong-first"):
            shelves = [self._weak_bay(), self._pathless_strong_bay()]
            if order == "strong-first":
                shelves.reverse()
            with self.subTest(order=order):
                targets = self._targets(shelves)
                # The weak path's own coordinate stays as the positive control.
                self.assertIn((WEAK_PATH, 3), targets)
                self.assertNotIn((WEAK_PATH, 4), targets)

    def test_remerged_pathless_element_never_takes_another_record_path(self) -> None:
        once = _merge_ses_enclosures([self._weak_bay(), self._pathless_strong_bay()])
        for label, kwargs, enclosures in (
            ("merged", {"enclosures_are_merged": True}, copy.deepcopy(once)),
            ("remerged", {}, copy.deepcopy(once)),
            ("reapplied", {}, [*copy.deepcopy(once), self._weak_bay()]),
        ):
            with self.subTest(label=label):
                targets = self._targets(enclosures, **kwargs)
                self.assertIn((WEAK_PATH, 3), targets)
                self.assertNotIn((WEAK_PATH, 4), targets)

    def test_pathless_element_keeps_its_own_record_path(self) -> None:
        # The stronger slot carries no path, but its record does: that path,
        # not the first record's, completes the coordinate.
        targets = self._targets([self._weak_bay(), self._pathless_strong_bay(STRONG_PATH)])
        self.assertIn((STRONG_PATH, 4), targets)
        self.assertIn((WEAK_PATH, 3), targets)
        self.assertNotIn((WEAK_PATH, 4), targets)

    def test_single_record_pathless_slot_still_uses_its_record_path(self) -> None:
        shelf = SESMapEnclosure(
            ses_device=WEAK_PATH,
            enclosure_id=SHELF_ID,
            slots={3: _pathless(_slot(3, 3, WEAK_PATH, "ses_device_slot_number"))},
        )
        self.assertIn((WEAK_PATH, 3), self._targets([shelf]))

    def test_pathless_unmapped_element_never_takes_another_record_path(self) -> None:
        orphan = _pathless(_slot(-1, 9, STRONG_PATH, "ses_description"))
        pathless = SESMapEnclosure(enclosure_id=SHELF_ID, unmapped_slots=[orphan])
        targets = self._targets([self._weak_bay(), pathless])
        self.assertIn((WEAK_PATH, 3), targets)
        self.assertNotIn((WEAK_PATH, 9), targets)

    def test_pathless_element_is_not_folded_into_the_same_element_on_another_path(self) -> None:
        # Element 4 on the weak path is bay 4. The pathless record's element 4
        # is bay 3; read with the weak path it would be bay 4's element and
        # merge the two bays' evidence.
        weak = SESMapEnclosure(
            ses_device=WEAK_PATH,
            enclosure_id=SHELF_ID,
            slots={4: _slot(4, 4, WEAK_PATH, "ses_device_slot_number")},
        )
        weak.slots[4].serial = "SERIAL-BAY-4"
        pathless = self._pathless_strong_bay()
        pathless.slots[3].serial = "SERIAL-BAY-3"
        (merged,) = _merge_ses_enclosures([weak, pathless])
        self.assertEqual(sorted(merged.slots), [3, 4])
        self.assertEqual(merged.slots[3].serial, "SERIAL-BAY-3")
        self.assertEqual(merged.slots[4].serial, "SERIAL-BAY-4")
        self.assertEqual(_coordinate(merged.slots[4]), (WEAK_PATH, 4))


if __name__ == "__main__":
    unittest.main()
