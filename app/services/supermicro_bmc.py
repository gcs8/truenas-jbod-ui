from __future__ import annotations

import base64
import http.cookiejar
import json
import logging
import re
import ssl
import xml.etree.ElementTree as ET
from contextvars import ContextVar
from dataclasses import dataclass, field
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener

from app.config import BMCConfig, normalize_text, normalize_value_text
from app.services.tls_context import SameOriginRedirectHandler
from app.services.truenas_ws import (
    INTERNAL_FAILURE_CATEGORY,
    LocalServiceError,
    TrueNASAPIError,
    classify_remote_failure,
    error_body_read_category,
    is_local_failure_message,
    log_failure,
)

logger = logging.getLogger(__name__)

SUPER_MICRO_STORAGE_PAGE = "/cgi/url_redirect.cgi?url_name=sys_storage"
SUPER_MICRO_LOGIN_PATH = "/cgi/login.cgi"
# Only the Redfish error object's message is kept, as the error's separate
# remote reason; the body is never part of the error message.
REDFISH_ERROR_BODY_LIMIT = 64 * 1024
# #737: bound web-XML discovery. One inventory pass makes four fixed requests
# plus three per controller, so the controller cap is also its request budget
# (196 requests at the cap). #1309: both counts only stop a runaway reply and
# sit far above real hardware: 64 controllers is more than one host has PCIe
# slots for, and 4,096 drives per controller is four full SAS expanders of
# 1,024 disks each. A reply over either is refused with a warning that names
# the count and the limit.
BMC_MAX_CONTROLLERS = 64
BMC_MAX_DRIVES_PER_CONTROLLER = 4096
# #1309: the drive count of one whole BMC inventory, across all controllers
# (and across the Redfish fallback resources). One BMC manages one host; this
# is eight full 1,024-disk expanders. It bounds the drive and disk records one
# inventory pass keeps in memory, which the two limits above do not.
BMC_MAX_DRIVES_PER_INVENTORY = 8192
# Every BMC reply body is read up to this many bytes and refused, unparsed, past
# it. A drive record is about 600 bytes of XML, so the largest reply a
# controller at its drive limit sends is about 2.5 MB; this admits 1,000 bytes
# per drive there.
BMC_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
# All web replies of one inventory pass together, sized the same way from the
# inventory drive limit. It bounds what one pass keeps, however the replies are
# shaped.
BMC_MAX_INVENTORY_RESPONSE_BYTES = 12 * 1024 * 1024
# A reported count is shown in a warning; past this it is shown as a bound.
BMC_SHOWN_COUNT_LIMIT = 1_000_000
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


def bmc_request_url(base_url: str, path: str) -> str:
    """The URL a BMC request goes to: the request path appended to the base URL as text.

    Everything the base URL carries is kept, a query included (a gateway in
    front of several BMCs can route by it). The persisted-detail binding
    reads where requests go from this function too.
    """
    return base_url + path


def _int_from_hex(value: Any) -> int | None:
    text = normalize_value_text(value)
    if not text or text == "0":
        return 0 if text == "0" else None
    try:
        return int(text, 16)
    except ValueError:
        return None


def _int_from_decimal(value: Any) -> int | None:
    text = normalize_value_text(value)
    if not text:
        return None
    try:
        return int(text, 10)
    except ValueError:
        return None


def _shown_count(count: int) -> str:
    """A BMC-reported count as warning text, never longer than the bound."""

    if count > BMC_SHOWN_COUNT_LIMIT:
        return f"more than {BMC_SHOWN_COUNT_LIMIT:,}"
    return f"{count:,}"


@dataclass(slots=True)
class _ReplyByteBudget:
    """Bytes the web replies of one inventory pass may still use."""

    remaining: int


_WEB_INVENTORY_BYTES: ContextVar[_ReplyByteBudget | None] = ContextVar("bmc_web_inventory_bytes", default=None)


