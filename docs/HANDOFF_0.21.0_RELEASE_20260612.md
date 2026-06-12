# v0.21.0 Release Handoff - 2026-06-12

Status timestamp: `2026-06-12T03:35:10Z`

This handoff is for a fresh session to continue the `gcs8/truenas-jbod-ui` `v0.21.0` release from the current release-final worktree. Treat `docs/RELEASE_CHECKLIST.md` and `docs/RELEASE_WRAP_0.21.0.md` as the source of truth; this file is a practical pickup note.

## Current state

- Worktree path: `/home/gcs8/workspace/truenas-jbod-ui-platform-route-registry-20260522`
- Active branch: `codex/v0.21.0-release-final-20260611`
- Upstream branch: `origin/codex/v0.21.0-release-final-20260611`
- Last committed release-prep commit: `c25a074 docs: update v0.21.0 release packet after SSH fanout merge`
- Latest merged main work included in this branch: `6fe534b Reduce SSH fanout for inventory enrichment (#7)`
- Public release/tag: **not cut**
- Ship state: automated pre-tag gates are now complete after the continuation
  update below, but the public tag/publish is still **not cut** and remains
  held for gcs8 human QA/acceptance plus the publish sequence.

## Continuation update - 2026-06-12T04:05Z

- Reviewed, secret-scanned, committed, and pushed the local release-gate changes
  as `535c61a chore: record v0.21.0 local release gates`.
- Ran the disposable Linux QA restore gate on `10.13.37.138` using
  `/docker-local/truenas-jbod-ui-qa-0.21.0-20260612T034656Z/repo` and ports
  `18080` UI, `18081` history, and `18082` admin.
- Restored the ignored Windows backup bundle through the disposable admin API,
  verified `11` restored systems, health on all three services, remote
  Playwright `26` passed / `1` intentional skip, feature API/UI checks,
  snapshot export/offline smoke, and serial main/history perf harnesses after
  waiting for the restored history collector to settle.
- Updated `docs/RELEASE_WRAP_0.21.0.md`: Linux QA restore, restored Linux QA
  perf, and Linux QA snapshot/export/offline evidence are now `Pass`.
- Strict pre-tag validation now passes:
  `.venv/bin/python scripts/validate_release_wrap.py 0.21.0 --phase pre-tag`.
- The disposable Linux QA stack is intentionally still running and should stay
  available until post-publish deployment sniff tests pass.
- Do **not** tag/publish until gcs8 accepts the running candidate and the final
  release mechanics are intentionally started.

Current tracked files modified before this handoff file was added:

- `CHANGELOG.md`
- `app/static/app.js`
- `docs/RELEASE_NOTES_0.21.0.md`
- `docs/RELEASE_WRAP_0.21.0.md`
- `docs/ROADMAP.md`
- `qa/ui-switching.spec.js`
- `wiki/Home.md`

This handoff file itself is also a new tracked-doc candidate: `docs/HANDOFF_0.21.0_RELEASE_20260612.md`.

## Safety boundaries

- Do **not** push a release tag until `scripts/validate_release_wrap.py 0.21.0 --phase pre-tag` passes **without** `--allow-blocked`.
- Do **not** commit ignored/private artifacts under `artifacts/`, `config/`, `data/`, `history/`, `logs/`, or SSH/credential material.
- Do **not** keep raw admin backup import/export responses as public evidence. They can echo configured systems and secret-bearing fields. Keep scrubbed summaries only.
- Linux QA restore must use a disposable stack on non-default ports `18080` / `18081` / `18082`; do not disturb long-running Windows/Linux deployments.
- Admin sidecar may need temporary remote binding for QA; do not leave it public-facing or long-running after validation.
- If live validation needs SSH keys/known-hosts/trust material, copy only into the isolated QA runtime directory. Never bind-mount a long-running deployment's config directories into the disposable stack.

## Local gates already completed

The local gates were rerun on the rebuilt release-candidate image after final local fixes. `docs/RELEASE_WRAP_0.21.0.md` rows 47-58 have been updated with evidence.

### Source / syntax / unit

Passed:

```bash
.venv/bin/python -m compileall -q app admin_service history_service scripts tests
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -q
node --check app/static/app.js
node --check app/static/sas_fabric_view.js
node --check admin_service/static/admin.js
node --check qa/public-demo.spec.js
node --check qa/ui-switching.spec.js
git diff --check
```

Evidence summary:

- Python unit discovery: `503` tests passed, `4` skipped.
- JS syntax checks passed.
- Diff whitespace check passed.

### Docker / health / optional sidecars

Passed on local rebuilt stack from `docker-compose.dev.yml`:

