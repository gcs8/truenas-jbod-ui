# Changelog

## v0.13.0 - 2026-04-21

### Added

- Selectable `Included Paths` pills in the admin full-backup flow, with locked
  secret paths (`config/ssh`, imported TLS trust material, and shared
  `known_hosts`) that force encrypted portable `.7z` export and restore
  together with the selected bundle
- Separate admin-side `Debug Bundle` export for frozen support snapshots, with
  distinct `Scrub obvious secrets` and `Scrub disk identifiers` toggles plus
  extra local stack-state capture
- `Add Demo Builder System` in the admin setup flow so enclosure/profile/view
  work can be tested against a seeded synthetic system without a real appliance
- Optional `Embedded Boot Media` virtual storage view for UniFi UNVR / UNVR
  Pro systems, including limited `/dev/boot` SMART collection through
  `smartctl -d scsi`
- Focused admin Playwright smoke coverage for bundle-path pills, locked-path
  forced encryption, and the split debug scrub controls

### Changed

- Checked-in operator docs now distinguish restore-grade full backup bundles,
  engineer-facing debug bundles, and the main UI's self-contained offline
  snapshot export
- The admin operations UI now makes bundle-path selection more obvious with
  explicit `[x]` / `[ ]` state, selected-count summaries, and clearer
  demo-builder save feedback
- `Boot SATADOMs` now render with photo-backed SATADOM tiles instead of the
  older generic boot-media cards, and the surrounding boot-device shelf/overlay
  spacing has been tuned to fit those cards more cleanly in the live UI
- UniFi UNVR / UNVR Pro inventory now keeps the internal `boot` disk as a
  limited boot-media candidate instead of dropping it from Linux inventory just
  because it is not named like a normal `sdX` / `vdX` data disk

### Fixed

- Demo-builder saves now tolerate missing request bodies, use optional system
  label/id overrides, and return readable success/error messages instead of the
  earlier opaque `[object Object]` failure path
- UniFi embedded-boot detail now strips inline smartctl parser noise such as
  `error: designator length` from hex-only SCSI identifier fields before they
  reach the hover text or the detail drawer

## v0.12.0 - 2026-04-21

### Added

- Dedicated `Enclosure / Profile Builder` workspace in the optional admin
  sidecar, including custom profile save/update/delete backed by the shared
  profile registry and `profiles.yaml`
- Slot-ordering presets plus an explicit `Custom Matrix` builder path so
  common row-major and column-major numbering patterns can be saved without
  hand-editing YAML
- Checked-in `v0.12.0` screenshots for the refreshed README/wiki pages,
  including the builder workspace, grouped runtime selector, updated admin
  setup flow, maintenance tools, and snapshot/export walkthroughs

### Changed

- Saved live-backed `ses_enclosure` views now reuse the same profile-driven
  geometry path as their live enclosure backing views, including row grouping,
  tray width, latch placement, LED spacing, empty-detail state, and click-off
  behavior
- Linux host-side runs now resolve config/data/log/history defaults relative to
  the checkout instead of requiring a writable `/app/...` harness, and startup
  logging now degrades cleanly when a Docker-owned log file is not writable
- Release-facing docs and wiki pages now describe the builder workspace,
  slot-ordering workflow, and Linux-first validation/deploy shape instead of
  the older sidebar-builder story

### Fixed

- Snapshot export estimate now uses the intended batched scope-history request
  path again instead of silently falling back to one request per slot when
  `slots=[...]` is present
- History-sidecar SQLite connections now use in-memory temp-store and a larger
  cache more safely, with WAL enablement treated as best-effort on filesystem
  quirks instead of fatal
- Host-side Linux path rebasing now keeps explicit legacy `/app/...` values
  usable outside containers instead of forcing a fake writable harness for
  repo-local runs

### Validation

- Local Windows Docker Desktop:
  - `234` Python tests passing
  - Playwright `9` passing / `1` skipped
  - perf label `release-candidate-0.12.0-local-windows`
- Linux dev target (`codex-dev-test-target`):
  - `234` Python tests passing
  - Playwright `10` passing
  - perf label `release-candidate-0.12.0-linux-dev-target`

## v0.11.0 - 2026-04-21

### Added

- Cluster-style Quantastor HA modeling under one saved system entry, including
  up to three explicit HA nodes, admin-side node discovery from the Quantastor
  API, and per-storage-view target-node binding for internal groups such as
  SATADOM pairs
