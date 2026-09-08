# Public demo site

The public demo is a static, read-only copy of the enclosure UI:

- [Open the public demo](https://gcs8.github.io/truenas-jbod-ui/)

It opens without Docker, a TrueNAS account, an API key, or an SSH connection.
You can also download the page and open it directly from disk.

## What you can explore

The demo shows an invented TrueNAS CORE system with a 60-bay top-loading
enclosure. It also includes two saved or virtual views, slot history, a heat-map
timeline, and a read-only Storage Fabric summary.

You can select slots, inspect disk and topology details, switch views, open slot
history, and scrub the heat-map timeline. The page starts with no selected bay
so you can see the full enclosure first.

![Synthetic 60-bay enclosure overview](images/public-demo-overview.png)

![Synthetic slot history panel](images/public-demo-history.png)

## Synthetic data and privacy

Every system name, disk identity, address, reading, and history sample in the
demo is invented. The page contains no live host data, target addresses,
credentials, SSH material, operator configuration, history database, logs,
caches, or backups.

The sample enclosure is an example for exploring the interface. It does not
claim support for a particular chassis, controller, disk, or multipath layout.

## Limitations

The demo is a snapshot, not a connected installation. It cannot refresh a
host, change mappings, run setup, operate a locator light, sync disk inventory,
or use the admin tools. Those actions require the Docker application and the
relevant live connection or optional sidecar.

## Useful links

- [[Quick Start|Quick-Start]]
- [[Visual Tour|Visual-Tour]]
- [[Demo and Offline Workflows|Demo-and-Offline-Workflows]]
- [[Live Enclosures and Storage Views|Live-Enclosures-and-Storage-Views]]
- [[Heat Map Mode|Heat-Map-Mode]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
