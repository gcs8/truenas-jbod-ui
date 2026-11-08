# Heat map mode

Heat map mode colors the physical bay layout by a selected numeric metric. Use it to compare occupied slots without changing the enclosure layout.

Heat map mode is read-only. It does not add disk controls, LED actions, or admin write actions.

## Open a heat map

1. Select a system and an enclosure or storage view.
2. Click `Heat Map` in the enclosure header.
3. Choose a `Metric`.
4. For `Temperature`, keep `Colours` on `Bands` to judge absolute temperature, or switch to `Relative` to compare bays.
5. Adjust `Scale` if relative colors are too flat or too strong.

The metric value appears in the center of each bay. Health, empty, identify, fault, and unknown indicators remain visible. Empty bays and missing values are neutral or hatched. The renderer does not treat them as zero or include them in the color scale.

Serial, GPTID, make, model, pool, vdev, firmware, and device name remain in the standard hover and detail views.

## Available metrics

- `Attention Score`
- `Temperature`
- `Temperature vs View Avg`
- `Power-On Hours`
- `Lifetime Read`
- `Lifetime Write`
- `Read Rate`
- `Write Rate`
- `Annualized Read`
- `Annualized Write`
- `Read/Write Ratio`
- `Endurance Used`
- `Endurance Remaining`
- `Estimated TBW Left`
- `Media Errors`
- `Predictive Errors`
- `Interface CRC Errors`
- `Unsafe Shutdowns`

Use temperature metrics to find hot bays, rate metrics to compare recent activity, and lifetime or error metrics to find outliers in the selected view.

## Temperature bands

`Temperature` colors each bay by an absolute band, so a bay's color does not depend on the other bays in view. Two healthy disks at 48 C and 49 C share a color.

| Band | HDD defaults | SSD/NVMe defaults |
| --- | --- | --- |
| Cool | below 30 C | below 30 C |
| Normal | 30-44 C | 30-59 C |
| Warm | 45-49 C | 60-69 C |
| Warning | 50-59 C | 70-79 C |
| Critical | 60 C and above | 80 C and above |

When a drive reports its own SMART temperature ratings, those replace the defaults for that bay: the warning rating starts `Warning` and the critical rating starts `Critical`. The app reads NVMe warning and critical composite thresholds, ATA SCT maximum operating and limit temperatures, and the SCSI drive trip temperature (as critical). If a drive reports only one rating, the other keeps the default 10 C spacing. `Warm` starts 5 C below `Warning` for HDDs and 10 C below for SSD/NVMe drives. SSD/NVMe defaults apply when SMART reports NVMe transport or a rotation rate of 0, or the device name starts with `nvme`; every other drive uses the HDD defaults.

The legend lists the bands and their source: drive SMART thresholds or the defaults. When bays in view use different bands, the legend shows the most common set and the hover text shows each bay's own band and thresholds.

To compare bays with each other instead, change `Colours` from `Bands` to `Relative`. Relative colors stretch from the coolest to the warmest bay in view, so they show which bays run warmer than their neighbours but not whether any of them is too hot. `Temperature vs View Avg` uses a fixed scale from -10 C to +10 C around the view average.

`Attention Score` is a rule-based heuristic, not machine learning. It adds points for specific conditions such as SMART health problems, high temperature, endurance risk, error counters, missing SMART data on occupied slots, and unhealthy slot state. Hover over a bay to see the reasons for its score.

## Use history-backed metrics

These metrics use the optional history sidecar:

- `Read Rate`
- `Write Rate`
- `Annualized Read`
- `Annualized Write`
- `Read/Write Ratio`
- timeline playback for sampled metrics such as temperature

For rate calculations, the UI requests only the required raw counter metric for visible bays and does not request slot events. If the history sidecar is unavailable, the main UI remains usable and the legend displays `History unavailable` for these metrics.

## Select a time window

For a history-backed metric, choose a trailing range with `Window`.

- Use `1h` or `24h` to inspect recent activity.
- Use `7d` or `30d` to reduce the effect of short bursts.
- Use `All` for the broadest range available from the sidecar.

The window changes rate calculations and timeline samples. It does not change current lifetime SMART totals.

## Scrub the timeline

For a metric with history samples, change `Mode` from `Current` to `Timeline`.

Timeline mode starts at the newest sample in the selected window. Drag the slider to move through recorded samples. After clicking the slider, use the left and right arrow keys to move one sample at a time. Missing samples remain neutral and are not displayed as zero.

Timeline playback is useful for temperature and `Temperature vs View Avg`. Any metric with a history sample source can provide the same control.

## Adjust the color scale

The `Scale` slider changes how the visible value range maps to the blue, green, yellow, and red colors. It does not change metric values. Temperature bands are fixed, so `Scale` is hidden while `Colours` is `Bands`.

Bay text (slot number, device, pool, serial) sits on a chip of the bay's heat color, in black or white, whichever contrasts more, so it meets the WCAG AA 4.5:1 contrast ratio on every heat color.

Heat-map preferences reset when the page reloads. Exported offline snapshots do not preserve the selected heat-map mode.

For history setup and snapshot behavior, see [[History and Snapshot Export|History-and-Snapshot-Export]].

## Related pages

- [[Visual Tour|Visual-Tour]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[Live Enclosures and Storage Views|Live-Enclosures-and-Storage-Views]]
- [[Demo and Offline Workflows|Demo-and-Offline-Workflows]]