- Admin-side history maintenance tools for:
  - deleting a saved system with optional history purge
  - purging orphaned history rows whose `system_id` no longer exists in the
    saved config
  - adopting removed-system history into a new saved system id after a rename
    or remap
- Richer history-sidecar disk identity capture, including persistent-id type,
  logical unit id, and SAS address fields that make later disk-follow or
  system-adoption flows safer
- Checked-in `v0.11.0` screenshots for the refreshed README/wiki pages,
  including the Quantastor HA SATADOM view and the admin maintenance surface

### Changed

- Inventory-bound storage views now behave much more like first-class runtime
  targets:
  - storage-view history stays available for internal views such as SATADOMs
  - disk-oriented metrics can auto-follow the same physical disk across homes
    when the history sidecar has a strong identity match
  - virtual storage views can pin a target Quantastor HA node instead of
    always resolving against the currently selected appliance member
- The admin setup flow now treats recommended SSH command lists as
  platform-owned defaults until the operator edits them, so switching a fresh
  form from CORE to Quantastor no longer saves inherited FreeBSD command lists
  into a new Quantastor system
- Quantastor storage-view SATADOM detail now surfaces richer ATA device
  statistics such as TRIM support, power cycles, power-on resets,
  command-count totals, interface resets, and endurance/spare values from the
  Silicon Motion SMART data

### Fixed

- Quantastor storage-view auto-refresh no longer repaints SATADOM views with a
  one-tick empty placeholder state before the matched runtime payload settles
- Storage-view slot hover/detail now lazy-loads the same richer SMART surface
  that live enclosure slots already had, instead of staying metadata-thin for
  inventory-bound views
- History reads no longer hide older local samples just because legacy rows
  were missing the newer `disk_identity_key`; local slot history is merged back
  in and eligible older rows are backfilled
- The admin storage-view editor no longer snaps away from the selected view
  while you edit the `View ID`, and view-label whitespace now survives normal
  rerenders
- Disabled admin buttons now use a normal blocked cursor instead of looking
  like a busy spinner forever

### Docs

- Refreshed the README screenshots and release-facing operator copy to the
  current `v0.11.0` behavior
- Rewrote the repo/wiki Quantastor guide around the live HA-node model instead
  of the old single-host `qs-cryostorage` shape
- Added a dedicated wiki page for history maintenance and recovery, covering
  `Delete + Purge History`, `Purge Orphaned Data`, and `Adopt Removed System
  History`

## v0.10.0 - 2026-04-19

### Added

- Dedicated optional `admin` sidecar profile with a full-page maintenance UI
  for full backup import/export, guided system setup, profile previewing, and
  SSH key management without embedding write paths into the main enclosure UI
- Full backup export/import that packages config, profile overrides,
  mapping/cache JSON, and the history SQLite database into one bundle, with
  selectable `tar.zst`, `zip`, `tar.gz`, or `.7z` packaging plus optional
  passphrase protection
- SSH key management for the admin walkthrough, including reusable `config/ssh`
  key discovery and one-click Ed25519 keypair generation that resolves
  directly onto `/run/ssh/...` runtime paths
- TLS trust inspection and import tooling in the admin sidecar so operators can
  inspect presented remote certificates, compare fingerprints, and then save
  either the remote chain or a private CA bundle for future verified
  connections
- Admin-side service-account bootstrap helpers, including sudoers preview
  generation and a clearer split between one-time bootstrap actions and the
  final saved runtime connection details
- Runtime control inside the admin sidecar so the read UI and history sidecar
  can be stopped or restarted around clean backup/import work
- Portable encrypted backups now use standard `.7z` archives instead of an
  app-only encrypted wrapper format
- Saved storage views as first-class runtime selector options alongside live
  enclosures, including separate `Saved Chassis Views` and
  `Virtual Storage Views` groupings in the main UI
- Dedicated storage-view SMART and history runtime routes so inventory-bound
  views like `Boot SATADOMs` and the NVMe carrier can participate in the main
  detail/history flows instead of staying metadata-only

### Changed

- The main enclosure UI now only shows a `System Setup` launch button when the
  admin sidecar is reachable, and that button opens the standalone admin page
  in a new tab instead of rendering embedded setup dialogs in the read UI
- The optional history sidecar is back to being history-only; complete backup
  and config mutation workflows now live behind the separate admin sidecar
- Saved `ses_enclosure` storage views now persist their own `profile_id`, and
  runtime/admin preview use that saved profile instead of always borrowing the
  currently selected live enclosure profile
