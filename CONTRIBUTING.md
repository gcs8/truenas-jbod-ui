# Contributing / Agent Rails

This file is the shared contributor and agent rails document for
`gcs8/truenas-jbod-ui`.

The project is an off-box Docker UI for physical enclosure, disk, history,
Storage Fabric, and maintenance visibility across validated TrueNAS CORE/SCALE,
Quantastor, generic Linux, ESXi, BMC/IPMI, and appliance paths. It is an
operator tool first. Keep changes anchored to correctness, supportability,
source-labeled evidence, and safe local operations.

## Startup Workflow

At the start of a work session:

1. Start from the actual repository root. If the repo path is not explicit,
   discover it before reading or editing files.
2. Read `AGENTS.md`, then this file. `HANDOFF.md`, `TODO.md`, and `PLANS.md`
   are maintainer-local worktree files that are not tracked here. Read them only
   when the current worktree provides them; do not search for or create them.
3. Treat the GitHub issue tracker as the public open-item queue and the active
   issue or PR thread as the source of truth for its scope. A maintainer-provided
   local handoff may add private operational context but does not replace live
   repository and GitHub state.
4. Do not revisit older decisions unless the user or current issue/PR thread
   says to do so.
5. Before editing, state the intended scope, likely files, risk tier, and
   validation tier.
6. Keep work in small bounded chunks. Finish or explicitly defer one chunk
   before starting another.
7. Record material progress in the working issue or PR thread:
   - what changed
   - what was verified
   - what remains open
   - what was intentionally deferred, with a tracked follow-up issue rather
     than only a closure comment

For release or release-adjacent work, review `docs/RELEASE_CHECKLIST.md` before
changes. Older cycle plans under `docs/` are historical records, not active
scope.

## Standing Maintenance Priorities

These priorities originated during the v0.21 confidence pitstop and remain the
default posture for maintenance work. Current release state lives in
`docs/ROADMAP.md` and `CHANGELOG.md`.

Prioritize work that improves operator confidence and future change safety:

- reduce Storage Fabric complexity without changing the operator contract
- improve tests, fixtures, test speed, and failure messages
- isolate platform-specific collection, parsing, and fabric-building seams
- make safe local validation obvious for humans and agents
- tighten release automation around the validation matrix
- harden backup import path validation
- harden embedded JSON/script escaping
- harden admin sidecar guardrails for the intended LAN/headless model

The operator contract must remain stable:

- slot identity remains trustworthy
- LED identify remains explicit and capability-gated
- physical-disk situational awareness remains primary
- live enclosures, saved chassis views, and virtual storage views remain aligned
  peer concepts
- platform visibility stays honest and source-labeled across CORE, SCALE,
  Quantastor, Linux, ESXi, and BMC/IPMI paths
- functional parity means a predictable operator experience, not identical
  feature sets on every platform

## Standing Non-Goals For Maintenance Cycles

Do not pull broad feature work into a maintenance cycle unless it fixes a live
regression or prevents operators from misreading existing data.

Defer these unless explicitly approved for the current cycle:

- deeper Linux sysfs/SAS/NVMe enrichment
- new Quantastor HA model changes
- ESXi RAID-management actions
- BMC write controls beyond existing identify/locator boundaries
- major visual redesign unless needed to fix a regression
- app-dev busywork that does not improve operator correctness or supportability
- large rewrites of Storage Fabric, inventory, or browser UI without a staged
  migration plan and tests

## Safety And Data Boundaries

Do not copy, paste, summarize, or store secret-bearing or local-only content in
prompts, notes, handoffs, tickets, test fixtures, or docs unless the user
explicitly approves the exact source and purpose.

Avoid copying or dumping:

- `.env`
- `config.yaml`
- SSH keys or known-hosts material
- `secrets.env`
- `.git` internals or repository metadata dumps
- logs
- history databases
- `data/`
- `node_modules/`
- caches
- generated public-demo artifacts
- unrelated shares or mounted data

Normal Git commands are allowed and expected. Use `git status`, `git diff`,
`git log`, and similar commands to understand work state, but do not paste raw
`.git` internals or broad repository metadata dumps into external notes.

Safe sources include:

