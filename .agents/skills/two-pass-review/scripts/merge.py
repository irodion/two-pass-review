#!/usr/bin/env python3
"""Assemble the merged artifact from a run directory. Stdlib only, 3.10 syntax.

Usage:
    merge.py --run-dir RUN_DIR --passes {parallel,sequential}
             --falsification {ran,skipped} --docs-check {ran,skipped}
             [--link ID,ID]... [--self-check FILE]
             [--model M] [--effort E]
             [--security-model M] [--security-effort E]
             [--quality-model M] [--quality-effort E]

Writes RUN_DIR/findings.json, validates it, and prints a JSON summary.

The merge used to be the orchestrator hand-writing that file: every finding
copied back out of the pass files, long escaped bodies included, beside the
run facts, the docs lists and a verdict. The validator checks the shape of
what comes out and cannot check the copy -- a finding dropped from the end of
a list, a disposition quietly changed, a body cut short all validate, because
the pass files are the only record of what the passes wrote and nothing read
them. That is the step a mid-tier model is likeliest to get wrong, and
nothing about it is judgment. In the field, an orchestrator wrote its own
version of this script in the run directory rather than copy by hand.

So the copying is here, and the orchestrator supplies only what needs a
reader: which findings corroborate each other, the self-check questions, and
which way each subagent ran. Everything else is read from the files the run
already wrote, by name:

    scope.json                      scope.py's output: scope, clock, repo_root
    findings.<producer>.jsonl       each pass's findings
    pass.<producer>.json            each pass's envelope
    falsification.json              the falsifier's answer   (--falsification ran)
    docs.json                       collect_docs.py's output (--docs-check ran)
    docs-notes.json                 the docs checker's answer (--docs-check ran)

Nothing a subagent wrote is judged here. Every contest is attached to the
finding it names -- even one the orchestrator would call wrong, because the
reader and the verifying agent adjudicate, not the merge -- and every doc note
is carried over for validate.py to check. The two states the orchestrator
cannot choose are decided here instead: a check that ran and left no readable
JSON array is recorded 'failed', never 'ran', which is the fail-open the
falsification check depends on and the fail-toward-silence the docs check
depends on.

Exit status: 0 merged and valid; 1 a pass's files or the merged artifact do
not validate, or the run directory lacks what a flag said it holds; 2 a
malformed command, or a flag refused by name -- a --link, or a check recorded
'skipped' beside its answer file.
"""

import argparse
import json
import os
import re
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # sibling script, same directory

FALSIFIER_ANSWER = "falsification.json"
DOCS_ANSWER = "docs-notes.json"

# What the falsification check says when an entry names a finding but gives no
# reason. Attached rather than dropped: the flag is the check's output, and a
# dropped one is a check that silently did not run.
NO_REASON = "The falsification check flagged this finding and gave no reason."


def warn(message: str) -> None:
    sys.stderr.write(f"Warning: {message}\n")


def refuse(message: str, status: int = 1) -> int:
    sys.stderr.write(f"Cannot merge: {message}\n")
    return status


# Where an answer can start: a `[` whose next non-space character opens an
# object or closes the array. Nothing else can begin an array of objects.
ANSWER_START = re.compile(r"\[\s*[{\]]")

# How many failed starts the parse tries before it gives up. A real answer
# needs a handful -- the prose and the fence around it. Past this the text is
# pathological, and giving up costs only the check, which is recorded 'failed'.
MAX_FAILED_STARTS = 1000