- The admin storage-view workflow now uses one grouped `Add Storage View` flow
  instead of a separate mirror-only shortcut, and the add picker can now hide
  profile-backed saved chassis layouts that already auto-populate as live
  discovered hardware on the loaded system
- CORE live discovery now surfaces the separate `24`-bay "brain" chassis on
  `archive-core` as a peer live enclosure option next to the combined `60`-bay
  shelf, while still keeping the small internal AHCI SGPIO shelf out of the
  main selector
- The main UI and admin copy now make the runtime model explicit:
  `Live Enclosures`, `Saved Chassis Views`, and `Virtual Storage Views` are
  related but intentionally different things
- The history sidecar compose path now runs as `root` on the validated Docker
  Desktop bind-mount setup, and the underlying history store now also tries to
  self-heal readonly SQLite write failures instead of silently stalling

### Fixed

- Quantastor systems with `verify_ssl: false` no longer fail certificate
  verification because the REST client now bypasses the custom verified-HTTPS
  path when TLS verification is intentionally disabled
- CORE SSH SMART fallback now reports missing `smartctl` sudo permission with a
  clear remediation hint instead of bubbling raw `sudo` stderr back into the
  UI
- The history collector now recovers from the validated readonly-history-DB
  failure mode that previously left storage-view history blank for NVMe and
  SATADOM views after admin-side or root-owned SQLite file changes
- Browser QA and UI switching logic now treat saved `view:` selectors as peer
  scope switches instead of assuming every non-system selector change is a live
  enclosure refresh

### Docs

- Added checked-in `0.10.0` draft release notes and refreshed the release
  checklist toward the current admin/storage-view/history scope
- Added a dedicated wiki page for `Live Enclosures and Storage Views` and
  refreshed README/wiki/admin copy so the discovered-versus-saved mental model
  is spelled out instead of implied
- Updated `docs/SSH_READ_ONLY_SETUP.md` to match the current validated CORE
  `jbodmap` path on `The-Archive`, including the command-limited
  `sudo_nopasswd=true` allow-list with `/usr/local/sbin/smartctl`

## v0.9.0 - 2026-04-17

Stabilization release focused on performance observability, cache-first scope
switching, browser-visible regression coverage, and a broader reusable profile
base before the next larger feature push.

### Added

- Opt-in request and workflow performance timing with `.env` toggles, staged
  inventory / SMART / export timing, native `Server-Timing` headers, and
  request ids in response headers when perf timing is enabled
- Lightweight read-only perf harness script for comparing inventory, SMART
  batch, and snapshot-export-estimate timing against a running local app
- Local perf-history capture under `data/perf/` with rolling `latest` artifacts,
  append-only JSONL / CSV history, and automatic compare-against-last-run output
- Optional startup warmers and a standalone-safe persistent slot-detail cache
  so stable slot facts and stable SMART detail can survive between refreshes
  without depending on the optional history sidecar
- Lightweight Playwright browser smoke coverage for scope switching, slot-detail
  reset, and post-switch auto-refresh regressions against a running app
- First reusable built-in generic profile batch from the Quantastor reference
  set, covering common `1x24`, `3x4`, `4x15`, `5x12`, and `6x14` chassis
  families without importing vendor artwork
- Sparse layout support for explicit chassis gap cells, plus a second generic
  profile batch for irregular `102`- and `106`-bay families so center beams
  and sidecar-module voids render honestly instead of as packed rows
- Release-prep browser QA coverage for a full configured-system and
  enclosure-view sweep against a live app, so validated platform views can be
  walked before a tagged cut instead of relying only on one-off manual checks

### Changed

- SMART batch loading now reuses one inventory snapshot per batch, supports
  tunable batch concurrency, and can prefer a single whole-shelf browser
  prefetch request with chunked fallback for faster system switching on large
  enclosures
- Snapshot export history collection now uses a scope-wide history sidecar read
  path instead of one per-slot request, reducing export and estimate overhead
  on large shelves
- Snapshot export and estimate now share a short-lived in-process cache for
  scope history payloads, rendered HTML, and ZIP bytes so repeated requests on
  the same snapshot avoid rebuilding the full export every time
- Read-only inventory and SMART routes can now return stale cached data fast
  while refreshing in the background, which makes switching away from and back
  to a large system feel much less expensive once it has been seen once
- Enclosure/layout switching now reuses one shared per-system source bundle
  across enclosure views, so changing chassis views does not need to redo the
  full API and SSH collection every time
