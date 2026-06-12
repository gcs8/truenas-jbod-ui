# v0.21.x Release Handoff - final as of 2026-06-12T18:25Z

This handoff is for the next Hermes/Codex session continuing the `gcs8/truenas-jbod-ui` `v0.21.0` release. It supersedes the earlier notes in this file: the first Linux QA attempts used incomplete/sanitized data and must not be treated as release evidence.

## Non-negotiable state

- Worktree: `/home/gcs8/workspace/truenas-jbod-ui-platform-route-registry-20260522`
- Branch: `codex/v0.21.0-release-final-20260611`
- Final release tag: **`v0.21.1`** at `cfb92f2576f7c0d0d7fdd3b3ff58918897c0fe7c`
- GitHub release: `https://github.com/gcs8/truenas-jbod-ui/releases/tag/v0.21.1`
- GHCR digest: `sha256:28e38a92dd77b9526cf2367bf151b44fafa85a82e425434fdbdc95c56a6ac6d1`
- Current release state: **published, GHCR verified, source and QA refreshed by digest, issue #6 closed, post-release development reopened as `0.21.2-dev`**.
- `v0.21.0` remains public audit history but is superseded by `v0.21.1` for deployment.

## What changed in the last session

### Corrected the real source of truth

The original real source was `10.13.37.67:8080/8081/8082`, not the partial long-running `.138` dataset.

Root cause found:

- Admin backup/export requests with `stop_services=true&restart_services=true` stopped `.67` UI/history.
- Docker then killed the UI after its 30-second stop timeout while background work was still running.
- The admin export path also builds a history snapshot in memory, so it is risky for this large history DB.

Avoid this path for this release gate:

```text
/api/admin/backup/export?stop_services=true&restart_services=true
```

Use the already-staged frozen copy instead of rerunning export from `.67`.

### Migrated `.67` into long-running `.138`

A point-in-time file-level migration was completed after freezing `.67` UI/history.

Copied from `.67`:

- `config/`
- `data/`
- `history/history.db`

Restored into live `.138` at `/srv/truenas-jbod-ui`, then restarted `.138` UI/history/admin.

Final restored/frozen state:

- History DB SHA256: `45d222d11906f8c48a8db3c6ee467483038487240666e7f64fa43e34e45e20b1`
- Systems: `11`
  - `archive-core`
  - `offsite-scale`
  - `gpu-server`
  - `unvr`
  - `unvr-pro`
  - `qsosn-ha`
  - `demo-builder-lab`
  - `cryostorage-esxi`
  - `ipmi-ft-1`
  - `esxi-ft-node-2`
  - `esxi-ft-node-3`
- Platform counts:
  - `core: 1`
  - `scale: 1`
  - `linux: 4`
  - `quantastor: 1`
  - `esxi: 3`
  - `ipmi: 1`
- Slots: `60`
- Storage views: `2`
- History:
  - tracked slots: `347`
  - events: `19,746`
  - metric samples: `1,372,353`
  - history scopes: `23`

After restore:

- Long-running `.138` is healthy on `8080/8081/8082`.
- `.67` TrueNAS JBOD UI stack was stopped completely; `8080/8081/8082` on `.67` were closed.

At `2026-06-12T11:49Z`, quick health showed:

- `http://10.13.37.138:8080/livez`: `status=ok`, `version=0.21.0`
- `http://10.13.37.138:8081/healthz`: `status=ok`, `collection_running=false`, `last_error=null`
- `http://10.13.37.138:8082/healthz`: `status=ok`

The long-running source may continue collecting history. Treat the frozen QA seed below as the release QA baseline.

### Created fresh full-data disposable QA stack

Fresh QA stack was built from the release branch and seeded from the frozen `.67` payload staged on `.138`.

- Host: `10.13.37.138`
- Runtime: `/docker-local/truenas-jbod-ui-qa-release-0.21.0-20260612T111913Z/repo`
- Compose project: `truenas_jbod_ui_qa_release_0210`
- Containers:
  - `truenas-jbod-ui-qa-release-0210`
  - `truenas-jbod-history-qa-release-0210`
  - `truenas-jbod-admin-qa-release-0210`
- Ports:
  - UI: `http://10.13.37.138:18080/`
  - History: `http://10.13.37.138:18081/`
  - Admin: `http://10.13.37.138:18082/`

At `2026-06-12T11:27Z`, QA health showed:

