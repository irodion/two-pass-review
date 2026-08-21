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

## Two constraints that are not negotiable

- **Stdlib only.** No dependencies, no network, no build step. The skill is copied into other people's
  repositories, and anything it needs installed is something that will be missing.
- **Escape first, then apply structure.** `html.escape(..., quote=True)` runs on the whole string before
  any structural regex touches it, so no code path can emit an unescaped byte.

There used to be a third: the page carried no JavaScript at all. It was removed deliberately. Escaping
is what prevents injection here, and it does that whether or not a script tag is present — the ban was
buying no safety the escaping did not already buy, while costing real functionality. Copy-to-clipboard
was the case that settled it: preserving the ban meant `user-select: all`, two gestures, and the payload
duplicated on the page as visible text, against fifteen lines of handler that reads a `data-` attribute
and never evaluates what it copies.

So: JavaScript on the page is allowed, and should stay proportionate. Prefer CSS where CSS is the
natural tool — collapsing is still `<details>`, filtering is still hidden radios and sibling selectors,
and neither wants rewriting. Reach for a handler when the alternative is contorting the page around its
absence.

## Python generates the page; JavaScript only adds behaviour to it

The obvious question, once the page runs a script at all, is why the generator is still Python. It is
not an inconsistency. **Use the runtime that is already present** — that rule just resolves differently
on a machine than it does in a browser.

The reader already has a browser, so script *in the page* costs nothing to run. Node is a runtime
somebody has to install, and macOS ships none. Python 3 is on macOS and on essentially every Linux box.
Since the skill is copied into other people's repositories and run by an agent, a rewrite to Node would
trade a dependency that is always there for one that frequently is not, and buy only the pleasure of
one language.

The sharpest version of the question is `markdown_subset.py`: 237 lines of hand-rolled markdown that
could, in principle, move to the browser and be deleted. Three reasons it does not:

- **You would hand-roll it again.** No CDN and no build step means no `marked` — the renderer would be
  inlined and written here, in a new language, for the same work.
- **Escaping would move into JavaScript**, where it is easier to get wrong. The Python path is correct
  *by construction* — escape the whole string, then apply structure — and that property is worth more
  than a deleted file.
- **The report would stop being a document.** It is one self-contained file that people forward by
  email. Render it client-side and the HTML holds JSON instead of text, so everything that reads the
  file without executing script sees nothing: mail previews, `grep`, text extraction, an agent reading
  it off disk. A report should be a document, not an application.

That last one is the load-bearing reason. So: **Python renders; JavaScript adds behaviour to what
Python rendered.** JavaScript does not become the rendering engine.

Revisit this if the page ever grows real interactivity — live search, sorting, thousands of findings —
because then the balance genuinely does shift. It has not yet.

## The rubrics are forked text; the contracts are not

`references/security.md` and `references/code-quality.md` each hold two things, split by the
`# Output contract` divider. Above it is the rubric, forked from Cursor's `thermos` plugin (MIT); the
authorised edits are enumerated and complete — see `.agents/skills/two-pass-review/NOTICE.md`. **The
rubric half is frozen: what the passes look at, weight, or consult is not ours to change there.**
Finding yourself improving a rubric means the change has left this repo's remit.

Below the divider is ours entirely, and it carries more than field tables: recording standards, and
procedure that makes a demand the rubric already states executable by a weaker model. The caller sweep
is the worked example — "tracing through possible side effects" is a rubric demand that a small model
does not act on until it is a numbered procedure, and making it one moved cross-file detection from
zero to complete on a seeded diff without adding a false positive. The line to hold: **the contract may
operationalize the rubric's own demands, never add a subject, a weighting, or a consultation the rubric
does not already require.** A contract procedure you cannot trace back to a sentence in the rubric half
does not belong in this repository.

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

- **`python 3.9` and `python 3.13`** — every script compiles on both, and on the modern one both imports
  *and runs* with `DeprecationWarning` and `SyntaxWarning` fatal: `scope.py` over a real revision range,
  `collect_docs.py` over that range's diff, `validate.py` and `render.py` down their refusal paths. Importing alone was not enough — a deprecation
  inside a `main()` is invisible to it, which is how `datetime.utcnow()` would have got through.
- **`constraints`** — `.github/checks.py`: every import is stdlib; `page.py`'s `SCRIPT` constant parses as
  JavaScript; `markdown_subset` refuses `javascript:`, `data:` and `vbscript:` when actually run on them;
  the committed `.claude/skills/` symlink is relative and resolves; every relative link in the docs points
  at a file a clone has.

  The `SCRIPT` check exists because the page's one script lives inside a Python string, where neither
  `py_compile` nor the 3.9 and 3.13 jobs can see it — a typo would ship a page that renders perfectly and
  a button that silently does nothing. It runs `node --check`, which parses without executing. Read it as
  narrowly as it is written: it catches a typo, not a mistake. Misspell `data-copy` or get the selector
  wrong and it passes while the button stays dead. It skips, loudly, where `node` is absent.

  The sanitiser check is the one that matters most and it survives the no-JavaScript rule's removal
  intact — arguably it matters more now. A pass quotes the code under review, so a hostile repository can
  get a string of its choosing into `body_md`, and the `href` allowlist is what stops that string
  becoming a `javascript:` link. That was never the same thing as the page carrying no script of its own.
- **`readme install`** — `.github/replay-readme-install.sh` extracts the `sh` blocks from `README.md` and
  runs them. It does not keep its own copy of the commands, because a copy would have passed every time
  the real ones were broken, which by then was three commands across two reviews.

None of this is a test corpus: there are no fixtures and no expected output, only invariants. And none of
it substitutes for (1) and (2). CI cannot tell you the report is wrong — it can only tell you the scripts
still start.

## Landing a change

`main` takes no direct pushes and no merge commits. Everything arrives as a pull request, rebased, and
**Rebase and merge** is the only button GitHub offers here — squash and merge commits are both turned off.
Both halves are enforced by a ruleset, so this is not a convention you can quietly decline; a merge commit
is rejected at push time with *"This branch must not contain merge commits."*

The history is therefore linear, and it is meant to stay readable commit by commit. That is not decoration
here: `git log` is one of the two surviving records of why anything is the way it is, which is the subject
of the next section. Squashing a branch into one commit would throw away the argument and keep only the
result.

So rebase onto `main` rather than merging it in, and keep each commit a change you would want someone to
read on its own.

Two things worth knowing. GitHub's **Rebase and merge** is not a true fast-forward — it rewrites committer
information and mints new SHAs even when the branch is already current, so the commits on `main` are not
byte-identical to the ones you pushed. And required approvals are set to **0**, not because review does not
matter but because GitHub forbids approving your own pull request: on a repository with one maintainer, any
other number blocks every change. Raise it the moment there is a second person who can review.

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
  **report**, **rule derivation**, **rule suggestion** — and mean them strictly. `CONTEXT.md` is the
  glossary. Upstream calls everything before the last two "review", which is the confusion this
  project exists downstream of; **rule derivation** and **rule suggestion** are this fork's own.
- Say **scope mode** in full, never "mode". `run.mode` and `run.scope.mode` sit two keys apart and mean
  nothing like each other.
