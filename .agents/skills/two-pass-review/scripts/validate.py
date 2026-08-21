#!/usr/bin/env python3
"""Validate two-pass-review artifacts. Stdlib only, Python 3.10 syntax.

Usage:
    validate.py [--repo PATH] FILE [FILE ...]

Give it a pass's two files together so the cross-file rules can run:

    validate.py findings.security.jsonl pass.security.json

or the merged artifact on its own:

    validate.py findings.json

With --repo, each finding's locations are also checked against the checkout.
Every path is confined to it, whatever else holds. A location carrying line
numbers must name a real file whose line count contains the range. A bare
path may name a file that does not exist yet -- the report's contract lets a
remedy cite a file it proposes, and a deletion leaves only the deleted name
to point at -- but what does exist at a bare path must be a file. Without
the flag those checks are skipped, so old artifacts validate as before.

Every problem is reported with an address the repair loop can act on -- a file
and, for line-oriented input, the line that carries the defect. Exit status is
0 when everything validates and 1 when anything does not.
"""

import argparse
import json
import os
import re
import sys
from collections import Counter

# The one sibling import here, and it is deliberate: the self-check naming rule
# is a claim about what the reader *sees*, and markdown_subset owns the page's
# definition of visible text. A second projection written in this file would be
# the two definitions drifting apart -- the validator passing a question whose
# ids the renderer hides, which is the defect this import fixes. Bare, with no
# sys.path mutation, for page.py's reason: run directly, this directory is
# already first on the path, and imported as a library, the entrypoint that
# imported it put the directory there.
from markdown_subset import Markdown  # noqa: E402  (sibling script, same directory)

SCHEMA_VERSION = 1

# The merged artifact is versioned apart from the pass files, whose shape has
# not changed. Version 2 is where the falsification check exists: it requires
# 'run.falsification' -- on a v2 artifact, absence would be indistinguishable
# from the ambiguity the field was added to remove -- and versions 2 and 3 are
# the only ones where a finding can carry 'falsified'. Version 3 is where the
# docs check exists, on the same terms: it requires 'run.docs_check' and is
# the first version that may carry the 'docs_check' object. Version 4 is where
# falsification stopped withdrawing and started contesting: 'falsified' does
# not exist there, a finding may instead carry the merge-written
# 'contested_md', and the verdict derives from dispositions alone -- measured
# grounds, not taste: the check's wrong-withdrawal rate on true findings ran
# near one in five at the weak tier, so its word became an annotation the
# reader adjudicates rather than a verdict-moving act. Older versions are the
# shapes from before each change, kept so every artifact already on disk
# validates and re-renders exactly as it always did.
MERGED_SCHEMA_VERSIONS = (1, 2, 3, 4)

PRODUCERS = ("security", "quality")
ID_PREFIX = {"security": "sec", "quality": "qa"}
DISPOSITIONS = ("blocking", "follow-up", "note")
SEVERITIES = ("critical", "high", "medium", "low")
CONFIDENCES = ("high", "medium", "low")
CATEGORIES = (
    "structural-regression",
    "simplification-missed",
    "branching-complexity",
    "boundary-contract",
    "modularity-decomposition",
    "legibility",
)
SCOPE_MODES = ("revisions", "local-patch")
RUN_MODES = ("parallel", "sequential")
# 'ran' means the check ran and its reply was read -- a run where the reply
# could not be parsed records 'failed', because fail-open kept every finding
# and a reader shown 'ran' would take that for everything having held.
FALSIFICATIONS = ("ran", "failed", "skipped")
# The docs check records the same three states at the same bar: 'ran' is a
# claim its reply was read, not that anything came of it.
DOCS_CHECKS = FALSIFICATIONS
DOC_NOTE_KINDS = ("stale", "missing")
VERDICTS = ("blocked", "clear")

TIER_MAX = 64

ID_RE = re.compile(r"^(sec|qa)-([0-9]+)$")
# Finding ids as they appear inside running text -- a self-check question names
# the findings it is about, and this is how the naming is checked. Bounded on
# both sides so 'sec-3' never matches inside 'sec-31'.
NAMED_ID_RE = re.compile(r"\b(?:sec|qa)-[0-9]+\b")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

FINDING_FIELDS = frozenset(
    [
        "id",
        "producer",
        "disposition",
        "title",
        "locations",
        "body_md",
        "severity",
        "category",
        "confidence",
        "confidence_rationale",
        "corroborated_by",
        "falsified",
        "contested_md",
    ]
)
LOCATION_FIELDS = frozenset(["path", "start_line", "end_line"])
PASS_FIELDS = frozenset(
    [
        "schema_version",
        "kind",
        "producer",
        "what_holds_up_md",
        "closing_md",
        "empty_reason_md",
        # Merge-written, like 'corroborated_by' on a finding: a pass cannot know
        # what model served it, and the orchestrator that chose is the only
        # party that does. Listed here so the standalone check can name them
        # instead of reporting them as fields the schema has never heard of.
        "requested_model",
        "requested_effort",
    ]
)
EMBEDDED_PASS_FIELDS = PASS_FIELDS - frozenset(["schema_version", "kind"])
MERGED_FIELDS = frozenset(
    ["schema_version", "kind", "run", "verdict", "passes", "findings", "self_check", "docs_check"]
)
RUN_FIELDS = frozenset(["mode", "falsification", "docs_check", "generated_at", "scope"])
DOCS_CHECK_FIELDS = frozenset(["examined", "skipped", "notes"])
DOC_NOTE_FIELDS = frozenset(["path", "kind", "claim_md", "why_md", "owed_md"])
DOC_SKIP_FIELDS = frozenset(["path", "reason"])
SELF_CHECK_FIELDS = frozenset(["question", "answer_md", "anchors"])
# Enough to prompt reflection, few enough that the reader is not being examined.
SELF_CHECK_MAX = 4
SCOPE_FIELDS = frozenset(
    ["repo", "mode", "base", "head", "files_changed", "diff_bytes", "untracked"]
)


