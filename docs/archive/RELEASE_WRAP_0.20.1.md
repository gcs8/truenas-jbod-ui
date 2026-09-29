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

External wiki sync completed at wiki commit `317b677` after the release cut.
The public demo Pages workflow completed as run `26203479985`, and the live
Pages artifact returned HTTP `200` with `0.20.1` and `Storage Fabric` present.

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

## Publish Result

- Release commit: `011bd1d`
- Tag: `v0.20.1`
- GitHub release:
  `https://github.com/gcs8/truenas-jbod-ui/releases/tag/v0.20.1`
- GHCR workflow run: `26203521398`
- GHCR tags `v0.20.1`, `0.20.1`, and `latest` converged to digest
  `sha256:e073f4e561c96379fe13b3064f24100781d69b39d48ed7ff5d31fde50f4163f3`
- External wiki sync: `317b677`
- Public demo workflow: `26203479985`

## Post-Publish Checklist Audit

After publication, the release process was audited against
`docs/RELEASE_CHECKLIST.md`. The release validated a broad local
release-candidate gate, but it used
`docs/V0_20_1_RELEASE_CANDIDATE_QA.md` as the active checklist instead of
recording a completed item-by-item evidence table from the global release
checklist.

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | `011bd1d` on `main`, tag `v0.20.1`, release branch reopened afterward as `0.21.0-dev` | Pass |  |
| Python unit and syntax gates | yes | full unit discovery passed `459` tests; targeted Python syntax gates passed | Pass |  |
| JavaScript syntax gates | yes | `node --check` passed for app, Storage Fabric view, admin, and public-demo QA files | Pass |  |
| Docker build and health gates | yes | local UI/history/admin Docker rebuild and health checks passed with `/livez` reporting `0.20.1` | Pass |  |
| Optional-sidecar runtime matrix | yes | not explicitly run and recorded before tag | Blocked |  |
| Full Playwright/browser gates | yes | full Playwright passed `27` / `27` against the final local `0.20.1` stack | Pass |  |
| Feature-specific live API/UI gates | yes | CORE, SCALE, Quantastor, ESXi, and admin Storage Fabric/API/browser RC smokes passed | Pass |  |
| Local release perf harnesses | yes | not explicitly run and recorded before tag | Blocked |  |
| Linux QA restore gate | yes | not explicitly run and recorded before tag | Blocked |  |
| Restored Linux QA perf harnesses | yes | not explicitly run and recorded before tag | Blocked |  |
| Snapshot/export/offline artifact gate | yes | restored-stack snapshot/offline smoke was not explicitly run and recorded before tag | Blocked |  |
| Docs/wiki/public-demo gate | yes | release docs, checked-in wiki source, external wiki commit `317b677`, public demo workflow `26203479985` | Pass |  |
| GHCR publish verification | yes | GHCR workflow `26203521398`; `v0.20.1`, `0.20.1`, and `latest` converged to digest `sha256:e073f4e561c96379fe13b3064f24100781d69b39d48ed7ff5d31fde50f4163f3` | Pass |  |
| Deployment refresh/sniff tests | yes | post-GHCR local Windows, Linux Docker, and production deployment refresh/sniff evidence was not explicitly recorded | Blocked |  |
| Post-release reopen | yes | branch `codex/v0.21.0-kickoff-2026-05-21-post-0.20.1`, commit `4d20569`, app/package metadata `0.21.0-dev` | Pass |  |

Confirmed evidence from the shipped `0.20.1` gate:

- full Python unit discovery passed with `459` tests
- targeted Python and JavaScript syntax gates passed
- local UI/history/admin Docker rebuild and health checks passed
- full Playwright passed `27` / `27`
- Storage Fabric API/browser smokes covered CORE, SCALE, Quantastor, ESXi, and
  admin setup paths
- public demo generation, static checks, and Playwright passed
- GitHub release, GHCR digest convergence, external wiki sync, and Pages deploy
  completed

Release-checklist gaps that were not explicitly run and recorded before the
tag:

- optional-sidecar runtime matrix for UI-only, UI+history, UI+admin, and full
  stack modes
- release performance harnesses for both local and restored Linux QA stacks
- Linux QA Docker restore/import gate from an exported admin backup
- snapshot export/download and offline artifact smoke against the restored
  Linux QA stack
- post-publish local Windows, Linux Docker, and production deployment
  refresh/sniff evidence after GHCR publish

Process correction:

- `docs/RELEASE_CHECKLIST.md` is now the mandatory release gate for every tag.
  Release-specific QA docs are addenda only.
- every future release wrap must include a completed checklist evidence table
  with `Pass`, `Blocked`, or `N/A` plus concrete evidence or reasons.
- already-published public artifacts should not be deleted, overwritten, or
  retagged except for malicious or catastrophic artifacts; use a SemVer patch
  correction release if a shipped release needs process remediation.
