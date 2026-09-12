# Release Notes - v0.14.0

Release date: `2026-04-26`

## Summary

`0.14.0` is the ESXi-readiness and runtime-responsiveness release.

The goal of this release is to add a narrow, operator-honest VMware ESXi path
without bloating the app into a generic hypervisor manager, while also making
the already validated CORE, SCALE, Linux, Quantastor, and storage-view paths
feel snappier during normal switching. This release keeps the ESXi slice
intentionally read-only, adds clear admin-side guardrails for non-Linux SSH
targets, and tightens the stale-cache-first read path so validated systems do
not keep paying the same full rebuild cost on every view hop.

## Highlights

- First-pass read-only VMware ESXi support:
  - new `esxi` platform option in setup and runtime
  - SSH `esxcli` plus StorCLI JSON parsing on the validated Supermicro
    `AOC-SLG4-2H8M2` host path
  - built-in `2`-slot AOC carrier profile plus photo-backed live rendering for
    physical RAID members `13:0` and `13:1`
  - LED, RAID-write, and Linux bootstrap flows stay intentionally disabled
- Runtime switching is less punishing on already known systems:
  - normal read paths are back to stale-cache-first behavior for page load and
    system/enclosure switching
  - `/livez` is now the lightweight container-health route
  - `/healthz` reports cached dependency status without forcing a fresh
    inventory build every time a probe lands
  - per-slot mutations now invalidate only the active enclosure/default scope
    instead of clearing the whole snapshot cache
- Quantastor LED truth no longer blocks the main read path:
  - the first render can return from the normal cached path
  - a Quantastor-only background follow-up verify keeps out-of-band identify
    state checks alive without making every switch wait on them
- The admin sidecar now tells the truth about ESXi:
  - the recommended saved SSH user stays `root`
  - saved sudo-password fields are hidden for ESXi
  - the Linux one-time bootstrap and sudoers preview flow are disabled for that
    platform instead of pretending the host supports Linux sudo

## Operator Notes

- ESXi support is intentionally narrow in this first pass:
  - SSH-only
  - read-only
  - validated on the `AOC-SLG4-2H8M2` Broadcom/SAS3808 host path
  - no LED identify path, RAID writes, or Linux bootstrap/sudo flow
- ESXi detail still participates in the same main UI and optional history
  workflows as the other validated systems. When the history sidecar is up, the
  drawer can still appear for saved ESXi slots; when it is absent, the runtime
  simply stays read-only without that extra history surface.
- The local Windows Docker secondary baseline is still slower than the Linux
  dev target for the `history_status` path. That caveat remains visible in the
  perf harness, but the branch no longer shows a new functional switching
  regression.

## Validation Snapshot

Validated on `codex/v0.14.0-release-prep-2026-04-26` after fresh local Docker
rebuilds:

- targeted release-checklist Python suite:
  - `.\.venv\Scripts\python.exe -m unittest tests.test_profiles tests.test_inventory tests.test_history_service tests.test_perf tests.test_perf_harness tests.test_snapshot_export -v`
  - result: `139` tests passed
- broad browser smoke:
  - `npx playwright test`
  - result: `15` passed / `1` skipped
- focused ESXi/admin browser smoke:
  - `npx playwright test qa/admin-operations.spec.js qa/esxi-smoke.spec.js`
  - result: `5` passed
- runtime and syntax sanity:
  - `docker compose --profile history --profile admin up -d --build`
  - `node --check app/static/app.js`
  - `node --check qa/admin-operations.spec.js`
  - `node --check qa/esxi-smoke.spec.js`
  - `GET http://127.0.0.1:8080/livez`
  - `GET http://127.0.0.1:8080/healthz`
  - `GET http://127.0.0.1:8081/healthz`
  - `GET http://127.0.0.1:8082/healthz`
- perf harness:
  - `.\.venv\Scripts\python.exe scripts/run_perf_harness.py --base-url http://127.0.0.1:8080 --iterations 3 --format markdown --label release-candidate-esxi-prep`
  - `inventory_force` avg `6841.2 ms`
  - `history_status` avg `5452.6 ms`
  - `snapshot_export_estimate` avg `4127.8 ms`
  - compared with the `0.13.0` baseline, `snapshot_export_estimate` improved
    sharply while the known local Windows `history_status` caveat remained the
    main slower path
- refreshed `v0.14.0` screenshots captured and staged under:
  - `docs/images/screenshots/`
  - `wiki/images/`

## Checked-In Artifacts

Release-facing artifacts for this cut should include:

- refreshed `v0.14.0` screenshot set for README/wiki references
- `docs/ESXI_PLATFORM_FEASIBILITY.md` capturing the validated adapter shape and
  host observations for the new ESXi path
- README/wiki refreshes that explain:
  - the new read-only ESXi support envelope
  - the split between `/livez` and cached `/healthz`
  - the current GHCR deployment examples for the `0.14.0` release

## Deployment Notes

- App version for the release commit is `0.14.0`.
- Operators should re-review:
  - `README.md`
  - `docs/RELEASE_CHECKLIST.md`
  - `docs/ESXI_PLATFORM_FEASIBILITY.md`
  - `wiki/Quick-Start.md`
  - `wiki/Docker-and-GHCR-Deployment.md`
  - `wiki/Admin-UI-and-System-Setup.md`
  - `wiki/SSH-Setup-and-Sudo.md`
- Final release prep for this cut should still include:
  - a final `git status` / commit-shape review
  - external wiki publish if the checked-in `wiki/` tree changed

## Suggested GitHub Release Intro

`0.14.0` adds a narrow, read-only VMware ESXi path on the validated
Supermicro `AOC-SLG4-2H8M2` host, using SSH `esxcli` plus StorCLI to map the
two RAID members back onto a photo-backed carrier-card view. It also tightens
the read path for the already validated systems with stale-cache-first
switching, a lightweight `/livez` health route, cached `/healthz` status, and
smaller invalidation scope after slot mutations.
