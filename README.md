# Atomic Review

An unusually strict two-pass code review that ends in a page you read, instead of eight thousand words
scrolling past in a terminal.

Two rubrics — security and correctness, then code quality — review one pinned diff. Each emits its
findings as validated JSON. The two are merged into a single list ordered by whether a finding blocks the
change, and rendered as one self-contained HTML file you open in your browser.

A fork of Cursor's `thermos` plugin. See [`NOTICE.md`](.agents/skills/atomic-review/NOTICE.md) for what
was forked, what changed, and what was read but not copied.

## What it is

```
.agents/skills/atomic-review/
├── SKILL.md          the orchestrator: scope → two passes → merge → render
├── references/       the two forked rubrics
└── scripts/          scope.py, validate.py, render.py — stdlib python3, no dependencies
```

No dependencies, no network, no build step. The report is one file with no JavaScript in it, which is why
it works over `file://` and still works after you email it to someone.

## Installing it somewhere else

There is no installer, because there is nothing an installer would do that these two commands do not.

**Into a project** — the skill travels with the repository, and everyone who clones it has the review:

```sh
cp -R /path/to/atomic-review/.agents/skills/atomic-review <your-repo>/.agents/skills/
ln -s ../../.agents/skills/atomic-review <your-repo>/.claude/skills/atomic-review
```

One real directory and one relative symlink. Cursor and Codex read `.agents/skills/`; Claude Code reads
`.claude/skills/` and follows symlinks, de-duplicating by target. Commit both.

**For yourself, everywhere** — one line per agent you use:

```sh
ln -s /path/to/atomic-review/.agents/skills/atomic-review ~/.claude/skills/atomic-review
ln -s /path/to/atomic-review/.agents/skills/atomic-review ~/.cursor/skills/atomic-review
```

A user-level install changes one documented thing: the re-render command becomes
`python3 ~/.claude/skills/atomic-review/scripts/render.py <findings.json>` rather than the repo-relative
path, because the skill no longer lives inside the repository being reviewed.

## Using it

`/atomic-review` in Claude Code and Cursor, `$atomic-review` in Codex. One string, three sigils.

Say what you want reviewed in your own words — a pull request, a commit, this branch against `main`, or
whatever is sitting uncommitted. There are no flags. If what you said does not pin down a range, you will
be asked rather than guessed at.

You get back two things: the verdict, and the path to the report.

## Reading the report

Findings are ordered by **disposition** — blocking first, then follow-ups, then notes — because that is
the only question the two rubrics both answer natively. Within a disposition they are ordered by how
loudly they shout, derived from severity for security findings and from category for quality ones.

- The **verdict** is derived from the list, never written by hand, so it cannot contradict what is beneath
  it. The validator enforces that.
- **Findings that corroborate each other** sit together with a banner, rather than being merged. Two
  passes reaching one defect from two directions is evidence, and collapsing it would throw that away.
- **Cross-references are live links.** When a pass writes "same root cause as sec-2", that is a link.
- **Locations are inert text**, on purpose. The page cannot know which editor you use, an editor link
  needs absolute paths and would break the moment you forward the report, and some cited paths are files a
  remedy is proposing and nothing has written yet. Selecting a chip selects the whole path.
- Filters for pass and for blocking-only are pure CSS. The page has no JavaScript at all.

## On Codex

Codex cannot open a browser — its sandbox denies both the platform opener and binding a port. The printed
path is the mechanism there, not a fallback.