- source files
- tests
- docs
- workflows
- `.env.example`
- `config/config.example.yaml`
- checked-in fixtures that are not generated or secret-bearing
- non-secret Docker runtime shape

When inspecting a live Docker stack, keep output at a non-secret shape level:

- container names
- images
- ports
- mount paths without file contents
- health state
- log shape without secret-bearing lines
- non-secret environment key names only

Do not crawl unrelated shares, home directories, backups, or mounted data unless
the user explicitly approves that scope.

## Admin Sidecar Framing

The admin sidecar is a supported local-ops control plane for setup,
backup/restore, runtime control, profile editing, and maintenance.

Important framing:

- It is intended for LAN/headless/local infrastructure use, not public Internet
  exposure.
- It is explicitly started when needed.
- It auto-stops by default after about 3600 seconds unless configured otherwise.
- It is powerful because it can touch config, runtime state, backups, and Docker
  control paths.

Do not describe the intended LAN/headless model as inherently wrong. Do keep the
operator guardrails clear: explicit start, time-limited runtime, no public-facing
assumptions, cautious backup/import handling, and careful treatment of secrets.

## Validation Tiers

Pick the lightest tier that proves the change, then escalate when risk or scope
requires it. Record exact commands and results in the working issue/PR or, for a
release, its release wrap.

### Tier 0: Read-Only Orientation

Use this before planning, scouting, or reviewing.

Allowed:

- read safe docs, source, and tests
- inspect file names and repo structure
- inspect GitHub workflow definitions
- inspect non-secret Docker runtime shape if a stack is already running

Do not:

- edit files
- install dependencies
- start or stop containers
- read secret/local-only files
- rely on hidden local history/data as universal truth
- claim validation passed just because code was inspected

Useful orientation files:

- `README.md`
- `package.json`
- `docker-compose.yml`
- `docker-compose.dev.yml`
- `.github/workflows/`
- `CHANGELOG.md`
- `docs/ROADMAP.md`
- `docs/RELEASE_CHECKLIST.md`
- relevant tests under `tests/` and `qa/`

### Tier 1: Safe Source Checks

Use for docs, source-only changes, parser work, tests, and JavaScript syntax
safety.

The authoritative Tier 1 source-validation entrypoint is the platform-aware
wrapper. Run it from the repository root:

```bash
python scripts/dev_check.py --safe
```

It runs the applicable Python suite, compileall, bounded Ruff, every maintained
JavaScript source and `qa/*.spec.js` syntax check, diff hygiene, JavaScript unit
tests, the checked-in performance baseline check, and Prometheus rule validation
when `promtool` is available. The final named summary reports every gate as
`PASS`, `FAIL`, or `SKIP`; a missing `promtool` is an explicit `SKIP` with a
reason rather than silent success.

On POSIX, `--safe` runs full unittest discovery to mirror the source-level CI
test boundary. On Windows, use the project virtualenv interpreter when present:

```powershell
.\.venv\Scripts\python.exe scripts\dev_check.py --safe
```

Windows runs the centrally classified portable Python suite. It explicitly
skips and names suites that transitively import the POSIX-only `fcntl` backup
path or assert POSIX ownership, permission, link, descriptor, or process-ID
semantics. Do not replace that honest result with full unittest discovery or
claim those POSIX contracts were validated on Windows. Run the POSIX CI/Linux
gate for their coverage.

Raw command reference (the wrapper remains authoritative):

```bash
python -m unittest discover -s tests -p "test_*.py" -v
coverage run -m unittest discover -s tests -p "test_*.py" -v && coverage report
python -m compileall app admin_service history_service scripts tests
node --check app/static/app.js
node --check app/static/sas_fabric_view.js
node --check admin_service/static/admin.js
node --check history_service/static/dashboard.js
for spec in qa/*.spec.js; do node --check "$spec"; done
git diff --check
ruff check app admin_service history_service scripts tests --select E4,E7,E9,F
npm run test:unit
python scripts/build_perf_baseline.py --check
promtool check rules prometheus/rules/truenas-jbod-ui-alerts-v1.yml
```

