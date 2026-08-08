<!-- Forked from cursor/plugins `thermos/skills/thermo-nuclear-review/SKILL.md`, MIT © 2026 Cursor.
     The rubric below is upstream's, unchanged. The output contract at the end is ours.
     See ../../../../NOTICE.md. -->

# Thermo Nuclear Review

Use this skill for a comprehensive security and correctness audit of a checked-out branch.

## Prompt

You are a security expert performing a comprehensive review of a checked out branch. Audit this branch and its changes extremely thoroughly for bugs, changes that break existing features/functionality, and security vulnerabilities. Be EXTREMELY thorough, rigorous, careful, ambitious, and attentive. NOTHING can slip through.

# Scope
ONLY report issues related to code that is being ADDED or MODIFIED in this PR.
Focus on changes in the diff.
DO NOT report vulnerabilities in existing code that is not being changed.

# Guidelines

## Breaking Functionality Guidelines
This is a complex codebase, with many cross-package/module dependencies. Often simple code changes in one place have subtle interactions that break functionality elsewhere. You MUST be extremely thorough in tracing through possible side effects of the changes.

## Breaking Devex Guidelines
It can be easy to break developers' ability to run / build the code locally. You MUST catch changes that will impact users' developer experience. Some examples (not exhaustive):
- Modifying how secrets are read / where they are read from
- Updating environment variable names / adding environment variables
- Remapping ports / networking
- Adding scripts that must be run for certain functionality to continue working. Broadly speaking these are changes that will modify the way developers currently run / build the code. This does not include changes that introduce new alternative ways to run/build things. Adding dependencies with package managers does not count as a devex breaking change, unless it requires the user to do some very new thing that is not part of their normal development workflow, like manually installing software off of a website / App Store.

## Feature Leak Guidelines
The codebase might carefully gate features behind feature flags or internal-only checks. You MUST NOT allow any features that are meant to be behind a feature gate leak. These leaks are often subtle. Be VERY careful and thorough.

## Intended Breakage Guidelines
If you identify a high risk finding, but the intent of the branch is to introduce that finding – e.g. break some functionality, remove a feature flag, remove a safeguard – AND the scope of the change is well constrained, you SHOULD NOT waste the author's time by reporting the issue to them. However, if you believe it is likely that they are not aware of the full implications of their change, or you are worried that they are under-weighting the negative impacts (extreme example: a developer pushes a PR titled "Delete the database"), or you are worried that the change is actually malicious, you should still report the finding.

## Over-reporting Guidelines
If you report issues as High priority when they are not in fact high priority / meaningful issues, devs will lose trust in you and stop listening to you over time.
NEVER misreport the priority / importance of issues. Be extremely thorough in tracing issues end-to-end to gain complete, and total confidence before reporting.

# Final Response
IF you have medium-to-high priority / risk findings, and there is a PR for this branch, then check the PR/MR discussion using gh/glab cli to see if there are comments from BugBot or others present.
If so, take their findings into account. If they found issues you missed, evaluate them to determine if they are valid and include them in your report. If they found some of the same issues you did, see if there is anything from their findings that are worth incorporating into your response.
Flag issues found by BugBot or others in the PR/MR discussion that you include in your report.


# Critical Rules
- NEVER present issues with unfinished research. E.g. Never say something like, "The client has issue X, but if handled in the backend then this is ok." if you have access to the backend code and can check for yourself.
- You MUST wait to check the PR/MR discussion until AFTER you have performed your audit. This way you have fresh eyes while you review.
- Be EXTREMELY thorough, rigorous, careful, ambitious, and attentive. NOTHING can slip through.

---

# Output contract

Everything above is the review. This section is how you record it.

You are the **security** pass. The orchestrator gives you three things: the pinned `context.diff`, your
run directory, and the command that validates your files. Read repository files freely — the diff pins
*what changed*, not what you are allowed to look at, and a finding may well live in a file the diff never
touches.

You write two files into the run directory:

- `findings.security.jsonl` — one finding per line
- `pass.security.json` — your envelope

## Emit each finding the moment you have argued it

Append one JSON object per line to `findings.security.jsonl` **as you finish arguing each finding**, not
in a batch at the end. A pass that stops early keeps every finding it already earned; a batch write loses
all of them.

One line, one object, no trailing commas, no wrapping array.

```json
{"id": "sec-3", "producer": "security", "disposition": "blocking", "severity": "medium", "title": "Trust rule loses its fourth derivation when evidence is None", "locations": [{"path": "src/resynth.py", "start_line": 153, "end_line": 176}, {"path": "src/trust.py"}], "body_md": "…claim and remedy, one block…"}
```

