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
`scope.json` records no `checkout`, run it again with `--checkout` and the user's repository. The
subagent is handed the run dir's `findings.json`, the pinned `context.diff`, and the reviewed repository
to read for itself. It is not starved the way the falsifier is, because starvation there is the
mechanism and here would be a handicap: this stage judges nothing, and a rule worth adopting has to
match the repository's real languages, its APIs, and whatever linter configuration already exists. Model
and effort are the user's to name in the asking; otherwise it inherits the session's, the same rule the
passes run under. Where the host offers no fresh subagent, do the work in your own window, following the
same prompt — there is no starvation requirement to protect.

The prompt asks for rules that catch a *recurrence* of a finding's defect class, anchored in code the
repository contains — a semgrep rule by preference, or a config change for a tool the repository already
runs — each headed with the finding ids it derives from, a contested source saying so with one line of
the contest's substance, and every finding with no mechanical form named on a line of its own, because
silence about a finding is not an option. It replies with the markdown body of the rules file and
nothing else.

Prepend the header and write `<run_dir>/rules.md`. The header is yours, never the subagent's — it
is what makes the file self-describing, so it must not depend on the judgment party:

```markdown
# Rule suggestions — derived, not enforced

- **Run:** `<run_dir>` (`findings.json` beside this file)
- **Scope:** `<repo>` — `<base>..<head>`, or local patch at `<base>`
- **Generated:** <UTC, the same convention as `generated_at`>
- **Derived from:** sec-1, sec-3, qa-2 (qa-2 contested)
- **Not derivable:** qa-1 — naming judgment, no mechanical form

Suggestions only: nothing here is installed, run, or committed by the skill, and a rule is only as
right as the finding it came from. Verify against the code before adopting.
```

Each suggestion below the header is one `##` section titled with a short imperative, `(from
sec-1, sec-3)` at its end, a paragraph stating the defect class and what the rule will and will not
catch, and one fenced block holding the rule. **Every finding id in the artifact appears exactly
once across `Derived from` and `Not derivable`** — that is the coverage claim, and it is checkable
by eye.

If no usable markdown comes back, send it back once to repair; a second failure ends this with a
written explanation and no file — never a half file, and never a mark anywhere in the artifact. An
absent `rules.md` is its own record.

**Then tell the user two things: where the file is, and its coverage** — which findings yielded
suggestions, which were named not derivable. The suggestions live in the file, not the transcript;
do not restate them.
