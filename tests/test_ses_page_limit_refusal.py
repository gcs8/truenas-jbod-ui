"""An SES page over the parser budget is a typed refusal, not "no enclosure" (#1308).

The ``sg_ses`` page readers refuse a page with more than ``MAX_SES_ELEMENTS``
element headers or more than ``MAX_SES_OUTPUT_CHARS`` characters. They used to
return ``None`` for it, the same answer as a page with no enclosure evidence, so
the host overlay dropped the page without a failure and the operator saw no
warning. A refused page now carries its reason and counts to the page warnings.

The pages are synthetic text and the SSH layer is a stub; no appliance is read.
"""

from __future__ import annotations

import asyncio
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

from app.config import Settings, SSHConfig, SystemConfig, TrueNASConfig
from app.models.domain import LedAction, SlotView
from app.services import parsers
from app.services.inventory import LINUX_ENCLOSURE_SYSFS_MAP_COMMAND, InventoryService
from app.services.mapping_store import MappingStore
from app.services.parsers import (
    parse_sg_ses_aes,
    parse_sg_ses_enclosure_status,
    parse_sg_ses_join_filter,
    parse_ssh_outputs,
)
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore
from app.services.ssh_probe import SSHCommandResult

HEADER = "  SYNTH     BIGSHELF      0100\n"


def join_page(count: int) -> str:
    return HEADER + "".join(
        f"Slot{element + 1:04d} [0,{element}]  Element type: Array device slot\n"
        "  Enclosure Status:\n"
        "    Predicted failure=0, Disabled=0, Swap=0, status: OK\n"
        "  Additional Element Status:\n"
        "    Transport protocol: SAS\n"
        f"    number of phys: 1, not all phys: 0, device slot number: {element + 1}\n"
        for element in range(count)
    )


def aes_page(count: int) -> str:
    return (
        HEADER
        + "Additional element status diagnostic page:\n"
        "  additional element status descriptor list\n"
        "    Element type: Array device slot, subenclosure id: 0 [ti=0]\n"
        + "".join(
            f"      Element index: {element}  eiioe=0\n"
            "        Transport protocol: SAS\n"
            f"        number of phys: 1, not all phys: 0, device slot number: {element + 1}\n"
            "        phy index: 0\n"
            f"          SAS address: 0x5aaaaaaa{element + 1:08x}\n"
            for element in range(count)
        )
    )


def ec_page(count: int) -> str:
    return (
        HEADER
        + "Enclosure Status diagnostic page:\n"
        "  status descriptor list\n"
        "    Element type: Array device slot, subenclosure id: 0 [ti=0]\n"
        + "".join(
            f"      Element {element} descriptor:\n"
            "        Predicted failure=0, Disabled=0, Swap=0, status: OK\n"
            "        Ready to insert=0, RMV=0, Ident=0, Report=0\n"
            for element in range(count)
        )
    )


READERS = {
    "aes": (parse_sg_ses_aes, aes_page, "sg_ses aes /dev/sg7"),
    "ec": (parse_sg_ses_enclosure_status, ec_page, "sg_ses ec /dev/sg7"),
    "join": (parse_sg_ses_join_filter, join_page, "sg_ses join /dev/sg7"),
}


def _slot_count(enclosure) -> int:  # noqa: ANN001
    return len(enclosure.slots) + len(enclosure.unmapped_slots)