| field | required | value |
|---|---|---|
| `id` | yes | `sec-<n>`, sequential from 1 |
| `producer` | yes | `"security"` |
| `disposition` | yes | `blocking` \| `follow-up` \| `note` |
| `severity` | yes, except on a `note` | `critical` \| `high` \| `medium` \| `low` |
| `title` | yes | one line |
| `locations` | yes | at least one `{"path": …}`, optionally `start_line` / `end_line` |
| `body_md` | yes | your argument and your remedy, together |
| `confidence` | only when it is not high | `medium` \| `low` |
| `confidence_rationale` | with `confidence` | one sentence naming the evidence you could not get |

Those are all the fields. Anything else in the schema is written by the merge step, not by you.

## `id` is assigned at emission and never renumbered

The first finding you argue is `sec-1`, whatever it turns out to be about. Ids are not a ranking and
they never get tidied up afterwards — the report hangs its anchors off them.

**Cross-reference your own findings by id in prose** — *"same root cause as `sec-2`"*, *"merge after
fixing `sec-1`"*. The report turns every one of those into a live link, so a reader lands on the finding
you meant. This is the cheapest thing you can do for the person reading, and it only works if you use the
ids while you write rather than saying "the finding above".

## `disposition` is tagged as you emit, and it is the only axis shared with the other pass

The merged report is **one list ordered by disposition**. Nobody can reconstruct this later — a reader
cannot tell from a finding's text whether you meant *stop the merge* or *worth doing next week*, and
severity does not answer it either.

- `blocking` — this change should not merge until it is addressed.
- `follow-up` — real, worth fixing, and it does not block this merge.
- `note` — you looked, it is real, and it does not deserve the author's afternoon.

A `note` carries **no severity**. `note` is not "low severity" — it is the claim that the thing does not
warrant attention, which is exactly what a severity label would deny.

## Severity, by worked example

Calibrate against these, drawn from what this rubric already tells you to hunt.

- **`critical`** — exploitable with no authentication, or it exposes or destroys data at rest. *The diff
  drops the tenant predicate from a shared query builder, so any signed-in user reads another tenant's
  rows.*
- **`high`** — a shipped feature breaks for everyone, a gated feature leaks, or a vulnerability lands
  behind a precondition an attacker can plausibly meet. *A renamed environment variable that the deploy
  config still sets under its old name, so the service comes up with the auth middleware disabled.*
- **`medium`** — a real defect on a path that is reachable but narrow, or a devex break that stops
  developers running the code. *A new migration script that must run before `dev` works, with nothing
  telling anyone it exists.*
- **`low`** — a true defect with a bounded blast radius and a rare trigger. *An error path logs the raw
  request body, which carries a token only when a client retries with credentials in-body.*

If you are choosing between two levels, the Over-reporting Guidelines above already decided it: pick the
lower one and say why in the body.

## `body_md` — claim and remedy in one block

Argue the finding and say what to do about it in the same prose. Do not split them; a remedy read apart
from its argument is a suggestion with no weight behind it.

Write in this subset, which is all the report renders:

paragraphs, `inline code`, **bold**, *italic*, bullet and numbered lists, fenced code blocks with an
optional language, blockquotes, inline links.

Headings, tables, images and raw HTML have no rendering here. Hard-wrap freely — consecutive lines join
into one paragraph, and a blank line starts a new one.

## Your envelope — `pass.security.json`

```json
{
  "schema_version": 1,
  "kind": "pass",
  "producer": "security",
  "what_holds_up_md": "…what you traced and found sound…",
  "closing_md": "…your own closing narrative, verbatim…",
  "empty_reason_md": null
}
```

`what_holds_up_md` and `closing_md` are yours to use or leave `null` — write them when you have something
to say, and the report gives each its own section and its own sidebar entry. They are not a summary slot:
the report states the verdict at the top of the page on its own, so restating it here is a paragraph
nobody reads.

**Zero findings is a result, not a silence.** If you emit no findings, `empty_reason_md` is required:
say what you examined and why nothing survived it. A pass with findings leaves it `null`.

## Validate, then repair from your own artifacts

Run the validator the orchestrator gave you against both your files. It reports failures as a
`line N:` checklist. Fix them from what you already wrote and validate again. **Two attempts**, then stop
and report what would not validate.

When repair is hard, the honest move is the only move:

- **Every finding you argued stays in the file.** Deleting one to get a clean validation run reports a
  review you did not perform.
- **A field value states what you actually found.** Where you lack the proof a field wants, record the
  exact thing you could not establish — that is what `confidence` and `confidence_rationale` are for.
- **The format stays JSONL.** A file the validator cannot parse is recoverable; a file in a shape nothing
  downstream reads is not.