- `http://10.13.37.138:18080/livez`: `status=ok`, `version=0.21.0`
- `http://10.13.37.138:18081/healthz`: `status=ok`, `collection_running=false`, `last_error=null`
- `http://10.13.37.138:18082/healthz`: `status=ok`

The QA stack was seeded with this exact frozen state:

- History DB SHA256: `45d222d11906f8c48a8db3c6ee467483038487240666e7f64fa43e34e45e20b1`
- tracked slots: `347`
- events: `19,746`
- metric samples: `1,372,353`
- systems/platforms/slots/storage/history scopes matched the restored `.138` source at the time of seeding.

## Release wrap refreshed after corrected QA rerun

`docs/RELEASE_WRAP_0.21.0.md` now records the corrected 11-system full-data QA evidence and no longer uses the old 6-system `.138`/`0bd14dc` runtime as release evidence.

Rows refreshed in this continuation:

- `Full Playwright/browser gates`
- `Feature-specific live API/UI gates`
- `Linux QA restore gate`
- `Restored Linux QA perf harnesses`
- `Snapshot/export/offline artifact gate`

Validation state as of `2026-06-12T16:50Z`:

- `.venv/bin/python scripts/validate_release_wrap.py 0.21.0 --phase pre-tag --allow-blocked` passed earlier: `docs/RELEASE_WRAP_0.21.0.md checklist evidence is complete.`
- After gcs8 visual acceptance and QA DB noise cleanup, `.venv/bin/python scripts/validate_release_wrap.py 0.21.0 --phase pre-tag` passed without `--allow-blocked`: `docs/RELEASE_WRAP_0.21.0.md checklist evidence is complete.`

## QA stack hotfix refresh after Quantastor SSH-warning fan-out

At `2026-06-12T15:05Z`, the existing full-data QA stack at `10.13.37.138:18080/18081/18082` was updated in place from the current dirty worktree to include the Quantastor optional-SSH warning collapse and the history-noise fixes.

Runtime path stayed the same:

- `/docker-local/truenas-jbod-ui-qa-release-0.21.0-20260612T111913Z/repo`

Rollback bundles/artifacts for overwritten files and DB cleanup on `.138`:

- first hotfix refresh: `/docker-local/truenas-jbod-ui-qa-release-0.21.0-20260612T111913Z/rollback-pre-hotfix-20260612T145745Z`
- follow-up warning-collapse tweak: `/docker-local/truenas-jbod-ui-qa-release-0.21.0-20260612T111913Z/rollback-pre-warning-collapse-20260612T151257Z`
- Quantastor `Visible On` cluster-scope tweak: `/docker-local/truenas-jbod-ui-qa-release-0.21.0-20260612T111913Z/rollback-pre-visible-on-scope-20260612T153228Z`
- pre-cleanup QA history DB backup: `/docker-local/truenas-jbod-ui-qa-release-0.21.0-20260612T111913Z/repo/history/manual-cleanup-backups/history-pre-noise-cleanup-20260612T164034Z.sqlite3`
- QA cleanup manifest with deleted row IDs: `/docker-local/truenas-jbod-ui-qa-release-0.21.0-20260612T111913Z/repo/history/manual-cleanup-backups/history-noise-cleanup-20260612T164034Z.json`
- long-running source stack hotfix rollback: `/srv/truenas-jbod-ui/migrations/rollback-pre-source-hotfix-cleanup-20260612T165436Z`
- pre-cleanup source history DB backup: `/srv/truenas-jbod-ui/history/manual-cleanup-backups/source-history-pre-noise-cleanup-20260612T165534Z.sqlite3`
- source cleanup manifest with deleted row IDs: `/srv/truenas-jbod-ui/history/manual-cleanup-backups/source-history-noise-cleanup-20260612T165534Z.json`
- source/QA SAS path-nibble history hotfix rollbacks:
  - `/srv/truenas-jbod-ui/migrations/rollback-pre-sas-nibble-history-20260612T171707Z`
  - `/docker-local/truenas-jbod-ui-qa-release-0.21.0-20260612T111913Z/repo/migrations/rollback-pre-sas-nibble-history-20260612T171707Z`
- source SAS path-nibble cleanup DB backup: `/srv/truenas-jbod-ui/history/manual-cleanup-backups/source-history-pre-sas-nibble-cleanup-20260612T171833Z.sqlite3`
- source SAS path-nibble cleanup manifest with deleted row IDs: `/srv/truenas-jbod-ui/history/manual-cleanup-backups/source-history-sas-nibble-cleanup-20260612T171833Z.json`

