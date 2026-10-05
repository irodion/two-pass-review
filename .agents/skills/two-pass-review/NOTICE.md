# NOTICE

`two-pass-review` is a fork. This file is voluntary — MIT requires only that the copyright and permission
notice be retained, which [`LICENSE`](LICENSE) does, and it does **not** require a statement of changes
(that is Apache-2.0 §4(b)). It exists because a reader deserves to know which parts of this skill someone
else wrote.

## What was forked

Both rubrics under [`references/`](references/) come from the **`thermos` plugin** in
[`cursor/plugins`](https://github.com/cursor/plugins), MIT © 2026 Cursor.

| This file | Upstream path | SHA-256 of the exact bytes forked |
|---|---|---|
| `references/security.md` | `thermos/skills/thermo-nuclear-review/SKILL.md` | `5091011c4490932d40658ae958fb55b9aaca8e2bba5196860295ab945180e434` |
| `references/code-quality.md` | `thermos/skills/thermo-nuclear-code-quality-review/SKILL.md` | `7faca08b51b643b2ddd0836f92af15574444024685dcc1e677dbbb39ae8c9e8f` |
| `LICENSE` | `thermos/LICENSE` | `702f5f331b56aff0e33d8c7826df5202559f894145eb70355c6477b55b5bb8a0` |

**The upstream commit was not recorded when the copies were taken, so it is not stated here.** The content
hashes above pin the exact bytes instead, which is what a commit id would have been standing in for.
Naming a plausible-looking commit would be worse than naming none.

Note that `cursor/plugins` carries no root licence; licensing there is per-plugin.

## What changed

- **`references/security.md`** — the rubric is upstream's, byte for byte, except that its YAML frontmatter
  is replaced by a provenance comment. An output contract is appended.
- **`references/code-quality.md`** — the file-size rule is inverted into a cohesion rule and every line-count
  threshold is removed, decomposition moves out of the presumptive-blocker list while staying on the
  Approval Bar, and clause 7 is reduced to atomicity. Thirteen sites in all. Its YAML frontmatter is
  replaced by a provenance comment, and an output contract is appended.
- **`LICENSE`** — upstream's permission text is retained to the byte; a second copyright line is added above
  Cursor's, and Cursor's is scoped to `references/`. This directory is copied around as a unit, so it has to
  carry a notice for the original work in it as well as for the forked rubrics. The hash in the table above
  is therefore the hash of `thermos/LICENSE` as taken, not of this file as it now stands.
- Everything else in this directory — `SKILL.md`, `references/prompts.md`, `scripts/`, `agents/` — is
  original work.

This list is complete.

## The rubric text CI pins

The forked text of each rubric — from the end of its provenance comment to the `# Output contract` line,
with LF line endings — as it stands after the changes above, by SHA-256. This repository's CI fails when
either one differs, and a failing check blocks a merge to `main`. The pins are kept here, beside the list
they have to agree with, so that changing one is an edit to this file: a change to a rubric lands only if
it is listed above and re-pinned below, together.

| File | SHA-256 of the forked text |
|---|---|
| `references/security.md` | `aefccbeee3debe2d7dd6e83f97196cc041c36e22b23d88477b19912a02d3efba` |
| `references/code-quality.md` | `07ad870e4aad29e26217e9fb9567b3b2409229a667976d29e6ca6a34e3864b4d` |

## Prior art that was read and not copied

`scripts/render.py` and `scripts/validate.py` were written against a specification, not against another
implementation. Two bodies of prior art informed that specification:

- **`cursor-team-kit/skills/pr-review-canvas/`** (MIT © 2026 Cursor) — a plain HTML/CSS/JS renderer. Its
  `</script>`-terminates-the-tag hazard is designed out here rather than mitigated: every string is
  escaped before any structural regex touches it, and the one script this page carries is a fixed
  constant with nothing a pass wrote interpolated into it.
- **OpenAI's `codex-security` plugin** — licensed **`Proprietary`**. It was read for lessons about report
  shape and validation, and **nothing was copied from it**: not a file, not a function, not a CSS rule. The
  interface shape of `scripts/scope.py` follows the same `--repo/--base/--mode/--head` argument vocabulary,
  which is an interface convention rather than an expression.
- **Alibaba's [`open-code-review`](https://github.com/alibaba/open-code-review)** (Apache-2.0) — its
  Independent Reflection stage (described in arXiv:2608.09290) is the design the merge step's
  falsification check adapts: a checker that sees only the diff, flags only what the diff directly
  contradicts, and fails open. The mechanism was adopted; no text was copied — the instruction `SKILL.md`
  gives its falsifier is written in this repository's own words.
