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
from typing import TypedDict, cast, overload

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import diff_paths  # sibling module, same directory
import validate

LARGE_BYTES = 500_000
LARGE_FILES = 150


# A memory-safety ceiling on captured git output, distinct from LARGE_BYTES:
# that one is a UX threshold that asks the reviewer to confirm a big diff; this
# is a backstop against a repository forcing a multi-gigabyte textual patch
# (a huge blob under --text) and exhausting the host before any measurement can
# run. No reviewable diff approaches it -- LARGE_BYTES gates real ones at half a
# megabyte -- so it only ever fires on the pathological case.
CAPTURE_CEILING = 256 * 1024 * 1024

# The manifest exists to save a pass one shell command, so no file is worth
# stalling the run over. LARGE_BYTES and CAPTURE_CEILING both measure the diff,
# and a one-line change to a huge tracked file is a tiny diff -- it passes both
# and then costs the whole file's bytes, before either pass has started. A file
# past this ceiling is recorded as null: exactly what the manifest already says
# about a file it cannot count, and the pass reads it, as it did before the
# manifest existed.
COUNT_CEILING = 8 * 1024 * 1024


class Patch(TypedDict):
    text: str
    bytes: int
    files: int
    untracked: int | None


@overload
def git(
    repo: str, *arguments: str, input: bytes | None = ..., max_bytes: None = ...
) -> tuple[int, bytes, str]: ...


@overload
def git(
    repo: str, *arguments: str, input: bytes | None = ..., max_bytes: int
) -> tuple[int, bytes | None, str]: ...


