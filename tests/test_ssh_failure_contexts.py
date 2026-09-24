"""Characterization of the SSH command failure contexts.

Refs #450. `InventoryService._ssh_command_failure_context` was a 95-line chain
of `if canonical_command == ...` returns with one test covering one of its
fifteen answers. These tests pin every answer so the chain can be replaced by a
table without changing what an operator reads in the debug output.
"""

from __future__ import annotations

import unittest

from app.services.inventory import InventoryService


EXPECTED_CONTEXTS: tuple[tuple[str, dict[str, str]], ...] = (
    (
        "dmesg mpr events",
        {
            "context": "sas_fabric_kernel_events",
            "context_label": "recent MPR/CAM event evidence",
            "criticality": "enrichment",
        },
    ),
    (
        "pciconf -lv",
        {
            "context": "sas_fabric_pci_inventory",
            "context_label": "HBA PCI inventory",
            "criticality": "enrichment",
        },
    ),
    (
        "dmidecode slot",
        {
            "context": "sas_fabric_pcie_slots",
            "context_label": "HBA slot labels",
            "criticality": "enrichment",
        },
    ),
    (
        "mpr sysctl pci locations",
        {
            "context": "sas_fabric_pci_location",
            "context_label": "HBA PCI location corroboration",
            "criticality": "enrichment",
        },
    ),
    (
        "lsscsi -g -t",
        {
            "context": "storage_fabric_linux_scsi_transport",
            "context_label": "Linux SCSI transport detail",
            "criticality": "enrichment",
        },
    ),
    (
        "lsscsi -g",
        {
            "context": "storage_fabric_linux_scsi_sg",
            "context_label": "Linux SG device discovery",
            "criticality": "enrichment",
        },
    ),
    (
        "lsblk -OJ",
        {
            "context": "linux_block_inventory",
            "context_label": "Linux block inventory",
            "criticality": "inventory",
        },
    ),
    (
        "nvme list-subsys -o json",
        {
            "context": "storage_fabric_linux_nvme_subsystems",
            "context_label": "Linux NVMe subsystem detail",
            "criticality": "enrichment",
        },
    ),
    (
        "enclosure sysfs map",
        {
            "context": "storage_fabric_linux_enclosure_map",
            "context_label": "Linux enclosure driver slot map",
            "criticality": "enrichment",
        },
    ),
    (
        "sg_ses join /dev/sg3",
        {
            "context": "storage_fabric_sg_ses_join",
            "context_label": "joined SG SES slot evidence",
            "criticality": "enrichment",
        },
    ),
    (
        "mprutil show adapters",
        {
            "context": "sas_fabric_adapter_discovery",
            "context_label": "adapter discovery",
            "criticality": "topology",
        },
    ),
    (
        "mprutil show anything-else",
        {
            "context": "sas_fabric_mprutil",
            "context_label": "mprutil topology evidence",
            "criticality": "topology",
        },
    ),
    (
        "zpool status",
        {
            "context": "ssh_command",
            "context_label": "SSH command",
            "criticality": "inventory",
        },
    ),
)


MPRUTIL_UNIT_LABELS: tuple[tuple[str, str], ...] = (
    ("adapter", "controller adapter detail"),
    ("devices", "MPR device rows"),
    ("enclosures", "MPR enclosure rows"),
    ("expanders", "MPR expander rows"),
    ("iocfacts", "IOC facts"),
    ("all", "MPR all-command detail"),
)


class SSHCommandFailureContextTests(unittest.TestCase):
    def test_every_named_command_keeps_its_context(self) -> None:
        for command, expected in EXPECTED_CONTEXTS:
            with self.subTest(command=command):
                self.assertEqual(
                    InventoryService._ssh_command_failure_context(command), expected
                )

    def test_mprutil_unit_commands_name_their_controller_and_subcommand(self) -> None:
        for subcommand, label in MPRUTIL_UNIT_LABELS:
            with self.subTest(subcommand=subcommand):
                self.assertEqual(
                    InventoryService._ssh_command_failure_context(
                        f"mprutil -u 2 show {subcommand}"
                    ),
                    {
                        "context": f"sas_fabric_mprutil_{subcommand}",
                        "context_label": label,
                        "controller": "mpr2",
                        "criticality": "enrichment",
                    },
                )

    def test_an_unlisted_mprutil_subcommand_falls_back_to_its_own_name(self) -> None:
        self.assertEqual(
            InventoryService._ssh_command_failure_context("mprutil -u 0 show phys"),
            {
                "context": "sas_fabric_mprutil_phys",
                "context_label": "mprutil phys",
                "controller": "mpr0",
                "criticality": "enrichment",
            },
        )

    def test_the_returned_context_is_not_the_shared_table_entry(self) -> None:
        """A caller that edits its copy must not edit the next caller's answer."""
        first = InventoryService._ssh_command_failure_context("lsblk -OJ")
        first["criticality"] = "mutated"

        self.assertEqual(
            InventoryService._ssh_command_failure_context("lsblk -OJ")["criticality"],
            "inventory",
        )


if __name__ == "__main__":
    unittest.main()
