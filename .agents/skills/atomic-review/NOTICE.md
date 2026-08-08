# NOTICE

`atomic-review` is a fork. This file is voluntary — MIT requires only that the copyright and permission
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

- **`references/security.md`** — the rubric is upstream's, byte for byte. An output contract is appended.
- **`references/code-quality.md`** — the file-size rule is inverted into a cohesion rule and every line-count
  threshold is removed, decomposition moves out of the presumptive-blocker list while staying on the
  Approval Bar, and clause 7 is reduced to atomicity. Thirteen sites in all. An output contract is appended.
- Everything else in this directory — `SKILL.md`, `scripts/`, `agents/` — is original work.

## Prior art that was read and not copied

`scripts/render.py` and `scripts/validate.py` were written against a specification, not against another
implementation. Two bodies of prior art informed that specification:

- **`cursor-team-kit/skills/pr-review-canvas/`** (MIT © 2026 Cursor) — a plain HTML/CSS/JS renderer. Its
  `</script>`-terminates-the-tag hazard is designed out here rather than mitigated: this renderer emits no
  JavaScript at all.
- **OpenAI's `codex-security` plugin** — licensed **`Proprietary`**. It was read for lessons about report
  shape and validation, and **nothing was copied from it**: not a file, not a function, not a CSS rule. The
  interface shape of `scripts/scope.py` follows the same `--repo/--base/--mode/--head` argument vocabulary,
  which is an interface convention rather than an expression.