Deployment notes:

- Copied the current local versions of:
  - `app/services/inventory.py`
  - `history_service/collector.py`
  - `history_service/domain.py`
  - `tests/test_inventory.py`
  - `tests/test_history_service.py`
  - this handoff and `docs/RELEASE_WRAP_0.21.0.md`
- Rebuilt `enclosure-ui`, `enclosure-history`, and `enclosure-admin` from `docker-compose.dev.yml` + `docker-compose.qa.yml`.
- Recreated only the QA containers using `COMPOSE_PROJECT_NAME=truenas_jbod_ui_qa_release_0210` and explicit QA port variables: `APP_PORT=18080`, `HISTORY_PORT=18081`, `ADMIN_PORT=18082`, `HISTORY_BIND_ADDRESS=0.0.0.0`, `ADMIN_BIND_ADDRESS=0.0.0.0`.
- Do not omit those port variables on future `docker compose up` calls; the base compose file otherwise attempts to bind the long-running `8080/8081/8082` ports.

Post-refresh verification:

- `http://10.13.37.138:18080/livez`: `status=ok`, `version=0.21.0`
- `http://10.13.37.138:18081/healthz`: `status=ok`, `collection_running=false`, `last_error=null`
- `http://10.13.37.138:18082/healthz`: `status=ok`
- Running UI container confirmed the new `InventoryService._optional_ssh_transport_failure_detail`, `_run_optional_ssh_command`, and `_collapse_quantastor_optional_ssh_backoff_warnings` helpers are present.
- The Quantastor `Visible On` path now scopes presence hints to the selected HA cluster's node IDs so remote QuantaStor systems returned by the same API do not appear as local/shared disk visibility.
- Focused Playwright smoke against the refreshed stack passed:
  - `page loads and exposes the main switching chrome`
  - `configured systems and enclosure views complete a release sweep cleanly`
- After the follow-up warning-collapse tweak, fresh forced `qsosn-ha` inventory returned only the HA context warning: `Quantastor HA detected. Cluster master is QSOSN-Left.` No optional SSH CLI/SES backoff warnings were user-visible in that run.
- After the `Visible On` tweak, fresh forced `qsosn-ha` inventory had no `QS-CryoStorage` entries in any `visible_on_labels`; slot 0 reported `QSOSN-Left, QSOSN-Right` only.
- QA history cleanup deleted same-day noise rows from `slot_events` only: `1,341` `slot_identity_changed` rows plus `564` `slot_topology_changed` rows observed on `2026-06-12`, total `1,905` rows. The `slot_state_changed` rows were preserved.
- Exact QA history counts after cleanup and one fast refresh: `347` tracked slots, `17,841` events, `1,372,400` metric samples, `23` scopes; no `slot_identity_changed`/`slot_topology_changed` rows remain for `2026-06-12`, and no new ones were introduced after the fast refresh.
- Long-running source `10.13.37.138:8080/8081/8082` was also refreshed with the same hotfix code because it was still running pre-fix inventory/history helpers. Post-refresh markers showed Quantastor warning collapse, `Visible On` cluster scoping, and history topology confirmation helpers present.
- Source history cleanup deleted same-day noise rows from `/srv/truenas-jbod-ui/history/history.db`: initially `1,850` `slot_identity_changed` rows plus `572` `slot_topology_changed` rows observed on `2026-06-12`, total `2,422` rows. It preserved `946` same-day `slot_state_changed` rows.
- A later full source collector pass exposed `27` more QSOSN HA SAS-address-only path-nibble identity flips (`...12` ↔ `...10` style) with unchanged serials. The release code now suppresses stable-disk SAS path-nibble flips, and the source/QA history containers were rebuilt with that final fix.
- After the final source SAS-nibble cleanup and a manual full refresh: `347` tracked slots, `18,169` events, `1,380,304` metric samples, `23` scopes; no `slot_identity_changed`/`slot_topology_changed` rows remain for `2026-06-12`, and the full refresh reintroduced `0` same-day identity/topology rows.

This hotfix refresh and cleanup became the final published `v0.21.1` release after `v0.21.0` post-publish verification exposed the presence-flap history-noise class.

Final publish/deployment evidence:

