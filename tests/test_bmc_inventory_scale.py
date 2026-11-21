"""BMC web inventory count limits sit above real hardware (Refs #1309).

A controller can reach many shelves and one SAS expander can carry up to 1,024
disks, so a large healthy inventory must be read in full. The count limits only
stop runaway replies, and a reply that reaches one is refused with a page
warning that names the count and the limit, never a silent drop. The drive
count of a whole inventory and every reply body are bounded too, and a reply
over its byte limit is refused after a bounded read, unparsed. The real BMC
service runs against synthetic in-memory XML replies; the Redfish readers are
replaced with no-ops so the result is the web inventory alone.
"""

from __future__ import annotations

import json
import unittest
import xml.etree.ElementTree as ET
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

from app.config import BMCConfig
from app.services import supermicro_bmc
from app.services.supermicro_bmc import BMCInventory, SupermicroBMCService
from app.services.truenas_ws import LocalServiceError

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



# One drive node padded to the size each drive may take in a reply at the drive
# limits. Real replies measure about 600 bytes per drive.
DRIVE_ALLOWANCE_BYTES = 1000


def _padded_drive_nodes(count: int, controller_id: int) -> str:
    nodes = []
    for index in range(count):
        node = (
            f'<HDD Vendor="SANITIZED" ModelName="SANITIZED" SN="SANITIZED-{controller_id}-{index}" '
            f'Status="1" SlotNumber="{index:x}" Pad=""/>'
        )
        pad = DRIVE_ALLOWANCE_BYTES - len(node)
        nodes.append(node.replace('Pad=""', f'Pad="{"x" * pad}"'))
    return "".join(nodes)


def _service() -> SupermicroBMCService:
    return SupermicroBMCService(
        BMCConfig(enabled=True, host="https://bmc.example.test", username="synthetic", password="***")
    )


class _Reply:
    """An in-memory reply body that records every read size it was asked for."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.reads: list[Any] = []
        self.bytes_given = 0

    def __enter__(self) -> "_Reply":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, amount: Any = None) -> bytes:
        self.reads.append(amount)
        data = self.body if amount is None or amount < 0 else self.body[:amount]
        self.bytes_given += len(data)
        return data


class _WebOpener:
    """Answers BMC web requests by operation name from a function of the request."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.replies: list[tuple[str, _Reply]] = []

    def open(self, request, timeout=None) -> _Reply:  # noqa: ARG002 - opener signature
        params = {key: values[0] for key, values in parse_qs(request.data.decode("utf-8")).items()}
        reply = _Reply(self.answer(params).encode("utf-8"))
        self.replies.append((params["op"], reply))
        return reply


def _fetch_through_web_replies(answer) -> tuple[BMCInventory, _WebOpener]:
    """The real inventory pass, reply reads and XML parsing included; only the transport is synthetic."""

    service = _service()
    opener = _WebOpener(answer)
    service._open_web_session = MagicMock(return_value=(opener, "synthetic-csrf"))
    with (
        patch.object(SupermicroBMCService, "_populate_redfish_inventory", lambda _self, _inventory: None),
        patch.object(SupermicroBMCService, "_populate_redfish_simple_storage_fallback", lambda _self, _inventory: None),
    ):
        return service.fetch_inventory(), opener


def _controller(params: dict[str, str]) -> int:
    return int(params["r"].strip("()").split(",")[0])


