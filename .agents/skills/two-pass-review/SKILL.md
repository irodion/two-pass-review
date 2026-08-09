---
name: two-pass-review
description: A single-file HTML report of an unusually strict two-pass code review
  — security and correctness, then code quality — that you open in your browser,
  with findings ordered by whether they block the change.
disable-model-invocation: true
---

# Two-Pass Review

Two rubrics review one pinned diff, each emits findings as validated JSON, the two are merged into one
list, and a script renders a single self-contained HTML file the user opens.

A fork of Cursor's `thermos` plugin — see [`NOTICE.md`](NOTICE.md).

**Scripts live beside this file.** Invoke them as `python3 <skill-dir>/scripts/<name>.py`, where
`<skill-dir>` is the directory holding this `SKILL.md`. In Claude Code that is `${CLAUDE_SKILL_DIR}`.
Never show that variable to the user — [re-rendering](#re-rendering) has one literal path that works
everywhere.

## 1. Resolve the scope

`scripts/scope.py` pins the diff both passes will read. **It never guesses a base**, and neither do you:
where the request does not determine the range, ask the user which of these they mean.

| The user is asking about | Resolve it to |
|---|---|
| a pull request | `--mode revisions --base <merge-base of the PR> --head <PR head>` |
| one commit | `--mode revisions --base <commit>^ --head <commit>` |
| this branch against another | `--mode revisions --base $(git merge-base <other> HEAD) --head HEAD` |
| uncommitted work | `--mode local-patch --base HEAD` |

```
python3 <skill-dir>/scripts/scope.py --repo <repo> --base <rev> --mode revisions --head <rev>
```

It prints JSON holding `run_dir`, `context_diff`, `latest` and the resolved `scope`. Keep all of them.

- **Exit 3** means the diff is large. Tell the user how large and ask. If they want it, add
  `--confirm-large`. It is never split into batches: both passes must see one identical input, or
  corroboration has nothing to compare across.
- **Exit 2 or 4** ends the run here, with no report. A local patch that resolves to nothing usually means
  the work is in files git has never been told about — say so.

## 2. Run the two passes

Both rubrics run over the same pinned diff:

- security and correctness → [`references/security.md`](references/security.md)
- code quality → [`references/code-quality.md`](references/code-quality.md)

**Run them as parallel subagents where your host offers them and the user approves.** Otherwise run them
one after the other, to the same standard, and hold the second to the same depth as the first.

**Which model, and at what effort, is the user's to say.** If the request named either, spawn the passes
with it. If it did not, they inherit the session's, and you do not raise or lower that on your own
initiative — a review that silently costs several times what the user expected is its own kind of failure,
and one that silently thinks less than they had set it to is worse. Where the host offers no per-pass
model, the session's is what ran; that is not a thing to work around.

**Both passes run on the same model at the same effort.** Corroboration is two passes reaching one defect
independently, and that is evidence only while they were peers — a cheap pass agreeing with an expensive
one is not a second opinion. If the user asks for a split anyway, run it and tell them which pass got what.
Never arrive at one yourself.

Give each pass exactly four things, and let it read the repository for itself:

1. its rubric, as the whole of its instructions
2. the path to `context.diff`
3. the `run_dir` to write into
4. the command that validates its files: `python3 <skill-dir>/scripts/validate.py <its two files>`

Each pass owns its output contract; it is written into the rubric and needs no repeating here.

**Record which way they ran.** That becomes `run.mode`, `parallel` or `sequential`, and the report says so
— because two subagents each get a fresh context window, while a sequential run puts both rubrics through
one, and on a large diff the second pass reviews with a badly degraded window. **A sequential run is the
weaker run, and the reader is owed that.**

**Pin the range, not the file contents.** The passes read repository files themselves. That is deliberate:
the review that shaped this design produced findings on files outside the diff and on files that do not
exist yet, which a pass restricted to a pasted blob cannot do.

## 3. Merge

Read both validated pass files and write `<run_dir>/findings.json`:

- `schema_version` 1, `kind` `"merged"`
- `run` — your `mode` from step 2, `generated_at` as `2026-08-08T14:02:11Z`, and `scope` exactly as
  `scope.py` printed it
- `passes` — each pass envelope minus its `schema_version` and `kind`
- `findings` — every finding from both passes, unchanged apart from the corroboration links below
- `verdict` — **derived, never authored**: any finding tagged `blocking` makes it `"blocked"`, otherwise
  `"clear"`. `clear` means nothing blocks, not that nothing was found.

### Corroboration

Both passes sometimes argue the same defect from different angles. Link those, and link nothing else:

1. **Link two findings when fixing one would make the other's argument redundant. If unsure, do not link.**
   Corroboration raises a finding's rank, so a wrong link promotes something, while a missed one merely
   leaves two cards apart. The doubt resolves toward not linking.
2. **Link only within one disposition.** If a `note` and a `blocking` finding really argued one defect, a
   pass mis-tagged it, and quietly promoting it would hide that.
3. **Write `corroborated_by` on both members.** The validator requires the link to be mutual.

Judge this by reading, not by matching strings — the two passes routinely describe one defect with no
shared phrasing. Findings that **disagree** get no link at all: both render, both argue, and that is the
information.

## 4. Render and deliver

```
python3 <skill-dir>/scripts/validate.py <run_dir>/findings.json
python3 <skill-dir>/scripts/render.py <run_dir>/findings.json --latest <latest>
```

`render.py` validates before it writes and refuses to render an invalid artifact. It opens the report in
the user's browser where it can, and always prints the path.

**Then tell the user two things: the verdict, and where the report is.** Nothing else. Do not summarise
the findings in the transcript — reproducing the review in prose is the thing this skill exists to
replace, and the reader is one click away from the real thing.

## When something fails

You are the error handler. There is no status field, no retry protocol and no degraded mode to build.

- A pass that dies mid-argument keeps every finding it already wrote, because emission is line-oriented.
  What it loses is its envelope.
- Invalid output goes back to that pass to repair from its own artifacts, twice at most. A run that cannot
  produce a valid artifact ends with a written explanation, never a half-rendered page.
- **If one pass never produced an envelope, merge the one that did.** `passes` holds one entry, the report
  renders one pass, and the absence is visible on the page with nothing added to the schema. Say which
  pass died, and offer to re-run just that one — each pass is independently re-runnable against the same
  pinned `context.diff`.

## Re-rendering

The user can rebuild the page from the artifact at any time, with one command that is the same in every
agent:

```
python3 .agents/skills/two-pass-review/scripts/render.py <path-to-findings.json>
```

## What this skill does not do

Read this before adding anything to it.

There is **no triage**, no checkboxes and no decisions handed back — v1 is read-only. There is **no live
link** from the page to a running agent. There is **no repository-wide mode**: the security rubric's
"only code being added or modified" clause is its main defence against over-reporting and it means nothing
without a diff. There is **no pass selector** — a one-pass run is upstream's two separate skills, which
this fork collapsed on purpose. There is **no configuration**.

**The rubrics' review behaviour is not yours to edit.** What they look at, what they weight, what they
consult — including upstream's PR-discussion step — stays exactly as written. Finding yourself drafting a
rubric edit means you have left this skill.
