"""Redfish fields the BMC reader consumes are type-checked (Refs #1312).

A Redfish resource that is a JSON object but carries a wrong-typed field the
reader uses must end as the typed ``response_invalid`` failure and the fixed
BMC warning, never as an internal error. Synthetic in-memory replies only.
"""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

from app.config import BMCConfig
from app.services.supermicro_bmc import BMCInventory, SupermicroBMCService
from app.services.truenas_ws import TrueNASAPIError, classify_remote_failure

SYSTEM_PATH = "/redfish/v1/Systems/1"
STORAGE_PATH = "/redfish/v1/Systems/1/Storage"
SIMPLE_STORAGE_1 = "/redfish/v1/Systems/1/SimpleStorage/1"
SIMPLE_STORAGE_2 = "/redfish/v1/Systems/1/SimpleStorage/2"
REDFISH_UNAVAILABLE = "BMC Redfish inventory unavailable."
FALLBACK_UNUSABLE = "BMC Redfish storage fallback returned an unusable response."


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, *_args: object) -> bytes:
        return self._body


class _RedfishOpener:
    """Answers each Redfish path from an in-memory table of JSON objects."""

    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.paths: list[str] = []

    def open(self, request, timeout=None) -> _Response:  # noqa: ARG002 - opener signature
        path = request.full_url.split("://", 1)[1].split("/", 1)[1]
        path = "/" + path
        self.paths.append(path)
        if path not in self.replies:
            raise OSError("synthetic: no such resource")
        return _Response(json.dumps(self.replies[path]).encode("utf-8"))


def _service() -> SupermicroBMCService:
    return SupermicroBMCService(
        BMCConfig(enabled=True, host="bmc.example.test", username="synthetic-user", password="synthetic-password")
    )


class RedfishSystemFieldTypeTests(unittest.TestCase):
    def _fetch(self, system: dict[str, Any]):
        service = _service()
        opener = _RedfishOpener({SYSTEM_PATH: system, STORAGE_PATH: {}})
        with (
            patch.object(service, "_build_https_opener", return_value=opener),
            patch.object(service, "_populate_web_inventory"),
        ):
            return service.fetch_inventory()

    def test_wrong_typed_system_text_field_is_response_invalid(self) -> None:
        for key, value in (("Model", []), ("SerialNumber", 5), ("IndicatorLED", {"x": 1}), ("Model", True)):
            with self.subTest(key=key, value=value):
                system: dict[str, Any] = {"Model": "synthetic-model", "SerialNumber": "SYN-0001", "IndicatorLED": "Off"}
                system[key] = value
                with self.assertLogs("app.services.supermicro_bmc", level="WARNING") as logs:
                    inventory = self._fetch(system)
                self.assertEqual(inventory.warnings, [REDFISH_UNAVAILABLE])
                self.assertIs(inventory.source_details["redfish_ok"], False)
                self.assertIsNone(inventory.system_model)
                self.assertTrue(any("category=response_invalid" in line for line in logs.output), logs.output)

    def test_reader_raises_typed_response_invalid(self) -> None:
        service = _service()
        opener = _RedfishOpener({SYSTEM_PATH: {"Model": []}, STORAGE_PATH: {}})
        with patch.object(service, "_build_https_opener", return_value=opener):
            with self.assertRaises(TrueNASAPIError) as caught:
                service._populate_redfish_inventory(BMCInventory())
        self.assertEqual(caught.exception.category, "response_invalid")
        self.assertEqual(classify_remote_failure(caught.exception)[0], "response_invalid")

    def test_valid_and_absent_text_fields_are_kept(self) -> None:
        inventory = self._fetch({"Model": " synthetic-model ", "SerialNumber": None})
        self.assertEqual(inventory.warnings, [])
        self.assertEqual(inventory.system_model, "synthetic-model")
        self.assertIsNone(inventory.system_serial)
        self.assertIsNone(inventory.system_indicator_led)
        self.assertIs(inventory.source_details["redfish_ok"], True)


class RedfishSimpleStorageFallbackTypeTests(unittest.TestCase):
    def _fetch(self, first: Any, second: Any | None = None):
        service = _service()
        replies: dict[str, Any] = {SYSTEM_PATH: {"Model": "synthetic-model"}, STORAGE_PATH: {}, SIMPLE_STORAGE_1: first}
        if second is not None:
            replies[SIMPLE_STORAGE_2] = second
        opener = _RedfishOpener(replies)
        with (
            patch.object(service, "_build_https_opener", return_value=opener),
            patch.object(service, "_populate_web_inventory", side_effect=TrueNASAPIError("synthetic web failure")),
            self.assertLogs("app.services.supermicro_bmc", level="WARNING"),
        ):
            return service.fetch_inventory()

    @staticmethod
    def _device(name: str, **overrides: Any) -> dict[str, Any]:
        device = {"Name": name, "Manufacturer": "Synthetic", "Model": "SYN-DISK", "Status": {"Health": "OK"}}
        device.update(overrides)
        return device

    def test_wrong_typed_fallback_member_is_reported_and_other_path_kept(self) -> None:
        good = {"Description": "SATA", "Devices": [self._device("Physical Drive 3")]}
        bad_cases = (
            {"Devices": "not-a-collection"},
            {"Devices": {"Name": "Physical Drive 1"}},
            {"Description": ["SATA"], "Devices": [self._device("Physical Drive 1")]},
            {"Devices": [self._device("Physical Drive 1", Model=[])]},
            {"Devices": [self._device("Physical Drive 1", Name=7)]},
            {"Devices": [self._device("Physical Drive 1", Status={"Health": 5})]},
            {"Devices": [self._device("Physical Drive 1"), self._device("Physical Drive 2", Manufacturer={})]},
        )
        for bad in bad_cases:
            with self.subTest(bad=bad):
                inventory = self._fetch(bad, good)
                self.assertIn(FALLBACK_UNUSABLE, inventory.warnings)
                # The unusable resource adds no partial drives; the usable one is kept.
                self.assertEqual([drive.slot_number for drive in inventory.drives], [3])
                self.assertEqual(inventory.drives[0].model, "SYN-DISK")

    def test_valid_fallback_devices_are_read(self) -> None:
        inventory = self._fetch(
            {"Description": "SATA", "Devices": [self._device("Physical Drive 1", Status=None), "skipped", None]},
            {"Devices": None},
        )
        self.assertNotIn(FALLBACK_UNUSABLE, inventory.warnings)
        self.assertEqual(len(inventory.drives), 1)
        drive = inventory.drives[0]
        self.assertEqual((drive.slot_number, drive.vendor, drive.model, drive.health, drive.interface_type),
                         (1, "Synthetic", "SYN-DISK", None, "SATA"))


if __name__ == "__main__":
    unittest.main()