def extract_array(text: str) -> list[dict[str, Any]] | None:
    """The last JSON array of objects in a subagent's answer, or None.

    Lenient on purpose. A subagent asked for a bare array still wraps it in a
    sentence or a fence often enough that the parse is load-bearing, not
    defensive -- measured once, on Haiku. Every place an array of objects
    could start is tried, a parsed array is skipped whole so an array nested
    inside it is never mistaken for the answer, and only an array of objects
    counts: `[]` is an answer, a stray `[1]` in prose is not.

    Only those starts, not every `[`, because a failed parse costs a scan to
    wherever it fails. A run of brackets -- which a falsifier quoting a
    hostile fixture can write -- made every `[` in it a scan of the rest, and
    twenty thousand of them took seconds; none of them can start an answer.
    A nest of `[{` can, so the failed starts are capped as well.
    """
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] | None = None
    failures = 0
    start = ANSWER_START.search(text)
    while start is not None and failures < MAX_FAILED_STARTS:
        try:
            value, end = decoder.raw_decode(text, start.start())
        # RecursionError is not a ValueError, and before 3.12 the decoder
        # raises it on deep nesting. Uncaught, it turned an unreadable answer
        # into a traceback and cost the whole run, where fail-open says it
        # costs only the check.
        except (ValueError, RecursionError):
            failures += 1
            start = ANSWER_START.search(text, start.start() + 1)
            continue
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            found = value
        start = ANSWER_START.search(text, end)
    return found


def read_answer(run_dir: str, name: str) -> list[dict[str, Any]] | None:
    """A subagent's answer file, as an array, or None when there is none to read."""
    try:
        with open(os.path.join(run_dir, name), encoding="utf-8", errors="replace") as handle:
            return extract_array(handle.read())
    except OSError:
        return None


def unread(run_dir: str, name: str) -> str:
    """Why an answer file gave nothing, in the words a warning needs."""
    if os.path.exists(os.path.join(run_dir, name)):
        return f"{name} held no readable JSON array"
    return f"{name} was never written"


def read_json(path: str) -> object:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def read_findings(path: str) -> list[dict[str, Any]]:
    """One pass's findings, in the order it emitted them. Validated beforehand."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def contests(
    answer: list[dict[str, Any]], known: dict[str, dict[str, Any]]
) -> dict[str, list[str]]:
    """The falsifier's entries, grouped by the finding each one names."""
    reasons: dict[str, list[str]] = {}
    for entry in answer:
        finding_id = entry.get("id")
        if not isinstance(finding_id, str) or finding_id not in known:
            warn(
                f"the falsification check named {finding_id!r}, which is not a finding in this "
                "run, so that contest could not be attached"
            )
            continue
        # `reason` as well as `reason_md`: the field name is the one thing a
        # weaker model gets wrong in an otherwise good answer, and refusing the
        # reason over it would hand the reader a bare flag.
        reason = entry.get("reason_md", entry.get("reason"))
        if not isinstance(reason, str) or not reason.strip():
            reason = NO_REASON
        reasons.setdefault(finding_id, []).append(reason.strip())
    return reasons


def links(pairs: list[str], known: dict[str, dict[str, Any]]) -> tuple[dict[str, list[str]], str]:
    """({id: partners}, problem) from the --link pairs.

    The validator would refuse every one of these too, by the same
    link_problems. Refused here first because this is where the orchestrator
    can act on it: the message names the flag it typed, not a field in a file
    it did not write.
    """
    partners: dict[str, list[str]] = {}
    for pair in pairs:
        ids = [part.strip() for part in pair.split(",")]
        if len(ids) != 2 or not all(ids):
            return {}, f"--link {pair!r} must name exactly two findings, like --link sec-1,qa-2"
        first, second = ids
        for finding_id in ids:
            if finding_id not in known:
                return (
                    {},
                    f"--link {pair!r} names {finding_id!r}, which is not a finding in this run",
                )
        problems = validate.link_problems(known[first], known[second])
        if problems:
            return {}, f"--link {pair!r} is refused: {problems[0]}. Leave the two unlinked"
        for source, target in ((first, second), (second, first)):
            if target not in partners.setdefault(source, []):
                partners[source].append(target)
    return partners, ""


