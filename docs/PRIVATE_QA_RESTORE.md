# Private QA restore and Compose matrix

This document defines the automated release QA path for Compose deployments. It
keeps public CI synthetic and moves the expensive container, restore, browser,
and performance work to a disposable private Linux QA host.

## QA layers

The release gate has three separate layers. A pass in one layer does not imply a
pass in another.

1. Repository tests validate Compose merge rules, writable paths, identities,
   archive safety, and controller guards without private data.
2. The synthetic runtime matrix builds one exact candidate image and exercises
   service combinations with public fixtures.
3. The private restore drill accepts a separately staged encrypted FULL backup,
   restores it into an isolated QA root, and compares aggregate counts from the
   validated archive with the running stack.

Do not put a production-derived archive, passphrase, raw restore response,
container log, browser trace, screenshot, history database, or admin state in
GitHub Actions or a public artifact.

## Service combinations

`scripts/run_compose_runtime_matrix.py` covers these production-Compose
combinations serially from one exact image ID whose OCI revision matches the
required full source commit:

| Combination | Required checks |
| --- | --- |
| UI only | UI health and Basic-auth boundary; SAS Fabric label and slot mapping save, host-file readback, UI restart, second readback, clear, and count restoration |
| UI + history | Exact running service set; UI and history health; history status through the UI; both pencil cycles |
| Admin only / initial setup | Admin health and auth; create a synthetic initial system; start the UI from the saved config; verify persistence |
| UI + admin | Exact running service set; UI/admin health; runtime cards; both pencil cycles |
| UI + history + admin | All three health paths; history and admin APIs; both pencil cycles |
| One-shot FULL backup | Separate `backup` profile contract; no network; disk-backed archive workspace; encrypted archive preflight and cleanup |

History-only is not a supported combination because the history service depends
on the UI. Admin-only is supported specifically for initial configuration and
recovery.

The matrix runs against `docker-compose.yml`. Repository tests also parse and
merge-check `docker-compose.dev.yml`, `docker-compose.secrets.yml`, and
`docker-compose.nonroot.yml`. The secrets overlay gets a separate synthetic
secret-file runtime check when that overlay changes. The non-root overlay must
remain equivalent to the hardened base identity contract. Do not multiply every
service combination by every overlay when the overlay does not change that
service. Record the exact Compose file sequence in the receipt.

## Write touchpoints

The read UI has two saved operator edits:

- SAS Fabric labels, written through `/api/sas-fabric/aliases` to
  `data/sas_fabric_aliases.json`;
- slot mappings, written through `/api/slots/{slot}/mapping` to
  `data/slot_mappings.json`.

Every synthetic UI-bearing combination runs save, response readback, host-file
readback, clear, and restored-count checks for both. The private egress-blocked
restore drill always exercises labels. Slot mappings need a resolved enclosure
layout, so the private drill exercises them only in the separately approved
live-read-only mode. This is not a gap in the standalone UI proof because the
synthetic UI-only matrix runs the real mapping route with no history or admin
sidecar.

Admin touchpoints include system/profile setup, SSH and TLS material, runtime
behavior overrides, backup inspection/import/export, container controls, and
recovery. The synthetic admin-only test uses generated data. The private restore
does not replace or delete a production-derived saved system merely to test the
form.

LED and system-locator actions are hardware mutations. Neither automated mode
runs them unless a separate device-safe target and approval are supplied.

## Private production-derived restore

Run `scripts/run_private_qa_restore.py` only on a disposable Docker host. The
normal mode is egress-blocked. The default Docker network is internal. A
task-owned loopback proxy provides UI, history, and admin access when Docker does
not publish ports from that network. If Docker does publish them, the controller
accepts only each exact `127.0.0.1` binding.
Restored appliance endpoints and credentials therefore cannot be contacted.

Required input rules:

- the archive is an encrypted FULL backup supplied through a separate authorized
  process. Its selected groups must include `config_file`,
  `runtime_overrides_file`, `profile_file`, `mapping_file`,
  `sas_fabric_alias_file`, `slot_detail_file`, `history_db`, `ssh_keys`,
  `tls_trust`, and `known_hosts`;
- the archive and passphrase file are regular, non-symlink files with mode
  `0600`;
- the command names a full source commit and exact `sha256:` image ID, and the
  image's `org.opencontainers.image.revision` label exactly matches that commit;
- the scratch and runtime roots are absolute paths, the scratch root is private,
  the runtime and evidence paths do not exist, and
  the selected ports and fixed container names are unused;
- the target handle uses the exact `run-<32 lowercase hex>` form. It is not a
  hostname, path, address, system name, or other restored value;
- memory and disk preflights pass before the controller creates a runtime root;
- browser and performance checks are mandatory. The compatibility
  `--skip-browser-and-performance` option fails closed and cannot produce PASS;
- the operator supplies `I_APPROVE_PRIVATE_QA_RESTORE` exactly.

The controller performs these phases:

1. Create a new private runtime root and an internal Docker network.
2. Prepare the non-root bind mounts with the repository ownership helper.
3. Start UI + history + admin from the exact image, validate each container's
   network metadata, and establish loopback-only host access.
4. Stream the archive to `/api/admin/backup/inspect`. Inspection validates the
   archive, manifest, member sizes and hashes, candidate config, mapping/profile
   data, SQLite databases, and segmented history without activating it.
5. Keep only the sanitized inspection fields and aggregate counts. The raw
   manifest, systems, identifiers, filenames, and restored paths never enter the
   receipt.
6. Stream the same archive to
   `/api/admin/backup/import?stop_services=true&restart_services=true`.
7. Wait for all three services, compare every known archive count with the
   restored stack, exercise allowed pencil edits, clear them, and compare again.
8. Run `docker compose restart`, wait for health, and compare counts a third
   time.
9. Run the private restore browser smoke and history performance check.
10. Capture private diagnostics.
11. Tear the stack down and remove the complete bind-mounted runtime root unless
    `--keep-running` was explicitly requested. A failed run always attempts the
    same cleanup.
12. Write `sanitized-receipt.json` only after default cleanup succeeds. A
    cleanup failure cannot produce a passing receipt.

The controller's help output is the command authority:

```bash
python scripts/run_private_qa_restore.py --help
```

Do not paste a real archive path, passphrase path, or generated credentials into
an issue, PR, release wrap, or chat transcript. The private operator invocation
belongs in the restricted QA run record.

## Optional live read-only QA

The egress-blocked run proves portability and local behavior. It does not prove
that restored appliance endpoints are reachable or compatible.

`--live-read-only` removes Docker egress isolation and requires the separate
phrase `I_APPROVE_LIVE_READ_ONLY_QA`. Use it only after confirming the QA host is
allowed to contact the restored endpoints. This mode adds:

- full saved-system and saved-view browser checks;
- platform-specific read-only checks such as ESXi rendering when applicable;
- the app API performance runner with mapping mutation disabled;
- the real slot-mapping pencil cycle against a resolved enclosure layout.

This flag does not authorize LED control, locator control, admin changes to saved
systems, credential rotation, production container control, or any write to an
appliance.

## Browser and performance checks

The offline browser run covers the restored UI shell, auth boundary, sidecar
links, admin operations page, label pencil flow, and browser console/page errors.
It reads generated QA Basic-auth credentials from temporary mode-`0600` files.
Credential values do not enter the Playwright process environment. The
controller supplies a minimal child environment instead of inheriting the
operator shell's full environment.

The history performance runner uses exact restored counts and writes its full
JSON only under `raw-private`. The app performance runner runs only in live
read-only mode because its inventory workflow contacts the configured source.
Release summaries may report pass/fail and bounded latency totals, not systems,
URLs, IDs, or raw response data.

## Evidence and logging

The evidence directory and every file under it use private permissions. Raw
container logs, command output, and performance JSON stay under `raw-private`.
Private Playwright mode disables traces, screenshots, video, and the HTML report;
any runner metadata stays in its mode-`0700` directory under `raw-private`.
These files may contain private topology or restored metadata. Do not attach that
directory to a PR or release.

The runtime root is not evidence. By default the controller deletes it after
capturing private diagnostics, including generated QA credentials and any
restored keys, TLS files, configuration, data, or history. `--keep-running`
retains that private state only for an explicitly supervised follow-up check.

`sanitized-receipt.json` contains only:

- run ID, target handle, source commit, exact image ID, archive SHA-256 and size;
- offline or live-read-only mode;
- schema, packaging, encryption, selected/present/absent group names, aggregate
  counts, and member totals;
- restore/restart counts without names or paths;
- pass/fail for count reconciliation, pencil cleanup, restart survival, browser,
  and performance gates;
- elapsed time and whether the private stack remains running.

Current application observability is useful but not complete. Services can emit
JSON with timestamp, severity, logger, and service name. HTTP metrics record
method, normalized route, status, and duration. History exposes collector state
and exact aggregate counts. The controller adds phase-specific command logs and
a bounded failure receipt.

### Request correlation gap

The services do not yet return one server-generated request ID and carry it
through completion and exception events. Restore operations also lack dedicated
success/failure/duration metrics. Until that changes, use the controller run ID,
phase, timestamp window, service name, and normalized route to correlate private
QA evidence. Treat request IDs and restore-specific metrics as an observability
backlog item, not as proof supplied by this controller.

The controller must never print or persist authorization headers, cookies,
passphrases, request bodies, raw admin state, system identifiers, restored paths,
or unrestricted exception messages. On failure, the public-safe summary is only
the failed phase and exception class. Diagnosis happens in `raw-private`.

## Release use

A release wrap records four independent outcomes:

- repository source tests;
- exact-image synthetic runtime matrix;
- private egress-blocked restore QA;
- optional live-read-only QA and human operator acceptance.

A missing private backup or unavailable QA host is `HOLD`, not `PASS`. A green
GitHub check does not replace the private restore gate. Production deployment and
v0.22.2 remain separate, explicitly authorized actions.