class SgSesReaderRefusalTests(unittest.TestCase):
    def assert_refusal(self, result, *, page: str, reason: str, count: int, limit: int) -> None:  # noqa: ANN001
        refusal_type = getattr(parsers, "SESPageRefusal", None)
        self.assertIsNotNone(result, "a refused page reads the same as a page with no enclosure")
        self.assertIsNotNone(refusal_type, "the parser has no typed SES page refusal")
        self.assertIsInstance(result, refusal_type)
        self.assertEqual(
            (result.page, result.reason, result.count, result.limit, result.ses_device),
            (page, reason, count, limit, "/dev/sg7"),
        )

    def test_real_element_limit_is_inclusive_and_the_next_element_is_a_typed_refusal(self) -> None:
        limit = parsers.MAX_SES_ELEMENTS
        self.assertEqual(limit, 8192)
        accepted = parse_sg_ses_join_filter(join_page(limit), "sg_ses join /dev/sg7")
        self.assertIsInstance(accepted, parsers.SESMapEnclosure)
        self.assertEqual(_slot_count(accepted), limit)

        refused = parse_sg_ses_join_filter(join_page(limit + 1), "sg_ses join /dev/sg7")
        self.assert_refusal(refused, page="join", reason="elements", count=limit + 1, limit=limit)

    def test_every_sg_ses_reader_refuses_with_its_page_and_counts(self) -> None:
        for page, (reader, build, command) in READERS.items():
            with self.subTest(page=page), mock.patch.object(parsers, "MAX_SES_ELEMENTS", 4):
                accepted = reader(build(4), command)
                self.assertIsInstance(accepted, parsers.SESMapEnclosure)
                self.assertEqual(_slot_count(accepted), 4)
                # The count is the page's real element count, not the first one over.
                self.assert_refusal(reader(build(9), command), page=page, reason="elements", count=9, limit=4)

    def test_text_budget_refusal_carries_the_character_count(self) -> None:
        for page, (reader, build, command) in READERS.items():
            text = build(2)
            with self.subTest(page=page):
                with mock.patch.object(parsers, "MAX_SES_OUTPUT_CHARS", len(text)):
                    self.assertIsInstance(reader(text, command), parsers.SESMapEnclosure)
                with mock.patch.object(parsers, "MAX_SES_OUTPUT_CHARS", len(text) - 1):
                    self.assert_refusal(
                        reader(text, command), page=page, reason="characters", count=len(text), limit=len(text) - 1
                    )

    def test_refusal_sentence_names_the_page_device_and_counts(self) -> None:
        with mock.patch.object(parsers, "MAX_SES_ELEMENTS", 4):
            refusal = parse_sg_ses_aes(aes_page(6), "sg_ses aes /dev/sg7")
        self.assertIsNotNone(refusal)
        sentence = refusal.describe("node.example.test")
        for part in ("AES page", "/dev/sg7", "node.example.test", "6 elements", "limit of 4"):
            self.assertIn(part, sentence)

    def test_page_without_enclosure_evidence_is_still_none(self) -> None:
        for page, (reader, _build, command) in READERS.items():
            with self.subTest(page=page):
                self.assertIsNone(reader("sg_ses: no enclosure here\n", command))


class ParseSshOutputsRefusalTests(unittest.TestCase):
    def test_refused_page_is_recorded_and_warned_not_counted_as_an_enclosure(self) -> None:
        with mock.patch.object(parsers, "MAX_SES_ELEMENTS", 4):
            parsed = parse_ssh_outputs({"sg_ses join /dev/sg7": join_page(5)}, 4, None, None)
        self.assertEqual(parsed.ses_enclosures, [])
        refusals = getattr(parsed, "ses_page_refusals", None)
        self.assertTrue(refusals, "the refused page left no record")
        self.assertEqual([(item.page, item.count, item.limit) for item in refusals], [("join", 5, 4)])
        self.assertEqual(len(parsed.warnings), 1, parsed.warnings)
        self.assertIn("5 elements", parsed.warnings[0])

    def test_page_within_budget_adds_no_warning(self) -> None:
        with mock.patch.object(parsers, "MAX_SES_ELEMENTS", 4):
            parsed = parse_ssh_outputs({"sg_ses join /dev/sg7": join_page(4)}, 4, None, None)
        self.assertEqual(len(parsed.ses_enclosures), 1)
        self.assertEqual(parsed.warnings, [])
        self.assertEqual(getattr(parsed, "ses_page_refusals", []), [])


