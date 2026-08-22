#!/usr/bin/env python3
"""Collect the agent-facing documents the docs check reads. Stdlib only, 3.10 syntax.

Usage:
    collect_docs.py --repo PATH --diff CONTEXT_DIFF

Prints JSON, and writes the same bytes to docs.json beside the diff:

    {"docs": [{"path": ..., "bytes": ...}], "skipped": [{"path": ..., "reason": ...}]}

The file exists because the merge has to put both lists into the artifact, and
copying a file is checkable where retyping a printed fragment is not: with
docs.json beside it, validate.py refuses an artifact whose stated coverage
disagrees with what was collected. The diff's directory is the run directory,
which is where the artifact lands too.

The docs check asks whether any instruction document a coding agent reads --
AGENTS.md, CLAUDE.md, a README -- states something the pinned diff makes false.
Which documents that means is decided here, deterministically, so the subagent
that does the reading is handed a list rather than sent exploring: a checker
that picks its own inputs is a checker whose coverage nobody can state.

Two tiers. The repository root is checked for every name in ROOT_DOCS, because
root-level instruction files apply to the whole tree and therefore to any diff.
Directories on the path from a changed file up to the root are checked for
NESTED_DOCS only, because a nested AGENTS.md scopes to its subtree and one in
an untouched subtree has nothing to say about this diff. Exact names, exact
case: these conventions are literal file names agents look up, and a fuzzy
match would collect files no agent actually reads.

Exit 0 with an empty list is an answer -- a repository with no such documents
has nothing to check. Exit 2 is an operator error: a repo that is not a
directory, a diff that is not a file.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import diff_paths  # sibling modules, same directory
import validate

# Root-level names, in the order they are emitted: instruction files for coding
# agents first, then the human-facing files that double as one. GUIDE.md is the
# least conventional name here and earns its place by costing one stat call.
ROOT_DOCS = ("AGENTS.md", "CLAUDE.md", "GEMINI.md", "README.md", "CONTRIBUTING.md", "GUIDE.md")
# What a subtree can scope: agent instruction files and a README. The rest of
# ROOT_DOCS is a root-level convention only.
NESTED_DOCS = ("AGENTS.md", "CLAUDE.md", "README.md")

# The check reads documents into a prompt, and the diff can come from a hostile
# repository -- so a single enormous "CLAUDE.md" must not be able to flood the
# subagent's window. Per-file first, then a total across everything collected;
# what will not fit is named in `skipped` rather than silently absent, because
# the report states what was examined and that statement has to be honest.
FILE_CEILING = 128 * 1024
TOTAL_CEILING = 384 * 1024


def changed_paths(diff_path: str) -> set[str]:
    """Every repository-relative path the diff names, old side and new side.

    Both sides, because a deleted file's directory can still hold a document
    whose claims the deletion invalidates.

    The reading is diff_paths.file_headers, which scope.py shares: this used to
    be a second quote decoder and a second header walk, written here and again
    there, and the two drifted by construction. What that swap buys this caller
    is what it never had -- header/body state, so a diffed patch file's own body
    cannot pass a content line off as a header, and the four block shapes that
    name no side in `---`/`+++` at all, whose directories this walked straight
    past before.

    Streamed rather than read whole: the diff is the one input a reviewed
    repository controls the size of, and this reader has no reason to hold it.

    Leniency is unchanged and still deliberate: a name that will not decode
    contributes no path, and the cost is a nested document not collected --
    never a crash, and the root documents are collected regardless.
    """
    paths: set[str] = set()
    with open(diff_path, encoding="utf-8", errors="replace") as handle:
        for header in diff_paths.file_headers(handle):
            for path in (header.old, header.new):
                if path is not None:
                    paths.add(path)
    return paths


def candidate_paths(repo: str, diff_path: str) -> list[str]:
    """ROOT_DOCS at the root, NESTED_DOCS up every changed file's ancestry."""
    candidates = list(ROOT_DOCS)
    nested: set[str] = set()
    for changed in changed_paths(diff_path):
        directory = os.path.dirname(changed)
        while directory:
            for name in NESTED_DOCS:
                nested.add(os.path.join(directory, name))
            directory = os.path.dirname(directory)
    candidates.extend(sorted(nested))
    return candidates


