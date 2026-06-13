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
2. Read these files when present, in this order:
   - `AGENTS.md`
   - `HANDOFF.md`
   - `TODO.md`
   - `PLANS.md`
3. Treat `HANDOFF.md` as the source of truth for current task state.
4. Treat `TODO.md` as the current open-item queue.
5. Do not revisit older decisions unless the user or current handoff files say
   to do so.
6. Before editing, state the intended scope, likely files, risk tier, and
   validation tier.
7. Keep work in small bounded chunks. Finish or explicitly defer one chunk
   before starting another.
8. Update `HANDOFF.md` and `TODO.md` as work progresses:
   - what changed
   - what was verified
   - what remains open
   - what was intentionally deferred

For v0.21 work, also review the relevant planning/checklist docs before code
changes:

- `docs/V0_21_CODE_QUALITY_PITSTOP_PLAN.md`
- `docs/V0_22_STORAGE_FABRIC_ENRICHMENT_NOTES.md`
- `docs/RELEASE_CHECKLIST.md` for release or release-adjacent work

## v0.21 Scope

v0.21.x is a maintenance and confidence pitstop after Storage Fabric expansion.
It is not a feature catch-all.

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

## v0.21 Non-Goals

Do not pull broad feature work into v0.21 unless it fixes a live regression or
prevents operators from misreading existing data.

Defer these to v0.22 or later unless explicitly approved:

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
requires it. Record exact commands and results in the handoff.

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
- `docs/V0_21_CODE_QUALITY_PITSTOP_PLAN.md`
- `docs/V0_22_STORAGE_FABRIC_ENRICHMENT_NOTES.md`
- `docs/RELEASE_CHECKLIST.md`
- relevant tests under `tests/` and `qa/`

### Tier 1: Safe Source Checks

Use for docs, source-only changes, parser work, tests, and JavaScript syntax
safety.

Common commands:

```bash
python -m unittest discover -s tests -p "test_*.py" -v
coverage run -m unittest discover -s tests -p "test_*.py" -v && coverage report
python -m compileall app admin_service history_service scripts tests
node --check app/static/app.js
node --check app/static/sas_fabric_view.js
node --check admin_service/static/admin.js
node --check qa/public-demo.spec.js
git diff --check
```

On Windows, use the project virtualenv interpreter when present, for example:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.\.venv\Scripts\coverage.exe run -m unittest discover -s tests -p "test_*.py" -v; .\.venv\Scripts\coverage.exe report
.\.venv\Scripts\python.exe -m compileall app admin_service history_service scripts tests
```

Install dev-only validation tools before running the coverage command in a fresh
environment:

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
npx playwright test
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
- Capture active state in `HANDOFF.md` when sessions get large.
- Defer intentionally in `HANDOFF.md` and `TODO.md`; do not leave silent loose
  ends.
- If a change does not improve operator correctness, supportability, safety, or
  release confidence, question whether it belongs in v0.21.
