# Two-Pass Review

A forked thermo-nuclear code review that emits a validated findings JSON and renders it as a self-contained, read-only HTML report in the browser. This glossary exists because upstream uses one word — "review" — for the rubric, the execution, the artifact, and the report, and every downstream decision needs them separated.

## Language

### The review itself

**Rubric**:
A document of review criteria, forked from upstream and edited. There are exactly two: security/correctness and code-quality.
_Avoid_: reviewer, skill, prompt

**Pass**:
One execution of one rubric over one review scope. A run has two passes.
_Avoid_: review, reviewer, agent, subagent

**Producer**:
Which pass a finding came from. An attribute on a finding, never a layout axis.
_Avoid_: source, author, origin

**Run**:
One invocation of the skill: a pinned review scope, two passes, a merge, and one report.

**Mode**:
Whether a run's passes executed concurrently or one after another. Recorded because a sequential run puts both rubrics through one context window and is the weaker run.
_Avoid_: strategy, execution model

**Scope mode**:
Which family of `git` invocation resolved the review scope — a revision range, or the local working patch. Always said in full, never shortened to "mode": the two live two keys apart in the same envelope (`run.mode`, `run.scope.mode`) and mean nothing like each other.
_Avoid_: mode, scan mode, target type

### What a review produces

**Finding**:
One defect a pass is willing to argue for, with its claim and remedy inseparable.
_Avoid_: issue, item, candidate, result

**Disposition**:
Whether a finding blocks the change or can follow it. Tagged by the pass at emission, and the axis the merged list is ordered by — it is the only axis both rubrics produce natively.
_Avoid_: severity, priority, status

**Confidence**:
How sure a pass is that a finding is real — strictly separate from how much it matters, which is disposition. Stated only when it is not high, and then it costs a sentence naming the missing evidence.
_Avoid_: certainty, likelihood, probability

**Corroboration**:
The relationship between two findings from different passes that argue the same defect from different angles. Both survive and are linked; neither is collapsed into the other.
_Avoid_: duplicate, dedupe, merge

**Falsification**:
The merge-time check that tries to disprove each finding from the pinned diff alone. A filter, not a third pass: it sees only the diff and the findings — none of the passes' context — and it can only contest, never add, edit, remove, or demote. Fails open, because it must never cost a true finding.
_Avoid_: verification, reflection, third pass, veto

**Contested**:
Of a finding, disputed by the falsification check: the diff appears to contradict its key claim. An annotation, never a withdrawal — the finding keeps its disposition, blocks and corroborates exactly as tagged, and carries the check's counter-evidence on the card and in both copy payloads, so whoever verifies holds claim and counter-claim together. Schema versions 2 and 3 instead *withdrew* such findings; those artifacts still render with the old semantics.
_Avoid_: falsified, withdrawn, suspicious, untrusted, false positive

**Docs check**:
The advisory check that asks whether any agent-facing document — AGENTS.md, CLAUDE.md, a README — states something the pinned diff makes false, or omits something it now owes. Not a third pass: no rubric, no findings, and nothing it reports blocks. The documents are collected deterministically by script; one subagent reads them against the diff, for explicit conflict only. Fails toward silence.
_Avoid_: docs pass, documentation review, third pass

**Doc note**:
One conflict the docs check reports — a stale claim quoted from the document, or coverage the diff now owes. Never a finding: no id, no disposition, no corroboration, and the verdict never reads it.
_Avoid_: finding, docs finding

**Verdict**:
The single blocked-or-not judgment for a run. Derived from the findings' dispositions, never authored, so it cannot contradict the list beneath it.
_Avoid_: summary, conclusion, recommendation

**Dismissal**:
A reader's session-scoped mark on a finding, meaning _I have dealt with this_. It has no bearing on the finding's disposition or on the verdict — dismissing every blocking finding does not clear a blocked report. Evaporates on reload, because a report is a snapshot of one diff and a stale mark against a regenerated one would mislead.
_Avoid_: resolved, fixed, waived, done

**Dismissed**:
Of a finding, carrying that mark. Never a synonym for resolved, fixed, or waived — none of which the report can observe.
_Avoid_: closed, handled, cleared

**Self-check**:
Up to four questions written at the merge for the reader to test their own grasp of the report, each addressing one specific standing finding — never the report's own mechanics — naming the findings it is about by id in the question itself, and answerable from the page alone. Never a gate: nothing scores, records, or depends on an answer.
_Avoid_: quiz, test, exam, comprehension gate

**Closing prose**:
The non-finding narrative a pass writes around its findings — what it checked and found sound, what it wants fixed and in what order. Preserved per pass, never synthesized across passes.
_Avoid_: summary, notes, commentary

### Artifacts

**Review scope**:
The exact range of code a run reviews, resolved once by the parent so both passes see identical input.
_Avoid_: diff, target, range

**Findings file**:
The JSON one pass writes. Each pass writes its own; a merge produces the combined artifact the report renders from.
_Avoid_: output, results file

**Report**:
The rendered HTML page — one self-contained file, opened over `file://`.
_Avoid_: cockpit, canvas, output, page