The source commands shared with CI are contract-tested against
`.github/workflows/ci.yml`; the performance baseline is an additional Tier 1
gate. Set `PROMTOOL_BINARY` to an installed executable when it is not on `PATH`.

Install dev-only validation tools before running the wrapper or coverage command
in a fresh environment:

```bash
python -m pip install -r requirements-dev.txt
```

Targeted suites by risk area:

```bash
python -m unittest tests.test_sas_fabric tests.test_inventory tests.test_parsers tests.test_platform_parity_fixtures -v
python -m unittest tests.test_admin_service tests.test_account_bootstrap tests.test_system_backup -v
python -m unittest tests.test_history_service tests.test_perf tests.test_perf_harness tests.test_snapshot_export -v
python -m unittest tests.test_release_status tests.test_release_wrap_validator -v
```

If browser QA dependencies are needed:

```bash
npm ci
npm run qa:ui:install
```

### Tier 2: Docker Dev Feedback

Use when behavior depends on the running app, health endpoints, optional
sidecars, browser flows, runtime settings, or source/container packaging.

Use source-build commands only in a development checkout or an approved dev
runtime workspace:

```bash
cp .env.example .env
cp config/config.example.yaml config/config.yaml
docker compose -f docker-compose.dev.yml up -d --build
```

Do not paste the resulting `.env` or `config/config.yaml` into prompts, notes,
handoffs, issues, or docs.

Default dev ports:

- main UI: `8080`
- history sidecar: `8081`
- admin sidecar: `8082`

Health checks:

```bash
curl -fsS http://localhost:8080/livez
curl -fsS http://localhost:8080/healthz
```

Optional sidecars:

```bash
docker compose -f docker-compose.dev.yml --profile history up -d --build
docker compose -f docker-compose.dev.yml --profile admin up -d --build enclosure-admin
docker compose -f docker-compose.dev.yml --profile history --profile admin up -d --build
```

Sidecar matrix to validate when relevant:

1. UI only:
   - history stopped
   - admin stopped
   - `:8080/livez` and `:8080/healthz` healthy
2. UI + history:
   - admin stopped
   - `:8080/livez`, `:8081/livez`, `:8081/healthz`
   - `/api/history/status`
3. UI + admin:
   - history stopped
   - `:8080/livez`, `:8082/livez`, `:8082/healthz`
   - admin runtime cards handle stopped history intentionally
4. UI + history + admin:
   - all services healthy
   - runtime cards show aligned running versions after startup/restart

Browser smoke:

```bash
npm ci
npm run qa:ui:install
npx playwright test qa/public-demo.spec.js
PLAYWRIGHT_ADMIN_BASE_URL=http://127.0.0.1:8082 npx playwright test qa/admin-operations.spec.js
```

The switching and ESXi suites are live-appliance contracts, not portable fixture
tests. Run them only against an intentionally configured stack:

```bash
PLAYWRIGHT_LIVE_APPLIANCE_QA=1 npx playwright test qa/ui-switching.spec.js qa/esxi-smoke.spec.js
```

If using non-default URLs, pass explicit base URLs:

```bash
PLAYWRIGHT_BASE_URL=http://127.0.0.1:8080 npx playwright test
PLAYWRIGHT_ADMIN_BASE_URL=http://127.0.0.1:8082 npx playwright test qa/admin-operations.spec.js
```

### Tier 3: Release / Full Validation

`docs/RELEASE_CHECKLIST.md` is the mandatory release gate. Do not replace it
with this file.

Release/full validation includes, as applicable:

- full Python unit discovery
- targeted Python suites for touched high-risk areas
- Python compileall
- JavaScript syntax gates
- `git diff --check`
- Docker dev build and health
- optional-sidecar runtime matrix
- full Playwright/browser gates
- feature-specific live API/UI gates
- public-demo artifact checks
- performance harnesses
- Linux QA restore gate
- release wrap validation
- post-publish GHCR/deployment sniff tests

Do not push a release tag until the release wrap evidence table has every
pre-tag gate recorded as `Pass` or justified `N/A`.

## Branch and CI policy

