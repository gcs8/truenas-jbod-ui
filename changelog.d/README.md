# Changelog fragments

A pull request records its changelog entry here, in one small file named for
its own pull request number, instead of editing `CHANGELOG.md`. Two pull
requests that each add a fragment never conflict. `CHANGELOG.md` is written
from these files when a release is prepared, and the files are deleted in that
release commit.

## Name

`changelog.d/<N>.<section>.md`

- `<N>`: the number of the pull request that adds the file, without leading
  zeros. The release appends `(#N)` to the bullet from this name.
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
an `upgrade` note. A second and later entry in the same section is
`changelog.d/<N>.<section>.<k>.md`, with `k` counting from 2. A pull request
adds only fragments named for its own number, including one that carries work
from other pull requests.

## Order of work

The number exists only once the pull request does, so:

1. Open the pull request as a draft. The first `Changelog entry` check is red
   because no fragment exists yet.
2. Read the number with `gh pr view --json number --jq .number`.
3. Add `changelog.d/<N>.<section>.md` and push. The check re-runs and passes.

## Content

The text of one bullet as one inline paragraph, in plain Markdown and past
tense:

- no leading `- `, no heading, no blank line, no code fence, no HTML comment,
  no link reference definition (`[label]: target`; use an inline link);
- no raw HTML: `<` followed by a letter, `/`, `!` or `?` is refused, in
  backticks too. Write a placeholder as `NAME` or `{name}` and describe a tag
  in words; `a < b` is fine;
- do not write your own pull request number; the release appends `(#N)` from
  the file name. Any `#N` inside parentheses is refused, because the release
  tooling reads every `(#N)` as a pull request reference. A plain mention of
  an issue or another pull request in the sentence is allowed, for example
  `see #123`;
- no control or formatting character: no tab, no lone carriage return, no
  line or paragraph separator, no bidi control or zero-width character. The
  check names the file and shows the character escaped, for example `\t`.
  Ordinary line ends, LF or CRLF, are fine;
- it may wrap over several lines; at most 4096 bytes.

A fragment that is already on `main` belongs to its pull request: another pull
request may not delete, rename or edit it, or change its mode, even with
`no-changelog`. A maintainer applies the `changelog-edit-ok` label after
reading the diff, for example on a revert or on the release pull request that
collects the fragments; it waives that protection and nothing else.

Advisory mode softens only a missing or invalid entry, for contributors who
cannot apply labels. A fragment named for another number and a change to a
fragment already on `main` fail in advisory mode too.

## Examples

`changelog.d/1234.fixed.md`:

```markdown
Kept the last inventory visible when a refresh is superseded by a newer one.
```

`changelog.d/1234.upgrade.md`:

```markdown
`EXAMPLE_SETTING` now defaults to off. Set it to `on` before upgrading to
keep the previous behaviour.
```

## Commands

```bash
python scripts/changelog_fragments.py check     # validate every fragment
python scripts/changelog_fragments.py preview   # Unreleased with the fragments merged in
python scripts/check_changelog_entry.py --base origin/main --pr <N>  # the CI gate
```

Release preparation runs `collect`; see `docs/RELEASE_CHECKLIST.md`.