```bash
docker compose -f docker-compose.dev.yml --profile history --profile admin up -d --build
```

Evidence summary:

- UI `/livez` and `/healthz` on `8080`: `status=ok`, version `0.21.0`.
- History `/livez` and `/healthz` on `8081`: `status=ok`, version `0.21.0`.
- Admin `/livez` and `/healthz` on `8082`: `status=ok`, version `0.21.0`.
- UI-only, UI+history, UI+admin, and full-stack optional-sidecar matrix all passed.

### Browser / Playwright

Passed against local restored stack:

```bash
PYTHON=.venv/bin/python \
PLAYWRIGHT_BASE_URL=http://127.0.0.1:8080 \
PLAYWRIGHT_ADMIN_BASE_URL=http://127.0.0.1:8082 \
npx playwright test
```

Evidence summary:

- `26` passed.
- `1` skipped: intentionally skipped perf-only auto-refresh test.
- `qa/public-demo.spec.js` passed as part of the full run.

### Feature-specific local API/UI checks

Passed against the restored local full stack, covering:

- CORE
- SCALE
- GPU/Linux
- UniFi UNVR
- UniFi UNVR Pro
- Quantastor HA
- ESXi
- BMC/IPMI

API/browser surfaces checked:

- `/api/inventory`
- `/api/storage-views`
- `/api/sas-fabric`
- dedicated `/sas-fabric` pages
- first-click slot selection
- horizontal overflow
- admin ESXi/runtime surfaces
- browser console errors/warnings

Final browser console/layout probe passed with no `error`/`warning` console messages. It recorded `3` SMART fallback debug messages, which are expected after the local fix below.

### Local issue found and fixed

During release-facing browser probing, rapid system/page switches exposed noisy opportunistic SMART prefetch failures:

- symptom: `SMART prefetch single-request path failed TypeError: Failed to fetch` appeared as browser `console.error`
- fix: `app/static/app.js` now routes transient abort/fallback SMART prefetch failures to `console.debug`, while genuine SMART prefetch failures still degrade through slot SMART summary UI state
- validation: rebuilt image, reran console/layout probe, and reran full source/browser gates

`qa/ui-switching.spec.js` was also adjusted so restored-data browser coverage:

- selects occupied restored systems for heat-map value assertions
- allows real restored ESXi/remote inventory latency rather than fixture-only timeout assumptions

### Snapshot/export/offline local gate

Passed against local restored stack:

- export estimate: Auto -> HTML allowed; HTML `3.5 MiB`; ZIP `948.1 KiB`
- forced ZIP artifact: `artifacts/private-v0.21.0/local-snapshot-export-force-zip.zip`
- forced ZIP size: `970822` bytes
- forced ZIP SHA-256: `3067e0e04d91a6b729c88accf4f6658f38bea8ccf2ec925a3b09441ba8f5a8be`
- extracted offline HTML opened in Playwright with `11` systems, `60` tiles, and no console messages

### Local perf harnesses

Passed on final rebuilt stack:

```bash
.venv/bin/python scripts/run_perf_harness.py \
  --base-url http://127.0.0.1:8080 \
  --iterations 3 \
  --format markdown \
  --label release-candidate

.venv/bin/python scripts/run_history_perf_harness.py \
  --base-url http://127.0.0.1:8081 \
  --iterations 3 \
  --format markdown \
  --label release-candidate-history
```

Evidence summary:

- Main latest artifact: `data/perf/latest.md`
  - `inventory_cached` avg `3.8 ms`
  - `inventory_force` avg `21569.8 ms`
- History latest artifact: `data/history-perf/latest.md`
  - `overview_estimated` avg `3.7 ms`
  - DB `989.4 MiB`
  - metric samples `1,362,917`

### Docs/wiki/public-demo local gate

Passed:

- `CHANGELOG.md`, `docs/RELEASE_NOTES_0.21.0.md`, `docs/ROADMAP.md`, and `wiki/Home.md` updated for PR #7 plus local-gate hardening/current-version wording.
- stale current-version scan found no `0.21.0-dev` or old-current wording in README, roadmap, public-demo README, or wiki home.
- checked-in public-demo artifact passed:

```bash
.venv/bin/python scripts/check_public_demo_artifact.py public-demo
```

Evidence summary:

- `public-demo/index.html`: `7178450` bytes

## Private restore bundle currently available locally

Ignored/private local artifact:

- `artifacts/private-v0.21.0/windows-restore-default.tar.zst`
- source: Windows admin API export from the running Windows Codex stack, used only as restore-grade data evidence
- size: `34,089,681` bytes
- SHA-256: `0a6980f2e6da37fbe8763dd5a3cce744f234fae89d35bd4620dfffb6826aeb25`

