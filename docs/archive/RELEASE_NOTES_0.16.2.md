# Release Notes - v0.16.2

Release date: `2026-05-14`

## Summary

`0.16.2` is a narrow operator-feedback hotfix on top of `0.16.1`.

This patch cleans up three issues found while using the live enclosure detail
rail and snapshot export dialog:

- the snapshot export dialog recalculated the full estimate when only the
  selected packaging changed from `Auto` to `Force ZIP`
- TrueNAS `{serial_lunid}` identifiers could appear in the GPTID row, even
  when zpool data had a stronger GPTID member path available
- detail-row `Copy` buttons could fail on LAN HTTP when the browser did not
  expose `navigator.clipboard.writeText`

## Fixed

- snapshot export packaging and oversize changes now reuse the estimate already
  loaded in the dialog, updating the selected-size/current-choice state
  client-side
- slot details now treat `{serial_lunid}` identifiers as a fallback
  `Serial/LUN ID` persistent identifier instead of presenting them as GPTIDs
- real `/dev/gptid/...` zpool paths now win over the `{serial_lunid}` fallback
  when both are available for the selected slot
- copy buttons now use the Clipboard API when available and fall back to a
  selection-based copy path when it is not
- browser QA now covers changing the export dialog from `Auto` to `Force ZIP`
  without another estimate request

## Validation Snapshot

Validated on `codex/v0.16.2-detail-export-hotfix-2026-05-14`:

- `node --check app/static/app.js`
- `.\.venv\Scripts\python.exe -m unittest tests.test_inventory tests.test_snapshot_export -v` (`117` tests)
- `.\.venv\Scripts\python.exe -m compileall app tests`
- `npx playwright test qa/ui-switching.spec.js -g "export snapshot dialog renders estimate UI"`
- `docker compose up -d --build enclosure-ui`
- `GET http://127.0.0.1:8080/livez` returned `{"status":"ok","version":"0.16.2"}`
- `git diff --check`

## Deployment Note

No screenshot refresh is needed for this patch because the layout and primary
workflows did not materially change. Fresh deployments from `0.16.2` include
the copy fallback, persistent-ID labeling fix, and cheaper export packaging
selection behavior.
