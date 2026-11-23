"""Synthetic AOC snapshots through real ESXi parsing/correlation, without transport."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.config import Settings, SSHConfig, SystemConfig, TrueNASConfig
from app.models.domain import InventorySnapshot, StorageViewRuntimePayload, SystemOption
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.parsers import parse_ssh_outputs
from app.services.profile_registry import ESXI_AOC_SLG4_2H8M2_PROFILE_ID, ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore
from app.services.truenas_ws import TrueNASRawData
from scripts import build_current_source_browser_fixture as builder

LISTING = "/opt/lsi/storcli64/storcli64 /c0/eall/sall show all J"


def build_case(kind):
    """Unread, complete empty census, or the maintained two-drive AOC shape."""
    if kind not in {"unread", "empty", "populated"}:
        raise ValueError(kind)
    system = SystemConfig(
        id="synthetic-esxi", label="Synthetic ESXi", truenas=TrueNASConfig(platform="esxi"),
        default_profile_id=ESXI_AOC_SLG4_2H8M2_PROFILE_ID,
        ssh=SSHConfig(enabled=False, commands=[LISTING]),
    )
    settings = Settings(systems=[system])
    drives = [
        {"EID:Slt": f"13:{slot}", "DID": slot, "State": "Onln", "DG": "0", "Size": "1.818 TB",
         "Intf": "NVMe", "Med": "SSD", "SeSz": "512B", "Model": "Samsung SSD 970 EVO 2TB",
         "SN": f"SANITIZED-AOC-{slot}"}
        for slot in range(2 if kind == "populated" else 1)
    ] if kind != "unread" else []
    outputs = {} if kind == "unread" else {LISTING: json.dumps({"Controllers": [{
        "Command Status": {"Controller": 0, "Status": "Success"},
        "Response Data": {"Drive Information": drives},
    }]})}
    warnings = ["StorCLI is not available on this ESXi host, so drive details behind the RAID "
                "controller are missing for this refresh."] if kind == "unread" else []
    with tempfile.TemporaryDirectory(prefix="aoc-") as directory:
        service = InventoryService(
            settings, system, MagicMock(), MagicMock(), None, MappingStore(str(Path(directory) / "m.json")),
            ProfileRegistry(settings), SlotDetailStore(str(Path(directory) / "d.json")),
        )
        slots, enclosures, meta, rows, count, columns = service._correlate(
            TrueNASRawData(enclosures=[], disks=[], pools=[], disk_temperatures={}, smart_test_results=[]),
            parse_ssh_outputs(outputs, 2, None), warnings,
        )
        snapshot = InventorySnapshot(
            slots=slots, enclosures=enclosures, warnings=warnings,
            systems=[SystemOption(id=system.id, label=system.label or system.id, platform="esxi")],
            selected_system_id=system.id, selected_system_label=system.label, selected_system_platform="esxi",
            selected_enclosure_id=meta["id"], selected_enclosure_label=meta["label"],
            selected_profile=service.profile_registry.get(ESXI_AOC_SLG4_2H8M2_PROFILE_ID),
            layout_rows=rows, layout_slot_count=count, layout_columns=columns, refresh_interval_seconds=60,
        )
        runtime = StorageViewRuntimePayload(system_id=system.id, views=[])
        with patch.object(builder, "build_synthetic_live_snapshot", return_value=snapshot):
            html = builder.build_live_fixture_html(runtime)
        return {"html": html, "snapshot": snapshot.model_dump(mode="json"),
                "runtime": runtime.model_dump(mode="json"),
                "smart": {str(slot.slot): service._build_esxi_smart_summary(slot).model_dump(mode="json")
                          for slot in slots}}


if __name__ == "__main__":
    print(json.dumps({kind: build_case(kind) for kind in ("unread", "populated", "empty")}))