CI runs on every branch push and on pull requests targeting `main`. The
all-branch push contract keeps pre-PR validation independent of naming prefixes;
contributors may use descriptive prefixes such as `feat/`, `fix/`, `refactor/`,
`docs/`, `perf/`, `test/`, `ci/`, `codex/`, or `claude/` without creating a CI
coverage gap. Tag pushes are not part of this preflight workflow.

## CI blocking policy

The following pull-request checks are release-blocking and required for `main`:

- `Diff hygiene`
- `Python compile and unittest (3.12)`
- `Python compile and unittest (3.14)`
- `Bounded Ruff`
- `Production container smoke`
- `JavaScript syntax and npm lock`
- `Checked-in public demo artifact`
- `Admin clean-room browser QA`
- `Changelog entry` (pull requests only; see "Changelog And Release Notes")

Coverage is report-only. CodeQL is report-only until repository branch
protection explicitly makes it required. Publish workflows are release gates,
not ordinary pull-request checks. `PR type labels` is a labelling helper, not a
check. If a check name changes, update branch protection and this list together
after the new workflow has run successfully.

## High-Risk File Rules

Treat these files as high-risk:

- `app/services/inventory.py`
- `app/services/sas_fabric.py`
- `app/static/app.js`
- `app/static/sas_fabric_view.js`
- `admin_service/static/admin.js`
- backup/import/export paths under `history_service/` and `admin_service/`

Rules for high-risk files:

1. Read relevant tests before editing.
2. Prefer small, reviewable changes.
3. Do not hardcode lab-only hostnames, system IDs, controller numbers, SAS
   addresses, slots, serials, chassis assumptions, or private network details.
4. Preserve source labels and capability boundaries.
5. Preserve stable slot/storage-view identity and history scope IDs.
6. Keep unsupported actions visibly unavailable with a reason.
7. Add or update fixtures/tests for new parser, topology, UI, admin, or backup
   behavior.
8. Run targeted Tier 1 checks at minimum.
9. Escalate to Tier 2 when UI/runtime behavior can regress.
10. Use Tier 3 and the release checklist for release work.

Additional notes by area:

- `app/services/inventory.py`
  - Preserve platform-specific collection seams.
  - Avoid changing source precedence without tests.
  - Do not let weak BMC/platform evidence overwrite stronger host-side identity.

- `app/services/sas_fabric.py`
  - Keep Storage Fabric read-only.
  - Preserve raw/evidence fields needed for support.
  - Label observed, inferred, weak, partial, unavailable, and unsupported states.

- Main/static JavaScript
  - Avoid geometry/rendering churn unless fixing a regression.
  - Watch for nested scroll, overflow, column overlap, first-click selection,
    stale selection, and source-label regressions.
  - Harden embedded JSON/script escaping when data crosses into HTML/JS.

- Admin service and admin JavaScript
  - Preserve explicit sidecar guardrails.
  - Treat backup import/export as sensitive.
  - Validate archive member paths and restore targets defensively.
  - Do not imply public-facing/cloud exposure is supported.

## Public Demo And Fixture Policy

Public-demo output should look realistic enough to represent the product well,
but tests must be deterministic and clean-checkout safe.

Rules:

1. Do not depend on a developer's local `history/history.db` for normal unit
   tests.
2. Do not assume hidden local 60-bay or production-like history data exists in
   CI or a clean checkout.
3. If a test needs representative data, add a deterministic sanitized fixture.
4. If a test truly needs local/live data, mark it as integration/local-data and
   skip by default unless an explicit environment variable enables it.
5. Generated public-demo artifacts should be produced by scripts, not manual
   edits.
6. Public-demo output must not contain real hostnames, private IPs, serials,
   WWNs/SAS addresses, keys, configured system names, or secrets.

When public-demo behavior or data changes, separate clean artifact validation
from local-data release regeneration.

Clean checkout / CI validation uses the checked-in artifact only:

```bash
python scripts/check_public_demo_artifact.py public-demo
PUBLIC_DEMO_ARTIFACT=public-demo/index.html npx playwright test qa/public-demo.spec.js
```

Release-maintainer regeneration requires ignored local `history/history.db`
input and must be explicit:

```bash
PUBLIC_DEMO_LOCAL_HISTORY=1 python -m unittest tests.test_public_demo_fixture -v
python scripts/build_public_demo.py --output public-demo/index.html
python scripts/build_public_demo.py --output public-demo/index.html --check
python scripts/check_public_demo_artifact.py public-demo
PUBLIC_DEMO_ARTIFACT=public-demo/index.html npx playwright test qa/public-demo.spec.js
```

Use `PUBLIC_DEMO_BUILD_FROM_HISTORY=1` only when the Playwright public-demo
smoke should build a temporary artifact from local ignored history data.
On Windows shells, adapt environment variable syntax as needed.

## Live Data Cautions

Live data is evidence, not universal truth.

- Summarize counts, statuses, source availability, and health instead of dumping
  raw API, admin, backup, import/export, logs, or history payloads.
- Scrub or avoid private identifiers in docs/tests:
  - hostnames
  - private IP ranges
  - serial numbers
  - WWNs/SAS addresses
  - configured system labels
  - user names
  - keys/tokens
- Do not use live-only data to make tests pass in a clean checkout.
- If a live behavior matters, convert it into a sanitized fixture or document it
  as a live/integration validation step.

## Operator-Facing Wording Guidance

Use wording that helps operators understand what the app knows, where it came
from, and what is safe to do.

Prefer:

- `read-only`
- `source-labeled`
- `observed from ...`
- `inferred from ...`
- `unavailable because ...`
- `unsupported on this platform/path`
- `partial evidence`
- `capability depends on ...`
- `Storage Fabric path context where evidence exists`

Avoid:

- implying all platforms expose identical features
- implying weak or inferred evidence is physical certainty
- calling unsupported capabilities `broken`
- promising ESXi RAID-management actions
- implying the app installs packages on TrueNAS or Quantastor appliances
- reframing the local/LAN/headless admin sidecar as inherently wrong
- describing history/admin sidecars as dev-only helpers; they are supported
  deployment options

Platform-specific wording:

- CORE can be described as the deepest validated physical/SAS reference path.
- SCALE/Linux should be described as Linux/SES/profile/source dependent.
- Quantastor should preserve REST-first plus optional SSH/CLI/SES wording.
- ESXi is read-only; no RAID management. BMC-backed identify is only where
  validated.
- BMC/IPMI evidence should be capability-scoped and should not replace stronger
  host-side facts.

## Changelog And Release Notes

Every pull request that operators could notice records itself in
`CHANGELOG.md`, and the GitHub release body is generated from pull request
labels plus that file. The rules:

- **One line per merged pull request** under `## Unreleased`, past tense,
  ending with the pull request number as `(#N)`. Several numbers may share a
  line as `(#12, #13, and #14)`, and the `(#N)` may sit on a wrapped
  continuation line. Wrap at about 80 columns like the rest of the file.
- **Subsections**, in this order: `Highlights`, `Breaking changes`,
  `Upgrade notes`, `Security`, `Added`, `Changed`, `Fixed`, `Performance`,
  `Docs`, `Internal`. Omit empty ones. Per-PR lines go in any subsection except
  `Highlights`, which holds three to five one-sentence release themes written
  at release time.
- **Upgrade notes rule.** A pull request labelled `breaking` must also add a
  bullet under `### Breaking changes` or `### Upgrade notes` that starts with
  the variable, file, or action the operator must handle, says what to do, and
  ends with `(#N)`.
- **Labels drive the release body.** `.github/workflows/pr-labels.yml` derives
  a label from the conventional title prefix, after stripping a leading
  `[tag]`: `feat` to `enhancement`, `fix` to `bug`, `perf` to `performance`,
  `docs` to `documentation`, `test` to `tests`, `ci` to `ci`,
  `refactor`/`chore`/`build` to `internal`, `security` (type or scope) to
  `security`, and a `!` after the type or scope (`feat!:`, `fix(api)!:`) adds
  `breaking`. Changing the title swaps the type label; `security` and
  `breaking` are only ever added automatically, so a hand-applied one stays.
  `.github/release.yml` maps those labels to the categories Breaking changes,
  Security, Features, Fixes, Performance, Documentation, Internal, and
  Dependencies.