class Report(object):
    """Collects problems as addressed lines, in the order they were found."""

    def __init__(self):
        self.problems = []

    def add(self, where, message):
        self.problems.append("{}: {}".format(where, message))

    @property
    def ok(self):
        return not self.problems


def _where(path, line=None):
    name = os.path.basename(path)
    if line is None:
        return name
    return "{} line {}".format(name, line)


# --- field helpers -----------------------------------------------------------


def _check_unknown(report, where, obj, allowed, what):
    for key in sorted(set(obj) - allowed):
        report.add(where, "unknown {} field {!r} -- the schema has no such field".format(what, key))


def _nonempty_str(report, where, obj, key, required=True):
    if key not in obj:
        if required:
            report.add(where, "missing required field {!r}".format(key))
        return None
    value = obj[key]
    if not isinstance(value, str) or not value.strip():
        report.add(where, "{!r} must be a non-empty string".format(key))
        return None
    return value


def _enum(report, where, obj, key, allowed, required=True):
    if key not in obj:
        if required:
            report.add(where, "missing required field {!r}".format(key))
        return None
    value = obj[key]
    if value not in allowed:
        report.add(
            where,
            "{!r} is {!r}; allowed values are {}".format(
                key, value, ", ".join(repr(a) for a in allowed)
            ),
        )
        return None
    return value


def _int(report, where, obj, key):
    if key not in obj:
        report.add(where, "missing required field {!r}".format(key))
        return None
    value = obj[key]
    # bool is an int subclass; a boolean here is a mistake, not a count.
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        report.add(where, "{!r} must be a non-negative integer".format(key))
        return None
    return value


def _prose_or_null(report, where, obj, key):
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        report.add(where, "{!r} must be a non-empty string or null".format(key))
        return None
    return value


def _tier_or_null(report, where, obj, key):
    """A model or effort label: one short line, or null for 'not recorded'.

    Deliberately not an enum. Both the level names and the model names differ
    per host and per month, and an artifact that rejected a model because this
    file predates it would be lying by omission about a run that happened. The
    cap only keeps a pasted paragraph from taking the scope grid apart; nothing
    downstream trusts the value further than escaping it.
    """
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        report.add(where, "{!r} must be a non-empty string or null".format(key))
        return None
    if "\n" in value or len(value) > TIER_MAX:
        report.add(
            where, "{!r} must be a single line of at most {} characters".format(key, TIER_MAX)
        )
        return None
    return value


def _conditional(report, where, obj, key, required_when, condition, advice=None):
    """A field required under a condition and forbidden outside it.

    The two axes are conditional rather than optional so that absence is fully
    determined: a reader can always tell "this rubric has no such axis" from
    "the pass forgot".

    `condition` names where the field belongs, once -- "on a quality finding" --
    and both sentences are written here. Callers state the rule; the grammar
    stays with the code that emits it.
    """
    present = key in obj and obj[key] is not None
    if required_when and not present:
        report.add(
            where, "{!r} is required {}{}".format(key, condition, " -- " + advice if advice else "")
        )
    elif present and not required_when:
        report.add(where, "{!r} is forbidden here; it belongs only {}".format(key, condition))
    return present


# --- findings ----------------------------------------------------------------


def check_finding(report, where, finding, in_merged, repo=None, version=None):
    # `version` is the merged artifact's schema version, None for a pass file.
    # The version-to-legality mapping for the merge-written mark fields lives
    # here, in one place, rather than as one boolean parameter per field --
    # the next mark field reads its legality from the same line these do.
    falsified_ok = version in (2, 3)
    contested_ok = version is not None and version >= 4
    if not isinstance(finding, dict):
        report.add(where, "a finding must be a JSON object")
        return None

    _check_unknown(report, where, finding, FINDING_FIELDS, "finding")

    finding_id = _nonempty_str(report, where, finding, "id")
    matched = ID_RE.match(finding_id) if finding_id else None
    if finding_id and not matched:
        report.add(where, "id {!r} must look like 'sec-3' or 'qa-14'".format(finding_id))

    producer = _enum(report, where, finding, "producer", PRODUCERS)
    if producer and matched and matched.group(1) != ID_PREFIX[producer]:
        report.add(
            where,
            "id {!r} disagrees with producer {!r}; a {} finding is numbered {}-<n>".format(
                finding_id, producer, producer, ID_PREFIX[producer]
            ),
        )

    disposition = _enum(report, where, finding, "disposition", DISPOSITIONS)

    title = _nonempty_str(report, where, finding, "title")
    if title and "\n" in title:
        report.add(where, "'title' must be a single line")

    _nonempty_str(report, where, finding, "body_md")

    check_locations(report, where, finding.get("locations"), repo)

    # severity iff security and not a note. A note is not "low severity" -- it
    # is the claim that the thing does not warrant attention, which is exactly
    # what a severity label would deny.
    wants_severity = producer == "security" and disposition != "note"
    if producer is not None and disposition is not None:
        if _conditional(
            report,
            where,
            finding,
            "severity",
            wants_severity,
            "on a security finding that is not a note",
        ):
            _enum(report, where, finding, "severity", SEVERITIES)

    wants_category = producer == "quality"
    if producer is not None:
        if _conditional(report, where, finding, "category", wants_category, "on a quality finding"):
            _enum(report, where, finding, "category", CATEGORIES)

    confidence = None
    if finding.get("confidence") is not None:
        confidence = _enum(report, where, finding, "confidence", CONFIDENCES)
    wants_rationale = confidence in ("medium", "low")
    if _conditional(
        report,
        where,
        finding,
        "confidence_rationale",
        wants_rationale,
        "when confidence is medium or low",
        advice="name the evidence you could not get",
    ):
        _nonempty_str(report, where, finding, "confidence_rationale")

    if "corroborated_by" in finding and not in_merged:
        report.add(
            where,
            "'corroborated_by' is written by the merge step; a pass file records findings only",
        )
    elif in_merged and "corroborated_by" in finding:
        links = finding["corroborated_by"]
        if not isinstance(links, list) or not all(isinstance(x, str) for x in links):
            report.add(where, "'corroborated_by' must be an array of finding ids")

    # Like 'corroborated_by': merge-written, because only the falsification
    # check -- which a pass never sees -- can set it. True or absent, never
    # false: a finding that stands says so by carrying no mark, and a second
    # way to say the same thing is a fork readers would have to reconcile.
    # Version-gated on top: a v1 merged artifact predates the check, so the
    # mark there is as unknown as it was before the check existed.
    if "falsified" in finding:
        if not in_merged:
            report.add(
                where, "'falsified' is written by the merge step; a pass file records findings only"
            )
        elif not falsified_ok:
            report.add(
                where,
                "'falsified' exists only in schema versions 2 and 3 -- a version-4 merge records a "
                "contest in 'contested_md', and version 1 predates the check",
            )
        elif finding["falsified"] is not True:
            report.add(
                where,
                "'falsified' must be true when present -- a finding that stands omits the field",
            )

    # The contest mark, merge-written like 'falsified' before it -- but an
    # annotation, not a withdrawal: it carries the check's counter-evidence and
    # changes nothing structural. Prose, not a boolean, because the whole point
    # of the mark is what travels to the reader and the verifying agent.
    if "contested_md" in finding:
        if not in_merged:
            report.add(
                where,
                "'contested_md' is written by the merge step; a pass file records findings only",
            )
        elif not contested_ok:
            report.add(where, "'contested_md' does not exist before schema version 4")
        else:
            _nonempty_str(report, where, finding, "contested_md")

    return finding_id