- GitHub Release `v0.21.1`: `https://github.com/gcs8/truenas-jbod-ui/releases/tag/v0.21.1`
- GHCR workflow `27434127617`: `https://github.com/gcs8/truenas-jbod-ui/actions/runs/27434127617`
- GHCR tags `v0.21.1`, `0.21.1`, and `latest` all converged to digest `sha256:28e38a92dd77b9526cf2367bf151b44fafa85a82e425434fdbdc95c56a6ac6d1` with OCI revision `cfb92f2576f7c0d0d7fdd3b3ff58918897c0fe7c`, version `0.21.1`.
- Source stack `10.13.37.138:8080/8081/8082` now runs the published digest on all three containers; UI `/livez` reports `version=0.21.1`, history/admin health are `status=ok`, and same-day identity/topology rows remained `0` after refresh. Counts: `347` tracked slots, `18,243` events, `1,381,523` metric samples.
- QA stack `10.13.37.138:18080/18081/18082` now runs the published digest on all three containers; UI `/livez` reports `version=0.21.1`, history/admin health are `status=ok`, and same-day identity/topology rows remained `0` after refresh. Counts: `347` tracked slots, `17,841` events, `1,372,400` metric samples.
- GitHub issue #6 was commented and closed as completed: `https://github.com/gcs8/truenas-jbod-ui/issues/6`.

## Private artifacts / rollback paths

Do not commit or print contents from these paths.

Local ignored artifacts:

- `artifacts/private-v0.21.0/migrate-67-to-138/final-freeze-20260612T111257Z`
- `artifacts/private-v0.21.0/migrate-67-to-138/latest-final-stage.txt`

Remote `.138` staging/rollback:

- frozen source staging: `/srv/truenas-jbod-ui/migrations/67-to-138-final-freeze-20260612T111257Z/source`
- rollback of previous `.138` state: `/srv/truenas-jbod-ui/migrations/rollback-pre-67-restore-20260612T110819Z`

The rollback is there if the `.67 → .138` replacement must be undone. Do not use it unless gcs8 explicitly asks or `.138` is proven bad.

## Safety boundaries

- Do not rewrite the published `v0.21.0` or `v0.21.1` tags/releases. Future fixes should ship as normal follow-up releases.
- Do not commit `artifacts/`, `config/`, `data/`, `history/`, `logs/`, `.env`, SSH keys, TLS material, known-hosts, passphrase files, or raw admin import/export responses.
- Do not run raw `docker compose config` or otherwise dump compose/env output into transcript/docs; one raw compose dump previously exposed secret env values in tool output. Avoid repeating that.
- Do not rerun risky admin export with `stop_services=true` against long-running/live stacks.
- Keep the disposable QA stack isolated on `18080/18081/18082`; do not disturb long-running `.138` on `8080/8081/8082` except for read-only checks.
- The QA history collector is healthy after one fast refresh; check `collection_running=false` before perf gates.
- Do not tear down the QA stack until post-publish deployment sniff tests are complete, unless gcs8 asks to stop it.

## Immediate pickup commands

```bash
cd /home/gcs8/workspace/truenas-jbod-ui-platform-route-registry-20260522
git status --short --branch
git diff --stat
```

Confirm source and QA are still up:

```bash
python - <<'PY'
import json, urllib.request
out = {}
for base in [
    '10.13.37.138:8080', '10.13.37.138:8081', '10.13.37.138:8082',
    '10.13.37.138:18080', '10.13.37.138:18081', '10.13.37.138:18082',
]:
    path = '/livez' if base.endswith(('8080', '18080')) else '/healthz'
    url = 'http://' + base + path
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.load(r)
        out[url] = {k: data.get(k) for k in ['status', 'version', 'last_error', 'collection_running'] if k in data}
    except Exception as exc:
        out[url] = {'error': type(exc).__name__ + ': ' + str(exc)[:160]}
print(json.dumps(out, sort_keys=True, indent=2))
PY
```

Confirm QA provenance before spending time on gates:

