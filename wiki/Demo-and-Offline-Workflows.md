# Demo and offline workflows

Use the workflow that matches what you need to share or restore. The public demo, snapshot export, debug bundle, and full backup contain different data and are not interchangeable.

## Choose a workflow

| Workflow | Needs Docker? | Uses local data? | Use it for |
| --- | --- | --- | --- |
| Public demo site | no | no | Explore synthetic sample data in a static browser page |
| Demo Builder Seed | yes | synthetic config only | Test the builder, profiles, and saved views locally |
| Export Snapshot | no after export | yes | Share one offline enclosure or storage-view HTML file |
| Debug Bundle | no after export | yes, with optional scrubbing | Send selected support evidence for inspection |
| Full Backup | yes for restore | yes | Restore or migrate application state |

## Open the public demo

[Open the public demo](https://gcs8.github.io/truenas-jbod-ui/).

The public demo is a static site generated from the synthetic fixture at `tests/fixtures/public_demo/public_demo.json`. It contains one invented 60-bay enclosure, two invented saved or virtual views, and seven days of invented history. It does not read local configuration or history.

The demo does not:

- connect to a visitor's storage host
- run the FastAPI backend
- provide admin maintenance actions
- contain real serials, hostnames, SSH keys, API keys, TLS trust material, or history databases

See [[Public Demo Site|Public-Demo-Site]] for more detail.

## Add the Demo Builder Seed

Open the admin sidecar and select `Add Demo Builder System`. The action writes these synthetic entries to the mounted local configuration:

- `demo-builder-lab`
- `demo-builder-lab-chassis`
- `Demo Chassis`
- `Demo 4x NVMe Carrier`
- `Demo Boot Pair`
- `Demo Manual Group`

Use the seed to test the profile builder, saved chassis views, or virtual storage views without connecting a real appliance. Restart the main UI after saving so the runtime selector reloads the system list.

See [[Admin UI and System Setup|Admin-UI-and-System-Setup]] for the setup flow.

## Export an offline snapshot

Select an enclosure or storage view in the main UI, then choose `Export Snapshot`. The export is one self-contained HTML file. It can include the selected slot and frozen history samples when history is available and selected.

Use this file to share a bay map without granting access to the live application. Review the file before sharing it. `Redact sensitive IDs` applies bounded aliases and masks, but it does not inspect every free-form value.

See [[History and Snapshot Export|History-and-Snapshot-Export]].

## Create a debug bundle

Use `Debug Bundle` in the admin sidecar to collect selected configuration, history, logs, and support files. Apply the scrub options that fit the recipient, then inspect the archive before sending it.

A debug bundle is not an HTML viewer and cannot be restored. See [[Backup, Restore, and Debug Bundles|Backup-Restore-and-Debug-Bundles]].

## Create a full backup

Use `Full Backup` when you need to restore or migrate application state. Full backups are restore-grade archives. Select the encrypted export path whenever the archive includes secret-material paths.

Test restore procedures in a disposable stack before changing a long-running deployment. Never use a full backup as public demo data.

See [[Backup, Restore, and Debug Bundles|Backup-Restore-and-Debug-Bundles]].

## Sharing rules

- Publish only synthetic data or data you have reviewed and scrubbed.
- Keep exported pages marked as frozen and offline.
- Do not use debug bundles as restore archives.
- Do not use full backups as demo fixtures.
- Review every export before sending it outside the deployment.

## Related pages

- [[Public Demo Site|Public-Demo-Site]]
- [[Visual Tour|Visual-Tour]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
- [[Backup, Restore, and Debug Bundles|Backup-Restore-and-Debug-Bundles]]