- TrueNAS websocket inventory and Quantastor REST inventory now collect their
  major fetch groups in parallel, lowering the cold first-hit floor for fresh
  inventory builds
- Perf-enabled browser sessions now show a lightweight `UI Timing` panel for
  real switch and refresh measurements, including request, repaint, history,
  SMART settle, and total page-settled timing
- Auto-refresh now uses a one-shot schedule that resets after completed
  refreshes and system switches, preventing an old timer tick from firing
  immediately after a manual switch
- System and enclosure switches now use the cached snapshot path before
  background refresh, so moving between previously seen views no longer forces a
  cold live rebuild every time
- The browser now also keeps the last seen snapshot for each visited scope, so
  switching back to a known system or enclosure can repaint immediately from
  client memory before the live refresh settles
- SMART summary cache entries now survive cross-system switches instead of being
  pruned on every snapshot change, so revisiting a shelf can reuse warmed slot
  summaries instead of immediately rebuilding them all again
- History status checks now use a short-lived client cache, which keeps switch
  settled time from paying the same sidecar status round-trip on every view hop
- Quantastor topology now requires authoritative `storagePoolDeviceEnum` data
  before replacing a trusted cached view, preventing transient middleware or
  switch-back churn from flattening mirrors into fake `disk > data` topology
  and from writing bogus topology events into history

### Docs

- Started `v0.9.0` planning with a dedicated release plan plus roadmap updates
  centered on opt-in performance instrumentation, release-to-release slowdown
  detection, and broader reusable enclosure-profile coverage

## v0.8.0 - 2026-04-16

Optional-history release that adds a lightweight SQLite sidecar, integrated
slot-history workflows inside the main UI, and frozen offline enclosure
snapshot export without breaking the original single-container deployment path.

### Added

- Optional Docker Compose `history` sidecar profile that polls the main UI API
  and persists lightweight slot history in SQLite without becoming a second
  inventory implementation
- Main-UI `History` button plus wide history drawer with a shared window picker,
  temperature history, combined read/write history, average-rate view, and
  change-only recent events
- Slot-history capture for practical comparison fields: temperature, bytes
  read, bytes written, annualized writes, power-on hours, topology context, and
  multipath state changes
- Self-contained offline HTML enclosure snapshot export with live size
  estimates, optional ZIP packaging, current-slot carry-through, and offline
  browsing across the selected enclosure
- Snapshot redaction controls with stable aliases and partial identifier
  masking, plus export-setting persistence in browser storage
- In-repo screenshot capture flow and new wiki walkthrough for the history
  drawer and snapshot export workflow

### Changed

- Main-UI timestamps now render with explicit browser-local timezone labeling
  instead of leaving operators to infer whether they are looking at UTC
- Slot history now opens in its own wide drawer under the enclosure instead of
  stretching the right-hand detail rail
- Snapshot exports now preserve the current History drawer window, selected
  slot, and open-drawer state so shared artifacts match what the operator was
  looking at when they exported
- History backup handling now keeps short-term rotating SQLite snapshots under
  `./history/backups` and also promotes weekly and monthly copies under
  `./history/backups/long-term` by default

### Fixed

- Offline snapshot export now degrades cleanly when the optional history
  sidecar is unavailable instead of stalling on doomed per-slot history fetches
- Redacted snapshot exports now preserve preloaded history correctly by re-keying
  the offline history cache to the redacted system and enclosure aliases
- Unreadable history databases are quarantined before a fresh SQLite file is
  created, reducing the chance of silent data loss after corruption
- Snapshot export metadata, timezone presentation, and dialog guidance now make
  it clearer when the artifact is frozen, which history window was used, and
  whether history will be omitted

### Docs

- README, roadmap, release checklist, wiki pages, and release-facing screenshot
  references now reflect the `0.8.0` history sidecar and snapshot export flow
- Added a draft release-notes page for the `0.8.0` cut so GitHub release text
  can be generated from a checked-in source instead of from memory

## v0.7.0 - 2026-04-16

Layout and topology release that tightens the profile-driven enclosure system,
refreshes the CORE top-loader presentation against real hardware, and cleans up
release-facing docs and screenshots around the currently validated platform set.

### Added

- Optional `bay_size` profile metadata so validated chassis can keep their
  intended physical bay geometry regardless of the currently installed media
- Refreshed screenshot capture script output for the currently validated
  platform set: CORE, SCALE, GPU Linux, UniFi UNVR, UniFi UNVR Pro, and
  Quantastor

### Changed

