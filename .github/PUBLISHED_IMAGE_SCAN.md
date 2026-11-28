# Published image scan

`published-image-scan.yml` scans the public GHCR images nightly at 05:41 UTC
and on manual dispatch from `main`. Pull requests run synthetic offline tests
with read-only permissions, not the registry scan or issue writer. No image
is built, published or deployed. The writer uses GitHub-hosted Ubuntu and only
`contents: read`, `packages: read`, `issues: write`. There are no added secrets,
services or paid scanner actions. Only the final reconciliation step receives
`github.token` through its environment. Checkout credentials are not persisted.
The writer's fixed concurrency group serializes schedule and dispatch without
cancelling a running reconciliation.

## Scanner and coverage

Trivy is pinned to **0.76.2**, not a moving latest release. The helper downloads
these official immutable release assets and checks both pinned SHA256 values
before extracting or executing the archive's single `trivy` executable:

| Asset | SHA256 |
| --- | --- |
| `trivy_0.76.2_Linux-64bit.tar.gz` | `bc0fc726e017b8927a62b2d3531d7a84ee01769daf5c99ed866dced54089fdbf` |
| `trivy_0.76.2_checksums.txt` | `802e4ac1418ed967cd3c4b901f4f71ca00b7363ede67778665a22d9f13f58c83` |

Source: [official release API](https://api.github.com/repos/aquasecurity/trivy/releases/tags/v0.76.2)
and its [checksum manifest](https://github.com/aquasecurity/trivy/releases/download/v0.76.2/trivy_0.76.2_checksums.txt).
A scanner upgrade requires reviewing those assets and changing the version,
archive and manifest pins together, then rerunning native qualification.

The helper enumerates every GHCR tag page and selects `latest`, `dev`, and the
semantically newest published strict `vX.Y.Z`. Prereleases and noncanonical
versions such as leading-zero versions do not qualify. Missing tags, malformed
pages, auth/network errors and digest mismatches fail the run. Each tag is
resolved to a verified immutable manifest digest before scanning. Digest aliases
are scanned once, with all their tag memberships retained. Scanning explicitly
uses remote `linux/amd64`, `--scanners vuln,secret`, `--severity HIGH,CRITICAL`,
`--ignore-unfixed`, and the validated explicit native ignore path. No path skip,
secret rule disablement or other suppression is added. Other architectures are
not qualified by this job.

The job summary records scanner version, vulnerability DB schema version and
`UpdatedAt` date, platform, and every tag/digest pair. Vulnerability totals are
package advisory scanner matches, not evidence that application behavior is
exploitable. Raw descriptions, paths and scanner-provided URLs are not published.
Public advisory links are constructed only from validated CVE or GHSA IDs.
Unknown native schemas and unsupported advisory IDs fail closed rather than
silently omitting findings.

## Ignore policy

`.trivyignore.yaml` uses Trivy's native `vulnerabilities` and `secrets` lists.
Both start empty. Validation happens before scanner acquisition or registry
network access and again before issue reconciliation. Unknown fields, duplicate
YAML keys or entries, aliases, merges, malformed dates and expired exceptions
fail the run. Every accepted vulnerability exception requires exactly `id`, a
nonblank `statement`, and canonical `expired_at: YYYY-MM-DD` strictly later than
the current UTC date. Quote dates if desired. This is a deliberately small
subset of the pinned native parser's schema. Vulnerability IDs are advisory-wide
exceptions, so review their full image coverage before accepting one. Package,
path and secret exceptions are not supported in this initial policy. Secret
exceptions must not be introduced as blanket rules or path exclusions.

## Private-triage stop

Any unignored secret match stops the entire run before any issue mutation or
closure. Output contains only safe aggregate counts. Native stdout and stderr
are discarded. Native JSON may contain `Match` or `Code` candidate bytes. It is
created owner-readable only in a private ephemeral directory outside source,
read without logging, deleted after parsing, and never uploaded. The scanner,
cache and private reports are removed before the allowlisted bundle is written.
No scanner/API raw exception is printed. Do not attach raw scanner reports to
issues, artifacts, notes or chat. A suspected real secret requires private
maintainer triage, not automatic public filing or remediation. A failed or
partial scan is not a clean result and cannot authorize closures.

## One issue per finding

A versioned SHA256 identity binds advisory ID, package and ecosystem. It excludes
tags, image digests, installed/fixed versions, severity, scan time, database date
and run URL. Occurrences aggregate all affected tag aliases and package versions.
Bodies are deterministic and contain only public allowlisted package/advisory
fields and tag aliases. They contain no volatile run metadata or image digest.

The writer lists authoritative GitHub issue pages in `state=all`, not search.
GitHub can return the repository's numeric URL in a next-page link. The writer
checks the origin, collection and query, then requests the next page on the same
named repository path used for writes and readbacks. Every page must retain
`state=all&per_page=100`. A different repository, ambiguous query or narrowing
filter stops reconciliation before writes. The writer manages only non-PR
issues authored by `github-actions[bot]` with its exact
`published-image-scan:v1` marker at the beginning of the body. Human copies,
other workflow markers and other marker versions are ignored. Conflicting
managed identities fail before writes. It constructs the complete change plan
only after validating all target scans, findings and all issue pages. Findings
reappear by reopening their existing closed issue. It closes an issue only when
the finding is absent across every successfully scanned target. An unchanged
second run makes zero POST, PATCH, comment or label writes. No comments or
label-management calls are used at all.

Each real mutation is followed by an exact issue GET and field verification.
Any issue API failure fails the job. There are no automatic HTTP retries.
Especially after an ambiguous POST response, do not immediately retry creation.
A later serialized complete run first lists all authoritative issue pages and
reuses the created issue if it exists. If GitHub accepted no creation, that
later run can create it. Application is not transactional across issues; a
mid-apply API failure can leave an already verified prefix applied, but prevents
further changes. The next full run reconciles the remaining differences.

## Qualification

Run the maintained offline helper tests and workflow contracts with the project's
Python environment:

```sh
python -m unittest discover -s .github/tests -p 'test_*.py' -v
python -m unittest tests.test_ci_contract -v
python .github/scripts/published_image_scan.py validate
```

Local HTTP fixtures exercise real GET/POST/PATCH transport, pagination,
idempotency, updates, closures, reopen, ownership and ambiguous creation.
Also run actionlint on the new workflow. Native acquisition, version/DB metadata
and published-digest scanning need separate private qualification. Local offline
tests do not prove live GHCR or GitHub issue sync. The first approved scheduled
or dispatch run after merge must verify the actual issue reconciliation and
its readbacks. Never create public proof issues during implementation testing.