def _read_reply(response: Any, path: str) -> bytes:
    """A BMC reply body, read no further than its byte limit; past it, refused unread."""

    budget = _WEB_INVENTORY_BYTES.get()
    limit = BMC_MAX_RESPONSE_BYTES if budget is None else min(BMC_MAX_RESPONSE_BYTES, budget.remaining)
    body = response.read(limit + 1)
    if len(body) > limit:
        if limit < BMC_MAX_RESPONSE_BYTES:
            message = (
                f"Supermicro BMC web inventory replies came to more than the supported "
                f"{BMC_MAX_INVENTORY_RESPONSE_BYTES:,} bytes for one inventory; "
                "the reply that passed it and any later one were not read."
            )
        else:
            message = (
                f"Supermicro BMC sent a reply larger than the supported {BMC_MAX_RESPONSE_BYTES:,} bytes; "
                "it was not read, and nothing after it was requested."
            )
        raise LocalServiceError(message, category="response_invalid", method=path)
    if budget is not None:
        budget.remaining -= len(body)
    return body


def _log_bmc_failure(operation: str, exc: BaseException) -> None:
    log_failure(logger, operation, exc)


def _redfish_error_message(body: bytes) -> str | None:
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error_block = payload.get("error")
    if not isinstance(error_block, dict):
        return None
    message = error_block.get("message")
    # The reason is optional text; a non-string message is ignored so the
    # typed HTTP failure stands (#1312).
    if not isinstance(message, str):
        return None
    return normalize_text(message)


def _redfish_unusable(path: str) -> TrueNASAPIError:
    return TrueNASAPIError(
        "Redfish returned an unusable response.",
        category="response_invalid",
        method=path,
    )


def _redfish_text(payload: dict[str, Any], key: str, path: str) -> str | None:
    """A Redfish text member: absent or null is unknown; a non-string is unusable (#1312)."""

    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _redfish_unusable(path)
    return normalize_text(value)


def _redfish_members(payload: dict[str, Any], key: str, path: str) -> list[Any]:
    """A Redfish collection member: absent or null is empty; a non-array is unusable (#1312)."""

    value = payload.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise _redfish_unusable(path)
    return value