def _overlay_stub() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        settings=types.SimpleNamespace(layout=types.SimpleNamespace(slot_count=4)),
        _optional_ssh_transport_failure=InventoryService._optional_ssh_transport_failure,
        _format_optional_ssh_transport_failure=InventoryService._format_optional_ssh_transport_failure,
        _local_failure_sentence=InventoryService._local_failure_sentence,
        _ssh_failure_public_summary=InventoryService._ssh_failure_public_summary,
        _tag_ses_overlay=InventoryService._tag_ses_overlay,
        _merge_ses_overlay_data=InventoryService._merge_ses_overlay_data,
    )


def _page_results(device: str, pages: dict[str, str]) -> list[SSHCommandResult]:
    results = []
    for page, text in pages.items():
        command = (
            f"sudo -n /usr/bin/sg_ses --join --filter {device}"
            if page == "join"
            else f"sudo -n /usr/bin/sg_ses -p {page} {device}"
        )
        results.append(SSHCommandResult(command=command, ok=True, stdout=text, stderr="", exit_code=0))
    return results


def _sysfs_result() -> SSHCommandResult:
    return SSHCommandResult(command=LINUX_ENCLOSURE_SYSFS_MAP_COMMAND, ok=True, stdout="", stderr="", exit_code=0)


class HostOverlayRefusalTests(unittest.TestCase):
    def fetch(self, pages_by_device: dict[str, dict[str, str]]):  # noqa: ANN201
        results = [result for device, pages in pages_by_device.items() for result in _page_results(device, pages)]
        return asyncio.run(
            InventoryService._fetch_sg_ses_host_overlay(
                _overlay_stub(),
                "node.example.test",
                list(pages_by_device),
                failure_prefix="TrueNAS SCALE SSH SES",
                command_results=[*results, _sysfs_result()],
            )
        )

    def test_completed_pages_over_the_bound_are_a_recorded_failure(self) -> None:
        with mock.patch.object(parsers, "MAX_SES_ELEMENTS", 4):
            overlay, failures = self.fetch({"/dev/sg7": {"aes": aes_page(6), "ec": ec_page(6), "join": join_page(6)}})
        self.assertEqual(overlay.ses_enclosures, [])
        self.assertEqual(len(failures), 3, failures)
        labels = ("AES page", "joined page", "enclosure status page")
        self.assertEqual([failure.split(" from ")[0] for failure in failures],
                         [f"TrueNAS SCALE SSH SES {label}" for label in labels])
        for failure in failures:
            for part in ("/dev/sg7", "node.example.test", "6 elements", "limit of 4"):
                self.assertIn(part, failure)

    def test_refused_page_is_reported_beside_a_device_that_still_maps(self) -> None:
        with mock.patch.object(parsers, "MAX_SES_ELEMENTS", 4):
            overlay, failures = self.fetch(
                {"/dev/sg7": {"aes": aes_page(4)}, "/dev/sg8": {"aes": aes_page(5)}}
            )
        self.assertEqual(len(overlay.ses_enclosures), 1)
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("/dev/sg8", failures[0])
        self.assertIn("5 elements", failures[0])

    def test_pages_within_the_bound_record_no_failure(self) -> None:
        with mock.patch.object(parsers, "MAX_SES_ELEMENTS", 4):
            overlay, failures = self.fetch({"/dev/sg7": {"aes": aes_page(4), "ec": ec_page(4), "join": join_page(4)}})
        self.assertEqual(failures, [])
        self.assertEqual(len(overlay.ses_enclosures), 1)


def _service(directory: str) -> InventoryService:
    settings = Settings()
    system = SystemConfig(
        id="synthetic-scale",
        truenas=TrueNASConfig(platform="scale"),
        ssh=SSHConfig(enabled=True, host="node.example.test", user="synthetic-operator", commands=[]),
    )
    return InventoryService(
        settings,
        system,
        AsyncMock(),
        AsyncMock(),
        None,
        MappingStore(str(Path(directory) / "slot_mappings.json")),
        ProfileRegistry(settings),
        SlotDetailStore(str(Path(directory) / "slot_detail_cache.json")),
    )


class ScaleOverlayRefusalReachesWarningsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._temp.cleanup()

    async def test_only_shelf_refused_returns_the_refusal_not_an_empty_answer(self) -> None:
        service = _service(self._temp.name)

        async def run_planned(planner, *, initial_commands, host):  # noqa: ANN001, ANN202
            del host
            discovery = SSHCommandResult(command=initial_commands[0], ok=True, stdout="/dev/sg7\n", exit_code=0)
            planned = planner([discovery])
            pages = {"aes": aes_page(5), "ec": ec_page(5), "join": join_page(5)}
            answers = {result.command: result for result in _page_results("/dev/sg7", pages)}
            return [
                discovery,
                *(
                    answers.get(command)
                    or SSHCommandResult(command=command, ok=True, stdout="", stderr="", exit_code=0)
                    for command in planned
                ),
            ]

        service._run_ssh_planned_commands = run_planned
        with mock.patch.object(parsers, "MAX_SES_ELEMENTS", 4):
            overlay, failures = await service._fetch_scale_ses_overlay()
        self.assertEqual(overlay.ses_enclosures, [])
        self.assertTrue(failures, "a refused shelf read the same as a host without SES")
        self.assertTrue(all("5 elements" in failure for failure in failures), failures)


REFUSED_HOST = "refused.example.test"
ACCEPTED_HOST = "accepted.example.test"


