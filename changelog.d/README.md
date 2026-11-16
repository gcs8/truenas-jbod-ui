# Changelog fragments

A pull request records its changelog entry here, in one small file, instead of
editing `CHANGELOG.md`. Two pull requests that each add a fragment never
conflict. `CHANGELOG.md` is written from these files when a release is
prepared, and the files are deleted in that release commit.

## Name

`changelog.d/<slug>.<section>.md`

- `<slug>`: 3 to 60 lowercase letters, digits and hyphens, starting with a
  letter or digit. Pick something short and unique, such as the branch topic.
- `<section>`: one of the subsections of a release, in this order:

| Section | Heading in `CHANGELOG.md` |
| --- | --- |
| `breaking` | Breaking changes |
| `upgrade` | Upgrade notes |
| `security` | Security |
| `added` | Added |
| `changed` | Changed |
| `fixed` | Fixed |
| `performance` | Performance |
| `docs` | Docs |
| `internal` | Internal |

There is no Highlights fragment. Highlights are written at release time.

A pull request may add more than one fragment, for example a `fixed` entry and
an `upgrade` note.

## Content

The text of one bullet as one inline paragraph, in plain Markdown and past
tense:

- no leading `- `, no heading, no blank line, no code fence, no HTML comment,
  no link reference definition (`[label]: target`; use an inline link);
- do not write your own pull request number. The release adds `(#N)` from the
  commit that merged the fragment (its squash subject ends in `(#N)`). Any
  `#N` inside parentheses is refused, because the release tooling reads every
  `(#N)` as a pull request reference. A plain mention of an issue or another
  pull request in the sentence is allowed, for example `see #123`;
- it may wrap over several lines; at most 4096 bytes.

A pull request may edit another pull request's fragment, but not delete or
rename it; the gate refuses that even with `no-changelog`. A maintainer
applies the `changelog-removal-ok` label after reading, for example on a
revert.

## Examples

`changelog.d/keep-last-inventory.fixed.md`:

```markdown
Kept the last inventory visible when a refresh is superseded by a newer one.
```

`changelog.d/example-setting.upgrade.md`:

```markdown
`EXAMPLE_SETTING` now defaults to off. Set it to `on` before upgrading to
keep the previous behaviour.
```

## Commands

```bash
python scripts/changelog_fragments.py check     # validate every fragment
python scripts/changelog_fragments.py preview   # Unreleased with fragments merged in, one line per commit
python scripts/check_changelog_entry.py --base origin/main --pr <N>  # the CI gate
```

Release preparation runs `collect`; see `docs/RELEASE_CHECKLIST.md`.