```bash
python - <<'PY'
import collections, json, urllib.request
for label, ui, hist in [
    ('source', 'http://10.13.37.138:8080', 'http://10.13.37.138:8081'),
    ('qa', 'http://10.13.37.138:18080', 'http://10.13.37.138:18081'),
]:
    with urllib.request.urlopen(ui + '/api/inventory', timeout=90) as r:
        inv = json.load(r)
    with urllib.request.urlopen(ui + '/api/storage-views', timeout=90) as r:
        storage = json.load(r)
    with urllib.request.urlopen(hist + '/api/history/overview?exact_counts=true', timeout=90) as r:
        overview = json.load(r)
    systems = inv.get('systems') or []
    print(json.dumps({
        'label': label,
        'default': inv.get('selected_system_id'),
        'system_ids': [s.get('id') for s in systems],
        'platform_counts': dict(sorted(collections.Counter(s.get('platform') or 'unknown' for s in systems).items())),
        'slot_count': len(inv.get('slots') or []),
        'storage_views': len(storage.get('views') or storage.get('storage_views') or []),
        'history_counts': overview.get('counts'),
        'history_scope_count': len(overview.get('scopes') or []),
        'collector': {k: (overview.get('collector') or {}).get(k) for k in ['collection_running', 'collection_kind', 'last_error']},
    }, sort_keys=True))
PY
```

Expected QA baseline:

- 11 systems
- platform counts `core=1`, `scale=1`, `linux=4`, `quantastor=1`, `esxi=3`, `ipmi=1`
- 60 slots
- 2 storage views
- history counts: tracked slots `347`, events `19746`, metric samples `1372353`
- 23 history scopes
- QA collector `collection_running=false`

## Corrected QA gates rerun from this stack

Completed against `10.13.37.138:18080/18081/18082` and recorded in `docs/RELEASE_WRAP_0.21.0.md`:

- Full Playwright/browser gate: `27`/`27` passed against `18080/18082`.
- Feature/API/browser gate: `11` systems, `60` slots, `2` storage views, cached SAS fabric `799` links / `13` warnings, forced SAS fabric `799` links / `22` warnings, representative CORE/SCALE/Linux/Quantastor/ESXi/IPMI-BMC browser pages clean with no horizontal overflow and no browser error/warning console messages.
- Snapshot/export/offline gate: artifact `artifacts/private-v0.21.0/linux-qa-fullsource-snapshot-export/linux-qa-fullsource-snapshot-export-20260612T114504Z.zip`, size `1,146,288`, SHA-256 `f62eac9d8b6fb3010b76c63b8718fc025f34d0595c49177a771412263516fefd`; offline HTML opened with `11` system options, `60` tiles, `2` storage-view options, `2` live-enclosure options, clean console, no horizontal overflow.
- Restored Linux QA perf gates: `data/perf/latest.md` label `release-candidate-linux-qa-fullsource`; `data/history-perf/latest.md` label `release-candidate-history-linux-qa-fullsource`; collector was `collection_running=false` before the perf run.
- This section records the pre-publish `v0.21.0` QA gate history. The final published release is `v0.21.1`; see `docs/RELEASE_WRAP_0.21.1.md` for post-publish GHCR/source/QA evidence.

## Final closeout state

Completed:

1. `v0.21.1` release commit/tag/release published.
2. GitHub Actions CI and release-triggered GHCR workflow passed.
3. Published GHCR digest pulled and sniffed locally.
4. Long-running source stack refreshed by digest and verified clean.
5. Full-data QA stack refreshed by digest and verified clean.
6. Issue #6 closed as completed.
7. Release wrap records post-publish evidence.
8. Post-release development reopened as `0.21.2-dev` on `main`; the `v0.21.1` tag remains immutable.

## Known pitfalls from this run

- The previous 6-system `.138` evidence is invalid for final release because `.67` had the fuller real deployment data.
- Admin backup/export with `stop_services=true` can stop/kill UI/history on busy stacks; do not use it casually.
- Raw compose/config/environment dumps may expose secrets; do not print or commit them.
- The base dev compose has fixed `container_name` values; disposable QA must use an override with unique container names.
- History collection can mutate counts after startup. Use frozen QA baseline counts for release evidence and wait for `collection_running=false` before perf.
- Restored ESXi/remote paths may be slower than fixture-only browser assumptions. Do not reduce timeouts based on fixture expectations.
- SMART prefetch abort/fallback noise was demoted to debug in `app/static/app.js`; verify genuine degradation still surfaces through UI state, not browser console spam.

## Minimal next-session checklist

- [x] Confirm this handoff is the current final release closeout.
- [x] Confirm `.138` source and QA health on published `v0.21.1` digest.
- [x] Confirm QA provenance still shows the full-data baseline with `347` tracked slots and no same-day identity/topology noise rows.
- [x] Confirm GitHub Release/GHCR publication and issue #6 closure.
- [ ] Optional later cleanup only if gcs8 asks: decide whether/when to tear down the temporary QA stack on `18080/18081/18082`.
