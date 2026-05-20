# SAS Diagnostic Decoder Sources

This note tracks local reference material for the `0.20.0` SAS Fabric
decoder work. The PDFs listed here are operator-supplied local references and
are not checked into the repository.

## Decoder Source Stack

| Reference | Best use | Notes |
| --- | --- | --- |
| INCITS T10 SCSI Operation Codes list | Standards-backed CDB opcode names | Public numeric list at <https://www.t10.org/lists/op-num.htm>. Use for high-confidence generic SCSI operation names such as RECEIVE DIAGNOSTIC RESULTS, LOG SENSE, WRITE(16), READ CAPACITY(10), and command-size families. |
| INCITS T10 ASC/ASCQ list | Standards-backed SCSI sense reason names | Public numeric list at <https://www.t10.org/lists/asc-num.htm>. Use for high-confidence ASC/ASCQ labels and fault families such as logical-unit communication, write/read errors, protocol CRC/parity errors, ACK/NAK timeout, NAK received, connection lost, data-buffer errors, and PCIe fabric errors. |
| `SCSI Commands oct 2016 rev J.pdf` | Generic SCSI command, sense, LOG SENSE, and SAS PHY log decoding | Strongest immediate decoder reference. It covers common CDBs, status/sense/ASC/ASCQ language, error counter log pages, and the Protocol-Specific Port log page fields for SAS PHY evidence such as invalid dwords, running disparity errors, loss of dword sync, and PHY reset problems. It also anchors CDB allocation/parameter-list length fields such as LOG SENSE, PERSISTENT RESERVE IN/OUT, RECEIVE COPY RESULTS, READ CAPACITY(16), and UNMAP. |
| `baruch/lsi_decode_loginfo` | Broadcom/LSI MPI/MPR `loginfo` decoding | MIT-licensed reference at <https://github.com/baruch/lsi_decode_loginfo>. Useful for breaking values such as `31120302` into Type `SAS`, Origin `PL`, Code `Abort`, and sub-code `Wrong relative offset or frame length`. The current app ports attributed SAS IOP boot/config/enclosure/target, PL open/discovery/enclosure, and IR compatibility/firmware tables without vendoring the whole script. |
| `08-309r1.pdf` | Standards anchor for generic SCSI Primary Commands behavior | SPC-3 draft text is useful for CDB structure, operation code framing, sense data, CHECK CONDITION behavior, LOG SENSE shape, ASC/ASCQ concepts, and service-action codes for MAINTENANCE IN/OUT, PERSISTENT RESERVE IN/OUT, and RECEIVE COPY RESULTS. Use it to keep parser categories generic and standards-shaped. |
| `services-specification-ultrastar-data102.pdf` | SES/enclosure diagnostic page and element mapping reference | Useful for SES model, Configuration page `01h`, Enclosure Control/Status `02h`, Element Descriptor `07h`, Additional Element Status `0Ah`, array-slot descriptors, SAS expander/connector elements, and SG3-style index semantics. Treat it as an SES/enclosure-profile reference, not as direct Archive CORE/Supermicro bay-zone truth. |
| `D:\BPN-SAS-F424-A6.pdf` and `D:\BPN-SAS3-F424-A2N2A.pdf` | Supermicro backplane connector and port-label context | Local operator-supplied PDFs. Use for physical connector/backplane language only, such as SAS Mini HD, SAS/SATA/NVMe port labeling, and "do not mix SAS3/SATA in the same port" style constraints. These do not provide MPR `loginfo`, ASC/ASCQ, or persistent PHY counter decoding. |
| `product-manual-ultrastar-dc-hc310-sata-oem-spec.pdf` | SATA disk command and SMART enrichment | Useful for SATA/ATA, NCQ, SMART attribute/error-log/self-test, SCT, IDENTIFY DEVICE, temperature, power-on-hours, reallocation, and read/write error context. It does not describe SAS fabric topology. |
| `product-manual-ultrastar-dc-hc555-sata.pdf` | SATA disk command and SMART enrichment | Same role as the HC310 manual: good disk-profile material for SATA/ATA/SMART interpretation, not HBA/path/expander topology. |

