# Dependency audits

`dependency-audit.yml` runs on dependency and audit-tooling pull requests into
`main`, every Monday at 06:23 UTC, and through `workflow_dispatch`. It uses only
an Ubuntu GitHub-hosted runner, a read-only repository token and no new secrets.
It does not install application dependencies or run npm lifecycle scripts.

The runner attempts all three audits, even when an earlier audit finds a problem:

- `pip-audit==2.10.1` audits `requirements.lock` with `--no-deps --disable-pip`.
  This checks the pinned production dependency set, including transitive pins,
  without resolving a newer set. pip-audit evaluates environment markers for
  the Ubuntu/Python 3.12 runner, matching the Linux image. Windows-only pins
  are not audited by this job. Python findings fail at every severity.
- The same scanner audits `requirements-dev.txt` with genuine pip resolution in
  its disposable environment. It does not audit the installed tool environment
  instead. Wheel-only resolution refuses dependency build code.
- `npm@10.9.8` runs `npm audit --package-lock-only --audit-level=high --json`.
  Both high and critical findings fail, including transitive dependencies.
  Lower severities are validated but do not fail the gate.

The scanner pin was verified against the official
[PyPI release metadata](https://pypi.org/pypi/pip-audit/2.10.1/json).
The workflow reuses the full-SHA checkout, setup-python and setup-node pins from
`ci.yml`. Audit tools live outside the source checkout. No audit runs `--fix`,
changes a lockfile or updates application dependencies.

## Accepted ignores

`.github/dependency-audit-ignore.json` is the only acceptance policy. It starts
empty. Every entry must have exactly these fields:

```json
{
  "version": 1,
  "ignores": [
    {
      "ecosystem": "python",
      "package": "example-lib",
      "advisory": "CVE-2099-12345",
      "reason": "Temporary acceptance pending a compatible release.",
      "expires": "2099-01-02"
    }
  ]
}
```

This is a synthetic example, not an accepted exception. A maintainer must review
an actual package/advisory acceptance, its reason and its expiry in a pull
request. Do not publish private advisory details or scanner diagnostics during
that review.

- `ecosystem` is exactly `python` or `npm`. The package must be specific.
  Python package names use case-insensitive hyphen/underscore/dot normalization.
  npm names, including scoped packages, match exactly.
- Python advisories match a PYSEC, GHSA or CVE identifier, including aliases
  reported by pip-audit. npm advisories match their GHSA identifier, such as
  `GHSA-2345-6789-cfgh`, or numeric source identifier such as `npm:123`.
- `reason` must be a nonblank string. `expires` must be a canonical `YYYY-MM-DD`
  calendar date strictly later than the current UTC date. An entry expires at
  the start of that date, not at its end.
- Missing fields, unknown fields, duplicate JSON keys, duplicate normalized
  ecosystem/package/advisory entries, invalid dates and expired entries reject
  the entire policy before any scanner or scanner installation runs. The runner
  validates again immediately before auditing.
- Wildcards, package-wide ignores, ecosystem-wide ignores and scanner-native
  suppression switches are not supported. An ignore applies to both Python
  inputs only when the package and advisory match. It never crosses ecosystems.

npm v10 reports root advisories as objects and propagated dependency causes as
package-name strings in `via`. The runner resolves those links, including cycles
with known advisory roots, and applies acceptance only to the original advisory
package. An ignore for a parent package does not accept its child's advisory.
Accepting one high advisory does not accept a sibling advisory, and a remaining
low advisory does not invent a high finding. Missing causes, unrooted cycles,
unknown severity/report versions, inconsistent counts or severity, malformed
reports, skipped Python packages and scanner/transport failures fail closed.

Exit status is `0` for completed audits with no unaccepted findings, `1` for
unaccepted findings, or `2` for an invalid policy or incomplete audit. Raw scanner
stderr is not echoed because it can contain private package-index URLs. An
incomplete audit is not a clean result and cannot be waived with an ignore.

## Local checks

Use a project Python environment for offline tests:

```bash
python -m unittest discover -s .github/tests -p 'test_dependency_audit.py' -v
python -m unittest tests.test_ci_contract -v
python .github/scripts/dependency_audit.py --validate-only
```

To run the audits, use a separate virtual environment with
`pip-audit==2.10.1`, npm 10.9.8, `PIP_ONLY_BINARY=:all:` and lifecycle scripts
disabled. Run that environment's Python on `.github/scripts/dependency_audit.py`.
Advisory queries and development dependency resolution require network access.
Keep native reports private until triaged. Do not treat a package-index or
transport failure as evidence that the dependencies are safe.