def collect(repo: str, diff_path: str) -> dict[str, object]:
    """Build the docs/skipped lists for the parsed candidates.

    Confinement is validate.confine, the same function the validator confines a
    finding's location with: the realpath of every collected document must stay
    inside the repository, because a symlink named CLAUDE.md pointing at a file
    outside the checkout would otherwise have that file's contents quoted into
    the report. A document that escapes is skipped and named, not silently
    dropped -- the skip list is part of what the report states.

    The existence test stays ahead of the confinement one, which is the order
    this had before sharing the check and is the order the output depends on: a
    candidate that simply is not there is not a skip anybody needs told about,
    while one that is there and points out of the checkout is exactly that.
    """
    root = os.path.realpath(repo)
    docs: list[dict[str, str | int]] = []
    skipped: list[dict[str, str]] = []
    seen: dict[str, str] = {}
    total = 0
    for path in candidate_paths(repo, diff_path):
        absolute = os.path.join(root, path)
        if not os.path.isfile(absolute):
            continue
        resolved = validate.confine(root, path)
        if resolved is None:
            skipped.append({"path": path, "reason": "resolves outside the repository"})
            continue
        # CLAUDE.md as a symlink to AGENTS.md is the convention this repo's own
        # README recommends, and collecting both would hand the checker the
        # same text twice under two names. `seen` records only what was
        # actually collected -- see the assignment below the ceilings -- so an
        # alias of a document the ceilings refused is re-checked and gets the
        # ceiling's own reason, never a claim that something was collected.
        if resolved in seen:
            skipped.append(
                {
                    "path": path,
                    "reason": f"the same file as {seen[resolved]}, already collected",
                }
            )
            continue
        size = os.path.getsize(absolute)
        if size > FILE_CEILING:
            skipped.append(
                {
                    "path": path,
                    "reason": f"larger than the {FILE_CEILING:,}-byte per-file ceiling",
                }
            )
            continue
        if total + size > TOTAL_CEILING:
            skipped.append(
                {
                    "path": path,
                    "reason": f"would exceed the {TOTAL_CEILING:,}-byte total ceiling",
                }
            )
            continue
        total += size
        docs.append({"path": path, "bytes": size})
        seen[resolved] = path
    return {"docs": docs, "skipped": skipped}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--diff", required=True)
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit:
        return 2

    repo = os.path.abspath(os.path.expanduser(args.repo))
    if not os.path.isdir(repo):
        sys.stderr.write(f"--repo {args.repo!r} is not a directory\n")
        return 2
    diff = os.path.abspath(os.path.expanduser(args.diff))
    if not os.path.isfile(diff):
        sys.stderr.write(f"--diff {args.diff!r} is not a file\n")
        return 2

    result = collect(repo, diff)
    printed = json.dumps(result, indent=2) + "\n"

    # One string, written twice: the file cannot drift from what was printed,
    # because there is nothing to drift -- the merge may copy either.
    manifest = os.path.join(os.path.dirname(diff), "docs.json")
    try:
        with open(manifest, "w", encoding="utf-8") as handle:
            handle.write(printed)
    except OSError as error:
        # Not fatal, and deliberately not an exit status: the collection is
        # what the check reads, and it is on stdout regardless. What the run
        # loses is the validator's cross-check of the artifact's coverage
        # claim against this file -- back to the retyping this replaced, so
        # say so rather than let the check quietly stop existing.
        sys.stderr.write(
            f"Warning: could not write docs.json: {error}.\nThe collection is unaffected and is "
            "printed as always -- what is lost is validate.py's\ncheck that the artifact states "
            "the coverage that was actually collected.\n"
        )

    sys.stdout.write(printed)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
