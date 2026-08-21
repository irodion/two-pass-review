#!/usr/bin/env python3
"""Collect the agent-facing documents the docs check reads. Stdlib only, 3.10 syntax.

Usage:
    collect_docs.py --repo PATH --diff CONTEXT_DIFF

Prints JSON:

    {"docs": [{"path": ..., "bytes": ...}], "skipped": [{"path": ..., "reason": ...}]}

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


def unquote(token):
    """Undo git's C-style quoting of unusual paths.

    git writes `"b/\\303\\244.md"` for a path it considers unusual. Decoded at
    the byte level, because the octal escapes are UTF-8 bytes, not characters.
    A token git did not quote passes through untouched.
    """
    if not (len(token) >= 2 and token.startswith('"') and token.endswith('"')):
        return token
    out = bytearray()
    body = token[1:-1]
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.extend(char.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(body):
            break
        escape = body[index]
        if escape in ("\\", '"'):
            out.append(ord(escape))
            index += 1
        elif escape == "t":
            out.append(9)
            index += 1
        elif escape == "n":
            out.append(10)
            index += 1
        elif escape.isdigit():
            octal = body[index : index + 3]
            out.append(int(octal, 8) & 0xFF)
            index += len(octal)
        else:
            out.extend(("\\" + escape).encode("utf-8"))
            index += 1
    return out.decode("utf-8", "replace")


def changed_paths(diff_path):
    """Every repository-relative path the diff names, old side and new side.

    Both sides, because a deleted file's directory can still hold a document
    whose claims the deletion invalidates. The `rename` lines cover the one
    case a 100%-similarity rename leaves no ---/+++ pair for. Header lines are
    parsed leniently: a line this function cannot read contributes no path,
    and the cost is a nested document not collected -- never a crash, and the
    root documents are collected regardless.
    """
    paths = set()
    with open(diff_path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            token = None
            for prefix in ("--- ", "+++ "):
                if line.startswith(prefix):
                    token = line[len(prefix) :].rstrip("\n").rstrip("\t")
            for prefix in ("rename from ", "rename to "):
                if line.startswith(prefix):
                    paths.add(unquote(line[len(prefix) :].rstrip("\n")))
            if token is None:
                continue
            token = unquote(token)
            if token == "/dev/null":
                continue
            # Standard git prefixes only. scope.py generates the diff and does
            # not pass --no-prefix, so a token shaped any other way is a header
            # this parser misread -- dropped, per the leniency above.
            if token.startswith(("a/", "b/")):
                paths.add(token[2:])
    return paths


def candidate_paths(repo, diff_path):
    """ROOT_DOCS at the root, NESTED_DOCS up every changed file's ancestry."""
    candidates = list(ROOT_DOCS)
    nested = set()
    for changed in changed_paths(diff_path):
        directory = os.path.dirname(changed)
        while directory:
            for name in NESTED_DOCS:
                nested.add(os.path.join(directory, name))
            directory = os.path.dirname(directory)
    candidates.extend(sorted(nested))
    return candidates


def collect(repo, diff_path):
    """Build the docs/skipped lists for the parsed candidates.

    Confinement mirrors validate.py: the realpath of every collected document
    must stay inside the repository, because a symlink named CLAUDE.md pointing
    at a file outside the checkout would otherwise have that file's contents
    quoted into the report. A document that escapes is skipped and named, not
    silently dropped -- the skip list is part of what the report states.
    """
    root = os.path.realpath(repo)
    docs = []
    skipped = []
    seen = {}
    total = 0
    for path in candidate_paths(repo, diff_path):
        absolute = os.path.join(root, path)
        if not os.path.isfile(absolute):
            continue
        resolved = os.path.realpath(absolute)
        if not resolved.startswith(root + os.sep):
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


def main(argv):
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
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