Do not commit this bundle. Copy it only to the disposable Linux QA target if reusing it for the restore gate. Re-export through the admin API if you suspect it has gone stale.

## Immediate next steps for the new session

### 1. Rehydrate repo state and validate blocked-wrap shape

```bash
cd /home/gcs8/workspace/truenas-jbod-ui-platform-route-registry-20260522
git status --short --branch
git diff --stat
.venv/bin/python scripts/validate_release_wrap.py 0.21.0 --phase pre-tag --allow-blocked
```

Expected: validator should pass only with `--allow-blocked` while Linux QA and post-publish rows remain blocked. If it fails, fix the wrap shape before doing Linux QA.

### 2. Review and commit the current local-gate changes

Do a real diff review first:

```bash
git diff -- CHANGELOG.md app/static/app.js docs/RELEASE_NOTES_0.21.0.md docs/RELEASE_WRAP_0.21.0.md docs/ROADMAP.md qa/ui-switching.spec.js wiki/Home.md docs/HANDOFF_0.21.0_RELEASE_20260612.md
```

Suggested commit after review:

```bash
git add CHANGELOG.md app/static/app.js docs/RELEASE_NOTES_0.21.0.md docs/RELEASE_WRAP_0.21.0.md docs/ROADMAP.md qa/ui-switching.spec.js wiki/Home.md docs/HANDOFF_0.21.0_RELEASE_20260612.md
git commit -m "chore: record v0.21.0 local release gates"
git push origin codex/v0.21.0-release-final-20260611
```

Commit before Linux QA unless there is a reason to run QA from a local uncommitted rsync. A pushed branch makes the disposable target reproducible.

### 3. Run disposable Linux QA restore gate

Target from prior release process:

- host: `codex-dev-test-target` / `10.13.37.138`
- required ports: `18080` UI, `18081` history, `18082` admin
- use an isolated runtime such as `/docker-local/truenas-jbod-ui-qa-0.21.0-<UTC>`

Checklist source: `docs/RELEASE_CHECKLIST.md` lines 158-222.

High-level procedure:

1. On the QA target, create a fresh disposable runtime directory.
2. Clone or fetch the pushed release branch into that directory.
3. Create a QA-only compose override because `docker-compose.dev.yml` has fixed `container_name` values. Avoid collisions with long-running containers.
4. Set non-default ports and temporary bind addresses.
5. Build and start the full stack from current release-candidate source.
6. Import the restore-grade bundle through the disposable admin API.
7. Summarize restored counts/status only; do not retain raw import response as evidence.
8. Run health, browser, feature-specific, snapshot/offline, and perf gates against `18080`/`18081`/`18082`.
9. Update `docs/RELEASE_WRAP_0.21.0.md` rows for Linux QA restore and restored Linux QA perf.
10. Keep the disposable QA stack available until post-publish deployment sniff tests pass.

Suggested QA compose override shape, written inside the disposable QA checkout as `docker-compose.qa.yml`:

```yaml
services:
  enclosure-ui:
    container_name: truenas-jbod-ui-qa-0210
  enclosure-history:
    container_name: truenas-jbod-history-qa-0210
  enclosure-admin:
    container_name: truenas-jbod-admin-qa-0210
    environment:
      ADMIN_CONTAINER_UI_NAME: truenas-jbod-ui-qa-0210
      ADMIN_CONTAINER_HISTORY_NAME: truenas-jbod-history-qa-0210
      ADMIN_CONTAINER_ADMIN_NAME: truenas-jbod-admin-qa-0210
```

Suggested env for the disposable QA stack:

```bash
export APP_PORT=18080
export HISTORY_PORT=18081
export ADMIN_PORT=18082
export HISTORY_BIND_ADDRESS=0.0.0.0
export ADMIN_BIND_ADDRESS=0.0.0.0
export ADMIN_AUTO_STOP_SECONDS=3600
```

Suggested start command on the QA target:

```bash
docker compose -f docker-compose.dev.yml -f docker-compose.qa.yml --profile history --profile admin up -d --build
```

Suggested health checks from the QA target:

```bash
curl -fsS http://127.0.0.1:18080/livez
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:18081/livez
curl -fsS http://127.0.0.1:18081/healthz
curl -fsS http://127.0.0.1:18082/livez
curl -fsS http://127.0.0.1:18082/healthz
```

Suggested restore import endpoint on the QA target:

```bash
curl -fsS \
  -X POST \
  -H 'Content-Type: application/octet-stream' \
  --data-binary @/path/to/windows-restore-default.tar.zst \
  'http://127.0.0.1:18082/api/admin/backup/import?stop_services=true&restart_services=true'
```

