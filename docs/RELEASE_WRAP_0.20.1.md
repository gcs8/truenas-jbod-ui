# Release Wrap - v0.20.1

Date: `2026-05-21`

## Scope

`0.20.1` wraps the operator-review-driven Storage Fabric polish that followed
the first `0.20.0` SAS Fabric release.

The release stays read-only. It does not add write actions, cabling automation,
or new certainty beyond collected evidence. It improves how existing evidence
is named, grouped, clicked, tested, and documented across CORE, SCALE/Linux,
Quantastor, ESXi, generic Linux, and BMC-backed systems.

## What This Release Locks In

- `Storage Fabric` as the operator-facing name for the dedicated fabric surface
- route/API compatibility for existing `/sas-fabric` links
- typed platform capability/status payloads for inventory snapshots
- platform fixture-pack coverage for SCALE, Quantastor, Linux/NVMe, and ESXi
- platform-specific admin setup requirement guidance
- SCALE/Linux SES-backed Storage Fabric maps from SG enclosure evidence
- optional Linux/NVMe subsystem enrichment as guarded best-effort evidence
- Quantastor, ESXi, Linux, and BMC source-provenance labeling
- local Disk Path card selection without reverse Impact Map hops
- stable Disk Path active-bay memory while inspecting host/HBA/path cards
- reused SCALE enclosure-view identity in Storage Fabric bay/path surfaces
- pool-level `spares` grouping in topology and the public demo
- explicit `0.21.x` code-quality pitstop and `0.22.x` enrichment parking lot

## Validation

The `0.20.1` release-candidate gate from
`docs/V0_20_1_RELEASE_CANDIDATE_QA.md` was run locally.

- Full unit discovery: `459` tests passed.
- Python syntax gate: targeted `py_compile` command passed.
- JavaScript syntax gate: `app/static/app.js`,
  `app/static/sas_fabric_view.js`, `admin_service/static/admin.js`, and
  `qa/public-demo.spec.js` passed `node --check`.
- Local Docker gate: `docker compose --profile admin up -d --build --force-recreate`
  plus an explicit history-sidecar recreate rebuilt the local UI/history/admin
  images and left UI, history, and admin healthy.
- Live API gate:
  - UI `/livez`: `status=ok`, `version=0.20.1`
  - history `/livez`: `status=ok`, `version=0.20.1`
  - `/healthz`: `status=ok`
  - Archive CORE inventory: `60` present slots
  - Archive CORE Storage Fabric: `available=true`, `controllers=2`,
    `paths=3`, `traces=63`, `links=467`
  - Offsite SCALE Storage Fabric: `linux_ses`, `paths=1`, `traces=25`,
    `links=99`, model/serial/size/LUN/HCTL/SMART identity present
  - Quantastor Storage Fabric: `storage_quantastor`, `traces=25`,
    `links=69`, read-only source-provenance warning present
  - ESXi Storage Fabric: `storage_esxi`, `paths=6`, `traces=12`, `links=28`,
    read-only controller/member warning present
- Browser RC gate:
  - CORE Disk Path clickability and first-click branch stability passed
  - CORE Impact Map loaded with current fault evidence
  - SCALE Disk Path showed reused model/serial/size/LUN/HCTL/SMART identity and
    no nested picker scrollbar
  - Quantastor and ESXi Disk Path pages used platform/source-specific copy with
    no CORE-only leakage
  - CORE, SCALE, Quantastor, and ESXi main pages loaded cleanly
  - admin setup rendered platform requirement guidance
  - no page console errors were observed
- Public demo gate:
  - `public-demo/index.html` regenerated from the current branch
  - public demo freshness check passed
  - public demo publishability checker passed
  - `PUBLIC_DEMO_ARTIFACT=public-demo/index.html npx playwright test` passed
    `27` / `27` browser tests against the final `0.20.1` local stack

## Evidence

Fresh RC screenshots were saved locally under `artifacts/`:

- `rc-final-storage-fabric-core-disk.png`
- `rc-final-storage-fabric-core-impact.png`
- `rc-final-storage-fabric-scale-disk.png`
- `rc-final-storage-fabric-quantastor-disk.png`
- `rc-final-storage-fabric-esxi-disk.png`
- `rc-final-admin-setup.png`

Earlier same-gate screenshots from the pre-metadata-bump smoke are also local
as `rc-storage-fabric-*.png` and `rc-admin-setup.png`.

Main-page release screenshots from the RC pass were also saved:

- `rc-main-core.png`
- `rc-main-scale.png`
- `rc-main-quantastor.png`
- `rc-main-esxi.png`

The old `docs/V0_11_0_PLAN.md` draft was moved out of active docs to
`artifacts/deferred-docs/V0_11_0_PLAN.md` for later review.

## Docs And Wiki

- `docs/RELEASE_NOTES_0.20.1.md` records the operator-facing release notes.
- `docs/V0_20_1_RELEASE_CANDIDATE_QA.md` records the release-specific gate.
- `docs/V0_21_CODE_QUALITY_PITSTOP_PLAN.md` defines `0.21.x` as a maintenance
  and coverage cycle.
- `docs/V0_22_STORAGE_FABRIC_ENRICHMENT_NOTES.md` parks richer feature work.
- Checked-in wiki public-demo wording now uses pool-level `spares`.
- Wiki Home now describes `0.20.1` as the current stable Storage Fabric polish
  release.

External wiki sync is still a publish step. Do it with the normal wiki publish
flow when the release commit is ready.

## Checkpoint Scope

Include the tracked Storage Fabric code, tests, docs, wiki, and regenerated
public demo artifact. Keep local evidence/config out unless explicitly
promoted:

- include `public-demo/index.html`
- include `docs/RELEASE_NOTES_0.20.1.md`
- include `docs/RELEASE_WRAP_0.20.1.md`
- include checked-in `wiki/` wording changes
- leave `artifacts/` local
- leave local config, known-hosts, and runtime databases local

## What Rolls Forward

- `0.21.x`: code quality, ownership cleanup, helper extraction, test
  reliability, and release automation polish.
- `0.22.x`: deeper Linux `/sys/class/sas_*` and NVMe detail, Quantastor HA
  owner/SES-host clarity, ESXi StorCLI/PercCLI breadth, BMC-only labeling, and
  further decoder-source growth.

## Remaining Publish Gate

- commit/tag `v0.20.1`
- push and create the GitHub release
- wait for GHCR publish and verify digest convergence
- sync the external GitHub wiki if the release docs/wiki diff is accepted
- refresh the public/demo deployment workflow or confirm the Pages workflow
  published the checked-in `public-demo/` artifact
