# Quantastor Notes

Working notes for the future `v0.5.0` OSNexus Quantastor adapter.

## Candidate Test Chassis

- Supermicro `SSG-2028R-DE2CR24L`
- Product page:
  - [Supermicro SSG-2028R-DE2CR24L](https://www.supermicro.com/en/products/system/2U/2028/SSG-2028R-DE2CR24L.php)

## Physical Notes

- `2U` chassis
- `24` shared front-access drive slots
- two nodes share the same `24`-slot enclosure face
- drive trays are `2.5"` style
- release tab / red latch is on the top of the tray face

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

## Profile Notes

- likely built-in profile target for `v0.5.0`
- tray styling should use `latch_edge: top`
- this chassis is a good test of whether the `0.4.0` profile system is truly
  flexible enough for dual-node shared-slot hardware
