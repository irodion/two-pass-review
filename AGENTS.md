# Working on this repository

This repo *is* the `two-pass-review` skill. The shipping artifact is
`.agents/skills/two-pass-review/`; everything at the root is scaffolding. `README.md` describes the skill
for the people who use it — this file is for whoever is changing it.

## Test with `/usr/bin/python3`, not `python3`

The scripts target **Python 3.9 syntax**, because macOS `/usr/bin/python3` is 3.9.6 and that is the
interpreter a stock Mac will run them with. The `python3` on your `PATH` is almost certainly much newer.

```sh
/usr/bin/python3 .agents/skills/two-pass-review/scripts/render.py <findings.json>
```

A 3.10+ construct passes silently under the newer interpreter and fails for the user. Run both when you
touch a script: the old one proves the syntax floor, the new one catches deprecations that get written to
stderr — which the orchestrator reads as though something failed.

## Three constraints that are not negotiable

- **Stdlib only.** No dependencies, no network, no build step. The skill is copied into other people's
  repositories, and anything it needs installed is something that will be missing.
- **The page has no JavaScript.** Not "very little" — none. It is opened over `file://` and forwarded by
  email, and emitting no script tags deletes an entire class of escaping bug rather than mitigating it.
  Collapsing is `<details>`; filtering is hidden radios and sibling selectors.
- **Escape first, then apply structure.** `html.escape(..., quote=True)` runs on the whole string before
  any structural regex touches it, so no code path can emit an unescaped byte.

## The rubrics are forked text

`references/security.md` and `references/code-quality.md` come from Cursor's `thermos` plugin (MIT). The
authorised edits are enumerated and complete — see `.agents/skills/two-pass-review/NOTICE.md`. **What the
passes look at, weight, or consult is not ours to change.** Finding yourself improving a rubric means the
change has left this repo's remit.

## How to check a change

There is no test runner, deliberately. CI checks the floor and nothing above it — what it covers is at the
bottom of this section, and passing it means only that the scripts still start. The verification that
decides whether a change is good is two things, and neither automates:

1. **Run the skill on this repo and read the report.** `/two-pass-review` on the current branch. This is
   the primary check, and it is not ceremonial: it has found real defects in its own implementation more
   than once, including several no fixture could reach.
2. **Render an artifact twice and `diff` the pages.** For a change that should not alter output — a
   refactor, a rename, moving escaping around — render before and after and compare. Byte-identical is a
   stronger guarantee than any assertion you would have written, and it is available because the renderer
   is deterministic and writes no timestamp into the page. Keep it that way.

Any `findings.json` from a previous run works as the input for (2); they accumulate under
`<temp-root>/two-pass-review/<repo-slug>-<hash>/`. A larger artifact is a better test, so prefer a real
review over a hand-made one.

Negative checks for `validate.py` are written ad-hoc and thrown away. Do the same — a committed test
corpus is out of scope.

## What CI does, and what it cannot

`.github/workflows/checks.yml` runs three things on every push and pull request, none of which know
anything about reviewing code:

- **`python 3.9` and `python 3.13`** — every script compiles on both, and imports cleanly on the modern one
  with `DeprecationWarning` and `SyntaxWarning` fatal. This is the check that enforces the floor above,
  which until now was a sentence in a document that nothing verified.
- **`constraints`** — `.github/checks.py`: every import is stdlib, `page.py` emits no script tag or inline
  handler, the committed `.claude/skills/` symlink is relative and resolves, and every relative link in the
  docs points at a file a clone has.
- **`readme install`** — `.github/replay-readme-install.sh` extracts the `sh` blocks from `README.md` and
  runs them. It does not keep its own copy of the commands, because a copy would have passed every time
  the real ones were broken, which by then was three commands across two reviews.

None of this is a test corpus: there are no fixtures and no expected output, only invariants. And none of
it substitutes for (1) and (2). CI cannot tell you the report is wrong — it can only tell you the scripts
still start.

## The reasoning behind the design is not in this repository

Every decision here was argued out before any code existed, in a planning record that is **deliberately
not distributed**: it quotes a third-party codebase that is not ours to publish, and it contains a copy
of a **`Proprietary`** upstream plugin that was read for lessons and must never be redistributed. That
record lives under `.scratch/` on the machine this was built on, and `.gitignore` keeps it there.

So if you are reading a clone, the surviving record is:

- **`git log`.** Commit messages here are long on purpose. They state what changed and *why*, including
  what was rejected. Read the message that introduced a line before deciding the line is wrong.
- **The comments.** They explain *why*, not what. The scripts are full of choices that look arbitrary
  until you know what they were chosen over.

Neither is a substitute for the full record, so where a decision looks strange and the reason is not
written down, treat it as load-bearing until proven otherwise and ask rather than assume.

## Conventions

- Comments explain *why*, not what. The scripts are dense with decisions that look arbitrary until you
  know what was rejected.
- Say **rubric**, **pass**, **run**, **finding**, **disposition**, **corroboration**, **verdict**,
  **report** — and mean them strictly. `CONTEXT.md` is the glossary; upstream calls all of these
  "review", which is the confusion this project exists downstream of.
- Say **scope mode** in full, never "mode". `run.mode` and `run.scope.mode` sit two keys apart and mean
  nothing like each other.