Do not paste the raw response into docs. Parse/summarize only safe counts, e.g. systems, profiles, storage views, history scopes, and service health.

Suggested browser gate from the local release checkout, targeting the QA host:

```bash
PYTHON=.venv/bin/python \
PLAYWRIGHT_BASE_URL=http://10.13.37.138:18080 \
PLAYWRIGHT_ADMIN_BASE_URL=http://10.13.37.138:18082 \
npx playwright test
```

Before perf, wait for history collection to settle. Check `http://10.13.37.138:18081/healthz`; do not run perf while `collection_running=true`. Do not run main/history perf in parallel.

Suggested restored Linux QA perf commands:

```bash
.venv/bin/python scripts/run_perf_harness.py \
  --base-url http://10.13.37.138:18080 \
  --iterations 3 \
  --format markdown \
  --label release-candidate-linux-qa-restore

.venv/bin/python scripts/run_history_perf_harness.py \
  --base-url http://10.13.37.138:18081 \
  --iterations 3 \
  --format markdown \
  --label release-candidate-history-linux-qa-restore
```

Also run restored Linux snapshot export estimate/download/offline smoke equivalent to the local one, but save evidence under a versioned ignored artifact folder and record only safe summary/hash in the release wrap.

### 4. Strict pre-tag validation

After Linux QA rows are updated and all pre-publish blocked rows are cleared:

```bash
.venv/bin/python scripts/validate_release_wrap.py 0.21.0 --phase pre-tag
```

Expected: pass without `--allow-blocked`. If it does not pass, do not tag.

### 5. Publish sequence only after strict pre-tag pass

Follow `docs/RELEASE_CHECKLIST.md` lines 353-406. Summary:

1. Inspect final status/log.
2. Commit final release-prep evidence if needed.
3. Merge the release branch into `main` locally with a release commit.
4. Tag the merged `main` commit, not the side-branch tip.
5. Push `main` and the annotated tag.
6. Publish GitHub release notes from the final changelog section.
7. Wait for the GHCR publish workflow.
8. Verify GHCR tags/digest:
   - `ghcr.io/gcs8/truenas-jbod-ui:v0.21.0`
   - `ghcr.io/gcs8/truenas-jbod-ui:0.21.0`
   - `ghcr.io/gcs8/truenas-jbod-ui:latest`
9. Update local Windows, Linux, and production deployments cleanly.
10. Record health/version/UI sniff tests for each deployment.
11. Only after post-publish sniff passes, tear down the temporary Linux QA stack.
12. Reopen next development (`Unreleased`, next dev version/branch), then update `HANDOFF.md`/`TODO.md` if appropriate.

## Release wrap rows still blocked now

As of the continuation update, all automated pre-tag rows are `Pass`; these
post-publish rows intentionally remain blocked in
`docs/RELEASE_WRAP_0.21.0.md`:

- GHCR publish verification
- Deployment refresh/sniff tests
- Post-release reopen

Local and Linux QA snapshot/export and docs/wiki/public-demo rows are already
`Pass`.

## Do not redo unless source changes

Do not spend time rerunning all local gates unless one of these files changes again:

- `app/static/app.js`
- `qa/ui-switching.spec.js`
- release docs/wrap files that need validator coverage
- Docker/runtime config files
- code touched during Linux QA fixes

If any code changes happen, rerun the relevant syntax/unit/browser subset plus `git diff --check`, then update the release wrap.

## Known pitfalls from this release run

- Windows Codex checkout was useful as a restore data source only. Its code/docs were stale/dirty; do not merge code from it.
- Restored ESXi paths can take around a minute to settle. The browser suite now allows that; avoid reverting to fixture-only `20s` assumptions.
- Heat-map value assertions must target occupied restored systems, not default empty/unknown systems.
- SMART prefetch single-request failures during rapid navigation may be expected fallback/abort noise; the current code demotes those to debug. Do not hide real UI degradation; verify slot SMART summary state still degrades when prefetch truly fails.
- After restore, history sidecar may perform forced collection. Wait for `collection_running=false` before perf.
- The base dev compose uses fixed `container_name`; use a QA override with unique names on shared targets.

## Minimal pickup checklist

- [x] `git status --short --branch`
- [x] `scripts/validate_release_wrap.py 0.21.0 --phase pre-tag --allow-blocked`
- [x] review/commit/push current local-gate changes
- [x] run Linux QA restore on `10.13.37.138:18080/18081/18082`
- [x] update `docs/RELEASE_WRAP_0.21.0.md` with Linux QA evidence
- [x] run strict pre-tag validator without `--allow-blocked`
- [ ] gcs8 human QA/acceptance of the running Linux candidate
- [ ] only then merge/tag/publish and verify GHCR/deployments
