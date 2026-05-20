# Release Wrap - v0.20.0

Date: `2026-05-20`

## Scope

`0.20.0` ships the first production SAS Fabric/topology slice for TrueNAS CORE.

The release is intentionally read-only. It adds SAS topology explanation,
diagnostic evidence, and operator-friendly naming around the existing enclosure
UI without introducing SAS write actions, cable-management automation, or
hardware-counter claims that the current CORE evidence cannot prove.

## What This Release Locks In

- normalized SAS Fabric graph shape: nodes, links, traces, controllers, paths,
  evidence, warnings, and raw source context
- CORE `mprutil` discovery with narrow read-only sudo rules
- `/var/log/messages` timestamped MPR/CAM event collection through the narrow
  optional tail rule, with `dmesg -a` fallback
- main-page `Topology` affordance synced to the physical enclosure view
- dedicated `/sas-fabric` workspace with `Fabric Lanes`, `Impact Map`,
  `Physical Trace`, and `Disk Path`
- `Fabric Inspector` drilldown language and behavior
- persisted friendly aliases for topology objects while retaining raw IDs
- Disk Path branch rendering that keeps path branches separate from
  path-leg-scoped fault evidence
- decoded kernel evidence with compact top findings and a paged/filterable full
  event table
- source-scoped decoder modules and confidence labels for standard,
  vendor-reference, observed, and unconfirmed decodes
- T10-backed decoder coverage for common service-action, 12-byte CDB, LOG SENSE,
  and peripheral write-fault evidence, with unknown numeric values kept visible
- HBA PCIe slot enrichment from CORE PCI/DMI/sysctl evidence
- focused Archive CORE bad-cable fixtures and regression coverage

## Validation

Current closeout validation before the release cut:

- `python -m unittest discover -s tests -p "test_*.py" -v` passed with
  `428` tests after the final release metadata pass.
- `python -m unittest tests.test_sas_fabric.SasFabricParserTests -v` passed
  with `12` tests.
- `python -m unittest tests.test_sas_fabric tests.test_parsers tests.test_inventory tests.test_admin_service.MainAppBoundaryTests -v`
  passed with `206` tests.
- SAS Fabric/diagnostics modules passed `py_compile`.
- `python -m compileall app admin_service history_service scripts tests`
  passed.
- `node --check app/static/app.js` passed.
- `node --check app/static/sas_fabric_view.js` passed.
- `node --check admin_service/static/admin.js` passed.
- `docker compose up -d --build enclosure-ui enclosure-history enclosure-admin`
  rebuilt/recreated the full local stack.
- `npx playwright test` passed with `27` tests.
- UI and admin `/livez` returned `0.20.0-dev`; history `/healthz` returned
  `status=ok`; all three Compose services were healthy.
- Forced live `/api/sas-fabric?system_id=archive-core&force_refresh=true`
  returned `available=true`, `warnings=0`, `controllers=2`, `traces=63`, and
  `400` controller event-table rows with source/confidence labels.
- Repo line-ending policy is now explicit through `.gitattributes` and
  `.editorconfig`; dirty text files were LF-normalized, and `git diff --check`
  now passes without CRLF normalization warnings.
- Four optional-sidecar runtime modes were live-smoked from the same local
  Compose/dev stack and the stack was restored to full service afterward:
  UI only, UI + history, UI + admin, and UI + history + admin.
- CSV-backed main UI perf harness was refreshed with label
  `release-candidate-0.20.0-sas-fabric-local`; artifacts were written to
  `data/perf/latest.*`, `data/perf/history.csv`, and
  `data/perf/history.jsonl`. Cached paths stayed healthy, while forced
  inventory averaged `19803.5 ms` on local Windows Docker and should be
  compared against the Linux release target before final ship/no-ship.
- CSV-backed history-sidecar perf harness was refreshed with label
  `release-candidate-0.20.0-history-local`; artifacts were written to
  `data/history-perf/latest.*`, `data/history-perf/history.csv`, and
  `data/history-perf/history.jsonl`. `overview_estimated` averaged `110.4 ms`
  with `227` tracked slots and `841858` metric samples.
- Linux QA Docker restore gate passed on `2026-05-20` using
  `codex-dev-test-target` (`10.13.37.138`) with an isolated stack under
  `/docker-local/truenas-jbod-ui-qa-0.20.0-20260520-002021` on
  `18080` / `18081` / `18082`.
- The restore-grade bundle was exported from the local Windows admin API and
  imported through the Linux QA admin API. Restored state showed `11` systems,
  `19` profiles, `2` Archive CORE storage views, `22` history scopes, and
  healthy UI/history/admin services reporting `0.20.0-dev`.