def check_state(
    run_dir: str, requested: str, flag: str, answer_name: str, has_input: bool
) -> tuple[str, list[dict[str, Any]], str]:
    """(state, answer, problem) for one subagent check -- falsification or docs.

    The rules the two checks share, stated once:

    - Recorded 'skipped' with an answer file beside it is refused. Either the
      check ran and the flag is wrong, or the file is stale, and the page would
      state the wrong one either way.
    - With nothing to hand a subagent -- no findings, no documents -- there was
      none to spawn. The check ran over an empty set and has no file to read.
    - A check whose answer holds no readable JSON array is 'failed', never
      'ran': the fail-open the falsification check depends on, and the
      fail-toward-silence the docs check depends on.
    - Otherwise it ran, and this is its answer.

    Each caller keeps its own warning, because what a failure costs differs.
    """
    if requested == "skipped":
        if os.path.exists(os.path.join(run_dir, answer_name)):
            return (
                "",
                [],
                f"{flag} skipped, but {answer_name} is in the run directory -- if the check ran, "
                f"say {flag} ran",
            )
        return "skipped", [], ""
    if not has_input:
        return "ran", [], ""
    answer = read_answer(run_dir, answer_name)
    if answer is None:
        return "failed", [], ""
    return "ran", answer, ""


def read_collection(run_dir: str) -> tuple[list[str], list[Any]] | None:
    """collect_docs.py's two lists from docs.json, or None when it is not there.

    Copied into the artifact as they stand: 'examined' is what the docs check
    was handed, and 'skipped' is what the collector refused and why.
    """
    collected = read_json(os.path.join(run_dir, "docs.json"))
    if (
        not isinstance(collected, dict)
        or not isinstance(collected.get("docs"), list)
        or not isinstance(collected.get("skipped"), list)
    ):
        return None
    return [entry["path"] for entry in collected["docs"]], collected["skipped"]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--run-dir", required=True)
    # Not `--mode`: that is scope.py's flag for the scope mode, and the two
    # mean nothing like each other. The page labels this value "Passes".
    parser.add_argument("--passes", required=True, choices=("parallel", "sequential"))
    parser.add_argument("--falsification", required=True, choices=("ran", "skipped"))
    parser.add_argument("--docs-check", required=True, choices=("ran", "skipped"))
    parser.add_argument("--link", action="append", default=[])
    parser.add_argument("--self-check")
    for prefix in ("", "security-", "quality-"):
        parser.add_argument(f"--{prefix}model")
        parser.add_argument(f"--{prefix}effort")
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit:
        return 2

    run_dir = os.path.abspath(os.path.expanduser(args.run_dir))
    pinned = read_json(os.path.join(run_dir, "scope.json"))
    if not isinstance(pinned, dict) or not isinstance(pinned.get("scope"), dict):
        return refuse(
            f"{run_dir} holds no scope.json written by scope.py -- merge the run directory "
            "scope.py printed",
            2,
        )

    # The tree the passes read. Gone means the worktree was removed before the
    # merge; the ranges were already proven against it when each pass validated,
    # so merging without it loses a re-check, not a check.
    repo: str | None = pinned.get("repo_root")
    if repo is not None and not os.path.isdir(repo):
        warn(f"{repo} no longer exists, so line ranges are not re-checked against it")
        repo = None

    # Each pass, validated before anything is copied out of it. A pass without
    # an envelope died before finishing, and its findings stay out: the merged
    # artifact requires an envelope for every pass it carries, and the page
    # shows the absence.
    passes: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for producer in validate.PRODUCERS:
        findings_path = os.path.join(run_dir, f"findings.{producer}.jsonl")
        envelope_path = os.path.join(run_dir, f"pass.{producer}.json")
        if not os.path.exists(envelope_path):
            if os.path.exists(findings_path):
                warn(
                    f"the {producer} pass wrote findings but no envelope, so none of its findings "
                    "are merged. Say which pass died, and offer to re-run just that one"
                )
            continue
        given = [envelope_path] + ([findings_path] if os.path.exists(findings_path) else [])
        problems = validate.validate_paths(given, repo)
        if problems:
            sys.stderr.write(
                f"Cannot merge: the {producer} pass's files do not validate. Send these back to "
                "that pass to repair:\n\n"
            )
            for problem in problems:
                sys.stderr.write(f"  {problem}\n")
            return 1
        envelope = read_json(envelope_path)
        assert isinstance(envelope, dict)  # validated above
        entry = {k: v for k, v in envelope.items() if k not in ("schema_version", "kind")}
        for field, shared, own in (
            ("requested_model", args.model, getattr(args, f"{producer}_model")),
            ("requested_effort", args.effort, getattr(args, f"{producer}_effort")),
        ):
            value = own if own is not None else shared
            if value is not None:
                entry[field] = value
        passes.append(entry)
        findings.extend(read_findings(findings_path))
    if not passes:
        return refuse("neither pass wrote an envelope, so there is no review to merge")

    known = {finding["id"]: finding for finding in findings}

    partners, problem = links(args.link, known)
    if problem:
        return refuse(problem, 2)
    for finding_id, linked in partners.items():
        known[finding_id]["corroborated_by"] = sorted(linked, key=validate.id_sort_key)

    falsification, answer, problem = check_state(
        run_dir, args.falsification, "--falsification", FALSIFIER_ANSWER, bool(findings)
    )
    if problem:
        return refuse(problem, 2)
    if falsification == "failed":
        warn(
            f"{unread(run_dir, FALSIFIER_ANSWER)}, so the falsification check is recorded as "
            "failed and every finding stands uncontested"
        )
    for finding_id, reasons in contests(answer, known).items():
        known[finding_id]["contested_md"] = "\n\n".join(reasons)

    examined: list[str] = []
    refused: list[Any] = []
    if args.docs_check == "ran":
        collection = read_collection(run_dir)
        if collection is None:
            return refuse(
                "--docs-check ran, but the run directory holds no docs.json. collect_docs.py "
                "writes it and the docs check reads its documents from it, so a run without it "
                "had no docs check -- merge with --docs-check skipped"
            )
        examined, refused = collection
    docs_state, notes, problem = check_state(
        run_dir, args.docs_check, "--docs-check", DOCS_ANSWER, bool(examined)
    )
    if problem:
        return refuse(problem, 2)
    if docs_state == "failed":
        warn(
            f"{unread(run_dir, DOCS_ANSWER)}, so the docs check is recorded as failed and "
            "carries no notes"
        )
    docs_check: dict[str, Any] | None = None
    if docs_state == "ran":
        docs_check = {"examined": examined, "skipped": refused, "notes": notes}

    self_check: object = None
    if args.self_check is not None:
        self_check = read_json(os.path.abspath(os.path.expanduser(args.self_check)))
        if self_check is None:
            return refuse(f"--self-check {args.self_check!r} is not a readable JSON file", 2)

    artifact: dict[str, Any] = {
        # The newest merged shape is the one a new merge writes. Named from
        # validate.py's list, not restated: validate.SCHEMA_VERSION is the
        # pass files' version, a different number under the same name.
        "schema_version": validate.MERGED_SCHEMA_VERSIONS[-1],
        "kind": "merged",
        "run": {
            "mode": args.passes,
            "falsification": falsification,
            "docs_check": docs_state,
            "generated_at": pinned.get("now"),
            "scope": pinned["scope"],
        },
        # Derived, never authored: a contested blocking finding still blocks,
        # because un-blocking on the check's word would hand a checker that is
        # wrong about one time in five the verdict.
        "verdict": "blocked" if any(f["disposition"] == "blocking" for f in findings) else "clear",
        "passes": passes,
        "findings": findings,
    }
    if docs_check is not None:
        artifact["docs_check"] = docs_check
    if self_check is not None:
        artifact["self_check"] = self_check

    target = os.path.join(run_dir, "findings.json")
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(artifact, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    problems = validate.validate_paths([target], repo)
    if problems:
        sys.stderr.write(
            "findings.json was written but does not validate. These come from what was passed "
            "to merge.py -- a --link, the --self-check file, or a docs note -- so fix that input "
            "and run merge.py again:\n\n"
        )
        for problem in problems:
            sys.stderr.write(f"  {problem}\n")
        return 1

    json.dump(
        {
            "findings_json": target,
            "verdict": artifact["verdict"],
            "findings": {
                producer: sum(1 for f in findings if f["producer"] == producer)
                for producer in validate.PRODUCERS
                if any(p["producer"] == producer for p in passes)
            },
            "contested": [f["id"] for f in findings if "contested_md" in f],
            "falsification": falsification,
            "docs_check": docs_state,
            "doc_notes": len(docs_check["notes"]) if docs_check is not None else 0,
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
