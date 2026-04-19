# M.2 Carrier Rendering Notes

Date: 2026-04-18

This note captures the current process for calibrating photo-backed M.2 carrier
layouts in the main read UI and admin preview flow, so we can reuse the same
approach for future carrier cards.

Primary current example:

- `4x NVMe Carrier Card`
- based on the ASUS Hyper M.2 x16 Gen3 image currently stored at:
  - `app/static/images/hyper-m2-gen3-card.png`

## Goal

For internal NVMe carrier views, the UI should not look like a generic bay
grid. It should look like the actual hardware and keep the overlayed M.2 cards
aligned to the physical mounting points on the board image.

The overlay rules should make the following obvious:

- slot order on the real card
- relative card length differences like `2280` vs `22110`
- constant board width of the M.2 modules
- where the module hole lands relative to the carrier standoff hole

## Current Runtime Approach

The read UI now uses a real board image instead of a CSS-only abstract
background.

Current rendering pattern:

1. Render the carrier board as a literal image layer in the runtime UI.
2. Treat the board image as the coordinate space.
3. Place each M.2 overlay with fixed pixel-derived anchors converted into
   percentages relative to the image dimensions.
4. Keep the connector side anchored, then vary only the card length.
5. Use small incremental screenshot-guided nudges rather than big geometry
   swings once the layout is close.

Relevant runtime files:

- `app/static/app.js`
- `app/static/style.css`

## Coordinate System

Current board image dimensions used for calibration:

- width: `902`
- height: `526`

Current runtime layout model in `app/static/app.js`:

- `connectorRight`
- `cardHeight`
- `holeInset`
- `rowCenters`
- `screwCenters`

These values should be treated as the stable hand-tuned calibration points for
this card until we deliberately replace the source image or add a new carrier
template.

## What To Tune First

When a carrier overlay looks wrong, use this order:

1. Lock the board image first.
2. Lock the slot order second.
3. Lock row heights / vertical centers third.
4. Lock the standoff-hole alignment fourth.
5. Only after that, adjust content layout like slot labels or form-factor tags.

This avoids chasing text or chip layout when the physical geometry is still
wrong.

## Slot Order Notes

For the current 4-slot carrier:

- `M2-1` is nearest the PCIe edge
- `M2-4` is top-most

That ordering is intentional and should not be re-flipped without checking the
real card again.

## M.2 Size Notes

Keep the semantic distinction clear:

- `2280` means `22mm` wide by `80mm` long
- `22110` means `22mm` wide by `110mm` long

Implication:

- the overlay should communicate length differences
- the overlay should *not* imply that different form factors are wider

The current UI uses a `Form Factor: ####` chip so the label stays explicit
without implying that the rendered green board width is changing.

## Visual Calibration Rules

For this style of carrier overlay:

- treat the board image as the source of truth
- align the module hole visually to the carrier standoff hole
- keep row spacing independent of text content
- if only one row is off, tune only that row
- once a row is correct, lock it and do not re-nudge it casually

This last rule mattered during the ASUS card pass:

- `M2-1`, `M2-2`, and `M2-3` were considered visually locked
- only `M2-4` needed a final top-row-only nudge

## Recommended Reuse Process For Future Carrier Cards

When adding another carrier card template:

1. Save a clean board image under `app/static/images/`.
2. Record the image dimensions.
3. Add or reuse a carrier template in the storage-view template registry.
4. Create a card-specific board-layout object in `app/static/app.js`.
5. Start with:
   - image width/height
   - connector anchor
   - row centers
   - screw/standoff centers for each supported form factor
6. Render live overlays on top of the board image.
7. Take screenshots and mark them up with colored bounding boxes if needed.
8. Make only tiny geometry nudges after the first close fit.
9. Write the final calibration notes down here or in a sibling card-specific
   note if the new hardware is materially different.

## Good Operator Feedback Patterns

The most useful review screenshots during this pass were:

- colored row bounding boxes
- explicit “this one is correct, lock it in” feedback
- direct notes like:
  - “too far down”
  - “too far right”
  - “only M2-4 needs to move”

That kind of constrained feedback is the fastest way to finish the last 10% of
alignment work.

## Follow-On Ideas

Possible future improvements:

- per-template calibration metadata instead of inline JS constants
- optional admin-side calibration overlay
- card-specific row guides or standoff markers in debug mode
- vendor-specific internal card registry for common carriers

