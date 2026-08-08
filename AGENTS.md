# Working on this repository

This repo *is* the `atomic-review` skill. The shipping artifact is
`.agents/skills/atomic-review/`; everything at the root is scaffolding. `README.md` describes the skill
for the people who use it — this file is for whoever is changing it.

## Test with `/usr/bin/python3`, not `python3`

The scripts target **Python 3.9 syntax**, because macOS `/usr/bin/python3` is 3.9.6 and that is the
interpreter a stock Mac will run them with. The `python3` on your `PATH` is almost certainly much newer.

```sh
/usr/bin/python3 .agents/skills/atomic-review/scripts/render.py <findings.json>
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
authorised edits are enumerated and complete — see `.agents/skills/atomic-review/NOTICE.md`. **What the
passes look at, weight, or consult is not ours to change.** Finding yourself improving a rubric means the
change has left this repo's remit.

## How to check a change

There is no CI and no test runner, deliberately. Verification is two things:

1. **Render the fixture and look at it.** `.scratch/review-cockpit/prototype/fixture/findings.json` is a
   real 36-finding review. It exercises the whole markdown subset, three corroboration pairs, and every
   renderer requirement. For a change that should not alter output, keep a copy of the page first and
   `diff` — byte-identical is a stronger guarantee than any assertion you would have written.
2. **Run the skill on this repo.** `/atomic-review` on the current branch. It has found real defects in
   its own implementation more than once, including two that no fixture could reach.

Negative checks for `validate.py` are written ad-hoc and thrown away. Do the same — a committed test
corpus is out of scope.

## `.scratch/` is gitignored, and must stay that way

It holds the planning provenance: the design map, the tickets, and `spec.md`, which decided every
question this implementation answers and says *why* for the load-bearing ones. Read it before changing a
design decision; the reasoning is usually load-bearing and usually not obvious.

It also holds `upstream/codex-security/`, which is licensed **`Proprietary`**. It was read for lessons.
**Nothing may be copied from it, and it must never be committed.**

## Conventions

- Comments explain *why*, not what. The scripts are dense with decisions that look arbitrary until you
  know what was rejected.
- Say **rubric**, **pass**, **run**, **finding**, **disposition**, **corroboration**, **verdict**,
  **report** — and mean them strictly. `CONTEXT.md` is the glossary; upstream calls all of these
  "review", which is the confusion this project exists downstream of.
- Say **scope mode** in full, never "mode". `run.mode` and `run.scope.mode` sit two keys apart and mean
  nothing like each other.