- The CORE `60`-bay top-loader presentation now uses denser row spacing,
  restored `6 + 6 + 3` metal divider rails, and better vertical fill inside
  the enclosure face
- Linux storage-topology labeling now prefers the best observed mounted or
  array-backed data volume over boot or swap partitions when naming pools and
  topology context
- The top summary pool card now describes pools as discovered from storage
  topology rather than implying they only come from `pool.query`
- Built-in validated profiles now declare `bay_size` explicitly where the bay
  hardware is fixed
- README, roadmap, wiki landing pages, profile docs, and sample config comments
  now reflect the current `0.7.0` release direction instead of older milestone
  wording

### Fixed

- CORE top-loader rows no longer leave large dead vertical gaps inside the
  enclosure tray area after layout scaling
- CORE top-loader row grouping now renders visible rail separators again after
  the denser chassis pass

## v0.6.0 - 2026-04-15

UniFi appliance release that extends the generic Linux path onto password-SSH
storage appliances, adds validated vendor-local LED control for the regular
UNVR, and tightens the default SSH/API security posture ahead of the release.

### Added

- Optional SSH password authentication alongside key-based SSH so password-only
  appliances can still use the generic Linux enrichment path
- First-pass built-in `ubiquiti-unvr-front-4` enclosure profile for UniFi UNVR
  style `4`-bay front-drive appliances
- First-pass built-in `ubiquiti-unvr-pro-front-7` enclosure profile for UniFi
  UNVR Pro appliances using the operator-confirmed `3`-over-`4` front layout
- Working notes for the UniFi UNVR discovery spike, including the verified
  Protect integration endpoints and the Debian/Linux storage stack observed
  over SSH
- Sample config and wiki guidance for password-only appliance-style Linux hosts,
  including the first-pass UniFi UNVR generic Linux example
- Keyboard-interactive SSH fallback for password-only Linux appliances that
  reject direct password auth, which is needed for the tested UniFi UNVR Pro
- First-pass observed slot hints for the validated UniFi UNVR Pro test unit so
  the first two reported bays can map automatically
- UniFi-specific `unifi-drive` tray styling for the built-in UNVR and UNVR Pro
  profiles so their rendered bays look closer to the vendor front-face design,
  including wide silver trays, no Supermicro-style red latch, and a right-side
  LED position

### Changed

- SSH host-key verification now defaults to strict trust-on-first-use pinning,
  storing the first observed key in `/app/data/known_hosts` and rejecting later
  mismatches unless the operator intentionally clears or replaces the saved
  entry
- Sample env/config/wiki guidance now defaults API TLS verification to `true`
  and points SSH known-host persistence at the writable app data path instead
  of the read-only SSH key mount
- Generic SSH command execution now degrades cleanly when appliance SSH
  connections or remote command execution fail, returning structured failure
  results instead of bubbling hard exceptions into inventory or SMART calls
- UniFi appliance inventory now uses `ubntstorage disk inspect` as the primary
  vendor slot source, so the regular UNVR and tested UNVR Pro can render
  vendor-numbered bays and explicit empty `nodisk` rows instead of relying only
  on HCTL hints
- Regular UniFi UNVR LED control is now validated through the on-box
  `ustd.hwmon.sata_led_sm.set_fault(slot, toggle)` path instead of SES, and the
  app can drive the left-to-right `4`-bay face over SSH using the vendor slot
  numbers exposed by `ubntstorage disk inspect`
- Generic Linux SMART parsing now extracts more ATA/SATA detail where the host
  exposes it, including SMART health state, read/write cache status, negotiated
  SATA link information, and ATA lifetime read/write volume counters
- Annualized write rate is now suppressed for very low-hour disks so fresh
  appliance installs do not project a few days of ATA SMART writes out to a
  misleading full-year rate
- UniFi placeholder HCTL slot hints are no longer treated as real device labels
  or SMART targets when no validated disk correlation exists, avoiding sticky
  bogus device placeholders in the UI
- The regular UniFi UNVR now polls `/sys/kernel/debug/gpio` as an extra
  host-local SSH probe so live identify/fault LED state can be reflected in the
  slot map after an LED action without depending on SES metadata
- The UniFi UNVR Pro now uses the same vendor-local `sata_led_sm.set_fault`
  SSH LED path as the regular UNVR, but it is surfaced as experimental until
  someone confirms operator-visible per-bay behavior on real hardware
- UniFi hint-only slots no longer get stuck in bogus `"SMART loading"` states
  when only placeholder HCTL observations are available