class OtherHostRefusalSurvivesSelectionTests(unittest.IsolatedAsyncioTestCase):
    """A refused page on one host is still reported when another host is selected."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def service(self, hosts: list[str], pages_by_host: dict[str, dict[str, dict[str, str]]]) -> InventoryService:
        service = _service(self._temp.name)
        service._build_scale_ssh_hosts = lambda: list(hosts)
        service._build_configured_quantastor_hosts = lambda **_kwargs: list(hosts)

        async def run_planned(planner, *, initial_commands, host):  # noqa: ANN001, ANN202
            pages_by_device = pages_by_host[host]
            discovery = SSHCommandResult(
                command=initial_commands[0], ok=True, stdout="".join(f"{device}\n" for device in pages_by_device),
                exit_code=0,
            )
            planned = planner([discovery])
            answers = {
                result.command: result
                for device, pages in pages_by_device.items()
                for result in _page_results(device, pages)
            }
            return [
                discovery,
                *(
                    answers.get(command)
                    or SSHCommandResult(command=command, ok=True, stdout="", stderr="", exit_code=0)
                    for command in planned
                ),
            ]

        service._run_ssh_planned_commands = run_planned
        return service

    @staticmethod
    def pages(count: int, shelf: str) -> dict[str, str]:
        return {
            name: build(count).replace("BIGSHELF", shelf, 1)
            for name, build in (("aes", aes_page), ("ec", ec_page), ("join", join_page))
        }

    async def fetch(self, wrapper: str, hosts: list[str], pages_by_host):  # noqa: ANN001, ANN201
        service = self.service(hosts, pages_by_host)
        if wrapper == "scale":
            overlay, failures = await service._fetch_scale_ses_overlay()
            return overlay, failures, service._scale_preferred_ses_host
        overlay, failures = await service._fetch_quantastor_ses_overlay()
        return overlay, failures, service._quantastor_preferred_ses_host

    def assert_refusals(self, failures: list[str], host: str, count: int, limit: int) -> None:
        refused = [failure for failure in failures if f"on {host} was refused" in failure]
        self.assertEqual(
            sorted(failure.split(" from ")[0].split(" SES ", 1)[1] for failure in refused),
            ["AES page", "enclosure status page", "joined page"],
            failures,
        )
        for failure in refused:
            self.assertIn(f"has {count} elements, over the limit of {limit}", failure)

    async def test_refusal_on_another_host_survives_best_host_selection_in_both_orders(self) -> None:
        pages_by_host = {
            REFUSED_HOST: {"/dev/sg7": self.pages(5, "REFUSEDSHELF")},
            ACCEPTED_HOST: {"/dev/sg8": self.pages(4, "GOODSHELF")},
        }
        for wrapper in ("scale", "quantastor"):
            for hosts in ([REFUSED_HOST, ACCEPTED_HOST], [ACCEPTED_HOST, REFUSED_HOST]):
                with self.subTest(wrapper=wrapper, hosts=hosts), mock.patch.object(parsers, "MAX_SES_ELEMENTS", 4):
                    overlay, failures, best_host = await self.fetch(wrapper, hosts, pages_by_host)
                    self.assertEqual(best_host, ACCEPTED_HOST)
                    self.assertEqual(len(overlay.ses_enclosures), 1)
                    self.assertEqual(len(failures), 3, failures)
                    self.assert_refusals(failures, REFUSED_HOST, 5, 4)

    async def test_refusal_on_another_host_survives_at_the_real_element_limit(self) -> None:
        limit = parsers.MAX_SES_ELEMENTS
        pages_by_host = {
            REFUSED_HOST: {"/dev/sg7": self.pages(limit + 1, "REFUSEDSHELF")},
            ACCEPTED_HOST: {"/dev/sg8": self.pages(4, "GOODSHELF")},
        }
        for wrapper in ("scale", "quantastor"):
            with self.subTest(wrapper=wrapper):
                overlay, failures, best_host = await self.fetch(wrapper, [REFUSED_HOST, ACCEPTED_HOST], pages_by_host)
                self.assertEqual(best_host, ACCEPTED_HOST)
                self.assertEqual(len(overlay.ses_enclosures), 1)
                self.assert_refusals(failures, REFUSED_HOST, limit + 1, limit)

    async def test_refusal_survives_when_the_selected_host_is_read_from_the_device_cache(self) -> None:
        pages_by_host = {
            REFUSED_HOST: {"/dev/sg7": self.pages(5, "REFUSEDSHELF")},
            ACCEPTED_HOST: {"/dev/sg8": self.pages(4, "GOODSHELF")},
        }
        for wrapper in ("scale", "quantastor"):
            with self.subTest(wrapper=wrapper), mock.patch.object(parsers, "MAX_SES_ELEMENTS", 4):
                service = self.service([ACCEPTED_HOST, REFUSED_HOST], pages_by_host)
                cached_pages = {
                    result.command: result for result in _page_results("/dev/sg8", pages_by_host[ACCEPTED_HOST]["/dev/sg8"])
                }

                async def run_commands(commands, host, **_kwargs):  # noqa: ANN001, ANN202
                    self.assertEqual(host, ACCEPTED_HOST)
                    return [
                        cached_pages.get(command)
                        or SSHCommandResult(command=command, ok=True, stdout="", stderr="", exit_code=0)
                        for command in commands
                    ]

                service._run_ssh_commands = run_commands
                fetch = (
                    service._fetch_scale_ses_overlay if wrapper == "scale" else service._fetch_quantastor_ses_overlay
                )
                await fetch()  # the first refresh discovers and caches the accepted host's device
                self.assertEqual(service._get_cached_sg_ses_devices(ACCEPTED_HOST), ["/dev/sg8"])
                overlay, failures = await fetch()
                self.assertEqual(len(overlay.ses_enclosures), 1)
                self.assertEqual(len(failures), 3, failures)
                self.assert_refusals(failures, REFUSED_HOST, 5, 4)

    async def test_cached_read_refusal_survives_when_rediscovery_finds_no_device(self) -> None:
        pages_by_host = {
            REFUSED_HOST: {"/dev/sg7": self.pages(3, "REFUSEDSHELF")},
            ACCEPTED_HOST: {"/dev/sg8": self.pages(4, "GOODSHELF")},
        }
        for wrapper in ("scale", "quantastor"):
            with self.subTest(wrapper=wrapper), mock.patch.object(parsers, "MAX_SES_ELEMENTS", 4):
                pages_by_host[REFUSED_HOST] = {"/dev/sg7": self.pages(3, "REFUSEDSHELF")}
                service = self.service([ACCEPTED_HOST, REFUSED_HOST], pages_by_host)
                fetch = (
                    service._fetch_scale_ses_overlay if wrapper == "scale" else service._fetch_quantastor_ses_overlay
                )
                await fetch()  # both hosts read within the bound: both device lists are cached
                self.assertEqual(service._get_cached_sg_ses_devices(REFUSED_HOST), ["/dev/sg7"])

                # The cached shelf now answers with a page over the bound, and rediscovery finds nothing.
                pages_by_host[REFUSED_HOST] = {"/dev/sg7": self.pages(5, "REFUSEDSHELF")}

                async def run_commands(commands, host, **_kwargs):  # noqa: ANN001, ANN202
                    answers = {
                        result.command: result
                        for device, pages in pages_by_host[host].items()
                        for result in _page_results(device, pages)
                    }
                    return [
                        answers.get(command)
                        or SSHCommandResult(command=command, ok=True, stdout="", stderr="", exit_code=0)
                        for command in commands
                    ]

                planned = service._run_ssh_planned_commands

                async def run_planned(planner, *, initial_commands, host):  # noqa: ANN001, ANN202
                    if host == REFUSED_HOST:
                        return [SSHCommandResult(command=initial_commands[0], ok=True, stdout="", exit_code=0)]
                    return await planned(planner, initial_commands=initial_commands, host=host)

                service._run_ssh_commands = run_commands
                service._run_ssh_planned_commands = run_planned
                overlay, failures = await fetch()
                self.assertEqual(len(overlay.ses_enclosures), 1)
                self.assert_refusals(failures, REFUSED_HOST, 5, 4)

    async def cached_rediscovery(self, wrapper: str, rediscovered: dict[str, dict[str, str]]):  # noqa: ANN201
        pages_by_host = {
            REFUSED_HOST: {
                "/dev/sg7": self.pages(3, "FIRSTSHELF"),
                "/dev/sg8": self.pages(4, "SECONDSHELF"),
            },
        }
        service = self.service([REFUSED_HOST], pages_by_host)
        fetch = service._fetch_scale_ses_overlay if wrapper == "scale" else service._fetch_quantastor_ses_overlay
        _overlay, failures = await fetch()
        self.assertEqual(failures, [])
        self.assertEqual(service._get_cached_sg_ses_devices(REFUSED_HOST), ["/dev/sg7", "/dev/sg8"])

        # The cached read refuses the first shelf, but still reads the second.
        cached_pages = {
            "/dev/sg7": self.pages(5, "FIRSTSHELF"),
            "/dev/sg8": self.pages(4, "SECONDSHELF"),
        }
        cached_answers = {
            result.command: result
            for device, pages in cached_pages.items()
            for result in _page_results(device, pages)
        }

        async def run_commands(commands, host, **_kwargs):  # noqa: ANN001, ANN202
            self.assertEqual(host, REFUSED_HOST)
            self.assertEqual(set(commands), {*cached_answers, LINUX_ENCLOSURE_SYSFS_MAP_COMMAND})
            return [
                cached_answers.get(command)
                or SSHCommandResult(command=command, ok=True, stdout="", stderr="", exit_code=0)
                for command in commands
            ]

        # Rediscovery can omit the refused shelf, repeat it, or find no healthy page.
        pages_by_host[REFUSED_HOST] = rediscovered
        service._run_ssh_commands = AsyncMock(side_effect=run_commands)
        service._run_ssh_planned_commands = AsyncMock(wraps=service._run_ssh_planned_commands)
        result = await fetch()
        service._run_ssh_commands.assert_awaited_once()
        service._run_ssh_planned_commands.assert_awaited_once()
        return result

    async def test_cached_refusal_survives_rediscovery_of_only_the_other_healthy_device(self) -> None:
        for wrapper in ("scale", "quantastor"):
            with self.subTest(wrapper=wrapper), mock.patch.object(parsers, "MAX_SES_ELEMENTS", 4):
                overlay, failures = await self.cached_rediscovery(
                    wrapper, {"/dev/sg8": self.pages(4, "SECONDSHELF")}
                )
                self.assertEqual(len(overlay.ses_enclosures), 1)
                self.assertEqual(overlay.ses_enclosures[0].ses_device, "/dev/sg8")
                self.assertEqual(len(failures), 3, failures)
                self.assert_refusals(failures, REFUSED_HOST, 5, 4)

    async def test_cached_and_rediscovered_refusal_sentences_are_reported_once(self) -> None:
        for wrapper in ("scale", "quantastor"):
            with self.subTest(wrapper=wrapper), mock.patch.object(parsers, "MAX_SES_ELEMENTS", 4):
                overlay, failures = await self.cached_rediscovery(wrapper, {
                    "/dev/sg7": self.pages(5, "FIRSTSHELF"),
                    "/dev/sg8": self.pages(4, "SECONDSHELF"),
                })
                self.assertEqual(len(overlay.ses_enclosures), 1)
                self.assertEqual(len(failures), 3, failures)
                self.assert_refusals(failures, REFUSED_HOST, 5, 4)
                for failure in failures:
                    self.assertEqual(failures.count(failure), 1, failures)

    async def test_cached_refusal_survives_rediscovery_without_a_healthy_page(self) -> None:
        for wrapper in ("scale", "quantastor"):
            with self.subTest(wrapper=wrapper), mock.patch.object(parsers, "MAX_SES_ELEMENTS", 4):
                overlay, failures = await self.cached_rediscovery(
                    wrapper, {"/dev/sg8": self.pages(6, "SECONDSHELF")}
                )
                self.assertEqual(overlay.ses_enclosures, [])
                self.assertEqual(len(failures), 6, failures)
                for device, count in (("/dev/sg7", 5), ("/dev/sg8", 6)):
                    refused = [failure for failure in failures if f" from {device} " in failure]
                    self.assert_refusals(refused, REFUSED_HOST, count, 4)

    async def test_selected_host_refusal_is_reported_once_beside_another_hosts(self) -> None:
        pages_by_host = {
            REFUSED_HOST: {"/dev/sg7": self.pages(5, "REFUSEDSHELF")},
            ACCEPTED_HOST: {"/dev/sg8": self.pages(4, "GOODSHELF"), "/dev/sg9": self.pages(6, "OTHERSHELF")},
        }
        for wrapper in ("scale", "quantastor"):
            for hosts in ([REFUSED_HOST, ACCEPTED_HOST], [ACCEPTED_HOST, REFUSED_HOST]):
                with self.subTest(wrapper=wrapper, hosts=hosts), mock.patch.object(parsers, "MAX_SES_ELEMENTS", 4):
                    _overlay, failures, best_host = await self.fetch(wrapper, hosts, pages_by_host)
                    self.assertEqual(best_host, ACCEPTED_HOST)
                    self.assertEqual(len(failures), 6, failures)
                    self.assertEqual(len(set(failures)), 6, failures)
                    self.assert_refusals(failures, REFUSED_HOST, 5, 4)
                    self.assert_refusals(failures, ACCEPTED_HOST, 6, 4)

    async def test_two_hosts_within_the_bound_report_no_failure(self) -> None:
        pages_by_host = {
            REFUSED_HOST: {"/dev/sg7": self.pages(3, "SMALLSHELF")},
            ACCEPTED_HOST: {"/dev/sg8": self.pages(4, "GOODSHELF")},
        }
        for wrapper in ("scale", "quantastor"):
            for hosts in ([REFUSED_HOST, ACCEPTED_HOST], [ACCEPTED_HOST, REFUSED_HOST]):
                with self.subTest(wrapper=wrapper, hosts=hosts), mock.patch.object(parsers, "MAX_SES_ELEMENTS", 4):
                    overlay, failures, best_host = await self.fetch(wrapper, hosts, pages_by_host)
                    self.assertEqual(best_host, ACCEPTED_HOST)
                    self.assertTrue(overlay.ses_enclosures)
                    self.assertEqual(failures, [])


class LedReadbackRefusalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._temp.cleanup()

    async def test_refused_readback_page_leaves_the_light_state_unknown(self) -> None:
        service = _service(self._temp.name)
        # No logical identifier on either side: the shelf check compares None with None.
        identity = "  SYNTH     BIGSHELF      0100\n"

        async def run_sequence(_host, commands, *, then=None):  # noqa: ANN001, ANN202
            preflight = [
                SSHCommandResult(command=command, ok=True, stdout=identity, exit_code=0) for command in commands
            ]
            planned = then(preflight) if then is not None else []
            later = [
                SSHCommandResult(
                    command=command,
                    ok=True,
                    stdout=ec_page(5) if " -p ec " in f" {command} " else "",
                    exit_code=0,
                )
                for command in planned
            ]
            return [(result, 0.0) for result in (*preflight, *later)]

        service._run_led_ssh_sequence = run_sequence
        slot = SlotView(slot=1, slot_label="01", row_index=0, column_index=0)
        target = {
            "ssh_host": None,
            "ses_device": "/dev/sg7",
            "ses_element_id": 1,
            "ses_slot_number": 2,
            "enclosure_id": None,
        }
        with mock.patch.object(parsers, "MAX_SES_ELEMENTS", 4):
            result = await service._set_sg_ses_slot_led_over_ssh(slot, LedAction.identify, True, [target])
        self.assertIsNone(result["identify_active"])
        self.assertFalse(result["confirmed"])
        self.assertEqual([path["identify_active"] for path in result["paths"]], [None])

    async def test_readback_page_within_budget_confirms_the_light(self) -> None:
        service = _service(self._temp.name)
        # No logical identifier on either side: the shelf check compares None with None.
        identity = "  SYNTH     BIGSHELF      0100\n"
        lit = ec_page(4).replace(
            "      Element 1 descriptor:\n        Predicted failure=0, Disabled=0, Swap=0, status: OK\n"
            "        Ready to insert=0, RMV=0, Ident=0, Report=0\n",
            "      Element 1 descriptor:\n        Predicted failure=0, Disabled=0, Swap=0, status: OK\n"
            "        Ready to insert=0, RMV=0, Ident=1, Report=0\n",
        )

        async def run_sequence(_host, commands, *, then=None):  # noqa: ANN001, ANN202
            preflight = [
                SSHCommandResult(command=command, ok=True, stdout=identity, exit_code=0) for command in commands
            ]
            planned = then(preflight) if then is not None else []
            later = [
                SSHCommandResult(command=command, ok=True, stdout=lit if " -p ec " in f" {command} " else "",
                                 exit_code=0)
                for command in planned
            ]
            return [(result, 0.0) for result in (*preflight, *later)]

        service._run_led_ssh_sequence = run_sequence
        slot = SlotView(slot=1, slot_label="01", row_index=0, column_index=0)
        target = {
            "ssh_host": None,
            "ses_device": "/dev/sg7",
            "ses_element_id": 1,
            "ses_slot_number": 2,
            "enclosure_id": None,
        }
        with mock.patch.object(parsers, "MAX_SES_ELEMENTS", 4):
            result = await service._set_sg_ses_slot_led_over_ssh(slot, LedAction.identify, True, [target])
        self.assertIs(result["identify_active"], True)
        self.assertTrue(result["confirmed"])


if __name__ == "__main__":
    unittest.main()
