# Quantastor Notes

Working notes for the shipped `v0.5.0` OSNexus Quantastor adapter.

## Current Target

- Supermicro `SSG-2028R-DE2CR24L`
- Product page:
  - [Supermicro SSG-2028R-DE2CR24L](https://www.supermicro.com/en/products/system/2U/2028/SSG-2028R-DE2CR24L.php)

## Physical Notes

- `2U` chassis
- `24` shared front-access drive slots
- two nodes share the same `24`-slot enclosure face
- drive trays are `2.5"` style
- release tab / red latch is on the top of the tray face

## API Notes

Official docs referenced for the first pass:

- [QuantaStor REST API Reference Guide](https://wiki.osnexus.com/index.php?title=QuantaStor_REST_API_Reference_Guide)
- [QuantaStor CLI Command Reference](https://wiki.osnexus.com/index.php?title=QuantaStor_CLI_Command_Reference)

Current first-pass REST endpoints:

- `storageSystemEnum`
- `physicalDiskEnum`
- `storagePoolEnum`
- `storagePoolDeviceEnum`
- `haGroupEnum`

Why this shape:

- these endpoints are enough to build storage-system views, disk lists, pool
  membership, and an operator warning that HA is present
- they let the app get useful quickly without taking on CLI parsing and
  shared-face ownership overlays all at once
- they fit the existing inventory model without introducing a new frontend
  rendering mode yet

## First-Pass Design

The current implementation treats Quantastor as:

- REST-first with optional SSH/CLI supplementation
- storage-system-scoped rather than enclosure-shared-first
- profile-backed through a built-in shared `24`-slot front view

Current built-in profile:

- `supermicro-ssg-2028r-shared-front-24`
- `1 x 24` layout
- top tray latch orientation

Quantastor appliance layout reference:

- local file provided for inspection: `D:\qs_enclosure_layout.conf`
- Quantastor ships a matching `supermicro/supermicro_cib_24bay` cluster layout
  for `SSG-2028R-DE2CR24L`
- that built-in Quantastor layout is currently modeled as:
  - `rows=1`
  - `columns=24`
  - `layoutFlow=0`
  - `diskOrientation=1`
- the app now matches the real physical `1 x 24` front layout of this chassis
- the current Quantastor web UI may show two enclosure rows after the recent
  SAS mapping issue, but one of those rows is a synthetic / virtual enclosure
  added by the appliance and should not be treated as a second physical face

Current behavior:

- each Quantastor storage system becomes a selectable app system/enclosure view
- disks are filtered to the selected storage system
- pool-device rows provide pool/member correlation when available
- HA and cluster metadata raise warnings instead of pretending shared-face
  ownership is fully solved
- current master-node warnings are surfaced from the active node metadata
- IO-fencing-disabled warnings now prefer the real node records over the
  synthetic cluster aggregate object, because the aggregate row can advertise
  stale or broader policy defaults that do not match the current node settings
- slot details now include shared-face operator context, including:
  - selected node view / presenting node
  - current pool-active owner
  - current fence owner
  - nodes that currently report visibility into the slot
- slot detail can now merge REST disk payloads with SSH `qs disk-list` and
  `qs hw-disk-list` rows when SSH is enabled on the Quantastor system
- slot SMART detail now pulls the appliance SMART-ish fields where they make
  sense, including:
  - SMART health status
  - drive temperature
  - firmware revision
  - block size
  - TRIM support
  - SSD life remaining
  - SAS address / transport hints
  - predictive, non-medium, and uncorrected read/write error counters
- mixed Quantastor slot strings such as `01`, `2`, and `12` are now normalized
  so the validated shared-front chassis renders the real occupied slots on this
  box as `0-7` and `12`
- when the node-scoped hardware rows disagree with the pool-device truth for a
  shared disk, the app now prefers the pool-device slot metadata so the live
  spare remains in physical slot `12`
- when verified SES metadata disagrees with the appliance slot hint for a
  shared disk, the app now lets the SES presence and SAS-address truth win so
  the validated spare stays on physical slot `12` instead of drifting back to
  `8`
- when one node exposes a real SES path and the peer node only exposes the
  broken short-status view, the app can now probe multiple SSH node hosts and
  use the first working `sg_ses` path for slot-identify state and LED control
- once that working SES node is known, the app now prefers that cached host for
  later SES, Quantastor CLI, and Quantastor `smartctl` probes instead of
  fanning out across both nodes every refresh
- nested raw booleans such as `isFaulty=false` no longer trip the bay-status
  keyword matcher, so healthy spares render as healthy instead of false red
  fault bays

Validated live CLI notes:

- after enabling a real shell and home directory for `jbodmap`, the `qs` CLI is
  usable over SSH with explicit `--server=<node-or-vip>,jbodmap,...`
- the local `~/.qs.cnf` token path still attempts to create transient auth
  files under `/run`, so the app currently uses explicit
  `--server=localhost,jbodmap,...` arguments instead of relying on token-file
  auth for `jbodmap`
- local-node CLI inventory works:
  - `qs hw-enclosure-list --json --server=10.13.37.30,jbodmap,...`
  - `qs hw-disk-list --json --controller=<controller-id> --server=10.13.37.30,jbodmap,...`
- the app now uses SSH `qs disk-list`, `qs hw-disk-list`, and
  `qs hw-enclosure-list` to supplement the REST payload for the validated
  cluster
- Quantastor snapshots now run the SES discovery pass before the CLI pass, so
  once the appliance's working SES node is known the later `qs` lookups can
  stay on that host instead of retrying the broken peer first
- the app now also uses SSH `sg_ses` where available to supplement the
  Quantastor shared-face view with verified SES slot metadata and identify
  state
- validated live SMART/detail examples now include:
  - occupied slot `0`: SMART `OK`, `92%` SSD life remaining, `TRIM` enabled,
    `4096 B` block size, firmware `DSF8`, SAS transport, `34 C`, and with
    `smartctl` sudo in place also verified power-on hours, SSD rotation,
    form factor, and read/write cache state
  - spare slot `12`: SMART `OK`, `100%` SSD life remaining, `TRIM` enabled,
    `4096 B` block size, firmware `GXF4`, SAS transport, and `33 C`
- documented Quantastor identify operations still fail on the validated
  cluster:
  - `qs hw-disk-identify ...` returns `Specified unable to specified devices... [err=5]`
  - `qs hw-enclosure-slot-identify ...` returns `Blinking operation ... FAILED [err=1]`
  - equivalent REST methods fail the same way
- direct SES control works on the validated right node instead:
  - `sudo -n /usr/bin/sg_ses -p aes /dev/sg11` exposes the real `24`-slot face
  - `sudo -n /usr/bin/sg_ses -p ec /dev/sg11` exposes live identify state
  - `sudo -n /usr/bin/sg_ses --dev-slot-num=0 --set=ident /dev/sg11` and
    `--clear=ident` both work
- the app now surfaces that working SES node in slot detail as the `SES Host`
  and can fall back to SES-side `Attached SAS` data when `smartctl` leaves it
  blank
- the left node currently exposes only the broken short-status path for its
  enclosure device, so the app now needs either a working right-node SSH host
  or an `ssh.extra_hosts` fallback list to reach the usable SES controller

Validated live operator-context example:

- selected app view: `QSOSN-Left`
- current cluster master: `QSOSN-Right`
- validated spare disk: physical slot `12`
- slot `12` is currently:
  - presented by `QSOSN-Left`
  - pool-owned on `QSOSN-Right`
  - fenced on `QSOSN-Right`
  - visible on both `QSOSN-Left` and `QSOSN-Right`

## Known Gaps

- current live validation is still centered on the shared-front `24`-bay
  `SSG-2028R-DE2CR24L` cluster, not a broader matrix of Quantastor hardware
- no fully generic Quantastor SES host-discovery story yet beyond the current
  SSH host plus `ssh.extra_hosts` fallback list
- no active-node polling / failover timeline yet
- no shared-face dual-node overlay yet beyond the current node-context rows
- no failover timeline / event history yet

## Design Questions For Later

- Should the UI represent this as:
  - one physical enclosure with node-aware ownership overlays
  - two logical systems sharing one enclosure face
  - or one active node view plus a peer-node context panel
- How should shared-slot identity be rendered when node A and node B both have
  inventory visibility into the same chassis?
- Should identify LED control stay enclosure-scoped while topology context
  becomes node-scoped?
- Do saved mappings need an optional node dimension for shared-enclosure
  systems, or is `system + enclosure + slot` still sufficient?
- Should HA warnings mature into a dedicated panel showing active owner,
  standby owner, quorum, and fence state?