- UniFi notes now document the tested vendor inventory path and the current
  LED-control findings: the regular UNVR and UNVR Pro both expose a working
  vendor-local Python LED path, and the Pro still appears to be backed by the
  kernel-owned `sata_sw_leds` / SGPO stack rather than SES

## v0.5.0 - 2026-04-14

Quantastor release that adds the first new storage-platform adapter after the
profile system, keeps CORE/SCALE/Linux behavior intact, and makes the shared
`24`-bay Quantastor HA chassis genuinely operator-usable.

### Added

- First-pass Quantastor REST adapter with support for
  `storageSystemEnum`, `physicalDiskEnum`, `storagePoolEnum`,
  `storagePoolDeviceEnum`, and `haGroupEnum`
- Shared-slot Quantastor profile groundwork for the Supermicro
  `SSG-2028R-DE2CR24L`, rendered as a first-pass shared front `24`-bay view
- Quantastor storage-system inventory correlation that can render pool, member,
  and active-owner context through the existing slot-map UI
- Dedicated Quantastor REST client coverage alongside the inventory/profile
  tests that already exercise the first-pass adapter end to end

### Changed

- Bumped the app to `v0.5.0`
- Refreshed config and env examples so Quantastor user/password auth and
  API-first setup are documented alongside CORE, SCALE, and generic Linux
- Quantastor snapshots now surface live HA context from cluster metadata,
  including current master-node warnings and IO-fencing-disabled warnings when
  the appliance reports them
- Quantastor shared-slot mapping now normalizes the validated appliance's mixed
  slot string formats so the real occupied bays render as physical slots
  `0-7` and `12` instead of collapsing the last occupied disk into slot `8`
- Quantastor pool-device slot metadata now outranks the broken per-node
  hardware slot rows when the appliance disagrees with itself, so the validated
  spare disk stays in physical slot `12` in the live UI instead of slipping
  back to slot `8`
- Quantastor HA warnings now prefer the real node records over the synthetic
  cluster aggregate object when evaluating IO fencing state, avoiding false
  "disabled" alerts on validated clusters where both nodes report fencing
  enabled
- Quantastor snapshots can now supplement the REST payload with SSH `qs`
  `disk-list`, `hw-disk-list`, and `hw-enclosure-list` rows, improving shared
  slot truth and slot-detail enrichment on validated clusters
- Quantastor slot details now surface richer operator context for shared-face
  HA systems, including the selected node view, the current cluster master,
  pool-active owner, fence owner, and which nodes currently report visibility
  into the selected slot
- Quantastor slot SMART views now surface the appliance fields that are useful
  without inventing history, including SMART health status, block size, TRIM
  support, SSD life remaining, firmware, transport, SAS address, temperature,
  and predictive / non-medium / uncorrected error counters when the REST or
  SSH `qs` payload provides them
- Quantastor SMART drill-down can now merge host `smartctl` output over SSH
  when sudo is available, surfacing verified power-on time, SSD rotation,
  form factor, and read/write cache state instead of leaving those rows blank
- Quantastor slot detail can now overlay live `sg_ses` AES / enclosure-status
  metadata, including real identify state and enclosure-side SAS slot mapping,
  when one of the HA nodes exposes a working SES path over SSH
- Slot-status matching now ignores nested raw key names like `isFaulty=false`,
  preventing healthy Quantastor spares from rendering as red faulted bays just
  because the raw payload includes a boolean flag name that contains "fault"
- Quantastor can now probe multiple SSH node hosts for a working SES path,
  overlay live `sg_ses` slot metadata onto the shared `24`-bay face, and drive
  identify LEDs through the validated node-local enclosure even when the
  selected storage-system view is the opposite HA node
- Quantastor LED control now prefers the validated SSH `sg_ses` path over the
  appliance REST and `qs` identify methods, because the documented controller
  actions are still being rejected by the active LSI driver path on this
  cluster
- Quantastor slot correlation now lets verified SES presence and SAS-address
  truth override stale appliance slot hints, keeping the validated spare on
  physical slot `12` instead of letting it slip back to slot `8`
- Quantastor SMART and SES probing now prefer the cached working enclosure host
  first, so shared-HA clusters stop fan-out probing both nodes once the app
  already knows which node can talk to the live SES path
- Quantastor CLI enrichment now follows the same cached-host preference and
  host-fallback order as SES and `smartctl`, and Quantastor snapshots now load
  SES discovery first so the later CLI pass can reuse the known working node
  instead of always starting on the default SSH host