def check_locations(report, where, locations, repo=None):
    if not isinstance(locations, list) or not locations:
        report.add(where, "'locations' must be an array holding at least one location")
        return
    for index, location in enumerate(locations):
        at = "{} (location {})".format(where, index + 1)
        if not isinstance(location, dict):
            report.add(at, "a location must be a JSON object")
            continue
        _check_unknown(report, at, location, LOCATION_FIELDS, "location")
        path = _nonempty_str(report, at, location, "path")
        start = end = None
        for key in ("start_line", "end_line"):
            if key not in location:
                continue
            value = location[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                report.add(at, "{!r} must be a positive integer".format(key))
            elif key == "start_line":
                start = value
            else:
                end = value
        if start is not None and end is not None and end < start:
            report.add(at, "'end_line' {} is before 'start_line' {}".format(end, start))
        if end is not None and start is None:
            report.add(at, "'end_line' without 'start_line'")
        if repo is not None and path is not None:
            _check_location_in_repo(report, at, path, start, end, repo)


def _check_location_in_repo(report, at, path, start, end, repo):
    """The half of a location only the checkout can judge.

    Everything in check_locations above the call is JSON shape and touches no
    file; everything here is filesystem. Kept apart so the shape checks stay
    provably I/O-free and this block -- the security-sensitive one -- reads
    as a unit.

    A pass that recalls line numbers instead of reading them writes ranges
    past the end of the file -- observed, not hypothetical: a correct
    argument about a 37-line file arrived located at 88-95. The quote in the
    body is checkable by a reader; the range is checkable only against the
    checkout, which is what --repo is for. Read the range check as narrowly
    as it is written: it catches a range the file cannot contain, not a
    range that points at the wrong real lines, which only reading the file
    can catch.
    """
    # `path` is written by a pass that read a possibly hostile repository,
    # so it is confined before it is used. An absolute path hands
    # os.path.join the right to discard `repo` entirely -- documented
    # behaviour, not an edge case -- and a relative one can walk out
    # through `..` or a symlink. realpath on both sides settles the
    # question by what the filesystem would actually open.
    if os.path.isabs(path):
        report.add(at, "'path' must be relative to the repository root")
        return
    root = os.path.realpath(repo)
    target = os.path.realpath(os.path.join(root, path))
    if target != root and not target.startswith(root + os.sep):
        report.add(
            at,
            "'path' {!r} resolves outside the repository -- a location names a file inside the checkout".format(
                path
            ),
        )
        return
    # Prospective is a claim about the future; a directory is a mistake in
    # the present. What exists must be a file -- checked before the bare-path
    # return below, or "." and every directory in the checkout would pass as
    # a location.
    if os.path.exists(target) and not os.path.isfile(target):
        report.add(
            at,
            "'path' {!r} exists but is not a file -- a location names a file, real or proposed".format(
                path
            ),
        )
        return
    # A bare path may be prospective: the report's documented contract says a
    # cited path can be a file a remedy proposes and nothing has written yet,
    # and a finding about a deletion has only the deleted name to point at.
    # Line numbers are a different claim -- code that exists at those lines
    # today -- so a location carrying them must resolve to a real file.
    if start is None:
        return
    if not os.path.isfile(target):
        report.add(
            at,
            "'path' {!r} carries line numbers but is not a file in the repository -- "
            "a proposed or deleted file is cited by bare path, with no lines".format(path),
        )
        return
    count = _line_count(target)
    last = start if end is None else end
    if last > count:
        span = "line {} runs".format(start) if end is None else "lines {}-{} run".format(start, end)
        report.add(
            at,
            "{} past the end of {!r}, which has {} line(s) -- "
            "line numbers are read off the file, never recalled from the diff".format(
                span, path, count
            ),
        )


def _line_count(path):
    # Bytes, not text: the located file can be anything the repository holds,
    # and an encoding error here would turn a valid finding into a traceback.
    # Streamed, for the same reason: validating one artifact should not cost
    # the largest located file's size in memory.
    count = 0
    chunk = b""
    with open(path, "rb") as handle:
        while True:
            chunk_read = handle.read(1 << 16)
            if not chunk_read:
                break
            chunk = chunk_read
            count += chunk.count(b"\n")
    if chunk and not chunk.endswith(b"\n"):
        count += 1
    return count


def check_ids_contiguous(report, where, ids_by_producer):
    """Ids run from 1 with no gaps, per pass.

    They are assigned at emission and never renumbered, so a gap means a
    finding was dropped between emission and here.
    """
    for producer in PRODUCERS:
        numbers = ids_by_producer.get(producer, [])
        if not numbers:
            continue
        seen = Counter(numbers)
        missing = sorted(set(range(1, len(numbers) + 1)) - set(seen))
        duplicated = sorted(number for number, count in seen.items() if count > 1)
        if not missing and not duplicated:
            continue

        def label(number):
            return "{}-{}".format(ID_PREFIX[producer], number)

        detail = []
        if missing:
            detail.append("missing " + ", ".join(label(n) for n in missing))
        if duplicated:
            detail.append("duplicated " + ", ".join(label(n) for n in duplicated))
        report.add(
            where, "{} ids must run from 1 with no gaps: {}".format(producer, "; ".join(detail))
        )


# --- pass files --------------------------------------------------------------


def load_jsonl(report, path):
    """Returns [(line_number, object)] for every line that parsed."""
    parsed = []
    with open(path, "r", encoding="utf-8") as handle:
        for number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                parsed.append((number, json.loads(raw)))
            except ValueError as error:
                report.add(_where(path, number), "not valid JSON -- {}".format(error))
    return parsed


def load_json(report, path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except ValueError as error:
        report.add(_where(path), "not valid JSON -- {}".format(error))
        return None


def validate_pass(report, producer, findings_path, envelope_path, repo=None):
    findings = []
    ids_by_producer = {}
    if findings_path is not None:
        for number, finding in load_jsonl(report, findings_path):
            where = _where(findings_path, number)
            finding_id = check_finding(report, where, finding, in_merged=False, repo=repo)
            if isinstance(finding, dict) and finding.get("producer") not in (None, producer):
                report.add(
                    where,
                    "producer {!r} does not belong in {}".format(
                        finding.get("producer"), os.path.basename(findings_path)
                    ),
                )
            matched = ID_RE.match(finding_id) if finding_id else None
            if matched and isinstance(finding, dict) and finding.get("producer") == producer:
                ids_by_producer.setdefault(producer, []).append(int(matched.group(2)))
            findings.append(finding)
        check_ids_contiguous(report, _where(findings_path), ids_by_producer)

    if envelope_path is None:
        return

    envelope = load_json(report, envelope_path)
    if envelope is None:
        return
    where = _where(envelope_path)
    if not isinstance(envelope, dict):
        report.add(where, "a pass envelope must be a JSON object")
        return

    _check_unknown(report, where, envelope, PASS_FIELDS, "envelope")
    if envelope.get("schema_version") != SCHEMA_VERSION:
        report.add(where, "'schema_version' must be {}".format(SCHEMA_VERSION))
    if envelope.get("kind") != "pass":
        report.add(where, "'kind' must be \"pass\"")
    if _enum(report, where, envelope, "producer", PRODUCERS) not in (None, producer):
        report.add(where, "'producer' does not match the file name")
    _prose_or_null(report, where, envelope, "what_holds_up_md")
    _prose_or_null(report, where, envelope, "closing_md")
    for key in ("requested_model", "requested_effort"):
        if key in envelope:
            report.add(
                where,
                "{!r} is written by the merge step; a pass has no way to know what served it".format(
                    key
                ),
            )

    check_empty_reason(
        report,
        where,
        envelope,
        has_findings=bool(findings),
        known_findings=findings_path is not None,
    )


def check_empty_reason(report, where, envelope, has_findings, known_findings=True):
    """A pass carries findings or an account of why it has none -- never neither."""
    if not known_findings:
        return
    stated = _prose_or_null(report, where, envelope, "empty_reason_md")
    if has_findings and stated is not None:
        report.add(where, "'empty_reason_md' is forbidden when the pass emitted findings")
    if not has_findings and stated is None:
        report.add(
            where,
            "a pass with no findings owes 'empty_reason_md' -- say what was examined and why nothing survived",
        )


# --- merged artifact ---------------------------------------------------------


def validate_merged(report, path, repo=None):
    merged = load_json(report, path)
    if merged is None:
        return
    where = _where(path)
    if not isinstance(merged, dict):
        report.add(where, "the merged artifact must be a JSON object")
        return

    _check_unknown(report, where, merged, MERGED_FIELDS, "artifact")
    version = merged.get("schema_version")
    if version not in MERGED_SCHEMA_VERSIONS:
        report.add(
            where,
            "'schema_version' must be one of {}".format(
                ", ".join(str(v) for v in MERGED_SCHEMA_VERSIONS)
            ),
        )
        # Check the rest under the current rules: an unknown version is its own
        # problem, and hiding every other one behind it would make the repair
        # loop fix this artifact one refusal at a time.
        version = MERGED_SCHEMA_VERSIONS[-1]
    if merged.get("kind") != "merged":
        report.add(where, "'kind' must be \"merged\"")

    check_run(report, where, merged.get("run"), version)

    findings = merged.get("findings")
    if not isinstance(findings, list):
        report.add(where, "'findings' must be an array")
        findings = []

    by_id = {}
    ids_by_producer = {}
    producers_seen = set()
    for index, finding in enumerate(findings):
        at = "{} findings[{}]".format(where, index)
        finding_id = check_finding(report, at, finding, in_merged=True, repo=repo, version=version)
        if not isinstance(finding, dict):
            continue
        producers_seen.add(finding.get("producer"))
        if finding_id:
            if finding_id in by_id:
                report.add(at, "id {!r} is used more than once".format(finding_id))
            by_id[finding_id] = finding
        matched = ID_RE.match(finding_id) if finding_id else None
        if matched and finding.get("producer") in PRODUCERS:
            ids_by_producer.setdefault(finding["producer"], []).append(int(matched.group(2)))
    check_ids_contiguous(report, where, ids_by_producer)

    # How the 'falsified' marks read on this artifact. "active" only where
    # they are legal -- a version-2 or -3 artifact whose check ran. On a v1
    # artifact, a v4 one -- where the mark itself was already refused, because
    # falsification contests there and withdraws nothing -- or beside a
    # skipped or unreadable check, the mark is itself the reported defect and
    # reads "inactive": honouring it would have one message demand its removal
    # while others instruct the repair loop to strip valid corroboration links
    # and let a blocking finding stop blocking. And where the state itself is
    # missing or unrecognisable, the marks are "undecided" -- not inactive,
    # because asserting either reading writes diagnostics that invert once the
    # state is repaired, and a repair loop obeying both rounds would burn its
    # bounded attempts flipping the verdict back and forth.
    run = merged.get("run")
    state = run.get("falsification") if isinstance(run, dict) else None
    if version in (2, 3) and state == "ran":
        marks = "active"
    elif version in (2, 3) and state not in FALSIFICATIONS:
        marks = "undecided"
    else:
        marks = "inactive"

    check_corroboration(report, where, findings, by_id, marks_active=marks == "active")

    # A mark is the falsification check's output -- 'falsified' on versions 2
    # and 3, 'contested_md' on version 4 -- so a run whose check was skipped
    # or whose reply could not be read cannot carry either: the state
    # alongside one is the artifact contradicting itself about what happened.
    if state in ("skipped", "failed"):
        marked = sorted(
            (f.get("id") for f in findings if isinstance(f, dict) and f.get("falsified") is True),
            key=str,
        )
        if version in (2, 3) and marked:
            reason = (
                "a skipped check withdrew nothing"
                if state == "skipped"
                else "a reply nobody could read withdrew nothing"
            )
            report.add(
                where,
                "run.falsification is {!r} but {} finding(s) carry 'falsified': {} -- {}".format(
                    state, len(marked), ", ".join(str(m) for m in marked), reason
                ),
            )
        contested = sorted(
            (f.get("id") for f in findings if isinstance(f, dict) and f.get("contested_md")),
            key=str,
        )
        if version >= 4 and contested:
            reason = (
                "a skipped check contested nothing"
                if state == "skipped"
                else "a reply nobody could read contested nothing"
            )
            report.add(
                where,
                "run.falsification is {!r} but {} finding(s) carry 'contested_md': {} -- {}".format(
                    state, len(contested), ", ".join(str(c) for c in contested), reason
                ),
            )

    check_verdict(report, where, merged.get("verdict"), findings, marks)
    check_passes(report, where, merged.get("passes"), findings)
    if "self_check" in merged:
        check_self_check(report, where, merged["self_check"], by_id, marks_active=marks == "active")

    # The docs check's object exists exactly while the run says its reply was
    # read: absent under "skipped" and "failed" because a check that never
    # answered has no examined set or notes to record, and required under
    # "ran" because a run claiming a reply was read owes what it said. Where
    # the state itself is missing or unrecognisable, presence is not judged --
    # the state error is already on the list, and either instruction here
    # would invert once it is repaired.
    docs_state = run.get("docs_check") if isinstance(run, dict) else None
    if "docs_check" in merged and version < 3:
        report.add(where, "'docs_check' does not exist before schema version 3")
    elif version >= 3 and docs_state == "ran" and "docs_check" not in merged:
        report.add(
            where,
            "run.docs_check is 'ran' but there is no 'docs_check' object -- a read reply is recorded",
        )
    elif docs_state in ("skipped", "failed") and "docs_check" in merged:
        report.add(
            where,
            "run.docs_check is {!r} but a 'docs_check' object is present -- a check that never answered "
            "recorded nothing".format(docs_state),
        )
    if "docs_check" in merged and version >= 3 and docs_state == "ran":
        check_docs_check(report, where, merged["docs_check"], repo)


def check_run(report, where, run, version):
    if not isinstance(run, dict):
        report.add(where, "'run' must be a JSON object")
        return
    at = "{} run".format(where)
    _check_unknown(report, at, run, RUN_FIELDS, "run")
    _enum(report, at, run, "mode", RUN_MODES)
    # Required at v2 and forbidden at v1, never optional: on a v2 artifact an
    # absent field would be exactly the ambiguity it exists to remove -- a run
    # nobody can tell apart from one where the check ran and everything held --
    # and on a v1 artifact the field predates its own meaning.
    if version >= 2:
        _enum(report, at, run, "falsification", FALSIFICATIONS)
    elif "falsification" in run:
        report.add(at, "'falsification' does not exist in schema version 1")
    # Same bargain, one version later.
    if version >= 3:
        _enum(report, at, run, "docs_check", DOCS_CHECKS)
    elif "docs_check" in run:
        report.add(at, "'docs_check' does not exist before schema version 3")
    generated_at = _nonempty_str(report, at, run, "generated_at")
    if generated_at and not TIMESTAMP_RE.match(generated_at):
        report.add(at, "'generated_at' must look like 2026-08-08T14:02:11Z")

    scope = run.get("scope")
    if not isinstance(scope, dict):
        report.add(at, "'run.scope' must be a JSON object")
        return
    at = "{} run.scope".format(where)
    _check_unknown(report, at, scope, SCOPE_FIELDS, "scope")
    _nonempty_str(report, at, scope, "repo")
    scope_mode = _enum(report, at, scope, "mode", SCOPE_MODES)
    _nonempty_str(report, at, scope, "base")
    _int(report, at, scope, "files_changed")
    _int(report, at, scope, "diff_bytes")

    head = scope.get("head")
    if scope_mode == "local-patch":
        if head is not None:
            report.add(
                at, "'head' must be null under scope mode 'local-patch' -- the patch has no commit"
            )
    elif scope_mode == "revisions":
        if not isinstance(head, str) or not head.strip():
            report.add(at, "'head' must be a resolved revision under scope mode 'revisions'")

    # Untracked files exist only as a concept for a working patch; a revision
    # range has none by construction, so the count would be meaningless there.
    if "untracked" in scope:
        if scope_mode != "local-patch":
            report.add(at, "'untracked' belongs only to scope mode 'local-patch'")
        else:
            _int(report, at, scope, "untracked")


def check_corroboration(report, where, findings, by_id, marks_active):
    """Links resolve, are mutual, and never cross a disposition.

    A link across dispositions means a pass mis-tagged one of the two, and a
    unit spanning dispositions would break the one ordering the page has.

    `marks_active` is whether a 'falsified' mark definitely reads as a
    withdrawal here -- a version-2 artifact whose check ran. Where it does
    not, either the mark or the run state was already reported as the defect,
    and judging links by the mark anyway would demand the removal of links
    that are valid once that is repaired; the falsified arms defer to the
    validation after it.
    """
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        source = finding.get("id")
        # Corroboration promotes, and a withdrawn finding promotes nothing --
        # in either direction. The reverse arm is checked at the partner
        # lookup below, so a one-sided link to a falsified finding is named
        # too, not just the mutual case.
        if marks_active and finding.get("falsified") is True and finding.get("corroborated_by"):
            report.add(
                "{} {}".format(where, source),
                "a falsified finding carries no corroboration links -- it was withdrawn at the merge",
            )
        for target in finding.get("corroborated_by") or []:
            at = "{} {}".format(where, source)
            if not isinstance(target, str):
                # Already reported as a schema violation. Looking it up would
                # raise on an unhashable value, and this function owes the
                # repair loop a checklist rather than a traceback.
                continue
            if target == source:
                report.add(at, "corroborates itself")
                continue
            partner = by_id.get(target)
            if partner is None:
                report.add(
                    at, "corroborated_by names {!r}, which is not in this artifact".format(target)
                )
                continue
            if marks_active and partner.get("falsified") is True:
                report.add(
                    at,
                    "corroborates {!r}, which is falsified -- a withdrawn finding cannot promote one that stands".format(
                        target
                    ),
                )
            if source not in (partner.get("corroborated_by") or []):
                report.add(
                    at,
                    "corroboration with {!r} is one-way; both findings carry the link or neither does".format(
                        target
                    ),
                )
            if finding.get("disposition") != partner.get("disposition"):
                report.add(
                    at,
                    "corroborates {!r} across dispositions ({} vs {}) -- one of the two is mis-tagged".format(
                        target, finding.get("disposition"), partner.get("disposition")
                    ),
                )
            # Corroboration is agreement between the two passes. Two findings
            # from one pass are one model in one context window, so linking them
            # promotes a finding on the strength of its own author agreeing with
            # itself. A three-way unit is still reachable, since union-find
            # groups through a partner in the other pass.
            if finding.get("producer") == partner.get("producer"):
                report.add(
                    at,
                    "corroborates {!r} from the same pass; corroboration links a finding to one "
                    "the other pass argued".format(target),
                )


def check_verdict(report, where, verdict, findings, marks):
    """The verdict agrees with its own list.

    This is what makes "derived, so it cannot contradict" checked rather than
    trusted. Falsified findings are outside the derivation entirely: the diff
    contradicts them, so a "blocking" tag on one is a claim the merge has
    already answered. Only while the marks read "active", though -- on an
    artifact where the mark is illegal ("inactive") the mark was already
    reported as the defect, and letting it excuse a blocking finding here
    would hand the repair loop a verdict problem that only appears after the
    mark is fixed.

    "undecided" -- a v2 artifact whose run.falsification is missing or
    unrecognisable -- reports only what holds under both readings. The
    honoured set is a subset of the ignored one, so that has a closed form:
    a clear verdict is wrong only when an unmarked blocking finding exists,
    a blocked one only when no blocking finding exists at all. Everything
    between defers behind the state error already on the list, so no verdict
    instruction is written that repairing the state would invert.
    """
    if verdict not in VERDICTS:
        report.add(where, "'verdict' must be 'blocked' or 'clear'")
        return
    honoured = [
        f.get("id")
        for f in findings
        if isinstance(f, dict)
        and f.get("disposition") == "blocking"
        and f.get("falsified") is not True
    ]
    ignored = [
        f.get("id") for f in findings if isinstance(f, dict) and f.get("disposition") == "blocking"
    ]

    # What certainly blocks: under "inactive" the marks do not exist, so
    # every blocking finding stands; under "active" and "undecided" the
    # unmarked ones do -- for "undecided" because they block either way.
    certain = ignored if marks == "inactive" else honoured
    if certain and verdict != "blocked":
        report.add(
            where,
            "verdict is 'clear' but {} finding(s) block: {}".format(
                len(certain), ", ".join(str(b) for b in certain)
            ),
        )

    # What certainly leaves nothing blocking: under "active" an empty
    # honoured set settles it; otherwise only an empty ignored set does --
    # for "undecided" because a marked blocking finding might stand.
    none_stand = not honoured if marks == "active" else not ignored
    if none_stand and verdict != "clear":
        if marks == "active" and ignored:
            report.add(
                where,
                "verdict is 'blocked' but every blocking finding is falsified, and a withdrawn finding does not block",
            )
        else:
            report.add(where, "verdict is 'blocked' but no finding is tagged 'blocking'")


def check_self_check(report, where, self_check, by_id, marks_active):
    """The reader's self-check: at most four questions, grounded in the findings.

    Optional and inert: it asserts nothing about the findings, promotes
    nothing, and cannot touch the verdict -- which is why, unlike 'falsified',
    it needs no schema version to interpret it. What it does owe is
    checkability: every answer names the findings it rests on, and an anchor
    the artifact does not hold would be the page telling the reader to check
    an answer against nothing.

    Standing findings only. A withdrawal is a question the merge already
    answered, and the reader is quizzed only on what they are being asked to
    act on -- so a falsified anchor is refused, under the same `marks_active`
    reading the corroboration check uses: where the mark itself is the
    reported defect, judging anchors by it would write diagnostics that
    invert once it is repaired.

    The caller gates on key presence, not on value: "no questions" is spelled
    by omitting the field, and an explicit null is a second spelling of the
    same thing -- the fork the schema refuses everywhere else -- so null
    falls into the array refusal below rather than passing as absence.
    """
    at = "{} self_check".format(where)
    if not isinstance(self_check, list) or not self_check:
        report.add(
            at,
            "'self_check' must be an array holding at least one question -- omit the field when there are none",
        )
        return
    if len(self_check) > SELF_CHECK_MAX:
        report.add(
            at,
            "'self_check' holds {} questions; at most {} -- a self-check is a nudge, not an exam".format(
                len(self_check), SELF_CHECK_MAX
            ),
        )
    for index, item in enumerate(self_check):
        at_item = "{} self_check[{}]".format(where, index)
        if not isinstance(item, dict):
            report.add(at_item, "a self-check question must be a JSON object")
            continue
        _check_unknown(report, at_item, item, SELF_CHECK_FIELDS, "self-check")
        question = _nonempty_str(report, at_item, item, "question")
        if question and "\n" in question:
            report.add(at_item, "'question' must be a single line")
        _nonempty_str(report, at_item, item, "answer_md")
        anchors = item.get("anchors")
        if (
            not isinstance(anchors, list)
            or not anchors
            or not all(isinstance(a, str) for a in anchors)
        ):
            report.add(at_item, "'anchors' must be an array holding at least one finding id")
            continue
        # The question says which findings it is about, in its own text: a
        # reader should never have to open the answer to learn what is being
        # asked. Named-but-unanchored is the reverse defect -- a question
        # citing a finding its own answer is not grounded in. Matched against
        # the plain projection, not the raw markdown, so an id hidden in a
        # link destination -- text the page never shows -- names nothing,
        # while one in a link label or inline code still counts.
        if question:
            named = set(NAMED_ID_RE.findall(Markdown.plain(question)))
            if not named & set(anchors):
                report.add(
                    at_item,
                    "the question names none of its anchors -- a self-check question says which "
                    "findings it is about, by id, in the question itself",
                )
            for name in sorted(named - set(anchors)):
                report.add(
                    at_item,
                    "the question names {!r}, which is not among its anchors -- every finding a "
                    "question cites is one its answer is grounded in".format(name),
                )
        seen = set()
        for anchor in anchors:
            if anchor in seen:
                report.add(at_item, "anchor {!r} is repeated".format(anchor))
            seen.add(anchor)
            if anchor not in by_id:
                report.add(
                    at_item,
                    "anchor {!r} is not a finding in this artifact -- an answer is grounded in findings "
                    "the reader can open".format(anchor),
                )
            elif marks_active and by_id[anchor].get("falsified") is True:
                report.add(
                    at_item,
                    "anchor {!r} is falsified -- a self-check question addresses findings that stand, "
                    "not ones the merge withdrew".format(anchor),
                )


def check_docs_check(report, where, docs_check, repo=None):
    """The docs check's record: what it read, what it refused, what conflicted.

    A doc note is not a finding, and nothing here resembles the finding rules
    on purpose: no ids, no dispositions, no corroboration, and the verdict
    never reads any of it. What the block owes instead is honesty about
    coverage -- 'examined' is the whole of what the check saw, so every note
    points into it, and an empty 'examined' with an empty 'notes' is a valid
    record of a repository with nothing to check.
    """
    at = "{} docs_check".format(where)
    if not isinstance(docs_check, dict):
        report.add(at, "'docs_check' must be a JSON object")
        return
    _check_unknown(report, at, docs_check, DOCS_CHECK_FIELDS, "docs_check")

    examined = docs_check.get("examined")
    if not isinstance(examined, list) or not all(
        isinstance(p, str) and p.strip() for p in examined
    ):
        report.add(at, "'examined' must be an array of repository-relative paths, possibly empty")
        examined = []
    seen = set()
    for path in examined:
        if path in seen:
            report.add(at, "examined path {!r} is repeated".format(path))
        seen.add(path)
        if repo is not None:
            _check_doc_path(report, at, path, repo)

    # Required, empty allowed, never optional: the collector always prints its
    # refusals, and an artifact allowed to drop the array could state fuller
    # coverage than the collection had. Absence must stay distinguishable from
    # "nothing was refused".
    if "skipped" not in docs_check:
        report.add(
            at,
            "'skipped' must be present -- the collector's refusals, an empty array when it refused nothing",
        )
        skipped = []
    else:
        skipped = docs_check["skipped"]
        if not isinstance(skipped, list):
            report.add(at, "'skipped' must be an array")
            skipped = []
        for index, entry in enumerate(skipped):
            at_skip = "{} skipped[{}]".format(at, index)
            if not isinstance(entry, dict):
                report.add(at_skip, "a skip entry must be a JSON object")
                continue
            _check_unknown(report, at_skip, entry, DOC_SKIP_FIELDS, "skip")
            _nonempty_str(report, at_skip, entry, "path")
            _nonempty_str(report, at_skip, entry, "reason")

    notes = docs_check.get("notes")
    if not isinstance(notes, list):
        report.add(at, "'notes' must be an array -- empty when nothing conflicted")
        return
    for index, note in enumerate(notes):
        at_note = "{} notes[{}]".format(at, index)
        if not isinstance(note, dict):
            report.add(at_note, "a doc note must be a JSON object")
            continue
        _check_unknown(report, at_note, note, DOC_NOTE_FIELDS, "doc note")
        path = _nonempty_str(report, at_note, note, "path")
        if path is not None and path not in seen:
            report.add(
                at_note,
                "path {!r} is not in 'examined' -- a note is about a document the check read".format(
                    path
                ),
            )
        kind = _enum(report, at_note, note, "kind", DOC_NOTE_KINDS)
        # The quote is what makes a stale claim checkable against the document,
        # and what a missing-coverage note by definition cannot have.
        if kind is not None:
            if _conditional(
                report,
                at_note,
                note,
                "claim_md",
                kind == "stale",
                "on a 'stale' note, quoting the document's own words",
            ):
                _nonempty_str(report, at_note, note, "claim_md")
        _nonempty_str(report, at_note, note, "why_md")
        if "owed_md" in note:
            _nonempty_str(report, at_note, note, "owed_md")


def _check_doc_path(report, at, path, repo):
    """A document the check states it read: confined, and a real file.

    Confined exactly as _check_location_in_repo confines a location, and for
    the same reason -- the path was written into an artifact a hostile
    repository can influence. Stricter after that: a location may be
    prospective, but 'examined' is a claim about files that were read, and a
    path that is not a file is a claim nothing can have read.
    """
    if os.path.isabs(path):
        report.add(at, "examined path {!r} must be relative to the repository root".format(path))
        return
    root = os.path.realpath(repo)
    target = os.path.realpath(os.path.join(root, path))
    if target != root and not target.startswith(root + os.sep):
        report.add(at, "examined path {!r} resolves outside the repository".format(path))
        return
    if not os.path.isfile(target):
        report.add(at, "examined path {!r} is not a file in the repository".format(path))


def check_passes(report, where, passes, findings):
    if not isinstance(passes, list) or not passes:
        report.add(where, "'passes' must be an array holding at least one pass")
        return
    seen = set()
    for index, envelope in enumerate(passes):
        at = "{} passes[{}]".format(where, index)
        if not isinstance(envelope, dict):
            report.add(at, "a pass envelope must be a JSON object")
            continue
        _check_unknown(report, at, envelope, EMBEDDED_PASS_FIELDS, "envelope")
        producer = _enum(report, at, envelope, "producer", PRODUCERS)
        if producer in seen:
            report.add(at, "producer {!r} appears twice".format(producer))
        seen.add(producer)
        _prose_or_null(report, at, envelope, "what_holds_up_md")
        _prose_or_null(report, at, envelope, "closing_md")
        # Both optional, and no rule that the two passes agree: a split is
        # something the user can ask for, so it is true rather than invalid.
        # The page is what tells the reader the passes were not peers.
        _tier_or_null(report, at, envelope, "requested_model")
        _tier_or_null(report, at, envelope, "requested_effort")
        has_findings = any(isinstance(f, dict) and f.get("producer") == producer for f in findings)
        check_empty_reason(report, at, envelope, has_findings=has_findings)

    orphans = sorted(
        {
            f.get("producer")
            for f in findings
            if isinstance(f, dict) and f.get("producer") not in seen
        }
        - {None}
    )
    for producer in orphans:
        report.add(
            where,
            "findings from the {} pass are present with no matching envelope in 'passes'".format(
                producer
            ),
        )


# --- entry point -------------------------------------------------------------


def validate_paths(paths, repo=None):
    """Validate the given artifacts. Returns a list of addressed problems.

    `repo` is the checkout the findings are about; when given, locations are
    checked against it. render.py calls this without one -- at render time
    there is no repository to hand, and a merge that already validated with
    --repo does not need the locations proven twice.
    """
    report = Report()

    merged = []
    pass_findings = {}
    pass_envelopes = {}
    for path in paths:
        if not os.path.exists(path):
            report.add(_where(path), "no such file")
            continue
        name = os.path.basename(path)
        matched = re.match(r"^findings\.({})\.jsonl$".format("|".join(PRODUCERS)), name)
        if matched:
            pass_findings[matched.group(1)] = path
            continue
        matched = re.match(r"^pass\.({})\.json$".format("|".join(PRODUCERS)), name)
        if matched:
            pass_envelopes[matched.group(1)] = path
            continue
        if name == "findings.json":
            merged.append(path)
            continue
        report.add(
            _where(path),
            "unrecognised artifact; expected findings.<producer>.jsonl, pass.<producer>.json or findings.json",
        )

    for producer in PRODUCERS:
        if producer in pass_findings or producer in pass_envelopes:
            validate_pass(
                report, producer, pass_findings.get(producer), pass_envelopes.get(producer), repo
            )

    for path in merged:
        validate_merged(report, path, repo)

    return report.problems


def main(argv):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("paths", metavar="FILE", nargs="+")
    parser.add_argument("--repo")
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit:
        return 2

    repo = None
    if args.repo is not None:
        repo = os.path.abspath(os.path.expanduser(args.repo))
        # A wrong --repo is the operator's mistake, not the artifact's:
        # refusing here keeps it out of the repair loop, which can fix
        # findings but not the command it was invoked with.
        if not os.path.isdir(repo):
            sys.stderr.write("--repo {!r} is not a directory\n".format(args.repo))
            return 2
    problems = validate_paths(args.paths, repo)
    if problems:
        sys.stderr.write("Validation failed. Fix each of these and validate again:\n\n")
        for problem in problems:
            sys.stderr.write("  {}\n".format(problem))
        sys.stderr.write("\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
