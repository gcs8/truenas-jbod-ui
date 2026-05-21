# Release Notes - v0.20.1

Release date: `2026-05-21`

## Summary

`0.20.1` is the Storage Fabric polish release after `0.20.0`.

The big change is not another feature grab. It turns the initial CORE-focused
SAS Fabric work into a steadier operator surface: the UI now says `Storage
Fabric`, keeps deep CORE HBA/SAS diagnostics where evidence exists, and gives
SCALE/Linux, Quantastor, ESXi, generic Linux, and BMC-backed systems honest
read-only maps with source-specific labels.

It also closes the operator-review issues from the first Storage Fabric pass:
Disk Path card clickability, first-click bay stability, SCALE identity reuse,
Quantastor/ESXi copy clarity, event-table readability, public-demo spare
labeling, and the high-coverage release validation gate.

## Changed

- Dedicated `/sas-fabric` copy now presents as `Storage Fabric` while retaining
  route/API compatibility.
- Inventory snapshots expose a platform capability contract for inventory,
  physical slots, SMART detail, history, identify LEDs, platform details, and
  diagnostics.
- SCALE/Linux snapshots with SG enclosure evidence now render Linux SES-backed
  Storage Fabric maps.
- SCALE/Linux Storage Fabric traces now reuse block, SCSI, SES, SG, HCTL,
  attached SAS, SMART-device, block-size, LUN, model, serial, and size evidence
  already visible in the enclosure view.
- Quantastor, ESXi, generic Linux, and BMC/IPMI maps now stay read-only and
  source-labeled instead of leaking CORE-only HBA/SAS/expander claims.
- Admin setup guidance now lists platform-specific Required, Optional, and
  Unsupported requirements, including `sg_ses` and StorCLI/PercCLI notes.
- ZFS spares normalize to pool-level `spares` peers in parsed topology, live
  peer highlighting, and the public demo fixture.
- Diagnostic decoding now includes broader T10 SCSI service-action/status,
  ASC/ASCQ, FreeBSD CAM retry, and attributed Broadcom/LSI `loginfo` coverage.

## Fixed

- Disk Path SAS/SES/Storage Path cards are clickable local inspector selectors.
- Clicking `Archive CORE`, HBA, or path cards after a fresh Disk Path load no
  longer changes the selected bay or branch order.
- SCALE Disk Path bay pickers no longer create a nested scrollbar for normal
  slot layouts.
- SCALE Selected Bay, Disk card, and Path Members surfaces now show the reused
  enclosure identity that was previously buried in raw slot data.
- CORE diagnostic event-table Impact and Type columns no longer overlap.
- Public demo tests/docs now match the current `spares` topology label.

## Validation Snapshot

Current local validation on
`codex/v0.20.1-kickoff-2026-05-20-post-0.20.0`:

- `.\.venv\Scripts\python.exe -m unittest discover -s tests -v` passed with
  `459` tests.
- `.\.venv\Scripts\python.exe -m py_compile app/services/sas_fabric.py app/services/inventory.py app/services/parsers.py app/services/system_setup.py app/services/public_demo_fixture.py admin_service/main.py tests/test_sas_fabric.py tests/test_inventory.py tests/test_admin_service.py tests/test_public_demo_fixture.py`
  passed.
- `node --check app/static/app.js` passed.
- `node --check app/static/sas_fabric_view.js` passed.
- `node --check admin_service/static/admin.js` passed.
- `node --check qa/public-demo.spec.js` passed.
- `docker compose --profile admin up -d --build --force-recreate` plus an
  explicit history-sidecar recreate rebuilt the local UI/history/admin images
  and ran all three containers healthy on `8080` / `8081` / `8082`.
- Local UI `/livez` and history `/livez` returned `status=ok`,
  `version=0.20.1`.
- Local `/healthz` returned `status=ok`.
- Forced Archive CORE inventory returned `60` present slots and available
  diagnostics, history, identify, inventory, physical slot, and SMART
  capabilities.
- Forced Archive CORE Storage Fabric returned `available=true`, `controllers=2`,
  `paths=3`, `traces=63`, `links=467`, and decoded MPR/CAM evidence.
- Forced Offsite SCALE Storage Fabric returned `fabric_kind=linux_ses`,
  `paths=1`, `traces=25`, `links=99`, and bay identity including
  `WUH721414AL4204`, `9RKSV2KC`, `12.7 TiB`, `5000cca264d473d4`, HCTL
  `1:0:1:0`, and SMART candidate `sdc`.
- Forced Quantastor Storage Fabric returned `fabric_kind=storage_quantastor`,
  `traces=25`, `links=69`, and source-provenance warning copy.
- Forced ESXi Storage Fabric returned `fabric_kind=storage_esxi`, `paths=6`,
  `traces=12`, `links=28`, and read-only controller/member warning copy.
- Custom browser RC smoke passed for CORE Disk Path, CORE Impact Map, SCALE
  Disk Path, Quantastor Disk Path, ESXi Disk Path, CORE/SCALE/Quantastor/ESXi
  main pages, and admin setup, with no page console errors.
- Fresh screenshot evidence was saved under `artifacts/rc-*.png`.
- `.\.venv\Scripts\python.exe scripts\build_public_demo.py --output public-demo\index.html`
  regenerated the public demo artifact.
- `.\.venv\Scripts\python.exe scripts\build_public_demo.py --output public-demo\index.html --check`
  passed.
- `.\.venv\Scripts\python.exe scripts\check_public_demo_artifact.py public-demo`
  passed.
- `PUBLIC_DEMO_ARTIFACT=public-demo/index.html npx playwright test` passed
  `27` / `27` browser tests against the final `0.20.1` local stack.

## Known Limits

- CORE remains the only platform with deep SAS/HBA/expander diagnostic detail.
- Non-CORE Storage Fabric maps are source-labeled best-effort views, not proof
  of complete SAS topology.
- Richer Linux `/sys/class/sas_*`, NVMe subsystem presentation, Quantastor HA
  owner clarity, ESXi StorCLI/PercCLI breadth, BMC-only source labeling, and
  further decoder growth are intentionally deferred to `0.22.x`.
- `0.21.x` should be used as a code-quality and test-confidence pitstop.