## Confidence Policy

- `standard`: decoded from a generic SCSI/T10 table or standards-shaped local
  PDF material, with the observed value present in the current lookup table.
- `standard-partial`: the major opcode or standard structure is known, but a
  service action or subfield is not yet in the local table.
- `vendor-reference`: decoded from the attributed Broadcom/LSI `loginfo`
  reference with no leftover low-order bits.
- `vendor-reference-partial`: the Broadcom/LSI code path is known, but the
  current local table leaves some subfield bits unparsed.
- `observed`: the kernel or appliance supplied a plain-language label, but the
  numeric value is not yet in a standards/vendor lookup table.
- `unconfirmed`: a numeric value is retained, but the current decoder cannot
  tie it to a known standard/vendor table entry or a reliable observed label.

## Implementation Guidance

- Keep the current MPR/CAM evidence decoder centered on generic facts first:
  CDB operation, direction, LBA, transfer blocks, allocation length, parameter
  list length, CAM status, retry markers, sense key, ASC/ASCQ, and
  plain-language fault family.
- Treat the currently displayed event list as a compact summary, not the whole
  evidence set. Group decoded events by stable fingerprint, sort by severity
  plus occurrence count, and provide a paged or expandable full-event view.
- Add lookup tables incrementally where the source is stable:
  common CDB opcodes, sense keys, common ASC/ASCQ labels, and LOG SENSE page
  names.
- Decode Broadcom/LSI `loginfo` values into their structured fields before
  falling back to generic controller-terminated labels. Mark source-backed
  rows as `vendor-reference` or `vendor-reference-partial`; keep unknown
  origins/codes visible as `unconfirmed` rather than hiding the raw value.
- Do not infer true hardware PHY counters from kernel messages alone. If a
  future safe read-only command exposes SAS PHY log pages, surface those as a
  separate evidence bucket from recent kernel event evidence.
- Use the Data102 service spec to improve SES/AES parsing concepts:
  element descriptors, additional element status, array-slot indexes,
  expander elements, connector elements, and enclosure-profile aliasing.
  Keep Supermicro CSE-946-specific bay/backplane mapping evidence separate.
- Use the HC310/HC555 manuals for disk-detail enrichment only:
  SMART/ATA/NCQ/SCT fields, vendor model families, and read/write error
  context. Do not let SATA-vs-SAS drive protocol determine enclosure identity.

## Still Missing

- Full Broadcom/LSI MPI/MPR `loginfo` coverage. The app now carries a broader
  attributed SAS IOP/PL/IR subset, including IOP firmware/upload, enclosure,
  and target-mode details plus more PL enclosure/discovery and IR
  compatibility/firmware entries, but it is still not a complete MPI decoder
  and non-SAS FC/iSCSI tables remain intentionally unexpanded.
- Full SCSI/SAS lookup coverage. Current code carries broader common CDB
  opcodes, selected 16-byte and 12-byte service actions, standards-backed
  MAINTENANCE IN/OUT and PERSISTENT RESERVE IN/OUT service actions,
  RECEIVE COPY RESULTS service actions, useful ASC/ASCQ labels, LOG SENSE
  page names/page-control fields, SAS PHY concepts, and SES/AES concepts; it
  is still curated rather than exhaustive.
- A proven TrueNAS CORE-safe source for persistent SAS PHY hardware counters.
  `mprutil show all` did not expose those counters on the current Archive CORE
  host; the current production collector therefore uses filtered non-sudo
  `dmesg` MPR/CAM evidence.
- A Supermicro CSE-946/BPN-SAS3 SES element mapping reference. Local
  backplane PDFs help with connector naming and physical constraints, but they
  do not prove SES element indexes or MPR diagnostic meanings.
