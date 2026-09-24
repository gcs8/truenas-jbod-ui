# Storage Fabric

The Storage Fabric page shows how each bay is connected: which HBA or storage
source it hangs off, which path and expander carry it, which enclosure holds
it, and which pool and vdev use the disk. It is read-only. Nothing on this page
changes the host.

Open it with the `Storage Fabric` button on the enclosure page, or add
`/sas-fabric` to the main UI address. The `System` and `Enclosure` selectors at
the top pick what the map shows; `Back to Enclosure` returns to the same
selection on the main page.

## What each platform can show

- **TrueNAS CORE**: HBAs (`mpr` controllers), active and standby paths, SAS
  expanders, MPR and SES enclosures, backplane zones and bays, plus kernel
  error counts per controller and per link. This is the deepest map.
- **TrueNAS SCALE and other Linux hosts**: SES enclosure devices seen over SSH
  (`sg_ses`, `lsscsi`), block devices, pools and vdevs. HBA and expander hops
  are not shown because Linux SES data does not report them. See
  [[TrueNAS SCALE Setup|TrueNAS-SCALE-Setup]] and
  [[Generic Linux Setup|Generic-Linux-Setup]] for the SSH commands involved.
- **QuantaStor**: HA node, enclosure, pool, vdev and disk from the REST API,
  with SES detail when SSH is configured. See
  [[Quantastor Setup|Quantastor-Setup]].
- **ESXi**: host, controller, member path, datastore and disk. Read-only; the
  app never changes RAID settings.

The muted note under the map title says which of these sources built the map.
It is information, not a fault.

## The four views

The buttons above the map switch between four views of the same data.

### Storage Lanes

One column per HBA or storage source. Each lane lists that source's paths, the
expanders and enclosures behind it, and the bays it reaches. Use it to answer
"what is connected to this HBA" and "which bays does this path carry".

Long bay lists are cut short with a `+N` button; click it to show every bay,
and again to fold the list back.

### Impact Map

One card per path, most useful when a path is degraded. Each card shows the
path state, the number of bays it carries, and the pools, vdevs and disk
devices on it. Click a bay chip to jump to that bay in Disk Path view.

### Physical Trace

Start from any item and follow its chain: host, HBA, path, expander,
enclosure, backplane zone, bay. The inspector on the right lists the item's
facts, the nodes and links on its trace, and related traces you can click into.

Each click adds a crumb to the trail above the map. Click a crumb to go back to
that point; click `Physical Trace` at the start of the trail to clear it.
Related traces you have already visited are marked `(visited)` and can still be
opened.

### Disk Path

Pick a bay and see one card per hop from the host to the disk. Each card shows
the main facts for that hop; a `More details` block at the bottom holds the
rest (addresses, SES element ids, sense and log summaries). On TrueNAS CORE the
kernel error panel for the controller or link sits under the cards.

The inspector shows the selected disk's model, serial, size, pool and vdev, the
path members, and a SMART summary fetched over SSH when SMART is available for
that platform.

## The focus strip

Under the map title, up to three shortcut cards point at the most useful
starting points for the current selection:

- `Fault focus`: the item with the most kernel errors. Opens Physical Trace.
- `Path focus`: the default path for the selection, with its state and bay
  count. Opens Impact Map.
- `Disk path`: a bay with a disk in it. Opens Disk Path.

## The status chip

The chip in the status strip tells you how much to trust the map:

| Chip | Meaning |
| --- | --- |
| `STORAGE OK` | Every enabled source answered and the map is current. |
| `STORAGE PARTIAL` | A source failed or a warning needs attention. The text next to the chip says which. |
| `STORAGE STALE` | The map was kept from an older snapshot because a fresh one was not available. |
| `STORAGE OFF` | No map is available for this selection. The text says why. |
| `STORAGE ERR` | The last refresh failed. The text shows the error. |
| `STORAGE LOADING` | A refresh is running. |

`Refresh Storage Fabric` re-reads the current inventory and rebuilds the map.
It does not force the host to be probed again; a normal inventory refresh on the
main page does that.

## Summary cards

The cards above the map count what the map contains: HBAs (or sources),
paths, expanders, enclosures, traces (the paths and bays you can follow) and
connections between them. The counts are for the selected system and
enclosure only.

## Renaming an item

Every HBA, path, expander, enclosure, pool, vdev and bay can carry a friendly
name. Select the item, click the pencil next to its name in the inspector, type
the name, and press `Save`. `Clear` removes a saved name and `Cancel` or the
Escape key leaves the editor without saving. A saved name shows in place of the
raw label, with the raw label underneath.

Names are stored by the app, per system. They are never written to the host.

When Basic authentication is turned on, renaming needs a sign-in. Use the
`Sign in` form that appears above the map; you stay signed in until you reload
the page or click `Sign out`. Reading the map never needs a sign-in. See
[[Advanced Configuration|Advanced-Configuration]] for the authentication
settings.

## Kernel errors on TrueNAS CORE

When the CORE kernel log holds errors for a controller or link, the Physical
Trace and Disk Path views show a collapsed kernel error panel (labelled
`Fault Evidence`) with a one-line summary. Open it to see:

- the affected devices, targets and the most likely layer,
- the most recent events, and
- a table of the newest events with filters for text, event type, severity
  and confidence, and pages of 25.

Only the newest events are collected, so the table says how many events the
kernel log holds in total and how many are listed. If the log has no
timestamps, events are listed in log order.

## When the page is empty

- **`No SES enclosure data was found for this ... system`** on SCALE or Linux:
  the host did not report an SES enclosure over SSH. Check that SSH works and
  that `sg_ses` is installed, then see
  [[SSH Setup and Sudo|SSH-Setup-and-Sudo]] and the "A Linux host has no SES
  devices" section of [[Troubleshooting]].
- **`No disk or enclosure data was found for ...`**: the selected system has no
  disks, enclosures or storage views in the current inventory. Check the
  system in the admin UI and refresh the main page first.
- **`No enclosures found`** in the `Enclosure` selector: discovery found no
  enclosure for the selected system. The map may still show disks and pools
  from the system inventory. See [[Troubleshooting]].
- **`STORAGE PARTIAL`** with a source named in the text: that source failed
  on the last refresh. The map is built from the sources that did answer.
