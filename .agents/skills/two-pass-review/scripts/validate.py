#!/usr/bin/env python3
"""Validate two-pass-review artifacts. Stdlib only, Python 3.9 syntax.

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

SCHEMA_VERSION = 1

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
VERDICTS = ("blocked", "clear")

TIER_MAX = 64

ID_RE = re.compile(r"^(sec|qa)-([0-9]+)$")
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
MERGED_FIELDS = frozenset(["schema_version", "kind", "run", "verdict", "passes", "findings"])
RUN_FIELDS = frozenset(["mode", "generated_at", "scope"])
SCOPE_FIELDS = frozenset(["repo", "mode", "base", "head", "files_changed", "diff_bytes", "untracked"])


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
            "{!r} is {!r}; allowed values are {}".format(key, value, ", ".join(repr(a) for a in allowed)),
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
        report.add(where, "{!r} must be a single line of at most {} characters".format(key, TIER_MAX))
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
        report.add(where, "{!r} is required {}{}".format(key, condition, " -- " + advice if advice else ""))
    elif present and not required_when:
        report.add(where, "{!r} is forbidden here; it belongs only {}".format(key, condition))
    return present


# --- findings ----------------------------------------------------------------


def check_finding(report, where, finding, in_merged, repo=None):
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
            report, where, finding, "severity", wants_severity, "on a security finding that is not a note"
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
            "'path' {!r} resolves outside the repository -- a location names a file inside the checkout".format(path),
        )
        return
    # Prospective is a claim about the future; a directory is a mistake in
    # the present. What exists must be a file -- checked before the bare-path
    # return below, or "." and every directory in the checkout would pass as
    # a location.
    if os.path.exists(target) and not os.path.isfile(target):
        report.add(
            at,
            "'path' {!r} exists but is not a file -- a location names a file, real or proposed".format(path),
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
            "line numbers are read off the file, never recalled from the diff".format(span, path, count),
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
        report.add(where, "{} ids must run from 1 with no gaps: {}".format(producer, "; ".join(detail)))


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
                "{!r} is written by the merge step; a pass has no way to know what served it".format(key),
            )

    check_empty_reason(report, where, envelope, has_findings=bool(findings), known_findings=findings_path is not None)


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
    if merged.get("schema_version") != SCHEMA_VERSION:
        report.add(where, "'schema_version' must be {}".format(SCHEMA_VERSION))
    if merged.get("kind") != "merged":
        report.add(where, "'kind' must be \"merged\"")

    check_run(report, where, merged.get("run"))

    findings = merged.get("findings")
    if not isinstance(findings, list):
        report.add(where, "'findings' must be an array")
        findings = []

    by_id = {}
    ids_by_producer = {}
    producers_seen = set()
    for index, finding in enumerate(findings):
        at = "{} findings[{}]".format(where, index)
        finding_id = check_finding(report, at, finding, in_merged=True, repo=repo)
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

    check_corroboration(report, where, findings, by_id)
    check_verdict(report, where, merged.get("verdict"), findings)
    check_passes(report, where, merged.get("passes"), findings)


def check_run(report, where, run):
    if not isinstance(run, dict):
        report.add(where, "'run' must be a JSON object")
        return
    at = "{} run".format(where)
    _check_unknown(report, at, run, RUN_FIELDS, "run")
    _enum(report, at, run, "mode", RUN_MODES)
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
            report.add(at, "'head' must be null under scope mode 'local-patch' -- the patch has no commit")
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


def check_corroboration(report, where, findings, by_id):
    """Links resolve, are mutual, and never cross a disposition.

    A link across dispositions means a pass mis-tagged one of the two, and a
    unit spanning dispositions would break the one ordering the page has.
    """
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        source = finding.get("id")
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
                report.add(at, "corroborated_by names {!r}, which is not in this artifact".format(target))
                continue
            if source not in (partner.get("corroborated_by") or []):
                report.add(
                    at,
                    "corroboration with {!r} is one-way; both findings carry the link or neither does".format(target),
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


def check_verdict(report, where, verdict, findings):
    """The verdict agrees with its own list.

    This is what makes "derived, so it cannot contradict" checked rather than
    trusted.
    """
    if verdict not in VERDICTS:
        report.add(where, "'verdict' must be 'blocked' or 'clear'")
        return
    blocking = [f.get("id") for f in findings if isinstance(f, dict) and f.get("disposition") == "blocking"]
    if blocking and verdict != "blocked":
        report.add(
            where,
            "verdict is 'clear' but {} finding(s) block: {}".format(len(blocking), ", ".join(str(b) for b in blocking)),
        )
    if not blocking and verdict != "clear":
        report.add(where, "verdict is 'blocked' but no finding is tagged 'blocking'")


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
        {f.get("producer") for f in findings if isinstance(f, dict) and f.get("producer") not in seen} - {None}
    )
    for producer in orphans:
        report.add(where, "findings from the {} pass are present with no matching envelope in 'passes'".format(producer))


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
            validate_pass(report, producer, pass_findings.get(producer), pass_envelopes.get(producer), repo)

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