- **`no-changelog` escape.** Apply the `no-changelog` label to a pull request
  that is invisible to operators (tests-only, CI-only, tooling). It needs no
  entry and is excluded from the generated release notes. Re-run the
  `Changelog entry` job after applying the label; it reads labels live.
  The `dependencies` label (Dependabot) skips the entry gate the same way,
  but those pull requests still appear in the release body under
  Dependencies.

Two scripts enforce this:

- `scripts/check_changelog_entry.py` runs as the `Changelog entry` job on
  every pull request. When the diff touches `app/`, `admin_service/`,
  `history_service/`, `scripts/`, `config/`, `wiki/`, `docs/` (excluding
  release wrap and release notes files), `docker-compose*.yml`, `Dockerfile*`,
  or `.env.example`, it requires an added `(#N)` bullet for this pull request
  under `## Unreleased`, and the upgrade-note bullet when `breaking` is set.
  The pull request number does not exist before `gh pr create`, so the first
  run of a new pull request fails until the changelog line is pushed; add it
  as the next commit. Locally:
  `python scripts/check_changelog_entry.py --base origin/main --pr <N>`.
- `scripts/check_release_changelog_coverage.py <previous tag> "<section
  header>"` runs during release prep. It collects merged pull request numbers
  from `git log <tag>..HEAD` squash and merge subjects and from
  `--merged-prs-json <file>`, produced with
  `gh pr list --state merged --search "merged:>=<tag date>" --limit 1000 --json number,mergedAt,labels`
  (squash subjects lose the number when the merge title is edited). It removes
  pull requests labelled `no-changelog`, then fails when any remaining number
  is missing from the target section. When `wiki/` changed
  since the tag it also requires `--wiki-commit <sha>` and verifies with
  `git ls-remote` that the sha is a branch tip of the GitHub wiki repository.
  Its `Changelog coverage: pass (<N> PRs)` and `External wiki commit: <sha>`
  lines are the evidence the release wrap validator requires.

Release body recipe, after the coverage gate passes and the release section
header is final:

```bash
python scripts/render_release_notes.py "## vX.Y.Z - YYYY-MM-DD" > release-notes.md
gh release create vX.Y.Z --title "vX.Y.Z" --generate-notes --notes-file release-notes.md
```

`render_release_notes.py` prints the section's Highlights and Upgrade notes;
GitHub appends the label-categorized pull request list from
`.github/release.yml`. Check the rendered release for pull requests that landed
in the wrong category and fix the label rather than editing the body by hand.

## AI / Codex Handoff Shape

Every substantial agent handoff should be concise and auditable.

Use this shape:

```markdown
## Scope
- Task:
- Branch/commit:
- Intended validation tier:
- Non-goals:

## Changed
- Files changed:
- Behavior changed:
- Docs/tests changed:
- Changelog line: (the `(#N)` bullet added under `## Unreleased`, or `no-changelog` and why)

## Verified
- Commands run:
- Results:
- Browser/API/runtime evidence:
- Docker/service health evidence:

## Data/Safety
- Secrets avoided:
- Live data used:
- Scrubbing notes:
- Any local-only assumptions:

## Risks / Open
- Known risks:
- Deferred items:
- Follow-up TODOs:
- Questions for Ryoko/user:
```

For release work, also update the required release wrap evidence table from
`docs/RELEASE_CHECKLIST.md`.

Do not paste raw logs, raw admin import/export responses, raw history DB rows,
secret config, SSH material, or unrelated local data into handoffs.

## Repo Working Style

- Prefer small bounded chunks.
- Do not commit, push, tag, publish, or restart shared/live stacks unless the
  user explicitly approves that action.
- Preserve accepted UI geometry/rendering work unless the user asks to reopen it
  or a regression requires it.
- Keep saved storage views and live enclosures aligned as peer concepts.
- Keep Storage Fabric read-only and honest about source strength.
- Keep the active issue/PR thread current when sessions get large. Update a
  maintainer-provided local handoff too when one exists.
- Create tracked follow-up issues for intentional deferrals; do not leave
  silent loose ends in local-only notes.
- If a change does not improve operator correctness, supportability, safety, or
  release confidence, question whether it belongs in the current cycle.
