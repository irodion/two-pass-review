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

No dependencies, no network, no build step. The report is one self-contained file — no sibling assets, no
embedded JSON, nothing fetched — which is why it works over `file://` and still works after you email it
to someone. Its whole script is a clipboard handler behind the copy buttons, a class toggle behind
`Mark dealt with`, and the counting that keeps the headings honest as you filter.

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

**For yourself, everywhere** — two links cover all three agents:

```sh
mkdir -p ~/.agents/skills ~/.claude/skills
ln -s /path/to/two-pass-review/.agents/skills/two-pass-review ~/.agents/skills/two-pass-review
ln -s /path/to/two-pass-review/.agents/skills/two-pass-review ~/.claude/skills/two-pass-review
```

`~/.agents/skills/` is the user-level location Cursor and Codex both read; `~/.claude/skills/` is Claude
Code's. Codex also reads `~/.codex/skills/`, and Cursor reads it for compatibility, but Codex's own source
marks that path deprecated and kept only for backward compatibility — so it is not what a new install
should write to.

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

Model and effort work the same way: name either in the same sentence and both passes run there, or say
nothing and they inherit whatever your session is set to. Nothing here will quietly move you onto a more
expensive model than the one you chose.

Both passes run on the same tier unless you ask for otherwise, because two passes reaching the same defect
is only evidence while they were peers. You can ask for otherwise — run security on the stronger model and
quality on the cheaper one, say — and it will do it and record what each pass got, but nothing will arrive
at that split on its own, and the report tells its reader when the passes were unequal.

Effort is worth a moment's thought before you invoke rather than after. The passes argue their findings and
the merge weighs which of them corroborate each other; none of that is a lookup, and a run at `low` is a
cheaper review in the sense that matters. The skill deliberately does not pin a level of its own, because
a pin that raised a low session would equally drag down one you had set high on purpose.

You get back two things: the verdict, and the path to the report.

## Reading the report

Findings are ordered by **disposition** — blocking first, then follow-ups, then notes — because that is
the only question the two rubrics both answer natively. Within a disposition they are ordered by how
loudly they shout, derived from severity for security findings and from category for quality ones.

- The **verdict** is derived from the list, never written by hand, so it cannot contradict what is beneath
  it. The validator enforces that.
- **Findings that corroborate each other** sit together with a banner, rather than being merged. Two
  passes reaching one defect from two directions is evidence, and collapsing it would throw that away.
