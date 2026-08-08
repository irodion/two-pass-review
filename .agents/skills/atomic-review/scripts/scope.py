#!/usr/bin/env python3
"""Resolve the review scope, pin it to disk, and author the artifact's scope block.

Usage:
    scope.py --repo PATH --base REV --mode revisions --head REV
    scope.py --repo PATH --base REV --mode local-patch

`--base` is required and never guessed. A tool that cannot guess a base cannot
be wrong about one -- so where the request does not determine the range, the
model asks the user rather than inferring a default. There is no auto-detection
ladder here, and no `main` fallback.

Both revision modes take resolved-or-symbolic revisions and record the resolved
SHAs, because a report saying `main...HEAD` is ambiguous the moment `main` moves.

Prints a JSON object describing the run to stdout. Flags are internal surface,
invoked by SKILL.md; natural language is what the user types.

Exit status: 0 resolved, 2 bad invocation, 3 needs confirmation, 4 unusable scope.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime

LARGE_BYTES = 500_000
LARGE_FILES = 150


def git(repo, *arguments):
    result = subprocess.run(
        ["git", "-C", repo] + list(arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    return result.returncode, result.stdout, result.stderr.strip()


def fail(message, status=4):
    sys.stderr.write("Cannot resolve the review scope: {}\n".format(message))
    return status


def resolve_commit(repo, revision):
    code, out, _ = git(repo, "rev-parse", "--verify", "--quiet", "{}^{{commit}}".format(revision))
    return out.strip() if code == 0 else None


def repo_slug(root):
    name = os.path.basename(os.path.abspath(root)) or "repo"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-").lower() or "repo"
    digest = hashlib.sha256(os.path.abspath(root).encode("utf-8")).hexdigest()[:8]
    return "{}-{}".format(slug, digest)


def build_diff(repo, mode, base, head):
    """The patch both passes see. One pinned input is what makes them comparable."""
    if mode == "revisions":
        selector = ["{}..{}".format(base, head)]
    else:
        # Working tree against the base, which covers staged and unstaged alike.
        # Untracked files are absent -- git has nothing to diff them against, and
        # the worked example does not reach for one either.
        selector = [base]
    code, diff, error = git(repo, "diff", *selector)
    if code != 0:
        return None, None, error
    code, names, error = git(repo, "diff", "--name-only", *selector)
    if code != 0:
        return None, None, error
    files = [line for line in names.splitlines() if line.strip()]
    return diff, files, None


def main(argv):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--mode", required=True, choices=("revisions", "local-patch"))
    parser.add_argument("--head")
    parser.add_argument("--confirm-large", action="store_true")
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit:
        return 2

    repo = os.path.abspath(os.path.expanduser(args.repo))
    if not os.path.isdir(repo):
        return fail("{} is not a directory".format(repo))
    code, root, _ = git(repo, "rev-parse", "--show-toplevel")
    if code != 0:
        return fail("{} is not inside a git repository".format(repo))
    root = root.strip()

    if args.mode == "revisions" and not args.head:
        return fail("--head is required under scope mode 'revisions'", 2)
    if args.mode == "local-patch" and args.head:
        return fail("--head does not apply to a local working patch", 2)

    base = resolve_commit(root, args.base)
    if base is None:
        return fail("{!r} does not name a commit in this repository".format(args.base))
    head = None
    if args.mode == "revisions":
        head = resolve_commit(root, args.head)
        if head is None:
            return fail("{!r} does not name a commit in this repository".format(args.head))

    diff, files, error = build_diff(root, args.mode, base, head)
    if diff is None:
        return fail(error or "git could not produce the diff")
    if not files:
        if args.mode == "local-patch":
            return fail(
                "the working tree matches {} -- there is nothing to review. Files that have never "
                "been added are invisible to git diff; stage or commit them to bring them in".format(args.base)
            )
        return fail("that range is empty -- there is nothing to review")

    diff_bytes = len(diff.encode("utf-8"))
    if not args.confirm_large and (diff_bytes > LARGE_BYTES or len(files) > LARGE_FILES):
        sys.stderr.write(
            "This scope is large: {:,} files and {:,} bytes of diff.\n"
            "Ask the user whether to review it whole, then re-run with --confirm-large.\n"
            "It is never split: both passes must see one identical input or corroboration "
            "has nothing to compare.\n".format(len(files), diff_bytes)
        )
        return 3

    report_dir = os.path.join(tempfile.gettempdir(), "atomic-review", repo_slug(root))
    run_dir = os.path.join(report_dir, datetime.utcnow().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(run_dir, exist_ok=True)

    context = os.path.join(run_dir, "context.diff")
    with open(context, "w", encoding="utf-8") as handle:
        handle.write(diff)

    scope = {
        "repo": os.path.basename(root),
        "mode": args.mode,
        "base": base,
        "head": head,
        "files_changed": len(files),
        "diff_bytes": diff_bytes,
    }
    with open(os.path.join(run_dir, "scope.json"), "w", encoding="utf-8") as handle:
        json.dump(scope, handle, indent=2)

    json.dump(
        {
            "run_dir": run_dir,
            "report_dir": report_dir,
            "context_diff": context,
            "latest": os.path.join(report_dir, "latest.html"),
            "scope": scope,
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
