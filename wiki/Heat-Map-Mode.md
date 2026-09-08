# Heat map mode

Heat map mode colors the physical bay layout by a selected numeric metric. Use it to compare occupied slots without changing the enclosure layout.

Heat map mode is read-only. It does not add disk controls, LED actions, or admin write actions.

## Open a heat map

1. Select a system and an enclosure or storage view.
2. Click `Heat Map` in the enclosure header.
3. Choose a `Metric`.
4. Adjust `Scale` if the colors are too flat or too strong.

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

The `Scale` slider changes how the visible value range maps to the blue, green, yellow, and red colors. It does not change metric values.

Heat-map preferences reset when the page reloads. Exported offline snapshots do not preserve the selected heat-map mode.

For history setup and snapshot behavior, see [[History and Snapshot Export|History-and-Snapshot-Export]].

## Related pages

- [[Visual Tour|Visual-Tour]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[Live Enclosures and Storage Views|Live-Enclosures-and-Storage-Views]]
- [[Demo and Offline Workflows|Demo-and-Offline-Workflows]]