def _bmc_warning(exc: TrueNASAPIError, operation: str, fixed_text: str) -> str:
    """This service's own sentence as it is; anything from the BMC as fixed text."""

    if is_local_failure_message(exc):
        return str(exc)
    if classify_remote_failure(exc)[0] == INTERNAL_FAILURE_CATEGORY:
        return f"{operation} stopped on an internal error; see the service log."
    return fixed_text


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
            _log_bmc_failure("BMC Redfish inventory", exc)
            inventory.warnings.append(_bmc_warning(exc, "BMC Redfish inventory", "BMC Redfish inventory unavailable."))
            inventory.source_details["redfish_ok"] = False

        try:
            self._populate_web_inventory(inventory)
        except TrueNASAPIError as exc:
            _log_bmc_failure("BMC web inventory", exc)
            inventory.warnings.append(_bmc_warning(exc, "BMC web inventory", "BMC web inventory unavailable."))
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
            raise LocalServiceError("Supermicro BMC UID status did not return a UID_INFO node.", category="response_invalid")
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
            raise LocalServiceError("Supermicro BMC UID control did not confirm success.", category="response_invalid")
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
            raise LocalServiceError("Supermicro BMC drive identify request did not return success.", category="response_invalid")

    def _populate_redfish_inventory(self, inventory: BMCInventory) -> None:
        system_path = "/redfish/v1/Systems/1"
        system_payload = self._redfish_get_json(system_path)
        # Every consumed member is checked before any is applied.
        model = _redfish_text(system_payload, "Model", system_path)
        serial = _redfish_text(system_payload, "SerialNumber", system_path)
        indicator_led = _redfish_text(system_payload, "IndicatorLED", system_path)
        inventory.system_model = model or inventory.system_model
        inventory.system_serial = serial or inventory.system_serial
        inventory.system_indicator_led = indicator_led or inventory.system_indicator_led
        inventory.source_details["redfish_ok"] = True

        try:
            self._redfish_get_json("/redfish/v1/Systems/1/Storage")
        except TrueNASAPIError as exc:
            _log_bmc_failure("BMC Redfish storage inventory", exc)
            inventory.source_details["redfish_storage_ok"] = False
            inventory.source_details["redfish_storage_error"] = _bmc_warning(
                exc,
                "BMC Redfish storage inventory",
                "BMC Redfish storage inventory unavailable.",
            )
        else:
            inventory.source_details["redfish_storage_ok"] = True

    def _populate_redfish_simple_storage_fallback(self, inventory: BMCInventory) -> None:
        for path in ("/redfish/v1/Systems/1/SimpleStorage/1", "/redfish/v1/Systems/1/SimpleStorage/2"):
            try:
                drives = self._read_redfish_simple_storage(path, BMC_MAX_DRIVES_PER_INVENTORY - len(inventory.drives))
            except TrueNASAPIError as exc:
                if exc.category == "response_invalid":
                    # An unusable resource adds no drives, and says so (#1312);
                    # a limit this service applies says which one.
                    _log_bmc_failure("BMC Redfish storage fallback", exc)
                    warning = (
                        str(exc)
                        if is_local_failure_message(exc)
                        else "BMC Redfish storage fallback returned an unusable response."
                    )
                    if warning not in inventory.warnings:
                        inventory.warnings.append(warning)
                continue
            inventory.drives.extend(drives)

    def _read_redfish_simple_storage(
        self, path: str, drive_limit: int = BMC_MAX_DRIVES_PER_INVENTORY
    ) -> list[BMCDriveRecord]:
        """One SimpleStorage resource's drives, all or none: a wrong-typed member is unusable."""

        payload = self._redfish_get_json(path)
        interface_type = _redfish_text(payload, "Description", path)
        devices = _redfish_members(payload, "Devices", path)
        if len(devices) > drive_limit:
            already = BMC_MAX_DRIVES_PER_INVENTORY - drive_limit
            raise LocalServiceError(
                f"BMC Redfish storage fallback reported {len(devices):,} devices in one storage resource, "
                f"which brings this inventory to {already + len(devices):,}, more than the supported "
                f"{BMC_MAX_DRIVES_PER_INVENTORY:,} for one BMC; {already:,} drives were read before it, "
                "and that resource was not read.",
                category="response_invalid",
                method=path,
            )
        drives: list[BMCDriveRecord] = []
        for index, device in enumerate(devices):
            if not isinstance(device, dict):
                continue
            name = _redfish_text(device, "Name", path)
            slot_number = None
            if name:
                match = re.search(r"Physical Drive\s+(\d+)", name)
                if match:
                    slot_number = int(match.group(1))
            status = device.get("Status")
            drives.append(
                BMCDriveRecord(
                    controller_id=0,
                    physical_index=index,
                    slot_number=slot_number,
                    enclosure_id=None,
                    vendor=_redfish_text(device, "Manufacturer", path),
                    model=_redfish_text(device, "Model", path) or name,
                    serial=None,
                    size_bytes=None,
                    health=_redfish_text(status, "Health", path) if isinstance(status, dict) else None,
                    interface_type=interface_type,
                    identify_active=False,
                    raw={"redfish_simple_storage": device},
                )
            )
        return drives

    def _populate_web_inventory(self, inventory: BMCInventory) -> None:
        # The login and storage-page replies belong to this pass, so they share
        # its byte budget with the inventory XML.
        token = _WEB_INVENTORY_BYTES.set(_ReplyByteBudget(BMC_MAX_INVENTORY_RESPONSE_BYTES))
        try:
            opener, csrf = self._open_web_session()
            self._read_web_inventory(inventory, opener, csrf)
        finally:
            _WEB_INVENTORY_BYTES.reset(token)

    def _read_web_inventory(self, inventory: BMCInventory, opener, csrf: str) -> None:
        drives_reported = 0
        ctrl_attr_root = self._post_web_xml(
            opener,
            csrf,
            "/cgi/ipmi.cgi",
            {"op": "Get_BrcmStorCtrlAttr.XML", "r": "(0,0)"},
        )
        ctrl_attr_node = ctrl_attr_root.find("BrcmStorCtrlAttr")
        controller_count = _int_from_decimal(ctrl_attr_node.get("ctrl_found_num")) if ctrl_attr_node is not None else None
        if not controller_count:
            raise LocalServiceError("Supermicro BMC did not report any Broadcom controllers.", category="response_invalid")
        # #737: the reported count sets how many requests follow (three per
        # controller), so an implausible value is refused before any of them.
        if not 0 < controller_count <= BMC_MAX_CONTROLLERS:
            raise LocalServiceError(
                f"Supermicro BMC reported {_shown_count(controller_count)} Broadcom controllers "
                f"(supported: 1 to {BMC_MAX_CONTROLLERS:,}); the BMC web inventory was not read.",
                category="response_invalid",
            )

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
            drive_nodes = hdd_info.findall("HDD")
            if len(drive_nodes) > BMC_MAX_DRIVES_PER_CONTROLLER:
                raise LocalServiceError(
                    f"Supermicro BMC reported {len(drive_nodes):,} physical drives on controller {controller_id}, "
                    f"more than the supported {BMC_MAX_DRIVES_PER_CONTROLLER:,}; "
                    f"drives on that and any later controller were not read.",
                    category="response_invalid",
                )
            if drives_reported + len(drive_nodes) > BMC_MAX_DRIVES_PER_INVENTORY:
                raise LocalServiceError(
                    f"Supermicro BMC reported {len(drive_nodes):,} physical drives on controller {controller_id}, "
                    f"which brings this inventory to {drives_reported + len(drive_nodes):,}, more than the supported "
                    f"{BMC_MAX_DRIVES_PER_INVENTORY:,} for one BMC; the {drives_reported:,} drives on earlier "
                    f"controllers were read, drives on that and any later controller were not read.",
                    category="response_invalid",
                )
            drives_reported += len(drive_nodes)
            for physical_index, drive_node in enumerate(drive_nodes):
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
            raise LocalServiceError("BMC host is not configured.")
        request = Request(
            bmc_request_url(self.base_url, path),
            headers={
                "Accept": "application/json",
                **self._basic_auth_header,
            },
        )
        try:
            with self._build_https_opener().open(request, timeout=self.config.timeout_seconds) as response:
                body = _read_reply(response, path)
            payload = json.loads(body.decode("utf-8", "replace"))
        except HTTPError as exc:
            try:
                with exc:
                    body = exc.read(REDFISH_ERROR_BODY_LIMIT)
            except (OSError, HTTPException) as read_exc:
                raise TrueNASAPIError(
                    f"Redfish request failed with HTTP {exc.code}; error response could not be read.",
                    code=exc.code,
                    category=error_body_read_category(read_exc),
                    method=path,
                ) from read_exc
            category = "authentication_failed" if exc.code in {401, 403} else "remote_error"
            raise TrueNASAPIError(
                f"Redfish request failed with HTTP {exc.code}.",
                code=exc.code,
                category=category,
                method=path,
                reason=_redfish_error_message(body),
            ) from exc
        except json.JSONDecodeError as exc:
            raise TrueNASAPIError(
                "Redfish returned invalid JSON.",
                category="response_invalid",
                method=path,
            ) from exc
        except (URLError, OSError) as exc:
            category, _code = classify_remote_failure(exc)
            raise TrueNASAPIError(
                "Redfish request failed.",
                category=category,
                method=path,
                reason=str(exc),
            ) from exc
        # Every Redfish resource is a JSON object; any other root is unusable.
        if not isinstance(payload, dict):
            raise TrueNASAPIError(
                "Redfish returned an unusable response.",
                category="response_invalid",
                method=path,
            )
        return payload

    def _open_web_session(self):
        if not self.base_url:
            raise LocalServiceError("BMC host is not configured.")
        cookie_jar = http.cookiejar.CookieJar()
        opener = self._build_https_opener(cookie_jar)
        login_body = urlencode({"name": self.config.username, "pwd": self.config.password}).encode("utf-8")
        request = Request(
            bmc_request_url(self.base_url, SUPER_MICRO_LOGIN_PATH),
            data=login_body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with opener.open(request, timeout=self.config.timeout_seconds) as response:
                body = _read_reply(response, SUPER_MICRO_LOGIN_PATH).decode("utf-8", "replace")
        except (HTTPError, URLError, OSError) as exc:
            category, code = classify_remote_failure(exc)
            raise TrueNASAPIError(
                "Supermicro BMC web login failed.",
                code=code,
                category=category,
                method=SUPER_MICRO_LOGIN_PATH,
                reason=str(exc),
            ) from exc
        if not list(cookie_jar):
            raise LocalServiceError(
                "Supermicro BMC web login did not establish a session cookie.",
                category="authentication_failed",
                method=SUPER_MICRO_LOGIN_PATH,
            )
        if "Invalid Username or Password" in body:
            raise LocalServiceError(
                "Supermicro BMC rejected the configured username or password.",
                category="authentication_failed",
                method=SUPER_MICRO_LOGIN_PATH,
            )
        page = self._fetch_web_text(opener, bmc_request_url(self.base_url, SUPER_MICRO_STORAGE_PAGE))
        csrf_match = re.search(r'SmcCsrfInsert\s*\(\s*"CSRF-TOKEN"\s*,\s*"([^"]+)"', page)
        if not csrf_match:
            raise LocalServiceError("Supermicro BMC storage page did not expose a CSRF token.", category="response_invalid")
        return opener, csrf_match.group(1)

    def _post_web_xml(self, opener, csrf: str, path: str, params: dict[str, Any]) -> ET.Element:
        text = self._post_web_text(opener, path, params, csrf=csrf, referer=self.base_url + SUPER_MICRO_STORAGE_PAGE)
        try:
            return ET.fromstring(text)
        except ET.ParseError as exc:
            raise LocalServiceError(
                "Supermicro BMC returned invalid XML.",
                category="response_invalid",
                method=path,
            ) from exc

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
            bmc_request_url(self.base_url, path),
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
                return _read_reply(response, path).decode("utf-8", "replace")
        except (HTTPError, URLError, OSError) as exc:
            category, code = classify_remote_failure(exc)
            raise TrueNASAPIError(
                "Supermicro BMC request failed.",
                code=code,
                category=category,
                method=path,
                reason=str(exc),
            ) from exc

    def _fetch_web_text(self, opener, url: str) -> str:
        try:
            with opener.open(url, timeout=self.config.timeout_seconds) as response:
                return _read_reply(response, urlsplit(url).path).decode("utf-8", "replace")
        except (HTTPError, URLError, OSError) as exc:
            category, code = classify_remote_failure(exc)
            raise TrueNASAPIError(
                "Supermicro BMC request failed.",
                code=code,
                category=category,
                method=urlsplit(url).path,
                reason=str(exc),
            ) from exc

    def _build_https_opener(self, cookie_jar: http.cookiejar.CookieJar | None = None):
        context = ssl.create_default_context() if self.config.verify_ssl else ssl._create_unverified_context()
        # Basic auth, the session cookie and the CSRF token follow same-origin
        # redirects only (#737).
        handlers: list[Any] = [HTTPSHandler(context=context), SameOriginRedirectHandler()]
        if cookie_jar is not None:
            handlers.insert(0, HTTPCookieProcessor(cookie_jar))
        return build_opener(*handlers)
