# Public Demo Site

This is the planning page for a future public, interactive demo.

It is not live yet.

## Can GitHub Host It?

Yes, with the right boundary.

GitHub Pages can host a static site made of HTML, CSS, and JavaScript from a
repository:

- [What is GitHub Pages?](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)
- [Creating a GitHub Pages site](https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site)

That makes Pages a good fit for a read-only public demo built from sanitized
sample data.

It does not make Pages a replacement for the Docker app. A Pages site cannot
run the FastAPI backend, the history/admin sidecars, SSH collection, middleware
API calls, LED actions, restore workflows, or private appliance credentials.

## What The Demo Should Be

The first useful demo should let someone explore the operator experience without
owning the same hardware:

- a static web page hosted from the project repository
- one or more scrubbed sample systems
- physical enclosure rendering
- slot details
- storage views
- heat-map mode
- sample history timeline playback when fixture data exists
- a visible `Demo / Offline` banner
- no live host connection, no admin maintenance actions, and no write paths

The closest existing concept is [[History and Snapshot Export|History-and-Snapshot-Export]].
The offline snapshot already proves that an enclosure view can be frozen into a
self-contained browser artifact. A public demo should use that same idea, but
with sample data and a site-shaped entry point.

## What It Should Not Be

- not the real `docker compose up -d` deployment
- not a hosted copy of a private lab
- not a restore or import target
- not a way to connect GitHub Pages to a visitor's TrueNAS, Quantastor, ESXi,
  Linux, UniFi, or BMC hosts
- not a place for real serial numbers, hostnames, SSH keys, API keys, TLS trust
  bundles, or history databases

## Possible First Version

1. Generate or hand-curate a synthetic demo fixture.
2. Build a static page that loads that fixture in the browser.
3. Reuse the normal enclosure, slot detail, storage-view, and heat-map
   interaction patterns where practical.
4. Hide or disable live-only controls.
5. Publish the static output with GitHub Pages after the fixture is scrubbed and
   deterministic.

Later, a local `Import demo snapshot` path could let people load a scrubbed
demo fixture in browser memory. That should stay separate from admin full
backup restore, which is a real local-stack maintenance workflow.

## Tracking

This is a `0.19.0-dev` follow-up candidate.

A `0.18.1` patch would only make sense if we intentionally want a docs-only
tracking release. The shipped `0.18.0` image does not need a runtime patch just
because this planning page exists.

## Related Pages

- [[Quick Start|Quick-Start]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
- [[Docker and GHCR Deployment|Docker-and-GHCR-Deployment]]