- Quantastor slot detail and tooltips now surface the active SES host plus
  SES-side attached-SAS / state-bit fallback data when `smartctl` or the
  appliance payload leaves those values blank
- Top-latch tray rendering now leaves the status LED visible and tones the
  latch down properly on dimmed peer slots, improving the shared-front `24`-bay
  Quantastor view
- Quantastor SMART candidate selection now prefers stable `disk/by-id/scsi-*`
  and direct `/dev/sd*` aliases over unsupported `disk/by-path/*` names, so
  validated spare slots keep their verified power-on, form-factor, rotation,
  and cache data after SES-driven slot remapping
- Enclosure views now warm SMART summaries in the background through a bounded
  batch API path, so tooltip/detail fields are ready faster without repeated
  per-hover round-trips once a view has loaded
- Background SMART warming is now generation-aware and self-healing across
  auto/manual refreshes, so the app keeps previously-known tooltip data visible
  while refreshing it in place and can recover late slots if a browser-side
  prefetch request gets interrupted instead of leaving them stuck loading
- Quantastor now auto-selects the live pool owner when you land on the shared
  HA view without an explicit node selection, and enclosure SMART warming can
  overlap a couple of chunk requests at once so late CORE slots stop trailing
  as far behind the early ones

### Docs

- Added a dedicated `v0.5.0` Quantastor execution plan and updated the
  Quantastor working note to record the current API-first design and current
  limitations
- Refreshed the README and in-repo wiki source so `v0.5.0` setup guidance now
  includes shipped Quantastor support instead of describing it as future work

## v0.4.0 - 2026-04-14

Chassis/profile release that generalizes enclosure rendering, extends Linux
support beyond TrueNAS, and hardens CORE/SCALE parity for richer SMART and
topology detail.

### Added

- First-pass `0.4.0` chassis/profile system work, including built-in validated
  enclosure profiles for the supported CORE and SCALE hardware
- Profile-driven tray latch/release-tab orientation so different chassis
  families can keep horizontal and vertical tray visuals aligned with the real
  hardware
- First-pass generic Linux SSH-only inventory support for profile-driven NVMe /
  `mdadm` hosts, validated against the `gpu-server` SYS-2029GP-TR right-side
  dual-bay NVMe layout
- NVMe SMART endurance/write-volume surfacing for Linux hosts, including wear
  remaining, available spare, bytes written, annualized write rate, estimated
  remaining write endurance, and richer slot hover tooltips
- Optional `nvme-cli` enrichment for Linux NVMe slots, including firmware
  revision, NVMe protocol version, namespace GUIDs, and warning/critical
  temperature thresholds
- SAS/SCSI lifetime read/write surfacing for CORE and SCALE slots when
  `smartctl` exposes processed-byte counters in the error log
- CORE alias matching for dual-path SAS devices so slot inventory can resolve
  pool topology through peer HBA legs, including `special` vdev members that
  only appear under alternate `da*` names in `pool.query`
- Profile-aware snapshot and rendering plumbing so enclosure eyebrow/summary,
  panel labels, edge labels, face styles, row grouping, and slot ordering now
  come from profile metadata instead of scattered hardware-specific UI logic
- Custom profile loading through `paths.profile_file` / `PATH_PROFILE_FILE`

### Planning

- Added detailed `0.3.x` and `0.4.0` execution-plan documents so ongoing work
  can resume from repo files instead of chat context alone.

### Docs

- Added an active roadmap note that captures the intended release order of
  `v0.3.x` parity work, `v0.4.0` chassis profiles, and `v0.5.0` Quantastor
  support
- Added a SCALE note about the appliance REST deprecation alert and recorded
  that this app currently uses the websocket / JSON-RPC middleware path rather
  than `/api/v2.0`
- Added a profile authoring guide and example custom profile file so the
  `0.4.0` chassis/profile system can be adopted without code edits
- Refreshed the README to point at `v0.4.0` CORE/SCALE screenshots and
  added profile migration notes for deployments moving from hardcoded layouts to
  built-in or custom profiles
- Added Ubuntu / `mdadm` / NVMe notes for the `gpu-server` generic Linux test
  host and refreshed config examples to show the SSH-only Linux command set
- Added a Quantastor planning note for the dual-node Supermicro
  `SSG-2028R-DE2CR24L` chassis so the shared-slot representation problem is
  captured ahead of `v0.5.0`
