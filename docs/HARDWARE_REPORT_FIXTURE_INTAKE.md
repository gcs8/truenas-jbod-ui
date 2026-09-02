# Hardware report fixture intake

Hardware reports are useful only when they can become a safe, repeatable parser or correlation test. This repository accepts public, sanitized evidence packs. It does not accept raw appliance exports, logs, configs, history databases, keys, or credentials.

## Public boundary

A report must not contain:

- passwords, tokens, API keys, SSH keys, known-hosts entries, certificates, or connection strings;
- private hostnames, IP addresses, DNS names, user names, mount paths, or topology labels;
- raw serials, WWNs, SAS addresses, disk identifiers, or vendor support material that the reporter cannot publish;
- entire command logs when only a parser section is relevant.

Replace identifiers consistently. `DISK_A` must refer to the same disk throughout an evidence pack. Preserve relationships, field names, slot numbers, command names, and parser-relevant structure.

If a reporter cannot safely sanitize the input, stop intake. Ask for a smaller synthetic reproduction or provide a bounded sanitization checklist. Do not move private material into an issue, PR, fixture, or agent prompt.

## Intake sequence

1. **Classify the symptom.** Identify the parser, correlation, rendering, or control path that consumes the evidence. Confirm the report covers a supported platform boundary.
2. **Minimize the evidence.** Keep only the command output needed to reproduce the behavior. Use the hardware-report template as the command menu, not as a request for every command.
3. **Sanitize deterministically.** Replace repeated identifiers with stable placeholders. Keep raw shape and ordering where they influence parsing.
4. **Build a public fixture pack.** Add files under `tests/fixtures/platform_parity/` with a descriptive platform and shape prefix, for example `scale_example_shelf_aes.txt` or `esxi_example_controller_physical.json`.
5. **Add one parity assertion.** Extend `tests/test_platform_parity_fixtures.py` or the nearest focused parser test. Assert the operator contract, such as enclosure count, slot identity, source label, empty-bay handling, or controller-qualified membership. Do not assert private identifiers.
6. **Prove the fixture matters.** The new test must fail against the pre-fix behavior when the report is tied to a bug. For an intake-only fixture, describe the coverage gap it closes and verify its parser path runs without inventing identity.
7. **Review publication safety.** Re-read every added fixture and test line. Check for credentials, private topology, raw identifiers, absolute paths, debug residue, and conflict markers before opening a PR.

## Fixture rules

- Fixtures are static, public, and synthetic after sanitization. Never copy from ignored runtime data during test execution.
- Use the smallest format that the production parser consumes. Keep command wrappers out unless the parser reads them.
- Name fixtures by platform and observable shape, not a private site, customer, or appliance name.
- A fixture must have a nearby test owner. Do not add a corpus file without an assertion that explains the supported contract.
- Preserve uncertainty. When evidence cannot resolve a physical identity, test for an explicit warning or unknown result instead of teaching the parser to guess.
- Do not turn a hardware report into a release claim. A fixture proves one offline input contract. Live validation remains a separate operator gate.

## Validation

For a fixture or parser intake change, run the smallest focused test first, then the parity suite when applicable:

```bash
python -m unittest tests.test_platform_parity_fixtures -v
python -m unittest tests.test_parsers tests.test_inventory tests.test_platform_parity_fixtures -v
```

Use the project interpreter. CI remains the source of truth for pinned optional tools that are not installed locally.
