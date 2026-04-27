from __future__ import annotations

import base64
import http.cookiejar
import json
import logging
import re
import ssl
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener

from app.config import BMCConfig, normalize_text
from app.services.truenas_ws import TrueNASAPIError

logger = logging.getLogger(__name__)

SUPER_MICRO_STORAGE_PAGE = "/cgi/url_redirect.cgi?url_name=sys_storage"
DRIVE_LINK_SPEEDS = {
    1: "1.5 Gbps",
    2: "3.0 Gbps",
    3: "6.0 Gbps",
    4: "12.0 Gbps",
}


@dataclass(slots=True)
class BMCControllerRecord:
    controller_id: int
    status: str | None = None
    product_name: str | None = None
    serial: str | None = None
    firmware_version: str | None = None
    bios_version: str | None = None
    package_version: str | None = None
    jbod_enabled: bool | None = None
    location: str | None = None
    logical_drive_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BMCDriveRecord:
    controller_id: int
    physical_index: int
    slot_number: int | None = None
    enclosure_id: str | None = None
    vendor: str | None = None
    model: str | None = None
    firmware: str | None = None
    serial: str | None = None
    size_bytes: int | None = None
    health: str | None = None
    link_speed: str | None = None
    temperature_c: int | None = None
    interface_type: str | None = None
    media_type: str | None = None
    identify_active: bool = False
    connected_logical_drive: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BMCInventory:
    system_model: str | None = None
    system_serial: str | None = None
    system_indicator_led: str | None = None
    uid_active: bool | None = None
    controllers: list[BMCControllerRecord] = field(default_factory=list)
    drives: list[BMCDriveRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_details: dict[str, Any] = field(default_factory=dict)


def _normalize_bmc_host(host: str) -> str:
    trimmed = normalize_text(host) or ""
    if not trimmed:
        return ""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", trimmed):
        return trimmed.rstrip("/")
    return f"https://{trimmed.rstrip('/')}"


def _int_from_hex(value: Any) -> int | None:
    text = normalize_text(str(value) if value is not None else None)
    if not text or text == "0":
        return 0 if text == "0" else None
    try:
        return int(text, 16)
    except ValueError:
        return None


def _int_from_decimal(value: Any) -> int | None:
    text = normalize_text(str(value) if value is not None else None)
    if not text:
        return None
    try:
        return int(text, 10)
    except ValueError:
        return None


def _bool_from_indicator_led(value: Any) -> bool:
    return (normalize_text(str(value) if value is not None else None) or "").lower() not in {"", "off"}


class SupermicroBMCService:
    def __init__(self, config: BMCConfig) -> None:
        self.config = config
        self.base_url = _normalize_bmc_host(config.host)
        self._basic_auth_header = {
            "Authorization": "Basic "
            + base64.b64encode(f"{config.username}:{config.password}".encode("utf-8")).decode("ascii")
        }

    def fetch_inventory(self) -> BMCInventory:
        inventory = BMCInventory()
        if not self.config.enabled or not self.base_url:
            return inventory

        try:
            self._populate_redfish_inventory(inventory)
        except TrueNASAPIError as exc:
            inventory.warnings.append(str(exc))
            inventory.source_details["redfish_ok"] = False

        try:
            self._populate_web_inventory(inventory)
        except TrueNASAPIError as exc:
            inventory.warnings.append(str(exc))
            inventory.source_details["web_xml_ok"] = False
            if not inventory.drives:
                self._populate_redfish_simple_storage_fallback(inventory)

        inventory.controllers.sort(key=lambda item: item.controller_id)
        inventory.drives.sort(
            key=lambda item: (
                item.controller_id,
                item.slot_number if isinstance(item.slot_number, int) else 1_000_000,
                item.physical_index,
            )
        )
        return inventory

    def get_uid_status(self) -> bool:
        opener, csrf = self._open_web_session()
        root = self._post_web_xml(opener, csrf, "/cgi/ipmi.cgi", {"op": "GET_UID_STATUS.XML", "r": "(0,0)"})
        uid_node = root.find("UID_INFO")
        if uid_node is None:
            raise TrueNASAPIError("Supermicro BMC UID status did not return a UID_INFO node.")
        return uid_node.get("uid_status") == "1"

    def set_uid_active(self, active: bool) -> bool:
        opener, csrf = self._open_web_session()
        response = self._post_web_text(
            opener,
            "/cgi/op.cgi",
            {"op": "misc_uid", "uid_setting": "1" if active else "0"},
            csrf=csrf,
            referer=self.base_url + SUPER_MICRO_STORAGE_PAGE,
        )
        if "ok" not in response.lower():
            raise TrueNASAPIError("Supermicro BMC UID control did not confirm success.")
        return self.get_uid_status()

    def set_drive_identify(self, controller_id: int, physical_index: int, active: bool) -> None:
        opener, csrf = self._open_web_session()
        root = self._post_web_xml(
            opener,
            csrf,
            "/cgi/ipmi.cgi",
            {
                "op": "Set_BrcmStorCtrlPhyDrvLocate.XML",
                "r": f"({int(controller_id)},{int(physical_index)})",
                "active": "1" if active else "0",
            },
        )
        result = root.find("RESULT")
        if result is None or normalize_text(result.get("STATUS")) != "SUCCESS":
            raise TrueNASAPIError("Supermicro BMC drive identify request did not return success.")

    def _populate_redfish_inventory(self, inventory: BMCInventory) -> None:
        system_payload = self._redfish_get_json("/redfish/v1/Systems/1")
        inventory.system_model = normalize_text(system_payload.get("Model")) or inventory.system_model
        inventory.system_serial = normalize_text(system_payload.get("SerialNumber")) or inventory.system_serial
        inventory.system_indicator_led = normalize_text(system_payload.get("IndicatorLED")) or inventory.system_indicator_led
        inventory.source_details["redfish_ok"] = True

        try:
            self._redfish_get_json("/redfish/v1/Systems/1/Storage")
        except TrueNASAPIError as exc:
            inventory.source_details["redfish_storage_ok"] = False
            inventory.source_details["redfish_storage_error"] = str(exc)
        else:
            inventory.source_details["redfish_storage_ok"] = True

    def _populate_redfish_simple_storage_fallback(self, inventory: BMCInventory) -> None:
        for path in ("/redfish/v1/Systems/1/SimpleStorage/1", "/redfish/v1/Systems/1/SimpleStorage/2"):
            try:
                payload = self._redfish_get_json(path)
            except TrueNASAPIError:
                continue
            devices = payload.get("Devices") or []
            for index, device in enumerate(devices):
                if not isinstance(device, dict):
                    continue
                name = normalize_text(device.get("Name"))
                slot_number = None
                if name:
                    match = re.search(r"Physical Drive\s+(\d+)", name)
                    if match:
                        slot_number = int(match.group(1))
                inventory.drives.append(
                    BMCDriveRecord(
                        controller_id=0,
                        physical_index=index,
                        slot_number=slot_number,
                        enclosure_id=None,
                        vendor=normalize_text(device.get("Manufacturer")),
                        model=normalize_text(device.get("Model")) or name,
                        serial=None,
                        size_bytes=None,
                        health=normalize_text(device.get("Status", {}).get("Health")) if isinstance(device.get("Status"), dict) else None,
                        interface_type=normalize_text(payload.get("Description")),
                        identify_active=False,
                        raw={"redfish_simple_storage": device},
                    )
                )

    def _populate_web_inventory(self, inventory: BMCInventory) -> None:
        opener, csrf = self._open_web_session()
        ctrl_attr_root = self._post_web_xml(
            opener,
            csrf,
            "/cgi/ipmi.cgi",
            {"op": "Get_BrcmStorCtrlAttr.XML", "r": "(0,0)"},
        )
        ctrl_attr_node = ctrl_attr_root.find("BrcmStorCtrlAttr")
        controller_count = _int_from_decimal(ctrl_attr_node.get("ctrl_found_num")) if ctrl_attr_node is not None else None
        if not controller_count:
            raise TrueNASAPIError("Supermicro BMC did not report any Broadcom controllers.")

        uid_root = self._post_web_xml(opener, csrf, "/cgi/ipmi.cgi", {"op": "GET_UID_STATUS.XML", "r": "(0,0)"})
        uid_node = uid_root.find("UID_INFO")
        if uid_node is not None:
            inventory.uid_active = uid_node.get("uid_status") == "1"

        for controller_id in range(controller_count):
            ctrl_info_root = self._post_web_xml(
                opener,
                csrf,
                "/cgi/ipmi.cgi",
                {"op": "Get_BrcmStorCtrlInfo.XML", "r": f"({controller_id},0)"},
            )
            ctrl_info_node = ctrl_info_root.find("CtrlInfo")
            logical_count = self._fetch_logical_drive_count(opener, csrf, controller_id)
            inventory.controllers.append(
                BMCControllerRecord(
                    controller_id=controller_id,
                    status=normalize_text(ctrl_info_node.get("Status")) if ctrl_info_node is not None else None,
                    product_name=normalize_text(ctrl_info_node.get("PN")) if ctrl_info_node is not None else None,
                    serial=normalize_text(ctrl_info_node.get("SN")) if ctrl_info_node is not None else None,
                    firmware_version=normalize_text(ctrl_info_node.get("FWVer")) if ctrl_info_node is not None else None,
                    bios_version=normalize_text(ctrl_info_node.get("BiosVer")) if ctrl_info_node is not None else None,
                    package_version=normalize_text(ctrl_info_node.get("PackVer")) if ctrl_info_node is not None else None,
                    jbod_enabled=ctrl_info_node.get("JBODMode") == "1" if ctrl_info_node is not None else None,
                    location=(
                        f"PCIe card: SXB{ctrl_info_node.get('PCIELocation')}, slot: {ctrl_info_node.get('PCIESlot')}"
                        if ctrl_info_node is not None and ctrl_info_node.get("PCIELocation") and ctrl_info_node.get("PCIESlot")
                        else None
                    ),
                    logical_drive_count=logical_count,
                    raw=dict(ctrl_info_node.attrib) if ctrl_info_node is not None else {},
                )
            )

            phy_root = self._post_web_xml(
                opener,
                csrf,
                "/cgi/ipmi.cgi",
                {"op": "Get_BrcmStorCtrlPhyDrvInfo.XML", "r": f"({controller_id},0)"},
            )
            hdd_info = phy_root.find("HDDInfo")
            if hdd_info is None:
                continue
            for physical_index, drive_node in enumerate(hdd_info.findall("HDD")):
                drive = self._parse_web_drive_node(controller_id, physical_index, drive_node)
                if drive is not None:
                    inventory.drives.append(drive)

        inventory.source_details["web_xml_ok"] = True

    def _fetch_logical_drive_count(self, opener, csrf: str, controller_id: int) -> int:
        root = self._post_web_xml(
            opener,
            csrf,
            "/cgi/ipmi.cgi",
            {"op": "Get_BrcmStorCtrlLogDrvInfo.XML", "r": f"({controller_id},0)"},
        )
        info = root.find("LogicHDDInfo")
        if info is None:
            return 0
        count = 0
        for logical_drive in info.findall("LogicHDD"):
            name = normalize_text(logical_drive.get("Name"))
            status = normalize_text(logical_drive.get("Status"))
            if name and name != "0" and status and status != "0":
                count += 1
        return count

    def _parse_web_drive_node(self, controller_id: int, physical_index: int, drive_node: ET.Element) -> BMCDriveRecord | None:
        vendor = normalize_text(drive_node.get("Vendor"))
        model = normalize_text(drive_node.get("ModelName"))
        serial = normalize_text(drive_node.get("SN"))
        status = normalize_text(drive_node.get("Status"))
        if not any(value and value != "0" for value in (vendor, model, serial)) and status in {None, "0"}:
            return None

        slot_number = _int_from_hex(drive_node.get("SlotNumber"))
        enclosure_id = _int_from_hex(drive_node.get("EnclosureID"))
        coerced_size_gb = _int_from_hex(drive_node.get("CoercedSize"))
        temperature_c = _int_from_hex(drive_node.get("Temperature"))
        link_speed_code = _int_from_hex(drive_node.get("LinkSpeed"))
        interface_type = normalize_text(drive_node.get("InterfaceType"))
        media_type = normalize_text(drive_node.get("MediaType"))
        raw = dict(drive_node.attrib)
        raw["controller_id"] = controller_id
        raw["physical_index"] = physical_index
        raw["slot_number"] = slot_number
        raw["enclosure_id_decimal"] = enclosure_id
        raw["logical_block_size"] = _int_from_hex(drive_node.get("SectorSize"))

        health = "ONLINE" if status == "1" else normalize_text(drive_node.get("FWState")) or "UNKNOWN"
        return BMCDriveRecord(
            controller_id=controller_id,
            physical_index=physical_index,
            slot_number=slot_number,
            enclosure_id=str(enclosure_id) if enclosure_id is not None else None,
            vendor=None if vendor == "0" else vendor,
            model=None if model == "0" else model,
            firmware=normalize_text(drive_node.get("Revision")),
            serial=None if serial == "0" else serial,
            size_bytes=(coerced_size_gb * 1_000_000_000) if coerced_size_gb is not None else None,
            health=health,
            link_speed=DRIVE_LINK_SPEEDS.get(link_speed_code) if link_speed_code is not None else None,
            temperature_c=temperature_c,
            interface_type=None if interface_type == "0" else interface_type,
            media_type=None if media_type == "0" else media_type,
            identify_active=drive_node.get("Locate") == "1",
            connected_logical_drive=normalize_text(drive_node.get("ConnectedLD")),
            raw=raw,
        )

    def _redfish_get_json(self, path: str) -> dict[str, Any]:
        if not self.base_url:
            raise TrueNASAPIError("BMC host is not configured.")
        request = Request(
            self.base_url + path,
            headers={
                "Accept": "application/json",
                **self._basic_auth_header,
            },
        )
        try:
            with self._build_https_opener().open(request, timeout=self.config.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                payload = None
            if isinstance(payload, dict):
                error_block = payload.get("error") or {}
                message = normalize_text(error_block.get("message"))
                if message:
                    raise TrueNASAPIError(message) from exc
            raise TrueNASAPIError(f"Redfish request failed with HTTP {exc.code} for {path}.") from exc
        except (URLError, OSError, json.JSONDecodeError) as exc:
            raise TrueNASAPIError(f"Redfish request failed for {path}: {exc}") from exc

    def _open_web_session(self):
        if not self.base_url:
            raise TrueNASAPIError("BMC host is not configured.")
        cookie_jar = http.cookiejar.CookieJar()
        opener = self._build_https_opener(cookie_jar)
        login_body = urlencode({"name": self.config.username, "pwd": self.config.password}).encode("utf-8")
        request = Request(
            self.base_url + "/cgi/login.cgi",
            data=login_body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with opener.open(request, timeout=self.config.timeout_seconds) as response:
                body = response.read().decode("utf-8", "replace")
        except (HTTPError, URLError, OSError) as exc:
            raise TrueNASAPIError(f"Supermicro BMC web login failed: {exc}") from exc
        if not list(cookie_jar):
            raise TrueNASAPIError("Supermicro BMC web login did not establish a session cookie.")
        if "Invalid Username or Password" in body:
            raise TrueNASAPIError("Supermicro BMC rejected the configured username or password.")
        page = self._fetch_web_text(opener, self.base_url + SUPER_MICRO_STORAGE_PAGE)
        csrf_match = re.search(r'SmcCsrfInsert\s*\(\s*"CSRF-TOKEN"\s*,\s*"([^"]+)"', page)
        if not csrf_match:
            raise TrueNASAPIError("Supermicro BMC storage page did not expose a CSRF token.")
        return opener, csrf_match.group(1)

    def _post_web_xml(self, opener, csrf: str, path: str, params: dict[str, Any]) -> ET.Element:
        text = self._post_web_text(opener, path, params, csrf=csrf, referer=self.base_url + SUPER_MICRO_STORAGE_PAGE)
        try:
            return ET.fromstring(text)
        except ET.ParseError as exc:
            raise TrueNASAPIError("Supermicro BMC returned invalid XML.") from exc

    def _post_web_text(
        self,
        opener,
        path: str,
        params: dict[str, Any],
        *,
        csrf: str,
        referer: str,
    ) -> str:
        request = Request(
            self.base_url + path,
            data=urlencode(params).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "CSRF-TOKEN": csrf,
                "Referer": referer,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        try:
            with opener.open(request, timeout=self.config.timeout_seconds) as response:
                return response.read().decode("utf-8", "replace")
        except (HTTPError, URLError, OSError) as exc:
            raise TrueNASAPIError(f"Supermicro BMC request failed for {path}: {exc}") from exc

    def _fetch_web_text(self, opener, url: str) -> str:
        try:
            with opener.open(url, timeout=self.config.timeout_seconds) as response:
                return response.read().decode("utf-8", "replace")
        except (HTTPError, URLError, OSError) as exc:
            raise TrueNASAPIError(f"Supermicro BMC request failed for {url}: {exc}") from exc

    def _build_https_opener(self, cookie_jar: http.cookiejar.CookieJar | None = None):
        context = ssl.create_default_context() if self.config.verify_ssl else ssl._create_unverified_context()
        handlers = [HTTPSHandler(context=context)]
        if cookie_jar is not None:
            handlers.insert(0, HTTPCookieProcessor(cookie_jar))
        return build_opener(*handlers)
