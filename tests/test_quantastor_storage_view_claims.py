"""Overlapping QuantaStor storage views claim each disk once, per node (#1210).

Synthetic fixtures only. Candidates come from the real producer
(`_build_quantastor_disk_records` then `_serialize_storage_view_candidate`),
so they carry the node-qualified `candidate_id`/`storage_system_id` shape the
runtime sees.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.config import (
    Settings,
    SSHConfig,
    StorageViewBindingConfig,
    StorageViewConfig,
    SystemConfig,
    TrueNASConfig,
)
from app.models.domain import EnclosureOption, InventorySnapshot, SourceStatus
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.truenas_ws import TrueNASRawData


def _service(temp_dir: str) -> InventoryService:
    system = SystemConfig(
        id="synthetic-qs",
        label="Synthetic QuantaStor",
        truenas=TrueNASConfig(platform="quantastor"),
        ssh=SSHConfig(enabled=False),
    )
    settings = Settings(systems=[system])
    return InventoryService(
        settings,
        system,
        AsyncMock(side_effect=AssertionError("no appliance call expected")),
        AsyncMock(side_effect=AssertionError("no SSH call expected")),
        None,
        MappingStore(str(Path(temp_dir) / "slot_mappings.json")),
        ProfileRegistry(settings),
    )


def _snapshot(service: InventoryService, node_id: str) -> InventorySnapshot:
    return InventorySnapshot(
        slots=[],
        refresh_interval_seconds=30,
        selected_system_id=service.system.id,
        selected_system_platform="quantastor",
        selected_enclosure_id=node_id,
        selected_enclosure_label=node_id,
        enclosures=[EnclosureOption(id=node_id, label=node_id)],
        sources={"api": SourceStatus(enabled=True, ok=True, message="Synthetic read")},
    )


def _view(view_id: str, *, node_id: str, **binding) -> StorageViewConfig:
    return StorageViewConfig(
        id=view_id,
        label=view_id,
        kind="nvme_carrier",
        template_id="nvme-carrier-4",
        binding=StorageViewBindingConfig(target_system_id=node_id, **binding),
    )


def _candidates(service: InventoryService, node_id: str, disks: list[dict]) -> list[dict]:
    raw = TrueNASRawData(
        enclosures=[],
        disks=disks,
        pools=[],
        disk_temperatures={},
        smart_test_results=[],
        systems=[{"id": "node-a"}, {"id": "node-b"}],
    )
    records = service._build_quantastor_disk_records(raw, node_id)
    return [service._serialize_storage_view_candidate(record) for record in records]


def _occupied(view) -> list[tuple[str | None, str | None]]:
    return [(slot.candidate_id, slot.device_name) for slot in view.slots if slot.occupied]


class QuantastorStorageViewClaimTests(unittest.TestCase):
    def test_overlapping_views_on_one_node_assign_a_disk_only_to_the_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _service(temp_dir)
            snapshot = _snapshot(service, "node-a")
            candidates = _candidates(service, "node-a", [{
                "id": "disk-a", "storageSystemId": "node-a", "devicePath": "/dev/sda",
                "serialNumber": "SANITIZED-QS-A", "size": 4096,
            }])
            self.assertEqual(service._candidate_identity_key(candidates[0]), "node-a|SANITIZED-QS-A")
            views = [
                _view("first", node_id="node-a", mode="serial", serials=["SANITIZED-QS-A"]),
                _view("second", node_id="node-a", mode="serial", serials=["SANITIZED-QS-A"]),
            ]

            result = service._serialize_storage_views_runtime(
                snapshot, views, {"node-a": snapshot}, {"node-a": candidates},
            )

            self.assertEqual([view.matched_count for view in result], [1, 0])
            self.assertEqual(_occupied(result[0]), [("SANITIZED-QS-A", "sda")])
            self.assertEqual(_occupied(result[1]), [])

    def test_serial_less_disk_is_claimed_by_its_persistent_candidate_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _service(temp_dir)
            snapshot = _snapshot(service, "node-a")
            candidates = _candidates(service, "node-a", [{
                "id": "disk-a", "storageSystemId": "node-a", "devicePath": "/dev/sdc",
                "wwn": "wwn-synthetic-a", "size": 4096,
            }])
            # Precondition: the persistent id is not the displayed device name.
            self.assertEqual(candidates[0]["candidate_id"], "wwn-synthetic-a")
            self.assertEqual(candidates[0]["device_names"][0], "sdc")
            views = [
                _view("first", node_id="node-a", mode="serial", device_names=["sdc"]),
                _view("second", node_id="node-a", mode="serial", device_names=["sdc"]),
            ]

            result = service._serialize_storage_views_runtime(
                snapshot, views, {"node-a": snapshot}, {"node-a": candidates},
            )

            self.assertEqual([view.matched_count for view in result], [1, 0])
            self.assertEqual(_occupied(result[0]), [("wwn-synthetic-a", "sdc")])

    def test_claims_keep_node_scope_for_distinct_disks_with_the_same_local_name(self) -> None:
        # Control: each node's view keeps its own disk at /dev/sdc.
        with tempfile.TemporaryDirectory() as temp_dir:
            service = _service(temp_dir)
            snapshot_a = _snapshot(service, "node-a")
            snapshot_b = _snapshot(service, "node-b")
            candidates = {
                node: _candidates(service, node, [{
                    "id": f"disk-{node}", "storageSystemId": node, "devicePath": "/dev/sdc", "size": 4096,
                }])
                for node in ("node-a", "node-b")
            }
            views = [
                _view("on-a", node_id="node-a", mode="serial", device_names=["sdc"]),
                _view("on-b", node_id="node-b", mode="serial", device_names=["sdc"]),
            ]

            result = service._serialize_storage_views_runtime(
                snapshot_a, views, {"node-a": snapshot_a, "node-b": snapshot_b}, candidates,
            )

            self.assertEqual([view.matched_count for view in result], [1, 1])
            self.assertEqual([slot.target_system_id for view in result for slot in view.slots if slot.occupied],
                             ["node-a", "node-b"])
            # Both disks fall back to the same local path as their persistent id;
            # only the node qualifier keeps their claims apart.
            self.assertEqual(
                [service._candidate_identity_key(candidates[node][0]) for node in ("node-a", "node-b")],
                ["node-a|/dev/sdc", "node-b|/dev/sdc"],
            )


if __name__ == "__main__":
    unittest.main()
