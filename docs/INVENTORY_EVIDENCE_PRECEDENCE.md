# Inventory evidence precedence

The inventory service combines several observations of the same physical bay. It uses source strength, not collection order or timestamps, to resolve contradictions.

## SES source strength

`app/services/parsers.py` assigns these strengths from lowest to highest:

| Strength | Source | Typical evidence |
|---:|---|---|
| 0 | unknown | A value without source provenance |
| 10 | `sesutil_show` | Summary status and device name |
| 20 | `sg_ses_ec` | Enclosure-control status |
| 30 | `sesutil_map` | CORE slot-to-device mapping |
| 40 | `sg_ses_aes` | Additional-element status, occupancy, and SAS address |
| 40 | `sg_ses_join` | Joined SES element evidence |
| 50 | `enclosure_sysfs` | Kernel enclosure-driver slot-to-device binding |

BMC, StorCLI, appliance APIs, and vendor tools do not enter the SES merge at an invented strength. Their disk matches run through the platform correlation layer described below.

## Field rules

### Occupancy

- A stronger source replaces a weaker `present` value.
- A stronger empty result clears weaker device names and the derived `device_hint`.
- Equal-strength present-versus-empty evidence resolves to present and sets `presence_conflict=true`. This avoids hiding a possibly occupied bay while preserving the contradiction for operators and tests.
- A sourced `present` boolean is final for empty-slot gating. Weaker auxiliary fields such as `sas_address=0` or `no SAS device attached` cannot override `present=true` from `enclosure_sysfs`.
- If no sourced boolean exists, a zero SAS hint or `no SAS device attached` counts as empty only when the candidate also contains SES evidence.
- A presence conflict prevents the empty-slot gate from suppressing correlation.

### Device names

- A stronger source replaces weaker names.
- Equal-strength names are combined without duplicates and sorted by case-insensitive natural device order, so merge input order cannot choose the first disk match.
- Device names from `enclosure_sysfs` outrank AES, join, `sesutil map`, EC, and `sesutil show` evidence.

### SES element and slot coordinates

- EC `Element N descriptor` values are type-local individual-element coordinates. AES evidence is merged with EC by `(ses_device, element_id)`, not by the device slot number.
- For the required first Device Slot/Array Device Slot type header, AES `eiioe=1` includes the Enclosure Status overall element. The parser subtracts that one-element offset before correlating with EC or exposing the element control coordinate. `eiioe=0` remains unchanged.
- The AES `device slot number` remains the physical bay coordinate and the `--dev-slot-num` control target after EC metadata is merged.
- The kernel enclosure-driver `slot` attribute is a device slot number. `enclosure_sysfs` device bindings are applied after the SES pages are merged and only to bays whose slot number came from `device slot number` or `SlotNN` descriptor text. Bays keyed by an EC element index or by the invalid-descriptor element-index fallback take no sysfs hint, so an EC-only enclosure reports no device names rather than a neighbouring bay's disk.
- One or two AES element descriptors may describe a bounded dual-path bay and are merged under the existing evidence rules. More than two distinct elements reporting one device slot number is degraded/systemic numbering evidence. Every affected element is preserved as unmapped evidence with a warning; none is silently collapsed into a physical bay.

### SAS addresses

- A stronger source replaces a weaker address.
- Equal-strength, nonzero addresses from independent enclosure evidence are treated as contradictory. Correlation clears the address and sets `sas_address_conflict=true` rather than choosing an input-order winner.
- Up to two descriptors for one bay inside one AES report represent a bounded dual-path shape. The parser keeps the first nonzero path address, while still allowing a later valid path to replace an earlier zero or absent value.

### Stored manual mappings

A live SES empty result is checked before a stored manual mapping. The mapping remains in the mapping store, but the slot renders empty, omits the mapping's disk identity, reports `mapping_source="ses-empty"`, and includes a stale-mapping annotation. An operator can then inspect or recalibrate the preserved mapping.

## Disk-resolution order and labels

The platform correlation layer stops at the first usable match in this order:

| Order | Evidence | `mapping_source` |
|---:|---|---|
| 1 | Authoritative live SES empty evidence | `ses-empty` |
| 2 | Stored mapping matched by serial, GPTID, or device | The mapping's source, normally `manual` |
| 3 | Quantastor SAS-address match | `sas-address` |
| 4 | Exact appliance API enclosure and slot | `api-slot` |
| 5 | Kernel enclosure binding | `enclosure-sysfs` |
| 6 | Other SES device name | `device-name` |
| 7 | Unscoped appliance API slot fallback | `api-slot-fallback` |
| 8 | Device, serial, or GPTID hint | `device-hint`, `serial-hint`, or `gptid-hint` |
| 9 | General SAS-address match | `sas-address` |
| 10 | Parsed SES slot-to-device map | `ses-slot-map` |
| 11 | No disk match | `unknown` |

Platform builders can supply a more specific direct source when they resolve a disk before this shared sequence. Current values include `storcli-slot`, `bmc-slot`, and `ubntstorage`.

The resolver does not use newest-wins behavior. Input ordering only has meaning for duplicate path descriptors within one AES report, as described above.