- **A contested finding is still a finding.** Before the merge, a falsification check that sees only the
  diff — none of the passes' context — tries to disprove each finding, and one whose key claim the diff
  appears to contradict is marked *contested*: it keeps its place, its disposition and its force — a
  contested blocking finding still blocks — and the check's counter-evidence renders on the card and
  travels in both copy buttons, so whoever verifies receives the claim and the objection together and
  adjudicates from the code. The check never removes anything, because it is measurably wrong about
  true findings often enough that its word is a lead, not a ruling. A claim it cannot check from the
  diff passes unchallenged. A run where the check could not run — or where it ran and its reply could
  not be read — says so in the masthead, because a report where nothing disputed the findings should
  not read like one where something tried and everything held. (Reports from older runs may instead
  show a *withdrawn* section; that was this check's earlier, harsher form.)
- **What the run was pointed at is in the sidebar**, under `Run`: repository, scope mode, the two object
  ids, the diff size, and the model and effort when the run chose them. They are what was asked for
  rather than a measurement — nothing in the pipeline can confirm which model answered — and the page
  says so. Anything that reduces what the report is worth goes the other way, into the masthead above the
  findings: untracked files that were never diffed, a sequential run, or two passes asked for different
  tiers, because corroboration counts for less between passes that were not peers.
- **Cross-references are live links.** When a pass writes "same root cause as sec-2", that is a link.
- **Locations are inert text**, on purpose. The page cannot know which editor you use, an editor link
  needs absolute paths and would break the moment you forward the report, and some cited paths are files a
  remedy is proposing and nothing has written yet. Selecting a chip selects the whole path.
- **Every finding has two copy buttons.** `Copy` puts it on the clipboard as markdown — title, pass,
  disposition, severity, locations and body, plus the confidence rationale when there is one. `Copy for
  agent` appends an instruction asking an agent to verify the finding against the real code and propose
  options. A corroborated finding names its partner rather than pasting it; the partner has its own button.
- **Every finding can be marked dealt with.** `✓ Mark dealt with` folds the card to its title, strikes
  the title through, and strikes the sidebar entry with it, so the nav stops advertising findings you
  have already dealt with and the page compacts as you read. Once anything is marked, the sidebar offers
  `Hide N dismissed`, which takes those findings out of the flow entirely, and a disposition whose
  findings are all dismissed takes its heading with it. Undo is where the card is. A heading counts what
  is left beside what the passes found — `Blocking · 3 of 7` — so your progress never overwrites the
  run's own number. It is a mark on your reading, not on the finding: it does not touch the verdict, and
  none of it survives a reload — a report is a snapshot of one diff, and a stale mark against a
  regenerated one would mislead.
- **A `Documentation` section may follow the findings.** An advisory docs check reads the diff against
  the agent-facing documents — `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, a README, `CONTRIBUTING.md` and
  `GUIDE.md` at the root, plus nested `AGENTS.md`, `CLAUDE.md` and READMEs on the changed paths — and
  quotes any claim the diff
  explicitly makes false or coverage it now owes. It is not a third pass and it never blocks: the notes
  carry no ids and no dispositions, the section names exactly which documents were read and which the
  collector refused, and it catches explicit contradiction only — drift a change merely implies is
  beyond it, and the section says so.
- **The report may end with a self-check** — up to four questions, each about one specific standing
  finding it names by id, with its answer folded underneath and linked to the findings it rests on. It is a
  nudge to engage before acting on the verdict, not a gate: the answers live on the page, nothing is
  scored or recorded, and skipping them costs nothing. A thin report carries none — a near-empty review
  earns no quiz.
- **Filters for pass, severity, blocking-only and dismissal** are hidden radios and sibling selectors:
  the state is a radio and CSS does the hiding. Severity filters the security findings only — a quality
  finding is rated by category and a security note by nothing at all, so both stay visible, and the
  sidebar says so while the filter is on. The script counts: a heading above a filtered list has to agree
  with what is under it. That, the clipboard handler and the dismissal toggle are the whole of it —
  none of them parses, renders or evaluates anything a pass wrote, and everything a pass wrote is escaped
  before it reaches the page, script or no script.

## On Codex

Codex cannot open a browser — its sandbox denies both the platform opener and binding a port. The printed
path is the mechanism there, not a fallback.

## On WSL

WSL reports itself as Linux but usually has no Linux browser to open, so the report is handed to the
Windows default browser through interop: `wslview` if `wslu` is installed, otherwise PowerShell or
Explorer, with the path translated by `wslpath`. Where interop is turned off and a Linux browser exists,
the ordinary Linux opener still runs; where neither is reachable, the printed path is the report, as on
Codex.

Opening it by hand is the one place this bites. `explorer.exe` and `cmd.exe` inherit the working
directory, a Linux one reaches Windows as `\\wsl.localhost\…`, and both refuse it with *"UNC paths are not
supported"* — so run them from a directory on a Windows drive, or the failure looks like a bad path when
it is a bad `cwd`.

## Working on it

[`AGENTS.md`](AGENTS.md) is the guide for changing this repository — the Python floor, the two
constraints that are not negotiable, where the line between Python and JavaScript sits, and how to check
a change when there is no CI by design.
`CLAUDE.md` is a symlink to it, so a coding agent picks it up whichever name it looks for.

[`CONTEXT.md`](CONTEXT.md) is the glossary. Upstream uses one word — "review" — for the rubric, the
execution, the artifact and the report, and every decision downstream of that needs them separated.

[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) applies to everyone here, maintainers included.
