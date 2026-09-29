# Public screenshot exact-byte review

This record covers the two desktop fixture screenshots generated from `public-demo/index.html`. GitHub Actions run `36060385480` used the pinned Playwright `v1.63.0-jammy` container, `file://`, UTC, a 1920 by 1080 viewport, reduced motion, and no network request. The job captured each image twice and produced byte-identical copies; `sha256sums.txt` verified.

Source revision: `2a7f11258083a4cd27b156fe4a073f83db2842a5`

Source artifact SHA-256: `a52fb93384f25902fe6668600101f82992560701d1a8528d51882b7429593446`

| Image | Dimensions | Bytes | SHA-256 | Pixel review |
|---|---:|---:|---|---|
| `public-demo-overview.png` | 1920 by 3059 | 851100 | `1c6242674f8a661f0851d0bef9842f3f501fb6a392f3f1bc4f292488ba9d55d7` | PASS |
| `public-demo-history.png` | 1920 by 3817 | 953927 | `66e61def4a0ca56ba68aae0bd95e1bc980be42d5f3c70ba1daf303475138de8d` | PASS |

Backend-lane integration (#568, source `2a7f112`): run `36060385480` captured the rebuilt artifact twice; run-1, run-2 and the candidate are byte-identical and `sha256sums.txt` verified. Both PNGs are byte-identical to the previously approved pair (same SHA-256 and bytes; an RGB comparison found 0 changed pixels), so the checked-in images are unchanged and only the recorded source revision and artifact hash move. Both exact PNGs were re-inspected with the Hermes `vision_analyze` tool: no source revision, commit hash, private host, address or real serial is visible, and there is no clipping, overlap or malformed text beyond the intended ellipses.

These exact images accompany the main-UI integration (#562, superseding #425, #508, #510, #477, #484, #485 and #491), which changes the declared demo inputs `app/static/app.js`, `app/static/style.css`, `app/templates/index.html`, `app/templates/base.html` and several services. The layout change is intended, so a pixel-for-pixel match with the previous pair is not expected: an RGB comparison differs from row 0 on (the header copy changed) and both pages are shorter (overview 4092 to 3059 px, history 4852 to 3817 px).

Intended differences, confirmed with Hermes `vision_analyze` on both exact PNGs:

- The snapshot banner (previously about 427 px, with "Frozen Sanitized Snapshot", "Public Demo Artifact" and the full Source revision and Build ID) is now a short "Demo data" banner that says what the app does, with four facts (Scope, History window, Captured, Identifiers). Provenance sits in the collapsed "About this demo" block, so no hash is visible above the fold (#485).
- The disabled Refresh Now / Auto-refresh / Refresh Every controls and the "Inventory evidence counters" block are hidden in the saved copy (#485).
- Plain-words copy: "Bays" status line, "Bay assignment" panel, "TrueNAS API: OK at capture" / "SSH: off at capture" / "History: included" chips, "Connections" panel, "Bay reported by" in Slot Details, SMART counters under a collapsed "SMART details" disclosure (#484, #508).
- Smaller text raised to the 12px floor on bay, fabric and disk-path labels (#477).
- No upgrade notice appears in the saved copy, as designed (#491).

The review checked both pages for clipping, overlap, corruption, unstyled elements, private data and malformed text. Nothing is broken. The selector values in the header and the bay labels use their existing ellipsis. The Connections panel repeats "This offline copy does not include the connection map." in the status line and the inspector card; that is the existing offline empty state, not a rendering fault. The selected slot remains 57, with consistent device, serial, persistent ID, health, temperature, pool, vdev and special-class topology across the enclosure, Slot Details, Topology Context, Bay assignment and the history panel. The history page shows both charts (temperature; read/write counters with Lifetime/Rate buttons), the six summary cards and the Recent Events empty state. The map still contains every bay from 00 through 59 exactly once, with 47 populated and 13 empty bays. The demo-data wording remains explicit.

Final pixel verdict: `PASS`

Hermes vision tool review (Ryoko lane main-ui) signs off the two exact PNGs and hashes recorded above. This approval applies only to those bytes. Any recapture resets `pixel_review` to `PENDING` and requires a new named review.
