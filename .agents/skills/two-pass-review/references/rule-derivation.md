# Deriving rule suggestions

Rule derivation is not a pass and not a check: it carries no rubric, emits no findings, and reads the
finding list without touching it. Its product is one new sibling file in the run dir; the artifact and
the report are never reopened, and re-rendering afterwards produces the identical page.

**Use the run the user names.** If they name none, ask which one, listing the newest few run
directories under `.two-pass-review/` at the top of their checkout — never pick one yourself. That
directory is `<run_dir>` below.

Fill the rule-derivation prompt from the skill as it stands now, never from a copy saved with the run:

```
python3 <skill-dir>/scripts/prompts.py --rules <run_dir>
```

It writes `<run_dir>/prompt.rules.md` from [`prompts.md`](prompts.md) and prints
one line; start one fresh subagent with that line as its whole prompt. If it refuses because the run's
`scope.json` records no `checkout`, run it again with `--checkout` and the user's repository. Before it
writes the prompt it checks out the run's review tree again — the reviewed head, or for a local patch
the commit `scope.py` made of the working tree when it pinned the run — so the subagent can test rules
on the code that was reviewed. If it refuses because that code cannot be rebuilt, tell the user its
message and stop.
The subagent is handed the run dir's `findings.json`, the pinned `context.diff`, the review tree, and
the user's repository to read for itself. It is not starved the way the falsifier is, because starvation there is the
mechanism and here would be a handicap: this stage judges nothing, and a rule worth adopting has to
match the repository's real languages, its APIs, and whatever linter configuration already exists. Model
and effort are the user's to name in the asking; otherwise it inherits the session's, the same rule the
passes run under. Where the host offers no fresh subagent, do the work in your own window, following the
same prompt — there is no starvation requirement to protect.

The prompt asks for rules that catch a *recurrence* of a finding's defect class, anchored in code the
repository contains — a semgrep rule by preference, or a config change for a tool the repository already
runs — each headed with the finding ids it derives from, a contested source saying so with one line of
the contest's substance, and every finding with no mechanical form named on a line of its own, because
silence about a finding is not an option. It also asks for every rule to be tested, because a rule that
does not parse, or does not flag the code it came from, looks as finished as one that works. The
subagent tests each rule in the review tree and in the checkout as it is now, and edits neither. Rule 4
of the rule-derivation block in [`prompts.md`](prompts.md), and
[`scripts/try_rule.py`](../scripts/try_rule.py), the one command it runs, are the whole of the testing
policy, with its reasons — which tools, how they are started, and what a test has to show — and neither
is restated here, so that nothing here can drift from them: doing the work in your own window, follow
that rule word for word. It replies with the markdown body of the rules file and nothing else.

Prepend the header and write `<run_dir>/rules.md`. The header is yours, never the subagent's — it
is what makes the file self-describing, so it must not depend on the judgment party:

```markdown
# Rule suggestions — derived, not enforced

- **Run:** `<run_dir>` (`findings.json` beside this file)
- **Scope:** `<repo>` — `<base>..<head>`, or local patch at `<base>`
- **Generated:** <UTC, the same convention as `generated_at`>
- **Derived from:** sec-1, sec-3, qa-2 (qa-2 contested)
- **Not derivable:** qa-1 — naming judgment, no mechanical form

Suggestions only: nothing here is installed, enforced, or committed by the skill, which runs a rule
only to test it, and a rule is only as right as the finding it came from. Verify against the code
before adopting.
```

Each suggestion below the header is one `##` section titled with a short imperative, `(from
sec-1, sec-3)` at its end, a paragraph stating the defect class and what the rule will and will not
catch, a `Tested:` line naming what it flagged in the reviewed code and in the checkout now — or
`Not tested:` and why — and one fenced block holding the rule. **Every finding id in the artifact
appears exactly once across `Derived from` and `Not derivable`** — that is the coverage claim, and it
is checkable by eye.

If no usable markdown comes back, send it back once to repair; a second failure ends this with a
written explanation and no file — never a half file, and never a mark anywhere in the artifact. An
absent `rules.md` is its own record.

**Release the review tree when the subagent is done**, after any repair, and whatever came of it — a
file, a second failure, a subagent that died:

```
python3 <skill-dir>/scripts/scope.py --release <run_dir>
```

**Then tell the user where the file is, its coverage, and any warning** — which findings yielded
suggestions, which were named not derivable, which suggestions say `Not tested:`, and anything
`prompts.py` or `--release` printed on stderr. The suggestions live in the file, not the transcript;
do not restate them.
