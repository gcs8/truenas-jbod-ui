# Visual tour

This tour introduces the main screens before you work through setup. For
installation steps, start with [[Quick Start|Quick-Start]]. You can also open
the [[Public Demo Site|Public-Demo-Site]] and try these views yourself.

The screenshots use invented sample data. They contain no live host names,
addresses, serial numbers, credentials, or history records.

## Enclosure view

The main screen is a physical slot map. It shows bay locations first, then disk
and topology details when you select a slot. This example uses a synthetic
60-bay top-loading enclosure.

![Synthetic 60-bay enclosure overview](images/public-demo-overview.png)

The runtime selector separates physical `Live Enclosures` from operator-created
`Saved Chassis Views` and logical `Virtual Storage Views`, such as boot devices
or carrier cards.

## Heat map

Heat map mode keeps the enclosure shape and colors each bay by a selected
numeric value. Available values include temperature, read and write rate,
endurance, and attention score. See [[Heat Map Mode|Heat-Map-Mode]] for details.

## Slot history

When the optional history service is running, a populated slot can open a
history panel below the enclosure. Storage views use the same panel when the
selected internal disk has a stable identity.

![Synthetic slot history panel](images/public-demo-history.png)

See [[History and Snapshot Export|History-and-Snapshot-Export]] for history
setup and snapshot export.

## Admin setup and profiles

The optional admin service provides guided setup, system configuration, runtime
controls, profile editing, backup and restore tools, and maintenance actions.
It runs separately from the main enclosure UI.

Use [[Admin UI and System Setup|Admin-UI-and-System-Setup]] to get started. For
custom enclosure layouts, see
[[Profiles and Custom Layouts|Profiles-and-Custom-Layouts]].

## Offline snapshots

The main UI can export a self-contained HTML snapshot of the current enclosure
or storage view. The exported file opens without a connection to the live app.
[[Demo and Offline Workflows|Demo-and-Offline-Workflows]] explains how a public
demo, an offline snapshot, a debug bundle, and a full backup differ.

## Demo limitations

The public demo does not include the admin service, setup wizard, live refresh,
mapping changes, locator controls, or writes to a target. It is meant for
exploring the enclosure, storage view, heat map, history, and fabric screens
with safe sample data.

## Related pages

- [[Quick Start|Quick-Start]]
- [[Architecture and Services|Architecture-and-Services]]
- [[Live Enclosures and Storage Views|Live-Enclosures-and-Storage-Views]]
- [[Heat Map Mode|Heat-Map-Mode]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