def git(
    repo: str, *arguments: str, input: bytes | None = None, max_bytes: int | None = None
) -> tuple[int, bytes | None, str]:
    """Raw bytes out.

    Git's output is not guaranteed to be UTF-8. `git diff` calls a file text on
    a heuristic, so a Latin-1 comment or a mis-encoded fixture arrives as bytes
    a strict decoder rejects -- and a strict decode here would kill the whole
    review in step one, on a repository doing nothing unusual.

    `input` is forwarded to subprocess for the one caller that pipes a payload
    to git -- check-attr --stdin -- so that caller stays on this helper rather
    than rebuilding the argument vector and its own error handling by hand.

    `max_bytes` bounds how much stdout is held in memory, for the one caller
    whose output size the reviewed repository controls -- the diff. Over the
    bound, git is killed and stdout comes back None, so build_diff can refuse
    rather than buffer the whole patch (and then a decoded copy) and risk the
    host. Left None everywhere else, where output is a ref, a file list or an
    attribute table and bounded by construction; those keep the plain path
    untouched. The two options do not combine -- the diff pipes no input.
    """
    if max_bytes is None:
        result = subprocess.run(
            ["git", "-C", repo, *arguments],
            input=input,
            capture_output=True,
        )
        return result.returncode, result.stdout, result.stderr.decode("utf-8", "replace").strip()

    # Read incrementally and stop the moment the ceiling is passed, so a hostile
    # patch cannot make this process grow without bound. stderr is drained only
    # after, which is safe because git's diff stderr is a few lines at most and
    # cannot fill its pipe while we read stdout.
    proc = subprocess.Popen(
        ["git", "-C", repo, *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    # Both pipes exist because both were asked for on the line above; the
    # assertion states that to the checker rather than guarding against it.
    assert proc.stdout is not None and proc.stderr is not None
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = proc.stdout.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            proc.kill()
            proc.wait()
            return cast(int, proc.returncode), None, ""
        chunks.append(chunk)
    error = proc.stderr.read().decode("utf-8", "replace").strip()
    proc.wait()
    return cast(int, proc.returncode), b"".join(chunks), error


def git_text(repo: str, *arguments: str) -> tuple[int, str, str]:
    """Git output as text, with undecodable bytes replaced rather than fatal.

    The replacement character is the honest answer: it says "this byte was not
    text" in the one place a reviewer can see it, and it costs one character
    instead of one review.
    """
    code, out, error = git(repo, *arguments)
    return code, out.decode("utf-8", "replace"), error


def fail(message: str, status: int = 4) -> int:
    sys.stderr.write(f"Cannot resolve the review scope: {message}\n")
    return status


def resolve_commit(repo: str, revision: str) -> str | None:
    code, out, _ = git_text(repo, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
    return out.strip() if code == 0 else None


def repo_slug(root: str) -> str:
    name = os.path.basename(os.path.abspath(root)) or "repo"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-").lower() or "repo"
    digest = hashlib.sha256(os.path.abspath(root).encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def make_private_dir(path: str) -> int:
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
        return fail(f"cannot create {path}: {error}")

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    except OSError:
        return fail(f"{path} is a symlink or not a directory; refusing to write reports through it")
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid():
            return fail(f"{path} is owned by another user; refusing to write reports into it")
        if created:
            # mkdir's mode is masked by the umask, so set it on the descriptor.
            os.fchmod(descriptor, 0o700)
        elif info.st_mode & 0o077:
            # Somebody chose this mode. Say so rather than silently undoing it.
            return fail(
                f"{path} is readable by other users. Reports are written here, so either "
                "`chmod 700` it or remove it and let this run recreate it"
            )
    finally:
        os.close(descriptor)
    return 0


def filter_overrides(repo: str) -> tuple[list[str] | None, str | None]:
    """(overrides, problem): one -c per configured filter driver, emptied.

    A .gitattributes line in the reviewed worktree selects a filter by name,
    but the command behind the name lives in config, which the checkout's
    author cannot write. Emptying every configured driver therefore closes the
    class: a name with no command behind it is a no-op, and required=false
    keeps the no-op from being an error. The realistic abuse needs no attacker
    config at all -- git-lfs registers a required process filter globally on
    most machines, and a hostile attributes file can point any path at it.

    An enumeration that fails leaves the overrides empty rather than failing
    the run: that is exactly today's behaviour, and config listing does not
    fail inside a repository that rev-parse already accepted.

    A name the -c syntax cannot express fails the run instead. -c splits its
    argument at the first equals sign, so a driver named with one -- legal in
    config, selectable from .gitattributes -- would take the override as a
    different variable and keep its command. GIT_CONFIG_KEY_n would express
    it, but older gits ignore those variables silently, which turns one
    unrepresentable name into no neutralization at all. No real tool names a
    filter that way, so refusing is a message to an attacker, not a user.
    """
    code, out, _ = git_text(repo, "config", "--list", "--null")
    names: set[str] = set()
    if code == 0:
        # --null ends each entry with NUL and splits key from value with the
        # first newline, so a value carrying either character cannot fake a key.
        for entry in out.split("\0"):
            key = entry.split("\n", 1)[0]
            if not key.startswith("filter."):
                continue
            name, _, attribute = key[len("filter.") :].rpartition(".")
            if name and attribute in ("clean", "smudge", "process", "required"):
                names.add(name)
    arguments: list[str] = []
    for name in sorted(names):
        if "=" in name:
            return None, (
                f"the configured git filter driver {name!r} has an equals sign in its name, "
                "which the -c override that keeps filters out of the review diff cannot "
                "express. Rename or remove that filter configuration and run again"
            )
        arguments += [
            "-c",
            f"filter.{name}.clean=",
            "-c",
            f"filter.{name}.process=",
            "-c",
            f"filter.{name}.required=false",
        ]
    return arguments, None


# The built-in attributes that rewrite worktree content on the way into a
# diff. Unlike filters these run no configured command, so the overrides above
# cannot reach them and there is no flag to refuse them; they are detected and
# the run stops instead. text/eol (CRLF) is deliberately absent: it is on
# nearly every repository, and the only change it hides is one of line endings,
# which the review does not examine -- listing it would refuse honest repos for
# no gain.
CONVERTING_ATTRS = ("ident", "working-tree-encoding")


def worktree_conversion_block(repo: str) -> str | None:
    """A message when a built-in conversion could hide a local change, else None.

    local-patch diffs the working tree, and git cleans each file through its
    attributes first, so a change inside an $Id$ span (ident) or under a
    working-tree-encoding transcoding cleans back to what HEAD holds -- the
    diff comes up empty and the run reports nothing to review while the payload
    sits on disk. Only local-patch is exposed: a revision range diffs blob to
    blob and never touches the working tree.

    Conservative by design: it refuses when the attribute is set on any tracked
    file, not only a changed one, because the concealment it guards against is
    the reason the file would not show as changed. The attribute is rare enough
    that the false refusal is cheaper than reading every candidate's bytes to
    narrow it, and the message points at the committed range that is immune.

    An enumeration that fails leaves the run to proceed rather than blocking on
    a git that could not answer: a non-zero exit here returns None, the same
    fail-toward-today's-behaviour as the filter enumeration, since the
    attribute is the rare case. A git binary too broken to run is not guarded
    for -- rev-parse at startup already proved it runnable, so like every other
    call in this file these two just assume it is.
    """
    code, files, _ = git(repo, "ls-files", "-z")
    if code != 0 or not files:
        return None
    code, out, _ = git(repo, "check-attr", "--stdin", "-z", *CONVERTING_ATTRS, input=files)
    if code != 0:
        return None
    # check-attr -z emits flat NUL-terminated triples: path, attribute, value.
    fields = out.split(b"\0")
    for index in range(0, len(fields) - 2, 3):
        value = fields[index + 2].decode("utf-8", "replace")
        if value not in ("unspecified", "unset"):
            return (
                "the file {!r} has the git attribute {!r} in effect, a built-in worktree "
                "conversion that can hide an uncommitted change from a local-patch diff. "
                "Review a committed range instead -- it diffs the stored bytes and does not "
                "convert".format(
                    fields[index].decode("utf-8", "replace"),
                    fields[index + 1].decode("utf-8", "replace"),
                )
            )
    return None


def build_diff(
    repo: str, mode: str, base: str, head: str | None
) -> tuple[Patch | None, str | None]:
    """The patch both passes see. One pinned input is what makes them comparable."""
    # Two axes of repository-controlled conversion, and they divide by data
    # source. The `diff` attribute -- custom diff driver, textconv, or `-diff`
    # marking source as binary -- governs how any diff is *rendered*, so it
    # reaches a blob-to-blob revision range as much as a worktree one; the
    # flags on the diff call below refuse all three of its forms and belong on
    # both modes. Clean/process filters and the built-in worktree conversions
    # only run when a diff *reads the working tree*, so their neutralization is
    # local-patch's alone -- applying it to a revision range would make blob
    # comparison depend on worktree filter config it never invokes, and refuse
    # a range over an unrepresentable filter name that could not matter.
    overrides: list[str] | None
    if mode == "revisions":
        selector = [f"{base}..{head}"]
        overrides = []
    else:
        # Working tree against the base, which covers staged and unstaged alike.
        selector = [base]
        problem = worktree_conversion_block(repo)
        if problem:
            return None, problem
        overrides, problem = filter_overrides(repo)
        if problem:
            return None, problem
        # filter_overrides returns the list or the problem, never neither, and
        # the problem returned above. Stated for the checker, which cannot see
        # that the two returns are exclusive.
        assert overrides is not None
    # --text forces textual diffing so a `-diff` attribute cannot pin a changed
    # source file as "Binary files differ" and withhold every line from the
    # passes; a genuine binary renders as text rather than as a hidden change,
    # which for a review is the safe direction. --no-ext-diff and --no-textconv
    # refuse the attribute's other two forms. --ignore-submodules=none overrides
    # an `ignore = all` in .gitmodules or config, under which a changed submodule
    # gitlink -- a pointer to whole other-repo commits -- would drop out of the
    # patch silently; it is a no-op wherever no such suppression is configured.
    # The two prefix options pin the a/ and b/ that every reader of a unified
    # diff expects, against three configs that change them: diff.noprefix drops
    # them, diff.srcPrefix and diff.dstPrefix replace them, and
    # diff.mnemonicPrefix swaps in a letter per side -- w/ for the worktree,
    # which is what a local patch diffs. None of that is the reviewed
    # repository's doing, which is why it is easy to miss: the config is the
    # user's, so the diff a report pins would depend on the machine it ran on.
    # Together with the worktree neutralization above, the patch is the bytes as
    # they sit in git and on disk, not a repository-chosen account of them.
    code, raw, error = git(
        repo,
        *overrides,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--text",
        "--ignore-submodules=none",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        *selector,
        max_bytes=CAPTURE_CEILING,
    )
    if raw is None:
        return None, (
            f"the diff exceeded {CAPTURE_CEILING} bytes and capture was stopped before it could exhaust memory. "
            "This usually means a large binary was forced to a textual patch; review a committed "
            "range or narrow the scope"
        )
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
    untracked: int | None = None
    if mode == "local-patch":
        code, others, _ = git_text(repo, "ls-files", "--others", "--exclude-standard")
        untracked = (
            len([line for line in others.splitlines() if line.strip()]) if code == 0 else None
        )

    return {"text": text, "bytes": len(raw), "files": files, "untracked": untracked}, None


def file_lines(root: str, paths: list[str]) -> dict[str, int | None]:
    """path -> its line count on disk, or null where the checkout holds no file.

    Confinement is validate.confine, called rather than copied: this manifest is
    a prediction of what that validator will say, so a path the two resolve
    differently is the one path it must not carry a number for, and two
    implementations of "inside the checkout" is how that happens.

    Null is not padding: it says the checkout holds no readable file at a path
    the patch's post-image named, so a range over it would be rejected however
    it was arrived at. A file the diff deletes is not this case and gets no
    entry at all -- git writes its post-image as /dev/null, and the patch the
    pass is reading says so on the same line.
    """
    counts: dict[str, int | None] = {}
    for path in paths:
        target = validate.confine(root, path)
        if target is None or not os.path.isfile(target):
            counts[path] = None
            continue
        try:
            if os.path.getsize(target) > COUNT_CEILING:
                counts[path] = None
                continue
            counts[path] = validate.line_count(target)
        except OSError:
            # An unreadable file is one the pass will find unreadable too. The
            # manifest says so and the run continues; a scope that dies here
            # would cost the whole review one permission bit.
            counts[path] = None
    return counts


def main(argv: list[str]) -> int:
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
        return fail(f"{repo} is not a directory")
    code, root, _ = git_text(repo, "rev-parse", "--show-toplevel")
    if code != 0:
        return fail(f"{repo} is not inside a git repository")
    root = root.strip()

    if args.mode == "revisions" and not args.head:
        return fail("--head is required under scope mode 'revisions'", 2)
    if args.mode == "local-patch" and args.head:
        return fail("--head does not apply to a local working patch", 2)

    base = resolve_commit(root, args.base)
    if base is None:
        return fail(f"{args.base!r} does not name a commit in this repository")
    head = None
    if args.mode == "revisions":
        head = resolve_commit(root, args.head)
        if head is None:
            return fail(f"{args.head!r} does not name a commit in this repository")

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

    # One instant, formatted twice: the directory prefix, and the `now` printed
    # below for the artifact's `generated_at`. Taking it once means the report's
    # stamp and its run directory can never name different seconds.
    pinned_at = datetime.now(timezone.utc)

    # mkdtemp creates the directory 0700 and guarantees it is new, so two runs
    # starting in the same second cannot share one and overwrite the diff the
    # other pinned.
    stamp = pinned_at.strftime("%Y%m%d-%H%M%S-")
    run_dir = tempfile.mkdtemp(prefix=stamp, dir=report_dir)

    context = os.path.join(run_dir, "context.diff")
    with open(context, "w", encoding="utf-8") as handle:
        handle.write(patch["text"])

    headers = diff_paths.file_headers(patch["text"].split("\n"))
    named = [header.new for header in headers if header.new is not None]
    omitted = sum(1 for header in headers if header.deleted)
    lines_path = os.path.join(run_dir, "file_lines.json")
    with open(lines_path, "w", encoding="utf-8") as handle:
        json.dump(file_lines(root, named), handle, indent=2, sort_keys=True)

    # Every file header is one path or one deliberate omission, so anything left
    # over is a header this parser did not understand. Say so. The alternative is
    # what happened three times in review: a manifest quietly short of the diff it
    # describes, with the scope line beside it claiming the full count. Not fatal,
    # and deliberately not an exit status -- the passes read those files for
    # themselves, exactly as they did before the manifest existed.
    unread = patch["files"] - len(named) - omitted
    if unread:
        sys.stderr.write(
            "Warning: {} of {} file header(s) in the diff named no path this could read, so "
            "file_lines.json is that many entries short.\nThe review is unaffected -- those "
            "files are counted by whoever cites them -- but the gap is a parser bug worth "
            "reporting.\n".format(unread, patch["files"])
        )

    scope: dict[str, str | int | None] = {
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
            # The artifact's `generated_at`, so the merge has a clock without
            # asking a shell for one. It sits outside `scope` deliberately: the
            # validator closes that object's field set, and this is not a fact
            # about the range.
            "now": pinned_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "context_diff": context,
            "file_lines": lines_path,
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