class BMCInventoryDriveLimitTests(unittest.TestCase):
    """The drive count of one whole inventory is bounded across controllers."""

    def _fetch(self, drives_on: list[int] | int, controllers: int) -> tuple[BMCInventory, list[tuple[str, int]]]:
        service = _service()
        calls: list[tuple[str, int]] = []
        replies: dict[int, ET.Element] = {}

        def post_web_xml(_opener, _csrf, _path, params):
            op = params["op"]
            controller = _controller(params)
            calls.append((op, controller))
            if op == CONTROLLER_ATTR:
                return ET.fromstring(f'<IPMI><BrcmStorCtrlAttr ctrl_found_num="{controllers}"/></IPMI>')
            if op == PHYSICAL_DRIVES:
                count = drives_on[controller] if isinstance(drives_on, list) else drives_on
                if count not in replies:
                    replies[count] = ET.fromstring(f"<IPMI><HDDInfo>{_drive_nodes(count)}</HDDInfo></IPMI>")
                return replies[count]
            return ET.fromstring("<IPMI/>")

        service._open_web_session = MagicMock(return_value=(object(), "synthetic-csrf"))
        service._post_web_xml = post_web_xml
        with (
            patch.object(SupermicroBMCService, "_populate_redfish_inventory", lambda _self, _inventory: None),
            patch.object(SupermicroBMCService, "_populate_redfish_simple_storage_fallback", lambda _self, _inventory: None),
        ):
            return service.fetch_inventory(), calls

    def test_inventory_drive_limit_is_far_above_one_host_and_admits_a_full_controller(self) -> None:
        limit = supermicro_bmc.BMC_MAX_DRIVES_PER_INVENTORY
        self.assertGreaterEqual(limit, 8 * 1024)
        self.assertGreaterEqual(limit, 2 * supermicro_bmc.BMC_MAX_DRIVES_PER_CONTROLLER)

    def test_full_controllers_on_every_slot_stop_at_the_inventory_limit_with_counts(self) -> None:
        per_controller = supermicro_bmc.BMC_MAX_DRIVES_PER_CONTROLLER
        limit = supermicro_bmc.BMC_MAX_DRIVES_PER_INVENTORY
        controllers = supermicro_bmc.BMC_MAX_CONTROLLERS
        read_controllers = limit // per_controller

        inventory, calls = self._fetch(per_controller, controllers)

        self.assertFalse(inventory.source_details["web_xml_ok"])
        self.assertEqual(len(inventory.drives), limit)
        self.assertLessEqual(len(inventory.drives), limit)
        self.assertEqual({drive.controller_id for drive in inventory.drives}, set(range(read_controllers)))
        # Nothing is requested for any controller after the one that passed the limit.
        self.assertEqual(max(controller for _op, controller in calls), read_controllers)
        self.assertEqual([c for op, c in calls if op == PHYSICAL_DRIVES], list(range(read_controllers + 1)))
        self.assertEqual(len(inventory.warnings), 1)
        warning = inventory.warnings[0]
        self.assertIn(f"{per_controller:,} physical drives on controller {read_controllers}", warning)
        self.assertIn(f"brings this inventory to {limit + per_controller:,}", warning)
        self.assertIn(f"supported {limit:,} for one BMC", warning)
        self.assertIn(f"the {limit:,} drives on earlier controllers were read", warning)
        self.assertIn("drives on that and any later controller were not read", warning)

    def test_one_drive_past_the_inventory_limit_is_refused_and_counted(self) -> None:
        per_controller = supermicro_bmc.BMC_MAX_DRIVES_PER_CONTROLLER
        limit = supermicro_bmc.BMC_MAX_DRIVES_PER_INVENTORY
        shape = [per_controller] * (limit // per_controller) + [1]

        inventory, _calls = self._fetch(shape, len(shape))

        self.assertFalse(inventory.source_details["web_xml_ok"])
        self.assertEqual(len(inventory.drives), limit)
        self.assertEqual(len(inventory.warnings), 1)
        self.assertIn(f"brings this inventory to {limit + 1:,}", inventory.warnings[0])

    def test_inventory_at_and_just_under_the_limit_is_read_in_full(self) -> None:
        per_controller = supermicro_bmc.BMC_MAX_DRIVES_PER_CONTROLLER
        limit = supermicro_bmc.BMC_MAX_DRIVES_PER_INVENTORY
        controllers = supermicro_bmc.BMC_MAX_CONTROLLERS
        full = [per_controller] * (limit // per_controller)
        for name, shape in (
            ("at the limit", full),
            ("just under", full[:-1] + [per_controller - 1]),
            ("every controller", [limit // controllers] * controllers),
        ):
            with self.subTest(shape=name):
                inventory, _calls = self._fetch(shape, len(shape))

                self.assertTrue(inventory.source_details["web_xml_ok"])
                self.assertEqual(inventory.warnings, [])
                self.assertEqual(len(inventory.drives), sum(shape))


class BMCReplyByteLimitTests(unittest.TestCase):
    """Every BMC reply body is read only up to its byte limit and refused unparsed past it."""

    def test_limits_admit_the_drive_limits_at_1000_bytes_per_drive(self) -> None:
        per_controller = supermicro_bmc.BMC_MAX_DRIVES_PER_CONTROLLER
        self.assertGreaterEqual(supermicro_bmc.BMC_MAX_RESPONSE_BYTES, per_controller * DRIVE_ALLOWANCE_BYTES)
        self.assertGreater(
            supermicro_bmc.BMC_MAX_INVENTORY_RESPONSE_BYTES,
            supermicro_bmc.BMC_MAX_DRIVES_PER_INVENTORY * DRIVE_ALLOWANCE_BYTES,
        )

    def _full_inventory_answer(self, controllers: int, per_controller: int):
        def answer(params: dict[str, str]) -> str:
            if params["op"] == CONTROLLER_ATTR:
                return f'<IPMI><BrcmStorCtrlAttr ctrl_found_num="{controllers}"/></IPMI>'
            if params["op"] == PHYSICAL_DRIVES:
                return f"<IPMI><HDDInfo>{_padded_drive_nodes(per_controller, _controller(params))}</HDDInfo></IPMI>"
            return "<IPMI/>"

        return answer

    def test_full_inventory_at_1000_bytes_per_drive_is_read_through_the_real_reply_path(self) -> None:
        per_controller = supermicro_bmc.BMC_MAX_DRIVES_PER_CONTROLLER
        limit = supermicro_bmc.BMC_MAX_DRIVES_PER_INVENTORY
        for controllers, drives in ((limit // per_controller, per_controller), (64, limit // 64)):
            with self.subTest(controllers=controllers):
                inventory, opener = _fetch_through_web_replies(self._full_inventory_answer(controllers, drives))

                self.assertTrue(inventory.source_details["web_xml_ok"])
                self.assertEqual(inventory.warnings, [])
                self.assertEqual(len(inventory.drives), limit)
                largest = max(len(reply.body) for _op, reply in opener.replies)
                self.assertGreaterEqual(largest, drives * DRIVE_ALLOWANCE_BYTES)

    def test_oversized_web_reply_is_refused_unparsed_after_a_bounded_read(self) -> None:
        limit = supermicro_bmc.BMC_MAX_RESPONSE_BYTES

        def answer(params: dict[str, str]) -> str:
            if params["op"] == CONTROLLER_ATTR:
                return '<IPMI><BrcmStorCtrlAttr ctrl_found_num="1"/></IPMI>'
            if params["op"] == PHYSICAL_DRIVES:
                return "<IPMI><HDDInfo>" + " " * (2 * limit) + "</HDDInfo></IPMI>"
            return "<IPMI/>"

        parsed: list[int] = []
        real_fromstring = ET.fromstring

        def spy_fromstring(text, *args, **kwargs):
            parsed.append(len(text))
            return real_fromstring(text, *args, **kwargs)

        with patch.object(supermicro_bmc.ET, "fromstring", spy_fromstring):
            inventory, opener = _fetch_through_web_replies(answer)

        op, reply = opener.replies[-1]
        self.assertEqual(op, PHYSICAL_DRIVES)
        self.assertEqual(reply.reads, [limit + 1])
        self.assertEqual(reply.bytes_given, limit + 1)
        self.assertTrue(all(size <= limit for size in parsed))
        self.assertEqual(len(parsed), len(opener.replies) - 1)
        self.assertFalse(inventory.source_details["web_xml_ok"])
        self.assertEqual(
            inventory.warnings,
            [
                f"Supermicro BMC sent a reply larger than the supported {limit:,} bytes; "
                "it was not read, and nothing after it was requested."
            ],
        )

    def test_inventory_replies_past_the_pass_limit_are_refused_with_a_bounded_read(self) -> None:
        per_reply = supermicro_bmc.BMC_MAX_RESPONSE_BYTES - 1024
        total = supermicro_bmc.BMC_MAX_INVENTORY_RESPONSE_BYTES
        controllers = total // per_reply + 2

        def answer(params: dict[str, str]) -> str:
            if params["op"] == CONTROLLER_ATTR:
                return f'<IPMI><BrcmStorCtrlAttr ctrl_found_num="{controllers}"/></IPMI>'
            if params["op"] == PHYSICAL_DRIVES:
                head = f"<IPMI><HDDInfo>{_drive_nodes(1).replace('SANITIZED-0', f'SANITIZED-{_controller(params)}')}"
                return head + " " * (per_reply - len(head) - 17) + "</HDDInfo></IPMI>"
            return "<IPMI/>"

        inventory, opener = _fetch_through_web_replies(answer)

        sizes = [len(reply.body) for _op, reply in opener.replies]
        refused = opener.replies[-1][1]
        before = sum(sizes[:-1])
        self.assertLessEqual(before, total)
        self.assertEqual(refused.reads, [total - before + 1])
        self.assertLessEqual(before + refused.bytes_given, total + 1)
        self.assertFalse(inventory.source_details["web_xml_ok"])
        self.assertEqual(len(inventory.drives), total // per_reply)
        self.assertEqual(
            inventory.warnings,
            [
                f"Supermicro BMC web inventory replies came to more than the supported {total:,} bytes "
                "for one inventory; the reply that passed it and any later one were not read."
            ],
        )

    def test_session_setup_replies_count_toward_the_pass_limit(self) -> None:
        # The login and storage-page replies of an inventory pass are part of
        # that pass: together with the inventory XML they stay within one
        # pass limit, not one limit plus two more full replies.
        per_reply = supermicro_bmc.BMC_MAX_RESPONSE_BYTES - 1024
        total = supermicro_bmc.BMC_MAX_INVENTORY_RESPONSE_BYTES
        session_bytes = supermicro_bmc.BMC_MAX_RESPONSE_BYTES - 1
        controllers = total // per_reply + 2
        service = _service()

        def answer(params: dict[str, str]) -> str:
            if params["op"] == CONTROLLER_ATTR:
                return f'<IPMI><BrcmStorCtrlAttr ctrl_found_num="{controllers}"/></IPMI>'
            if params["op"] == PHYSICAL_DRIVES:
                head = f"<IPMI><HDDInfo>{_drive_nodes(1).replace('SANITIZED-0', f'SANITIZED-{_controller(params)}')}"
                return head + " " * (per_reply - len(head) - 17) + "</HDDInfo></IPMI>"
            return "<IPMI/>"

        opener = _WebOpener(answer)
        session_reply = _Reply(b" " * session_bytes)

        def open_web_session():
            supermicro_bmc._read_reply(session_reply, "/cgi/login.cgi")
            return opener, "synthetic-csrf"

        service._open_web_session = MagicMock(side_effect=open_web_session)
        with (
            patch.object(SupermicroBMCService, "_populate_redfish_inventory", lambda _self, _inventory: None),
            patch.object(SupermicroBMCService, "_populate_redfish_simple_storage_fallback", lambda _self, _inventory: None),
        ):
            inventory = service.fetch_inventory()

        xml_bytes = sum(reply.bytes_given for _op, reply in opener.replies)
        self.assertLessEqual(session_reply.bytes_given + xml_bytes, total + 1)
        self.assertFalse(inventory.source_details["web_xml_ok"])
        self.assertEqual(len(inventory.drives), (total - session_bytes) // per_reply)
        self.assertEqual(
            inventory.warnings,
            [
                f"Supermicro BMC web inventory replies came to more than the supported {total:,} bytes "
                "for one inventory; the reply that passed it and any later one were not read."
            ],
        )
        self.assertIsNone(supermicro_bmc._WEB_INVENTORY_BYTES.get())

    def test_every_other_reply_read_is_bounded(self) -> None:
        limit = supermicro_bmc.BMC_MAX_RESPONSE_BYTES
        oversized = b" " * (2 * limit)
        service = _service()

        reply = _Reply(oversized)
        opener = MagicMock()
        opener.open.return_value = reply
        with self.assertRaises(LocalServiceError):
            service._post_web_text(opener, "/cgi/op.cgi", {"op": "misc_uid"}, csrf="c", referer="r")
        self.assertEqual(reply.reads, [limit + 1])

        reply = _Reply(oversized)
        opener.open.return_value = reply
        with self.assertRaises(LocalServiceError):
            service._fetch_web_text(opener, "https://bmc.example.test/cgi/url_redirect.cgi")
        self.assertEqual(reply.reads, [limit + 1])

        reply = _Reply(oversized)
        opener.open.return_value = reply
        with (
            patch.object(service, "_build_https_opener", return_value=opener),
            self.assertRaises(LocalServiceError),
        ):
            service._open_web_session()
        self.assertEqual(reply.reads, [limit + 1])

        reply = _Reply(b"{" + oversized + b"}")
        opener.open.return_value = reply
        with (
            patch.object(service, "_build_https_opener", return_value=opener),
            patch.object(supermicro_bmc.json, "loads", side_effect=AssertionError("parsed")),
            self.assertRaises(LocalServiceError),
        ):
            service._redfish_get_json("/redfish/v1/Systems/1")
        self.assertEqual(reply.reads, [limit + 1])

    def test_oversized_redfish_reply_is_a_counted_warning(self) -> None:
        limit = supermicro_bmc.BMC_MAX_RESPONSE_BYTES
        service = _service()
        opener = MagicMock()
        opener.open.side_effect = lambda *_a, **_k: _Reply(b" " * (2 * limit))
        with (
            patch.object(service, "_build_https_opener", return_value=opener),
            patch.object(service, "_populate_web_inventory"),
        ):
            inventory = service.fetch_inventory()

        self.assertFalse(inventory.source_details["redfish_ok"])
        self.assertIn(f"larger than the supported {limit:,} bytes", inventory.warnings[0])


class RedfishFallbackDriveLimitTests(unittest.TestCase):
    """The Redfish storage fallback keeps to the same inventory drive limit."""

    def _fallback(self, first: int, second: int) -> tuple[BMCInventory, list[_Reply]]:
        service = _service()
        replies = {
            "/redfish/v1/Systems/1/SimpleStorage/1": first,
            "/redfish/v1/Systems/1/SimpleStorage/2": second,
        }
        read: list[Any] = []

        def open_(request, timeout=None):  # noqa: ARG001 - opener signature
            path = "/" + request.full_url.split("://", 1)[1].split("/", 1)[1]
            devices = [{"Name": f"Physical Drive {index}"} for index in range(replies[path])]
            reply = _Reply(json.dumps({"Description": "SAS", "Devices": devices}).encode("utf-8"))
            read.append(reply)
            return reply

        opener = MagicMock()
        opener.open.side_effect = open_
        inventory = BMCInventory()
        with patch.object(service, "_build_https_opener", return_value=opener):
            service._populate_redfish_simple_storage_fallback(inventory)
        return inventory, read

    def _assert_bounded_reads(self, read: list[_Reply]) -> None:
        self.assertEqual([reply.reads for reply in read], [[supermicro_bmc.BMC_MAX_RESPONSE_BYTES + 1]] * len(read))

    def test_fallback_at_the_limit_is_read(self) -> None:
        limit = supermicro_bmc.BMC_MAX_DRIVES_PER_INVENTORY
        inventory, read = self._fallback(limit - 10, 10)

        self.assertEqual(len(inventory.drives), limit)
        self.assertEqual(inventory.warnings, [])
        self._assert_bounded_reads(read)

    def test_fallback_past_the_limit_refuses_that_resource_with_counts(self) -> None:
        limit = supermicro_bmc.BMC_MAX_DRIVES_PER_INVENTORY
        for first, second, kept, total, already in (
            (limit + 1, 0, 0, limit + 1, 0),
            (limit - 10, 11, limit - 10, limit + 1, limit - 10),
        ):
            with self.subTest(first=first, second=second):
                inventory, read = self._fallback(first, second)

                self.assertEqual(len(inventory.drives), kept)
                self.assertLessEqual(len(inventory.drives), limit)
                self.assertEqual(len(inventory.warnings), 1)
                warning = inventory.warnings[0]
                self.assertIn(f"which brings this inventory to {total:,}", warning)
                self.assertIn(f"supported {limit:,} for one BMC", warning)
                self.assertIn(f"{already:,} drives were read before it", warning)
                self.assertIn("that resource was not read", warning)
                self._assert_bounded_reads(read)


if __name__ == "__main__":
    unittest.main()