- Added a GitHub-wiki-ready page set under `wiki/` so beginner quick-start
  docs, platform setup guides, profile docs, and troubleshooting can be
  reviewed in-repo before being published to the GitHub wiki

## v0.3.1 - 2026-04-14

Patch release focused on CORE SMART detail restoration and refreshed release
screenshots.

### Fixed

- CORE slot SMART summaries now merge sparse API JSON with `disk.smartctl -x`
  text output before falling back to SSH, restoring fields such as read/write
  cache state, transport, SAS addresses, and negotiated link rate
- SSH SMART text enrichment now carries the same transport and cache fields when
  the app does need to fall back to host-side commands
- README screenshots now wait for populated slot details and use fresh `v0.3.1`
  captures for both the validated CORE and SCALE views

## v0.3.0 - 2026-04-13

Third public release focused on first-pass TrueNAS SCALE support, richer slot
identity, and Linux-side enclosure awareness.

### Added

- First-pass TrueNAS SCALE rough-in with a selectable `offsite-scale` system profile
- Linux SES AES parsing for SCALE through `sg_ses -p aes`
- Linux SES enclosure-status parsing for SCALE through `sg_ses -p ec`
- Split SCALE enclosure views for a front `24`-bay map and rear `12`-bay map
- SCALE disk-to-slot correlation using Linux `lunid` plus parsed SAS addresses from AES pages
- SCALE per-slot SMART summary through SSH `smartctl -x -j` when the websocket API does not expose detailed SMART JSON
- SCALE transport detail in slot summaries through SSH `smartctl -x -j`, including logical unit ID, SAS address, attached SAS address, and negotiated link rate
- SCALE identify LED control through `sg_ses --dev-slot-num=<slot> --set=ident` and `--clear=ident`
- Persistent identifier labeling in slot details so SCALE slots can show `PARTUUID`
  or `WWN` instead of falling back to an empty CORE-style GPTID field
- Fresh release screenshots for both the validated CORE and SCALE views

### Changed

- SCALE slot warnings now explicitly call out Linux SES AES parsing when TrueNAS does not expose enclosure rows
- SCALE slot warnings now reflect Linux SES fallback and live identify-state reads through enclosure-status pages
- SCALE SMART fallback now prefers SSH `smartctl` JSON and only falls back to metadata-only placeholders when SSH SMART reads fail
- SCALE SMART fallback now accepts advisory non-zero `smartctl` exit codes when valid JSON/text output is still available
- SCALE front and rear enclosure views now use front/rear chassis wording instead of inheriting the older top-loader UI labels
- README, sample config, and sample env docs now describe the current SCALE
  `sg_ses` and on-demand `smartctl` workflow instead of the older placeholder
  story

## v0.2.0 - 2026-04-12

Second public release focused on operator awareness, multi-system selection,
and richer CORE shelf context.

### Added

- Multi-system inventory registry groundwork with per-system connection settings
- Header `System` and `Enclosure` pickers wired through inventory, LED, and mapping routes
- System-aware persistent mapping keys so calibration can be scoped per appliance
- Mapping export/import workflow for the active system or enclosure scope
- Selected-slot topology context with clickable sibling awareness for the current vdev
- Selected-slot peer highlighting that dims non-sibling bays and accents the active vdev set on the enclosure map
- SMART phase 1 basics with per-slot temperature, last SMART test result, power-on age, and logical/physical sector size
- Optional `gmultipath list` enrichment for multipath device mode, path state, and member-device awareness
- Optional `camcontrol devlist -v` enrichment for per-member controller labels such as `mpr0` and `mpr1`
- Multipath presentation cards that summarize active, passive, and failed HBA/controller paths when available
- Example multi-system YAML config showing the intended `v0.2` shape
- Stable enclosure geometry so slot carriers keep a consistent physical-looking size when selection state changes

## v0.1.0 - 2026-04-12

Initial public release of the TrueNAS JBOD Enclosure UI.

### Highlights

- 60-bay Supermicro CSE-946 style top-loading enclosure view
- TrueNAS CORE middleware websocket integration for disk, pool, and enclosure data
- Optional SSH enrichment for `glabel`, `zpool`, and SES-backed shelf mapping
- SSH identify LED control through `sesutil locate` when the TrueNAS enclosure API
  does not expose writable rows
- Per-slot detail pane with pool, vdev, topology, serial, gptid, and health data
- Manual calibration workflow with persistent JSON mappings
- Single-container Docker Compose deployment with bind mounts for config, data,
  logs, and SSH material
