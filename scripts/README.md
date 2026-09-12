# Scripts index

Run Python CLIs from a checkout with the project dependencies installed. Use
`python scripts/<name>.py --help` to inspect arguments before execution. Help is
not permission to run a migration, restore, deployment update, or publication.
The rows below classify intended use, not native Windows certification.

## Operator CLIs

| Script | Purpose and where to run |
| --- | --- |
| `prepare_nonroot_bind_mounts.py` | Bounded ownership preflight on the POSIX deployment host; `--apply` requires root and changes ownership and permission modes. Requires descriptor-relative, no-follow filesystem APIs and `resource`. Not shipped in the application image. Native Windows execution is unsupported; help remains available. |
| `update_immutable_deployment.py` | Update an immutable GHCR deployment from the deployment host with Docker control and POSIX process identity support. Not an in-container helper. See the [immutable deployment runbook](../docs/IMMUTABLE_GHCR_DEPLOYMENT.md). |
| `migrate_segmented_history.py` | Advanced opt-in history migration/rollback on a quiesced database. Requires POSIX locking; shipped at `/app/scripts/migrate_segmented_history.py` in the application image. Run only with the intended history mounts and writer shutdown procedure. |
| `rotate_segmented_history.py` | Advanced opt-in segmented-history rotation/recovery. Requires POSIX locking; shipped at `/app/scripts/rotate_segmented_history.py`. Requires the same deliberate mount and writer coordination as migration. |
| `seal_history_segment.py` | Seal a quiesced SQLite history database into an immutable segment and print a JSON receipt. Shipped at `/app/scripts/seal_history_segment.py`; also usable from a dependency-equipped checkout. Help does not establish Windows filesystem compatibility. |
| `query_segmented_history.py` | Read slot events across hot history and immutable segments. Shipped at `/app/scripts/query_segmented_history.py`; also usable from a dependency-equipped checkout with access to the intended history files. |

For history setup and operational constraints, see [segmented history](../docs/SEGMENTED_HISTORY_V2.md).
Do not infer that every script in this directory exists in a published image.
Image availability and host filesystem ownership are separate requirements.

### Ownership helper

Start with read-only preflight, replacing the example path with the approved
local deployment directory:

```sh
python scripts/prepare_nonroot_bind_mounts.py /srv/enclosure --uid 10001 --gid 10001
```

Review the bounded path selection before considering `--apply`. The helper covers
`data`, `history`, `logs`, and selected `config` entries, not every deployment
secret or mount. It is not a recursive `chown` substitute and must not be used as
a blanket repair for sealed segmented-history files. See
[troubleshooting](../wiki/Troubleshooting.md) for deployment context.

### Segment sealer values

- `--source` is an existing quiesced SQLite database, not a live writer's database.
- `--output-dir` receives `<segment-id>.sqlite3`; an existing segment is not overwritten.
- `--segment-id` is 1-128 ASCII letters, digits, dots, underscores or hyphens and must start with a letter or digit, for example `segment-0001`.
- `--cutoff` is an ISO-8601 timestamp with an offset, for example `2026-01-01T00:00:00+00:00`. The sealer converts it to UTC midnight and keeps rows strictly before that boundary. It is not an epoch integer or an exact intraday cutoff.
- `--key-id` is a nonempty identifier up to 128 characters, for example `archive-key-1`. It is receipt metadata, not secret key material. This command does not encrypt or sign the segment.
- `--sequence` is a positive integer for receipt metadata, default `1`.

## Maintainer CLIs

These are checkout tools, not supported application-container entrypoints.
Install the dependencies appropriate to each tool. Runtime checks need an
explicitly approved target; they are not part of ordinary offline source checks.

| Script | Purpose and where to run |
| --- | --- |
| `dev_check.py` | Platform-aware source validation in a development checkout. `--safe` excludes the checked-in public-demo artifact check; `--full` includes it. Windows uses explicitly classified suites, not the full POSIX test set. |
| `build_perf_baseline.py` | Check or refresh deterministic modeled performance budgets in a development checkout. |
| `benchmark_snapshot_export_cache.py` | Modeled export-cache size and timing benchmark in a development checkout. |
| `run_perf_harness.py` | Read-only API performance checks from a checkout against an explicitly selected running app. |
| `run_history_perf_harness.py` | History-sidecar performance checks from a checkout against an explicitly selected running sidecar. |
| `run_compose_runtime_matrix.py` | Synthetic service-combination QA on a disposable Linux Docker host; runtime checks use `/proc/meminfo`. |
| `run_private_qa_restore.py` | Private restore drill on an approved disposable Linux Docker QA host; requires private inputs and uses `/proc/meminfo`. Never run as a routine source check. |
| `build_current_source_browser_fixture.py` | Build deterministic current-source browser fixtures in a development checkout. |
| `build_public_demo.py` | Generate/check the synthetic public demo in a maintainer checkout. Local-history modes require separate authorization. |
| `capture_public_demo_screenshots.js` | Node/Playwright screenshot capture in a maintainer checkout; not a Python argparse CLI. Requires browser dependencies. |
| `check_public_demo_artifact.py` | Validate local demo artifact bytes and provenance in a maintainer checkout. |
| `check_public_demo_deployment.py` | Network readback of an explicitly selected published demo against expected artifact bytes. |
| `check_public_screenshots.py` | Validate public screenshot bytes and fixture provenance in a maintainer checkout. |
| `check_public_docs.py` | Check README/wiki navigation, examples and screenshot inventory in a checkout. |
| `check_changelog_entry.py` | PR changelog admission check in CI or a maintainer checkout with explicit PR/change inputs. |
| `check_release_changelog_coverage.py` | Reconcile release changelog coverage in a maintainer checkout with release/PR evidence. |
| `render_release_notes.py` | Render release notes from the changelog in a maintainer checkout. |
| `validate_release_wrap.py` | Validate release-wrap evidence in a maintainer checkout; does not execute missing release gates. |
| `verify_wiki_drift.py` | Compare committed repository wiki content with an external wiki commit using Git from a maintainer checkout. |

## Import-only modules

- `public_demo_inputs.py` defines the declared public-demo source inputs.
- `public_demo_source_parity.py` provides public-demo source-parity helpers.

Import these modules through the `scripts` package from the repository root.
They are not CLIs and do not promise `--help` or direct file execution.

## Portability boundary

The CLI changes here cover ownership-helper help before POSIX dependency checks
and the sealer's argument descriptions. Missing POSIX dependencies can be
simulated in Linux tests, but that is not native Windows validation. Other CLI
help fixes, Windows runtime checks, broader `dev_check.py` fixes, and the immutable
updater remain separate work. The new portable CLI tests are registered in
`dev_check.py`; the POSIX ownership suite remains excluded on Windows.
In particular, migration/rotation imports and Linux QA
host requirements must not be hidden behind generic advice to run any script
inside the application container.
