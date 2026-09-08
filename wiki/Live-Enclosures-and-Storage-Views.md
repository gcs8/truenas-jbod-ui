# Live enclosures and storage views

The runtime selector groups physical hardware and saved layouts by type. Check the type before treating an entry as a discovered chassis.

## Runtime categories

### Live Enclosure

A `Live Enclosure` is a chassis or backplane discovered from the host API, SSH SES data, or both. The available evidence depends on the platform and system configuration.

Live enclosures appear automatically when the host exposes them. You do not need to create a storage view to display discovered hardware.

### Saved Chassis View

A `Saved Chassis View` mirrors a live enclosure through a selected profile or layout. Use one when you want a curated view of discovered bays without replacing the live hardware view.

A saved chassis view is an overlay. It is not another physical enclosure.

### Virtual Storage View

A `Virtual Storage View` binds real disks to a layout that does not represent a live SES enclosure. Use virtual views for internal NVMe carriers, boot devices, SATADOM pairs, and other fixed-disk groups.

On a Quantastor HA deployment, a virtual view can pin an HA node. Use this setting when an internal layout must resolve against a specific shared-SES member.

## Create saved and virtual views

Open the admin sidecar and select `Add Storage View`. Use this flow to create:

- a saved chassis layout for a live enclosure
- an internal NVMe carrier layout
- a SATADOM or boot-device group
- a manual group for fixed internal disks

The admin sidecar saves configuration-backed views and bindings. It does not create or emulate live hardware.

For a `ses_enclosure` view, select the profile that should render the saved view. The view keeps its own `profile_id`, so it can remain on a layout such as `Generic Front 24` even when the live enclosure uses a different profile.

## Understand profiles and views

A profile defines enclosure geometry. It controls tray rows, latch placement, LED spacing, and row dividers.

The same profile geometry is used by:

- the live main UI
- saved chassis views
- the admin setup preview
- the profile builder preview
- the storage-view preview
- offline snapshots

When these screens use the same profile, their geometry should match.

The selector entries have separate roles:

- a `profile` defines how a chassis is drawn
- a `live enclosure` represents hardware discovered at runtime
- a `saved chassis view` presents discovered hardware through a saved profile-backed layout
- a `virtual storage view` groups disks that are not represented by a live enclosure backplane

## Example

A system can have all of these entries at once:

- one discovered 60-bay shelf
- one discovered 24-bay chassis
- one internal 4-disk NVMe carrier
- one boot SATADOM pair

The shelves appear under `Live Enclosures`. The NVMe carrier and SATADOM pair appear under `Virtual Storage Views`. If you add a saved `Front Bays` view for the 24-bay chassis, it appears under `Saved Chassis Views`; it does not represent a second chassis.

The `Saved Chassis Views` group appears only when at least one saved view exists.

## Related pages

- [[Visual Tour|Visual-Tour]]
- [[Architecture and Services|Architecture-and-Services]]
- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
- [[Profiles and Custom Layouts|Profiles-and-Custom-Layouts]]
- [[Heat Map Mode|Heat-Map-Mode]]
