# Demo and Offline Workflows

This page keeps the demo-like workflows straight.

Several tools let you look at data away from the normal live path, but they do
different jobs and should not be treated as interchangeable.

## Pick The Right Workflow

| Workflow | Exists today? | Needs Docker? | Uses real local data? | Purpose |
| --- | --- | --- | --- | --- |
| Public demo site | yes, static Pages workflow | no | no | let visitors explore scrubbed sample data in a static browser page |
| Demo Builder Seed | yes | yes | synthetic config only | create a local fake system/profile/views for builder testing |
| Export Snapshot | yes | no after export | yes, but frozen/redacted by export settings | share one offline enclosure or storage-view HTML artifact |
| Debug Bundle | yes | no after export | yes, optionally scrubbed | send support evidence for inspection |
| Full Backup | yes | yes for restore | yes | restore or migrate app state |

## Public Demo Site

The public demo is a GitHub Pages-compatible static site. The current source
tree generates `public-demo/index.html` from live-derived TN Core / Supermicro
CSE-946-style data through the offline snapshot exporter, and the page clearly
marks itself as an offline artifact. Critical serial, SAS, NAA, and persistent
identifiers are scrambled consistently, while make, model, capacity, configured
storage-view names, SMART summaries, and history samples come from the source
data. Its pool topology follows the validated CORE 60-bay membership pattern,
including data `raidz2` groups, the spare bay, special mirror members, matching
empty bays, the `4x NVMe Carrier Card`, and `Boot SATADOMs`. It opens with no
bay selected and preserves a 7-day history window.

Public demo:

- https://gcs8.github.io/truenas-jbod-ui/

It must not:

- connect to a visitor's storage host
- run the FastAPI backend
- expose admin maintenance actions
- contain real serials, hostnames, SSH keys, API keys, TLS trust material, or
  history databases

See [[Public Demo Site|Public-Demo-Site]].

## Demo Builder Seed

The admin sidecar has an `Add Demo Builder System` action for local layout
testing.

It creates:

- `demo-builder-lab`
- `demo-builder-lab-chassis`
- `Demo Chassis`
- `Demo 4x NVMe Carrier`
- `Demo Boot Pair`
- `Demo Manual Group`

That seed is useful when you want to try the builder, saved chassis views, or
virtual storage-view flow without pointing at a real appliance first.

It still writes to your mounted local config. Restart the main UI after saving
so the runtime selector reloads the new system list.

Use [[Admin UI and System Setup|Admin-UI-and-System-Setup]] for the button
location and setup flow.

## Export Snapshot

`Export Snapshot` is the main UI's offline viewer path.

It creates one self-contained HTML artifact for the current enclosure or
storage view. It can include visible slot detail and, when history is available
and selected, frozen history samples.

Use it when you want to share the current bay map without giving someone access
to the live app.

See [[History and Snapshot Export|History-and-Snapshot-Export]].

## Debug Bundle

`Debug Bundle` is for support review.

It exports selected local state into a normal archive, with scrub toggles for
obvious secrets and disk identifiers. It is not an HTML viewer and not a
restore path.

See [[Backup, Restore, and Debug Bundles|Backup-Restore-and-Debug-Bundles]].

## Full Backup

`Full Backup` is for restore and migration.

It is the only workflow in this group that should be treated as a restore-grade
bundle. If you include secret-material paths, use the encrypted export path.

See [[Backup, Restore, and Debug Bundles|Backup-Restore-and-Debug-Bundles]].

## Good Rules

- publish only synthetic or thoroughly scrubbed data
- keep demo/offline pages visibly marked as not live
- do not use debug bundles as restore artifacts
- do not use full backups as public demo fixtures
- test import/restore flows in a disposable stack before touching a long-running
  deployment

## Related Pages

- [[Public Demo Site|Public-Demo-Site]]
- [[Visual Tour|Visual-Tour]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
- [[Backup, Restore, and Debug Bundles|Backup-Restore-and-Debug-Bundles]]
