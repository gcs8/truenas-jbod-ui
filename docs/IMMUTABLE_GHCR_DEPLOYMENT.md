# Immutable GHCR deployment

This maintainer and controlled-operations runbook pins an image, Compose files, and the deployment helper to one source revision. For a normal installation, use the [Docker and GHCR deployment](../wiki/Docker-and-GHCR-Deployment.md) guide instead.

## Pick an image reference

Every registry tag is a mutable pointer, including `latest`, version tags, and `sha-...` tags. Release automation intends version tags to stay stable, but the registry can still repoint them. Only a digest reference is immutable:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui@sha256:<64-hex-digest>
```

The GHCR publish workflow records this exact reference under **Immutable manifest digest** in its job summary. Use that receipt, or resolve and verify the tag locally, before a controlled deployment.

## Update the app by digest

An image digest does not freeze a Compose file downloaded from `main`. A controlled update must bind the image to the **Source revision** from the same GHCR workflow receipt. Use the repository's transaction helper instead of assembling separate shell snippets.

Download the helper from that exact source revision:

```bash
set -euo pipefail
cd /docker-local/truenas-jbod-ui
release_revision='REPLACE_WITH_40_HEX_SOURCE_REVISION'
printf '%s\n' "$release_revision" | grep -Eq '^[0-9a-f]{40}$'
test ! -L scripts
install -d -m 700 scripts
helper_tmp="$(mktemp scripts/.update-immutable-deployment.XXXXXX)"
trap 'rm -f "$helper_tmp"' EXIT
curl -fsSL \
  -o "$helper_tmp" \
  "https://raw.githubusercontent.com/gcs8/truenas-jbod-ui/$release_revision/scripts/update_immutable_deployment.py"
chmod 700 "$helper_tmp"
python3 "$helper_tmp" --help >/dev/null
mv -f "$helper_tmp" scripts/update_immutable_deployment.py
trap - EXIT
```

The helper and every Compose source must come from the same exact 40-hex source revision.

The update command takes the complete ordered Compose chain, active profiles, expected running services, and local health endpoints. This example uses the base file plus the non-root overlay with history active:

```bash
set -euo pipefail
cd /docker-local/truenas-jbod-ui
release_revision='REPLACE_WITH_40_HEX_SOURCE_REVISION'
expected_image='REPLACE_WITH_WORKFLOW_IMMUTABLE_IMAGE'
candidate_tag='ghcr.io/gcs8/truenas-jbod-ui:v0.22.2'
python3 scripts/update_immutable_deployment.py update . \
  --project-name truenas-jbod-ui \
  --source-revision "$release_revision" \
  --expected-image "$expected_image" \
  --candidate-tag "$candidate_tag" \
  --compose docker-compose.yml=compose.yaml \
  --compose docker-compose.nonroot.yml=docker-compose.nonroot.yml \
  --profile history \
  --service enclosure-ui \
  --service enclosure-history \
  --health-url http://127.0.0.1:8080/livez \
  --health-url http://127.0.0.1:8080/healthz \
  --health-url http://127.0.0.1:8081/healthz
```

Adjust the arguments to match the running deployment exactly:

- list every active tracked Compose file with one ordered `--compose SOURCE=LIVE` argument;
- pass the live `com.docker.compose.project` value with `--project-name`;
- list every active profile with `--profile`;
- list every expected running app service with `--service`;
- list each enabled loopback health endpoint with `--health-url`.

For example, add `docker-compose.secrets.yml=docker-compose.secrets.yml` when the secrets overlay is active. Add `--profile admin`, `--service enclosure-admin`, and its loopback health URL only when admin is expected to be running through the update. The helper stops before mutation if the declared service set differs from Compose's running service set.

The helper then performs one bounded transaction. It:

1. validates the Compose project name, working directory, ordered file chain, and exact running service set against every live container's Compose labels;
2. records each service's container image ID, immutable GHCR digest, health, and restart count;
3. requires all declared app services to share one rollback digest;
4. pulls the moving candidate tag and requires its sole GHCR `RepoDigest` to equal the full immutable image from the workflow receipt;
5. downloads every declared Compose source from the same exact 40-hex source revision and validates the candidate chain;
6. writes a private `0700` `.jbod-ui-image-update` directory containing a strict `0600` JSON receipt plus hashed previous and candidate Compose and `.env` bytes;
7. activates the candidate bytes and digest, then requires the exact service set, image IDs, health, zero restart counts, and health URLs to converge.

The receipt is JSON data, not shell code. The helper rejects symlinks, wrong owners or modes, duplicate or extra keys, missing or extra files, hash changes, and cardinality mismatches before verify or rollback touches Docker. An existing receipt blocks another update so a rerun cannot erase rollback evidence.

### Verify Runtime Convergence

Re-read the private receipt and verify the running deployment again:

```bash
set -euo pipefail
cd /docker-local/truenas-jbod-ui
python3 scripts/update_immutable_deployment.py verify .
```

Keep `.env` pinned to the full `name@sha256` value. Record the helper's source revision, candidate digest, per-service image IDs, health results, and restart counts in the release evidence.

### Rollback To The Previous Digest

Any failure after receipt publication triggers automatic rollback. The helper restores every prior Compose file, pins `.env` to the recorded previous digest, recreates the exact recorded profiles and services, and verifies image IDs, health, restart counts, and health URLs before reporting rollback complete.

You can request the same receipt-validated rollback later:

```bash
set -euo pipefail
cd /docker-local/truenas-jbod-ui
python3 scripts/update_immutable_deployment.py rollback .
```

Keep the receipt through operator acceptance. Before a later update, archive the whole `0700` receipt directory outside the deployment directory. Never retag or overwrite a failed candidate digest.
