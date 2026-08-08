# Two-Pass Review

An unusually strict code review that ends in a page you read, instead of eight thousand words scrolling
past in a terminal.

Two rubrics — security and correctness, then code quality — review one pinned diff. Each emits its
findings as validated JSON. The two are merged into a single list ordered by whether a finding blocks the
change, and rendered as one self-contained HTML file you open in your browser.

A fork of Cursor's `thermos` plugin. See [`NOTICE.md`](.agents/skills/two-pass-review/NOTICE.md) for what
was forked, what changed, and what was read but not copied.

## What it is

Everything lives under `.agents/skills/two-pass-review/`. `SKILL.md` is the orchestrator; `references/`
holds the two forked rubrics; `scripts/` holds the pipeline.

The pipeline is three steps, and each one refuses to paper over the step before it. **Scope** pins one
diff to disk, so both passes read an identical input and corroboration has something to compare across.
The **validator** stands between the passes and the page: it checks the rules that would let the artifact
lie — chiefly that the verdict agrees with the list beneath it — and nothing invalid is ever rendered.
The **renderer** turns the merged artifact into the page, and calls the validator itself rather than
trusting whoever invoked it.

No dependencies, no network, no build step. The report is one file with no JavaScript in it, which is why
it works over `file://` and still works after you email it to someone.

## Installing it somewhere else

There is no installer, because there is nothing an installer would do that these commands do not. Both
recipes below copy from a clone, which is what `/path/to/two-pass-review` means:

```sh
git clone https://github.com/irodion/two-pass-review
```

**Into a project** — the skill travels with the repository, and everyone who clones it has the review:

```sh
mkdir -p <your-repo>/.agents/skills <your-repo>/.claude/skills
cp -R /path/to/two-pass-review/.agents/skills/two-pass-review <your-repo>/.agents/skills/
ln -s ../../.agents/skills/two-pass-review <your-repo>/.claude/skills/two-pass-review
```

One real directory and one relative symlink. Cursor and Codex read `.agents/skills/`; Claude Code reads
`.claude/skills/` and follows symlinks, de-duplicating by target. Commit both.

**The committed symlink has to stay relative.** An absolute one works on the machine that wrote it and is
broken for everyone who clones afterwards, and since git stores the path as the file's contents, nothing
about the repository looks wrong in the meantime. The user-level links below are absolute instead, which is
not an inconsistency: nothing commits them, and they point outside any repository.

**For yourself, everywhere** — one line per agent you use:

```sh
mkdir -p ~/.claude/skills ~/.cursor/skills
ln -s /path/to/two-pass-review/.agents/skills/two-pass-review ~/.claude/skills/two-pass-review
ln -s /path/to/two-pass-review/.agents/skills/two-pass-review ~/.cursor/skills/two-pass-review
```

A user-level install changes one documented thing: the re-render command becomes
`python3 ~/.claude/skills/two-pass-review/scripts/render.py <findings.json>` rather than the repo-relative
path, because the skill no longer lives inside the repository being reviewed.

**Handing it to your agent** — if you would rather ask than type, paste this:

> Install the skill from `https://github.com/irodion/two-pass-review` into this repository, following the
> "Into a project" commands in its README. The symlink must stay relative. Do not run the review
> afterwards.

That last sentence is worth keeping. The skill sets `disable-model-invocation: true`, so nothing invokes it
but you — and an agent that has just installed something has an obvious urge to prove it works, which on a
real branch is a full two-pass review you did not ask for.

## Using it

`/two-pass-review` in Claude Code and Cursor, `$two-pass-review` in Codex. One string, three sigils.

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

## Working on it

[`AGENTS.md`](AGENTS.md) is the guide for changing this repository — the Python floor, the three
constraints that are not negotiable, and how to check a change when there is no CI by design.
`CLAUDE.md` is a symlink to it, so a coding agent picks it up whichever name it looks for.

[`CONTEXT.md`](CONTEXT.md) is the glossary. Upstream uses one word — "review" — for the rubric, the
execution, the artifact and the report, and every decision downstream of that needs them separated.

[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) applies to everyone here, maintainers included.
