"""BMC web inventory count limits sit above real hardware (Refs #1309).

A controller can reach many shelves and one SAS expander can carry up to 1,024
disks, so a large healthy inventory must be read in full. The count limits only
stop runaway replies, and a reply that reaches one is refused with a page
warning that names the count and the limit, never a silent drop. The real BMC
service runs against synthetic in-memory XML replies; the Redfish readers are
replaced with no-ops so the result is the web inventory alone.
"""

from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

from app.config import BMCConfig
from app.services import supermicro_bmc
from app.services.supermicro_bmc import BMCInventory, SupermicroBMCService

CONTROLLER_ATTR = "Get_BrcmStorCtrlAttr.XML"
PHYSICAL_DRIVES = "Get_BrcmStorCtrlPhyDrvInfo.XML"


def _drive_nodes(count: int) -> str:
    return "".join(
        f'<HDD Vendor="SANITIZED" ModelName="SANITIZED" SN="SANITIZED-{index}" Status="1" SlotNumber="{index:x}"/>'
        for index in range(count)
    )


class BMCInventoryScaleTests(unittest.TestCase):
    def _fetch(self, *, controllers: str, drives_per_controller: int = 0) -> tuple[BMCInventory, list[str]]:
        service = SupermicroBMCService(
            BMCConfig(enabled=True, host="https://bmc.example.test", username="synthetic", password="***")
        )
        calls: list[str] = []
        drive_reply = f"<IPMI><HDDInfo>{_drive_nodes(drives_per_controller)}</HDDInfo></IPMI>"

        def post_web_xml(_opener, _csrf, _path, params):
            calls.append(params["op"])
            if params["op"] == CONTROLLER_ATTR:
                return ET.fromstring(f'<IPMI><BrcmStorCtrlAttr ctrl_found_num="{controllers}"/></IPMI>')
            if params["op"] == PHYSICAL_DRIVES:
                return ET.fromstring(drive_reply)
            return ET.fromstring("<IPMI/>")

        service._open_web_session = MagicMock(return_value=(object(), "synthetic-csrf"))
        service._post_web_xml = post_web_xml
        with (
            patch.object(SupermicroBMCService, "_populate_redfish_inventory", lambda _self, _inventory: None),
            patch.object(SupermicroBMCService, "_populate_redfish_simple_storage_fallback", lambda _self, _inventory: None),
        ):
            return service.fetch_inventory(), calls

    def test_a_full_expander_of_drives_on_one_controller_is_read(self) -> None:
        inventory, _calls = self._fetch(controllers="1", drives_per_controller=1024)

        self.assertTrue(inventory.source_details["web_xml_ok"])
        self.assertEqual(inventory.warnings, [])
        self.assertEqual(len(inventory.drives), 1024)

    def test_drive_limit_is_far_above_one_expander(self) -> None:
        self.assertGreaterEqual(supermicro_bmc.BMC_MAX_DRIVES_PER_CONTROLLER, 4 * 1024)

    def test_drives_at_the_limit_are_read_and_one_more_is_refused_with_counts(self) -> None:
        limit = supermicro_bmc.BMC_MAX_DRIVES_PER_CONTROLLER

        at_limit, _calls = self._fetch(controllers="1", drives_per_controller=limit)
        self.assertTrue(at_limit.source_details["web_xml_ok"])
        self.assertEqual(len(at_limit.drives), limit)
        self.assertEqual(at_limit.warnings, [])

        over, _calls = self._fetch(controllers="1", drives_per_controller=limit + 1)
        self.assertFalse(over.source_details["web_xml_ok"])
        self.assertEqual(over.drives, [])
        self.assertEqual(len(over.warnings), 1)
        warning = over.warnings[0]
        self.assertIn(f"{limit + 1:,} physical drives on controller 0", warning)
        self.assertIn(f"{limit:,}", warning)
        self.assertIn("not read", warning)

    def test_many_controllers_are_read(self) -> None:
        inventory, calls = self._fetch(controllers="64", drives_per_controller=1)

        self.assertTrue(inventory.source_details["web_xml_ok"])
        self.assertEqual(inventory.warnings, [])
        self.assertEqual(len(inventory.controllers), 64)
        self.assertEqual(len(inventory.drives), 64)
        # Two fixed web requests, then three per controller: the request budget.
        self.assertEqual(len(calls), 2 + 3 * 64)

    def test_controller_count_over_the_limit_is_refused_with_counts_before_more_requests(self) -> None:
        limit = supermicro_bmc.BMC_MAX_CONTROLLERS
        for reported, shown in ((str(limit + 1), f"{limit + 1:,}"), ("1000000000", "more than 1,000,000")):
            with self.subTest(reported=reported):
                inventory, calls = self._fetch(controllers=reported)

                self.assertEqual(calls, [CONTROLLER_ATTR])
                self.assertFalse(inventory.source_details["web_xml_ok"])
                self.assertEqual(inventory.controllers, [])
                self.assertEqual(len(inventory.warnings), 1)
                warning = inventory.warnings[0]
                self.assertIn(f"reported {shown} Broadcom controllers", warning)
                self.assertIn(f"1 to {limit:,}", warning)
                self.assertIn("not read", warning)


if __name__ == "__main__":
    unittest.main()
