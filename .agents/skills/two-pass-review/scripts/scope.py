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
from datetime import datetime, timezone

LARGE_BYTES = 500_000
LARGE_FILES = 150


def git(repo, *arguments):
    """Raw bytes out.

    Git's output is not guaranteed to be UTF-8. `git diff` calls a file text on
    a heuristic, so a Latin-1 comment or a mis-encoded fixture arrives as bytes
    a strict decoder rejects -- and a strict decode here would kill the whole
    review in step one, on a repository doing nothing unusual.
    """
    result = subprocess.run(
        ["git", "-C", repo] + list(arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.returncode, result.stdout, result.stderr.decode("utf-8", "replace").strip()


def git_text(repo, *arguments):
    """Git output as text, with undecodable bytes replaced rather than fatal.

    The replacement character is the honest answer: it says "this byte was not
    text" in the one place a reviewer can see it, and it costs one character
    instead of one review.
    """
    code, out, error = git(repo, *arguments)
    return code, out.decode("utf-8", "replace"), error


def fail(message, status=4):
    sys.stderr.write("Cannot resolve the review scope: {}\n".format(message))
    return status


def resolve_commit(repo, revision):
    code, out, _ = git_text(repo, "rev-parse", "--verify", "--quiet", "{}^{{commit}}".format(revision))
    return out.strip() if code == 0 else None


def repo_slug(root):
    name = os.path.basename(os.path.abspath(root)) or "repo"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-").lower() or "repo"
    digest = hashlib.sha256(os.path.abspath(root).encode("utf-8")).hexdigest()[:8]
    return "{}-{}".format(slug, digest)


def make_private_dir(path):
    """Create one directory readable only by its owner. Returns an exit status.

    Call it on each component the tool creates, outermost first: the security of
    a path is the security of every component, and a private directory under a
    parent nobody checked is not private.

    This is modest housekeeping rather than a defence against a determined
    attacker -- anyone with a shell on the machine has easier targets than a
    code review. It earns its lines on a shared build host, where `gettempdir()`
    is the common `/tmp` and this path is derived from the repository's location
    and so is guessable.

    Every check runs against an open descriptor rather than the name, so what is
    inspected is what was opened. `O_NOFOLLOW` makes a planted symlink an error
    rather than something to detect and then act on separately.
    """
    created = False
    try:
        os.mkdir(path, 0o700)
        created = True
    except FileExistsError:
        pass
    except OSError as error:
        return fail("cannot create {}: {}".format(path, error))

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    except OSError:
        return fail("{} is a symlink or not a directory; refusing to write reports through it".format(path))
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid():
            return fail("{} is owned by another user; refusing to write reports into it".format(path))
        if created:
            # mkdir's mode is masked by the umask, so set it on the descriptor.
            os.fchmod(descriptor, 0o700)
        elif info.st_mode & 0o077:
            # Somebody chose this mode. Say so rather than silently undoing it.
            return fail(
                "{} is readable by other users. Reports are written here, so either "
                "`chmod 700` it or remove it and let this run recreate it".format(path)
            )
    finally:
        os.close(descriptor)
    return 0


def build_diff(repo, mode, base, head):
    """The patch both passes see. One pinned input is what makes them comparable."""
    if mode == "revisions":
        selector = ["{}..{}".format(base, head)]
    else:
        # Working tree against the base, which covers staged and unstaged alike.
        selector = [base]
    # Porcelain diff honours external diff drivers and textconv filters from
    # the checkout's own attributes and config -- arbitrary commands the
    # repository under review gets to choose, which can hang this step or
    # quietly rewrite the patch. Refusing both pins the diff to the bytes git
    # tracks, so what the passes read is the change and not a filter's account
    # of it.
    code, raw, error = git(repo, "diff", "--no-ext-diff", "--no-textconv", *selector)
    if code != 0:
        return None, error
    text = raw.decode("utf-8", "replace")

    # Counted from the patch itself rather than from a second `git diff`. Two
    # invocations run at different instants against a working tree the user may
    # still be editing, so a count taken from one and a patch pinned from the
    # other can disagree -- and a scope line contradicting its own context.diff
    # is the one thing this number must never do.
    files = sum(1 for line in text.split("\n") if line.startswith("diff --git "))

    # Files git has never been told about cannot appear in any diff. Reviewing
    # them is out of scope; counting them is not, because a review that skips
    # new code without saying so is the one thing a review must not do.
    untracked = None
    if mode == "local-patch":
        code, others, _ = git_text(repo, "ls-files", "--others", "--exclude-standard")
        untracked = len([line for line in others.splitlines() if line.strip()]) if code == 0 else None

    return {"text": text, "bytes": len(raw), "files": files, "untracked": untracked}, None


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
    code, root, _ = git_text(repo, "rev-parse", "--show-toplevel")
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

    patch, error = build_diff(root, args.mode, base, head)
    if patch is None:
        return fail(error or "git could not produce the diff")
    if not patch["files"]:
        if args.mode == "local-patch":
            return fail(
                "the working tree matches {} -- there is nothing to review. The {} untracked file(s) "
                "here are invisible to git diff; stage or commit them to bring them in".format(
                    args.base, patch["untracked"] if patch["untracked"] is not None else 0
                )
            )
        return fail("that range is empty -- there is nothing to review")

    if not args.confirm_large and (patch["bytes"] > LARGE_BYTES or patch["files"] > LARGE_FILES):
        sys.stderr.write(
            "This scope is large: {:,} files and {:,} bytes of diff.\n"
            "Ask the user whether to review it whole, then re-run with --confirm-large.\n"
            "It is never split: both passes must see one identical input or corroboration "
            "has nothing to compare.\n".format(patch["files"], patch["bytes"])
        )
        return 3

    temp_root = os.path.join(tempfile.gettempdir(), "two-pass-review")
    report_dir = os.path.join(temp_root, repo_slug(root))
    for directory in (temp_root, report_dir):
        status = make_private_dir(directory)
        if status:
            return status

    # mkdtemp creates the directory 0700 and guarantees it is new, so two runs
    # starting in the same second cannot share one and overwrite the diff the
    # other pinned.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-")
    run_dir = tempfile.mkdtemp(prefix=stamp, dir=report_dir)

    context = os.path.join(run_dir, "context.diff")
    with open(context, "w", encoding="utf-8") as handle:
        handle.write(patch["text"])

    scope = {
        "repo": os.path.basename(root),
        "mode": args.mode,
        "base": base,
        "head": head,
        "files_changed": patch["files"],
        "diff_bytes": patch["bytes"],
    }
    if patch["untracked"] is not None:
        scope["untracked"] = patch["untracked"]
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
