# Documentation inventory and finding ledger

This inventory fixes the review baseline at commit `c3c819f87211ace3ee5ec82e3be058df7b9b8191`. It covers `README.md` and every checked-in Wiki page present at that commit: 25 documents in all.

The inventory is a release gate. `scripts/check_public_docs.py` checks the same set from a clean checkout and rejects missing pages, bad links, stale screenshot files, invalid YAML examples, missing command paths, and configuration keys that are absent from checked-in source or `.env.example`.

## Document disposition

`Keep` means the page matched current source and needed no substantive rewrite. `Revise` means the page remains useful but needs a bounded correction. `Replace` means stale assumptions made a clean rewrite safer than a line-by-line patch.

| Document | Disposition | Reason |
|---|---|---|
| `README.md` | Revise | Replace historical live-derived images with current synthetic-demo screenshots; link the product and documentation ledgers. |
| `wiki/Admin-UI-and-System-Setup.md` | Revise | Remove obsolete live-derived images while retaining the current trust-boundary and setup text. |
| `wiki/Advanced-Configuration.md` | Keep | Current configuration precedence, system lists, and profile guidance match source. |
| `wiki/Architecture-and-Services.md` | Keep | Current three-service responsibilities and trust boundaries match Compose and application source. |
| `wiki/Backup-Restore-and-Debug-Bundles.md` | Revise | Remove obsolete live-derived images; retain encrypted export and restore safety guidance. |
| `wiki/Demo-and-Offline-Workflows.md` | Revise | Replace stale live-derived demo claims while retaining the workflow comparison. |
| `wiki/Docker-and-GHCR-Deployment.md` | Keep | Stable-tag and current-main deployment paths remain separated. |
| `wiki/Generic-Linux-Setup.md` | Keep | SSH, sudo, and profile guidance matches the current adapter. |
| `wiki/Heat-Map-Mode.md` | Revise | Remove obsolete live-derived images; retain current metric and history behavior. |
| `wiki/History-Maintenance-and-Recovery.md` | Revise | Remove obsolete live-derived images; retain current admin-only maintenance flow. |
| `wiki/History-and-Snapshot-Export.md` | Revise | Remove obsolete live-derived images and link the fixture-only public-demo visual examples. |
| `wiki/Home.md` | Revise | Remove the obsolete screenshot and point new readers to the current synthetic visual tour. |
| `wiki/Live-Enclosures-and-Storage-Views.md` | Revise | Remove obsolete live-derived images; retain current selector and view semantics. |
| `wiki/Operations-Logging-and-Metrics.md` | Revise | Mark UDP syslog as trusted-network only and name an authenticated encrypted alternative. |
| `wiki/Profiles-and-Custom-Layouts.md` | Revise | Remove obsolete live-derived images; retain profile validation and builder guidance. |
| `wiki/Public-Demo-Site.md` | Replace | Replace live-derived build instructions with the checked-in synthetic fixture, exact source/build identities, and manual Pages gate. |
| `wiki/Publishing-the-Wiki.md` | Revise | Remove live screenshot-capture instructions and document fixture-only screenshot publication. |
| `wiki/Quantastor-Setup.md` | Revise | Remove obsolete live-derived images; retain the current shared-slot HA guidance. |
| `wiki/Quick-Start.md` | Revise | Make CA-verified TLS the first path and isolate the temporary insecure diagnostic exception. |
| `wiki/SSH-Setup-and-Sudo.md` | Keep | Current generated grants and strict host-key guidance match source. |
| `wiki/Troubleshooting.md` | Keep | Current startup, auth, history, and export symptoms match current behavior. |
| `wiki/TrueNAS-CORE-Setup.md` | Keep | Current CORE API, SSH, and sudo guidance matches source. |
| `wiki/TrueNAS-SCALE-Setup.md` | Keep | Current SCALE API, SSH, and sudo guidance matches source. |
| `wiki/Visual-Tour.md` | Replace | Replace the v0.18 gallery with three current fixture-only public-demo screenshots and honest omissions. |
| `wiki/_Sidebar.md` | Keep | Current navigation names resolve to the checked-in page set. |

## Finding ledger

The umbrella issues do not replace focused finding records. They reconcile the public documentation and demo work that remains after those records closed.

| Source | Disposition | Evidence |
|---|---|---|
| issue #328 | Closed by republishing and byte-checking the reviewed Wiki tree. The new Wiki edits remain a separate publication gate. | [Issue #328](https://github.com/gcs8/truenas-jbod-ui/issues/328) |
| issue #329 | Closed by updating admin origin, authentication, export, and troubleshooting guidance. This pass keeps those rules intact. | [Issue #329](https://github.com/gcs8/truenas-jbod-ui/issues/329) |
| issue #330 | Closed by correcting generated sudo entries and strict host-key setup. This pass does not reopen that lane. | [Issue #330](https://github.com/gcs8/truenas-jbod-ui/issues/330) |
| issue #331 | Closed by reconciling version, deployment, segmented-history, redaction, profile, pin, and link claims. The automated docs checker guards those classes now. | [Issue #331](https://github.com/gcs8/truenas-jbod-ui/issues/331) |
| Fable review findings | Accepted public-doc and demo findings were routed to focused issues, including #297, #300, #304, and #328 through #331. No untracked Fable finding is folded into this pass. | [Issue #336](https://github.com/gcs8/truenas-jbod-ui/issues/336) and [Issue #337](https://github.com/gcs8/truenas-jbod-ui/issues/337) |
| Codex review findings | Accepted source-parity, privacy, workflow, and browser findings use the same focused issue records. Suggestions that were not accepted remain outside this milestone. | [Issue #337](https://github.com/gcs8/truenas-jbod-ui/issues/337) |
| Connector and security reviews | The public-demo source graph, privacy scanner, checked artifact, and manual publication boundary carry the accepted findings. Private or live evidence is excluded from this repository. | [Issue #300](https://github.com/gcs8/truenas-jbod-ui/issues/300) |

The release-metadata scrub replaced 32 historical deployment identifiers in 10
release notes or wraps without changing the surrounding evidence claims. It
removed the 10 historical-release exceptions plus five stale exceptions for
deleted screenshot scripts and corrected Wiki examples. The repository-wide
privacy test now matches the remaining reviewed exception registry exactly.

## Evidence required before publication

The final candidate must pass these checks from a clean checkout:

```bash
python3 scripts/check_public_docs.py --check-external
python3 scripts/check_public_screenshots.py
python3 scripts/check_public_demo_artifact.py public-demo
```

The checked-in screenshot manifest binds each PNG byte string to the synthetic public-demo artifact. A separate exact-image review records the pixel decision. GitHub Pages and the external Wiki remain manual publication steps after merge; neither is implied by a green pull request.
