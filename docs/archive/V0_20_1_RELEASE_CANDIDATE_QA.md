# v0.20.1 Release Candidate QA

Status: draft release gate for `0.20.1`.

Use this as the release-specific addendum to `docs/RELEASE_CHECKLIST.md`.
The goal for `0.20.1` is high-confidence coverage of the Storage Fabric
polish already landed, not one more feature bite.

## Feature Cutoff

`0.20.1` is now in release-candidate mode. Do not add new platform enrichment
unless a failing test or live UI regression proves it is required for the
release.

Defer richer feature work to `0.22.x`, including:

- deeper Linux `/sys/class/sas_*` and NVMe subsystem presentation
- Quantastor HA owner, SES host, and node clarity beyond the current contract
- broader ESXi StorCLI/PercCLI controller/tool coverage
- BMC-only source labeling beyond current best-effort maps
- additional source-backed decoder table growth
- major Storage Fabric readability or map model changes not tied to a bug

Use `0.21.x` for code quality, test reliability, and maintenance hardening.

## Checklist A - RC Validation

Run this before ship/no-ship:

- `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`
- `.\.venv\Scripts\python.exe -m py_compile app/services/sas_fabric.py app/services/inventory.py app/services/parsers.py app/services/system_setup.py admin_service/main.py tests/test_sas_fabric.py tests/test_inventory.py tests/test_admin_service.py`
- `node --check app/static/app.js`
- `node --check app/static/sas_fabric_view.js`
- `node --check admin_service/static/admin.js`
- `node --check qa/public-demo.spec.js`
- `git diff --check`
- confirm `git diff --cached --name-only` is empty before intentionally staging
- confirm no ignored local evidence/config has been staged

Run focused live API checks against the local RC stack:

- `/livez`, `/healthz`
- `/api/inventory?system_id=archive-core&force=true`
- `/api/sas-fabric?system_id=archive-core&force=true`
- `/api/inventory?system_id=offsite-scale&force=true`
- `/api/sas-fabric?system_id=offsite-scale&force=true`
- `/api/sas-fabric?system_id=qsosn-ha&force=true`
- `/api/sas-fabric?system_id=esxi-ft-node-2&force=true`

Expected high-signal observations:

- Archive CORE Storage Fabric remains available with two controllers, populated
  paths/traces/links, decoded diagnostic event table evidence, and stable Disk
  Path click behavior.
- SCALE Front 24 Bay remains available as `linux_ses`, uses `/dev/sg26`, maps
  24 bays, and promotes enclosure-view identity into bay trace/path data.
- Quantastor remains a read-only Storage Fabric graph when snapshot evidence is
  present, with honest source-provenance warnings when endpoints are partial.
- ESXi remains read-only and uses controller/member evidence without implying
  unsupported RAID write actions.
- Unsupported or weak-evidence platform states are explicit and do not pretend
  to expose CORE-style HBA/expander detail.

Run browser smokes:

- dedicated `/sas-fabric?system_id=archive-core&mode=disk`
- dedicated `/sas-fabric?system_id=archive-core&mode=impact`
- dedicated `/sas-fabric?system_id=offsite-scale&mode=disk`
- dedicated `/sas-fabric?system_id=qsosn-ha&mode=disk`
- dedicated `/sas-fabric?system_id=esxi-ft-node-2&mode=disk`
- main enclosure page for CORE, SCALE, Quantastor, and ESXi saved/live views
- admin setup/requirements page

Expected browser observations:

- no page console errors
- no nested picker scrollbar regression on SCALE Front 24 Bay
- no Disk Path first-click branch reorder on Archive CORE
- SAS/SES/Storage Path cards are clickable local inspector selectors, not
  reverse hops to Impact Map
- SCALE selected disk summary, Disk card, Path Members, and Selected Bay
  inspector visibly show reused enclosure identity such as model, serial, size,
  LUN, SG device, HCTL, block size, and SMART candidate device
- diagnostic table Impact/Type columns do not overlap
- Quantastor/ESXi/Linux copy does not leak CORE-only HBA/SAS/expander claims

## Checklist B - Release Docs, Wiki, And Demo Site

Do this after Checklist A is green:

- draft `docs/RELEASE_NOTES_0.20.1.md`
- draft `docs/RELEASE_WRAP_0.20.1.md`
- move the `CHANGELOG.md` `Unreleased` bullets into a `0.20.1` section
- update app/package metadata from `0.20.1-dev` to `0.20.1`
- review `README.md`, `docs/ROADMAP.md`, and platform docs for stale
  `0.20.0` or pre-Storage-Fabric wording
- review checked-in `wiki/` pages that mention setup, Storage Fabric, platform
  support, public demo, GHCR, and troubleshooting
- regenerate or intentionally retain screenshot assets; record the decision in
  the wrap doc either way
- refresh the public demo artifact from the current branch:
  `.\.venv\Scripts\python.exe scripts\build_public_demo.py --output public-demo\index.html`
- verify the demo artifact:
  `.\.venv\Scripts\python.exe scripts\check_public_demo_artifact.py public-demo`
- run the public demo smoke:
  `set PUBLIC_DEMO_ARTIFACT=public-demo/index.html`
  `npx playwright test qa/public-demo.spec.js`
- if wiki pages changed, sync the checked-in `wiki/` tree to the external wiki
  repo after the release docs pass
- after merge/tag/publish, run the public demo Pages workflow or confirm the
  workflow published the refreshed checked-in `public-demo/` artifact

## Evidence To Capture

- command outputs for the full unit discovery, syntax checks, `git diff --check`,
  and no-staged-files check
- API summaries for CORE, SCALE, Quantastor, and ESXi Storage Fabric payloads
- browser screenshot artifacts for the dedicated Storage Fabric pages above
- public demo artifact check output and Playwright output
- wiki commit hash if the external wiki is pushed
- GHCR digest and GitHub release URL after publish

## Ship Blockers

- any failing unit/syntax/hygiene check
- any live UI console error on the release-facing Storage Fabric paths
- Archive CORE Disk Path branch order changing after clicking local cards
- SCALE Storage Fabric losing enclosure-view disk identity in the dedicated UI
- stale wiki/docs that tell operators to use obsolete setup commands
- a public demo artifact that fails static checks or Playwright smoke
