---
name: Hardware report
about: Submit sanitized parser and correlation evidence for an unsupported chassis or platform shape
title: "hardware: "
labels: ""
assignees: ""
---

## Before posting

Do not attach or paste raw logs, config files, history databases, SSH keys, known-hosts files, API tokens, passwords, private hostnames, private IP addresses, serials, WWNs, SAS addresses, or filesystem paths.

Replace sensitive identifiers consistently within this report. For example, use `DISK_A` everywhere the same serial appears. Keep command structure, field names, slot numbers, and relationships intact.

If you are unsure how to sanitize a report, post the platform and chassis details only. A maintainer can provide a minimal intake checklist before you share parser input.

## Environment

- App version or commit:
- Platform: CORE, SCALE, Linux, Quantastor, ESXi, IPMI, or UniFi
- Chassis, enclosure, expander, or controller model:
- Profile or storage view selected:
- What is wrong or missing:
- Expected operator result:

## Sanitized parser inputs

Include only the commands relevant to the symptom. State which command produced each block.

### Linux or SCALE enclosure evidence

```text
# lsscsi -g -t

# sg_ses -p aes <enclosure-device>

# sg_ses -p ec <enclosure-device>

# sg_ses --join --filter <enclosure-device>

# lsblk -OJ

# sanitized slot readings from /sys/class/enclosure/*/*/slot
```

### Representative SMART evidence

```json
# smartctl -x -j for one representative disk
```

### Other platform evidence

```text
# CORE: sesutil map and sesutil show
# ESXi: StorCLI or PercCLI JSON and relevant esxcli rows
# Quantastor: sanitized API or qs JSON rows
# BMC/IPMI: sanitized slot or drive rows
```

## Reproduction notes

1. Which source or command is authoritative for the affected bay?
2. Does the result change across refreshes or only with a particular device state?
3. Are empty bays, multiple enclosures, multipath disks, or HA nodes involved?
4. Which sanitized identifier represents the affected disk or bay?

## Fixture consent

- [ ] These inputs are sanitized and safe to publish.
- [ ] I consent to maintainers converting the sanitized inputs into a public synthetic fixture.
- [ ] I understand that maintainers may reduce, rename, or replace identifiers while preserving parser relationships.