- The Linux QA stack was built from the current dirty `0.20.0-dev` source
  package. Local SSH material was copied only into the isolated QA runtime so
  live SAS Fabric validation could use the restored Archive CORE config without
  mounting long-running config directories.
- Restored Linux `/api/sas-fabric?system_id=archive-core&force=true` returned
  `available=true`, `controllers=2`, `traces=63`, `paths=3`, `warnings=0`,
  and current MPR/CAM evidence on `mpr0` with `47` event rows and `6` top
  findings.
- Browser QA against the restored Linux ports passed `27` / `27` tests.
- CSV-backed Linux restore perf harnesses passed with labels
  `release-candidate-linux-qa-restore` and
  `release-candidate-history-linux-qa-restore`. The first main harness attempt
  collided with a restored forced history collection and timed out; the rerun
  after that background pass settled is the recorded passing run. Checklist
  lesson: after import, wait for `:18081/healthz` to show
  `collection_running=false` before running perf, and do not run main/history
  perf harnesses in parallel against the restored stack.
- Snapshot export gate passed on the restored Linux UI: Auto estimated HTML,
  forced ZIP produced a redacted ZIP artifact, and that real exported artifact
  opened offline through Playwright.
- Restored SAS Fabric UI gate passed on `/sas-fabric`: Disk Path fault
  evidence, decoded event-table pagination, and alias save/clear persistence
  all worked.
- Evidence-handling lessons were folded into `docs/RELEASE_CHECKLIST.md`: raw
  admin import responses can echo configured systems and secret-bearing fields,
  so keep scrubbed summaries only; live SSH material must live only in the
  isolated QA runtime; and SAS Fabric diagnostic summaries should read the
  current controller `kernel_diagnostics` payload.
- Long-running stacks were not disturbed: local Windows remained healthy on
  `8080` / `8081` / `8082`, and the long-running Linux UI/history stack
  remained healthy on `8080` / `8081` with admin `8082` still intentionally
  absent.

## Remaining Post-Publish Gate

The standard Linux QA Docker restore gate captured in
`docs/RELEASE_CHECKLIST.md` is now complete for the release-candidate source:

- evidence lives under
  `artifacts/release-qa-0.20.0-linux-restore-20260520-002021/`
- the temporary Linux QA stack is still running intentionally so it can remain
  available until the post-publish deployment sniff test passes
- keep the disposable QA stack available until after the release is pushed and
  the new image is published, then update the local Windows, Linux, and
  production instances cleanly and record a final health/version/UI sniff test
  for each one
- after that post-publish sniff is clean, tear down only the temporary Linux QA
  restore containers/networks/runtime directories and prove the long-running
  stacks were not disturbed

## Checkpoint Scope

The release checkpoint should include the tracked SAS Fabric source, admin/setup
permission updates, docs/wiki updates, SAS fixtures/tests, and new source docs:

- `app/services/sas_fabric.py`
- `app/services/sas_fabric_alias_store.py`
- `app/services/sas_diagnostics/`
- `app/static/sas_fabric_view.js`
- `app/templates/sas_fabric.html`
- `tests/test_sas_fabric.py`
- `tests/fixtures/sas_fabric/archive_core_bad_cable_dmesg.txt`
- `.gitattributes`
- `.editorconfig`
- `.gitignore`
- `docs/V0_20_SAS_FABRIC_PLAN.md`
- `docs/SAS_DIAGNOSTIC_DECODER_SOURCES.md`
- `docs/RELEASE_CHECKLIST.md`
- `docs/RELEASE_NOTES_0.20.0.md`
- `docs/RELEASE_WRAP_0.20.0.md`

Leave the local evidence/config files out unless they are explicitly promoted:

- `artifacts/`
- `config/profiles.yaml`
- `data/known_hosts`
- `docs/V0_11_0_PLAN.md`

## What Still Rolls Forward

- Full Broadcom/LSI MPI/MPR `loginfo` coverage remains a future decoder-growth
  project.
- A proven CORE-safe source for persistent SAS PHY hardware counters is still
  open. Kernel event evidence should remain labeled as recent event evidence.
- Supermicro CSE-946/BPN SES element mapping needs a stronger source before
  the UI should claim more than inferred backplane zones.
- Operator-pinned expander/enclosure ordering can be added later if the current
  inferred Fabric Lanes order is not stable enough across hardware.
- Final version/date metadata is set; tag, push, image publication, and
  post-publish sniff still need to run before declaring `0.20.0` complete.
- After GHCR publishes the new image, update local Windows, Linux, and
  production deployments and keep the temporary Linux QA restore stack around
  until that final sniff test passes.
- After the tag is published, reopen on `0.20.1-dev` and carry only concrete
  follow-up items forward.
