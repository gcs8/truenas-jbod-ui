# Disk Replacement on TrueNAS CORE with SAS Multipath

Operator runbook for replacing or hot-adding a disk in a dual-path SAS
enclosure attached to TrueNAS CORE 13, using the **TrueNAS disk inventory**
controls in this app's enclosure header (issue #357).

## Why this runbook exists

On CORE with `gmultipath`, the kernel can assemble a new disk's two paths into
one multipath geom and ZFS can start using it while the TrueNAS middleware disk
table still lists the active member as a plain disk with no `multipath_name` and
never registers the passive member. Until the middleware re-reads its disks,
the TrueNAS UI and this app show the bay wrong (or not at all).

The app can ask the middleware to re-read over the same SSH channel it already
uses for `sesutil`:

| Control | Middleware call | What it does | Platforms |
| --- | --- | --- | --- |
| Sync multipath table | `disk.multipath_sync` | Rebuilds the middleware multipath table from the kernel's `gmultipath` geoms. Synchronous. | CORE only |
| Full disk sync | `disk.sync_all` | Re-reads every disk into the middleware inventory. Runs as a job; the app polls `core.get_jobs` until it finishes (180 s default timeout). | CORE and SCALE |

Neither call touches pools, vdevs, or data. They only refresh the middleware's
view of which disks exist and how their paths group.

## Prerequisites

- SSH is enabled for the system in this app and the service account has the
  exact-argument sudo grants below (the admin bootstrap and
  `docs/SSH_READ_ONLY_SETUP.md` include them):
  - CORE: `/usr/local/bin/midclt call disk.multipath_sync`,
    `/usr/local/bin/midclt call disk.sync_all`,
    `/usr/local/bin/midclt call core.get_jobs *`
  - SCALE: `/usr/bin/midclt call disk.sync_all`,
    `/usr/bin/midclt call core.get_jobs *`
- The app runs with `ADMIN_AUTH_MODE=basic` and a configured public origin, the
  same gate the identify LED controls use. In network mode the controls are
  disabled with a reason.
- You are looking at the live view, not an offline snapshot or the public demo.
  Snapshot exports hide these controls.

The controls live in the enclosure header, to the right of the Heat Map,
Topology, and Storage Fabric view toggles. Each one is a two-step confirm: the
first click arms it and shows what the call does, the second click within a few
seconds runs it. Clicking anywhere else disarms it.

## Procedure

Example names below are synthetic (`da12`, `da44`, `multipath/disk7`,
serial `SYNTH-0001`). Use your own.

1. **Identify the bay.** Select the failed or empty bay in this app and use
   Identify On so the enclosure LED marks it. Note the bay label and, for a
   replacement, the old disk's serial from Slot Details.
2. **Offline the old disk in TrueNAS** (replacement only) from Storage > Pools >
   Status, then pull it once the LED shows which bay to pull.
3. **Insert the new disk.** Wait for the kernel to see both paths. In a TrueNAS
   shell, confirm the geom exists and is optimal:

   ```sh
   gmultipath list
   ```

   You want a geom such as `multipath/disk7` with `State: OPTIMAL` and two
   consumers (for example `da12` and `da44`). If only one consumer appears,
   check the second cable or expander before continuing.
4. **Sync the multipath table.** In this app's enclosure header, click
   **Sync multipath table**, then **Confirm sync**. The status line shows
   progress, then `TrueNAS rebuilt its multipath table. Refresh to see the
   updated bays.` The app clears its cached inventory and refreshes.
5. **Check the bay.** The bay should now show the multipath device and the
   new serial. If the TrueNAS disk list still disagrees with `gmultipath list`
   (for example the new disk is missing from Storage > Disks), click
   **Full disk sync**, then **Confirm sync**. The status line reports the job
   id, terminal state, and elapsed time.
6. **Replace in TrueNAS.** Storage > Pools > Status > Replace on the offlined
   member, choosing the new multipath device. Let the resilver finish.
7. **Clear the LED and refresh.** Identify Off in this app, then Refresh.
   The bay should render as a pool member (or `spares > spare` for a hot spare)
   and, for spares, peer-highlight with its partner.

## If something does not line up

- **Buttons disabled with a reason.** Hover the button: the title says whether
  SSH is off for the system, the mode is unsupported on this platform, writes
  are disabled by policy, or a sync is already running.
- **`sudo: a password is required` or `not allowed to execute`.** The service
  account is missing one of the exact grants above. Re-run the admin bootstrap
  or add the entries by hand as described in `docs/SSH_READ_ONLY_SETUP.md`.
- **Full disk sync timed out.** The middleware job is still running. Check
  Tasks in the TrueNAS UI (the status line gives the job id), then Refresh
  here once it completes. The timeout is `APP_DISK_INVENTORY_SYNC_TIMEOUT_SECONDS`
  (default 180).
- **`FAILED` or `ABORTED`.** The status line carries the middleware's error
  sentence. Fix the underlying cabling or disk issue, then run the sync again.
- **Another sync is already running (409).** Wait for it to finish; the app
  allows one sync per system at a time.

## Related

- `docs/SSH_READ_ONLY_SETUP.md` for the CORE and SCALE sudo allow-lists.
- Issue #355 for the app-side multipath backfill that covers the window before
  a sync runs.
