# Stable rebuild scan

Refs #1509, phase 1 only. This workflow builds and scans the current published
stable release without publishing an image or changing a release tag. Phase 2
publishing and tag policy remain an owner decision. The managed comparison issue
stays open even when a rebuild clears no findings. This workflow never closes
#1509 or any other issue.

## Source and base refresh

The helper reads every page of the public GHCR tag list, admits only strict
`vX.Y.Z` stable tags and selects the maximum numeric semantic version. It fetches
that exact public Git tag, resolves it to a commit and builds the detached source,
not application code from `main` or the pull request. The published comparison
image is pinned to an independently verified manifest digest for `linux/amd64`.

The implementation's conservative refresh method is to resolve today's immutable
`linux/amd64` digest of the exact Python minor/distribution tag already named by
the stable Dockerfile. In a disposable source copy it replaces only the `FROM`
reference suffix, preserving every other Dockerfile byte. It accepts a single
`FROM python:3.N-slim` line, optionally with a supported Debian distribution
suffix and an existing digest. It refuses multi-stage builds, build variables,
platform overrides, aliases and other bases instead of changing the minor or
distribution. This is an implementation choice for review, not a publishing or
owner policy. In particular, `v0.23.0` uses `FROM python:3.12-slim`, not the pinned
base described in the original proposal. The tracked Dockerfile is not edited.

Buildx runs `--pull --no-cache --platform linux/amd64 --load` with a local-only
image tag. The only build argument is the resolved source commit for its existing
revision label. There is no dispatch input for arbitrary refs or build arguments.
There is no GHCR login, `--push`, or package-write permission.

## Native qualification

Both the read-only pull-request job and the weekly/manual main job execute the
same complete operation. The pull-request job has no issue writer or write
permissions. Jobs run only on disposable GitHub-hosted Ubuntu runners.

Production qualification reuses the stable release's complete `container-smoke`
run recipes from its checked-in CI workflow. A disposable fixture points at the
already-built local image with `--no-build`, keeping its health, identity,
filesystem, capability, service and synthetic access assertions. Its UI port binds
only to loopback. Disposable Compose copies omit the Docker socket mount because
the service identity probes do not require Docker control. No useful host HOME or
Docker daemon is mounted into application containers.

Image-only qualification uses the maintained `scripts/run_image_upgrade_smoke.py`
`hardened` scenario. The baseline is the published current stable digest and its
own Compose files. It changes only the image pin, validates the rebuilt revision,
retained synthetic state and health, writes synthetic successor state, and rolls
back to the published stable image. The existing helper's literal `compose pull`
procedure requires a disposable registry. The sole allowed candidate push is to
`127.0.0.1:5000/stable-rebuild-scan:local`, with a digest-pinned registry listening
only on loopback. This is not a GHCR publication. Registry teardown runs on both
success and failure. Incompatible stable smoke contracts fail the job and prevent
issue synchronization; they must not be skipped to obtain a comparison.

## Scanner and comparison

Trivy `0.76.2` is downloaded by fixed version. Both its checksum manifest and
Linux archive must match the SHA-256 constants in the helper before execution.
Both images are saved locally and scanned with that same verified binary and
`linux/amd64` platform. The job downloads the vulnerability DB once into a fresh
private cache. Each scan explicitly skips DB and Java DB updates. DB version,
update time and database-file hash must remain identical before and after every
scan. Ambient Trivy configuration and ignore files are not used. The helper uses
an explicitly empty controlled ignore file, with no added vulnerability or secret
baseline or path/rule exclusion.

Each image gets a fixable High/Critical vulnerability scan and a separate
all-severity secret scan. Any secret match, malformed/incomplete report,
identity mismatch, changed DB, or failed build/smoke prevents every public write.
Native command output and reports are never printed or uploaded. Raw reports,
image archives, scanner cache and disposable source live outside the checkout in
a private ephemeral directory, removed before the final issue-sync step. The
inter-step receipt contains only validated, allowlisted comparison evidence and
is removed at job end. Errors contain no native traceback.

Qualification diagnostics contain only fixed allowlisted phase/event/kind fields
and numeric statuses. Phase begin/complete events use status 0. Native events
preserve the actual subprocess exit, including negative signal statuses. HTTP
failures preserve the numeric HTTP status; successful responses also record their
status. `python`, `timeout`, `os` and `transport` failures use the generic status 1,
not an invented native exit. Cleanup has its own phase, so its status does not
hide an already-recorded smoke failure. Command arguments, URLs, headers, source
snippets, native stdout/stderr, arbitrary exception text and secret reports never
enter these diagnostics. A phase completion is not proof that later phases ran.

Counts use advisory, ecosystem and package identity. Python package spelling is
normalized, advisory aliases are merged within that package/ecosystem, and
installed versions and source paths do not multiply the count. Cleared matches
are published minus rebuilt; remaining matches are the intersection; new matches
are rebuilt minus published. These are scanner matches, not exploitability claims.

The job summary records the stable tag, source commit, published platform digest,
refreshed base reference/digest, local rebuilt image ID, DB metadata and run URL.
A Docker image ID is not a published registry digest. Rebuilt image IDs, DB times
and run URLs stay out of the issue body so a repeat of the same comparison has no
public mutation merely because the run changed.

## Managed comparison issue

Only scheduled or manual runs on this repository's `main` can synchronize one
comparison issue. The writer uses a fixed versioned current-stable fingerprint
marker and requires `github-actions[bot]` ownership. It enumerates all open and
closed issue pages, validates the full pair and builds the complete deterministic
plan before writing. A closed managed issue is reopened, not duplicated. Multiple
markers, foreign ownership, API/authentication failures, incomplete pagination or
readback mismatch fail closed. It never comments, labels, closes or edits unrelated
issues. Every mutation gets an exact GET readback. An ambiguous POST is not retried.
An unchanged second run performs zero POST/PATCH/comment/label writes.

The token is exposed only to the final sync step. Top-level permissions are read
only; only the main schedule/dispatch job grants `issues: write`, alongside
`contents: read` and `packages: read`. Writer concurrency is serialized and never
cancelled in progress. There is no `pull_request_target` workflow.

## Qualification boundary

Offline unit tests exercise comparison admission, build commands, base refresh,
registry validation and actual issue HTTP transport against synthetic loopback
fixtures. They do not prove a Docker build. The read-only hosted pull-request
job must pass the actual stable build, both smokes and paired native scans before
this change is accepted. Local machines without Docker cannot provide that
qualification. The first real scheduled issue synchronization remains a separate
post-merge acceptance check. Do not create synthetic public proof issues.

Run the focused tests from the repository root:

```sh
python -m unittest discover -s .github/tests -p 'test_stable_rebuild_scan.py' -v
python -m unittest tests.test_ci_contract -v
```
