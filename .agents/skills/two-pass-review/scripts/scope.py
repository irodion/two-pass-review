#!/usr/bin/env python3
"""Resolve the review scope, pin it to disk, and author the artifact's scope block.

Usage:
    scope.py --repo PATH --base REV --mode revisions --head REV
    scope.py --repo PATH --base REV --mode local-patch
    scope.py --release RUN_DIR

`--base` is required and never guessed. A tool that cannot guess a base cannot
be wrong about one -- so where the request does not determine the range, the
model asks the user rather than inferring a default. There is no auto-detection
ladder here, and no `main` fallback.

Both revision modes take resolved-or-symbolic revisions and record the resolved
SHAs, because a report saying `main...HEAD` is ambiguous the moment `main` moves.

Prints a JSON object describing the run to stdout, and writes the same bytes to
scope.json in the run directory, where merge.py reads them. The run directory
sits in `.two-pass-review/` at the top of the checkout; see reports_dir. Its
`repo_root` is the tree the rest of the run reads -- the checkout, or a
worktree at the reviewed head when the checkout holds something else; see
review_tree. `--release` undoes that at the end of the run; see release. Flags
are internal surface, invoked by SKILL.md; natural language is what the user
types.

Exit status: 0 resolved, 2 bad invocation, 3 needs confirmation, 4 unusable scope.
`--release` exits 0 when nothing of the run's tree is left, and 4 when git
could not remove it.
"""

import argparse
import json
import os
import re
import shutil
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


# Where runs are kept: one directory at the top of the user's checkout, holding
# a run directory per run and the stable latest.html. See reports_dir.
REPORTS_DIR = ".two-pass-review"

# The whole of what makes REPORTS_DIR invisible to git. A `.gitignore` applies
# to its own directory, and `*` matches the file itself too, so nothing under
# REPORTS_DIR is ever untracked, staged, or diffed.
SELF_IGNORE = "# Written by two-pass-review. Review runs stay on this machine.\n*\n"

# How many runs REPORTS_DIR keeps; see prune.
KEEP_RUNS = 20

# A run directory's name begins with the second it was pinned, written by
# new_run and read back by prune, so name order is age order. The pattern
# matches only what the stamp writes -- mkdtemp's suffix after it is not this
# file's to describe -- and nothing else in REPORTS_DIR begins that way.
RUN_STAMP = "%Y%m%d-%H%M%S-"
RUN_NAME = re.compile(r"\d{8}-\d{6}-")


def reports_dir(root: str) -> tuple[str | None, str | None]:
    """(path, problem): the directory in the checkout that holds every run.

    In the checkout rather than the temp directory, because temp is swept: on
    macOS every file there not touched for about three days is deleted, and a
    run has to outlive that -- it is re-rendered from its artifact, asked for
    rule suggestions later, and used as the input for render-and-diff checks.
    A run is also a directory the passes write into, and the checkout is where
    every agent's sandbox lets them write. A cache in the home directory, the
    other obvious place, fails that second test.

    It ignores itself. The repository's own `.gitignore` is never touched, which
    was the objection that first put runs in temp: an edit to it would show up in
    the diff of the next review. SELF_IGNORE lives inside the directory instead,
    the way pytest, mypy and ruff keep their caches out of git, so a run never
    appears in `git status`, in a local patch, or in its untracked count.

    A repository that tracks anything under the path is refused, because there
    the self-ignore cannot hold -- git never ignores a tracked file -- and runs
    would land among the reviewed code's own files. A symlink or a file at the
    path is refused as make_private_dir refuses one: `O_NOFOLLOW` makes it an
    error rather than something to detect and then act on separately.

    Deliberately not private the way the review tree's directory is. Those checks
    exist for a shared `/tmp`; here the checkout's own permissions already decide
    who can read the code, and a report is no more private than the code it
    quotes. Each run directory is still created 0700, by mkdtemp.
    """
    path = os.path.join(root, REPORTS_DIR)
    code, tracked, _ = git_text(root, "ls-files", "-z", "--", REPORTS_DIR)
    if code != 0:
        return None, f"could not check whether this repository tracks anything under {REPORTS_DIR}/"
    if tracked:
        return None, (
            f"this repository tracks files under {REPORTS_DIR}/, which is where review runs are "
            "written. Runs there would show up as changes to those files, so move or untrack them "
            "first"
        )
    try:
        os.mkdir(path)
    except FileExistsError:
        pass
    except OSError as error:
        return None, f"cannot create {path}: {error}"
    try:
        os.close(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY))
    except OSError:
        return None, f"{path} is a symlink or not a directory; refusing to write runs through it"
    # O_EXCL: a .gitignore already here was written by an earlier run, or by the
    # user on purpose, and either way it is not this run's to replace.
    try:
        descriptor = os.open(
            os.path.join(path, ".gitignore"),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
        )
    except FileExistsError:
        return path, None
    except OSError as error:
        return None, f"cannot write {path}/.gitignore: {error}"
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(SELF_IGNORE)
    return path, None


def new_run(reports: str, pinned_at: datetime) -> str:
    """Make this run's directory, after pruning room for it. Returns its path.

    The one place a run's name is written, a few lines from RUN_NAME, which
    reads it: prune is right only while the two agree, and it is silent when
    they do not. mkdtemp creates the directory 0700 and guarantees it is new,
    so two runs starting in the same second cannot share one and overwrite the
    diff the other pinned. Pruning first means the run being made can never be
    among what is pruned.
    """
    prune(reports, KEEP_RUNS - 1)
    return tempfile.mkdtemp(prefix=pinned_at.strftime(RUN_STAMP), dir=reports)


def prune(reports: str, keep: int) -> None:
    """Remove all but the newest `keep` runs. Best effort, and silent.

    Runs moved out of temp so that they would last, and without this they would
    last for ever, a full copy of every diff reviewed. Twenty is far more than
    re-rendering or rule derivation ever reaches back for.

    Only directories named like a run are candidates, so latest.html, the
    .gitignore and anything the user put here are never touched. A run whose
    review tree still stands is kept however old it is: its scope.json is the
    only record of the worktree registered in the user's repository, and
    `--release` needs it.

    Silent, because the orchestrator reports anything on stderr as a warning
    about the review, and failing to delete an old run does not weaken this one.
    """
    try:
        names = sorted(name for name in os.listdir(reports) if RUN_NAME.match(name))
    except OSError:
        return
    for name in names[:-keep]:
        path = os.path.join(reports, name)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        try:
            with open(os.path.join(path, "scope.json"), encoding="utf-8") as handle:
                worktree = json.load(handle).get("worktree")
        except (OSError, ValueError, AttributeError):
            worktree = None
        if isinstance(worktree, str) and os.path.exists(worktree):
            continue
        shutil.rmtree(path, ignore_errors=True)


def filter_overrides(repo: str) -> tuple[list[str] | None, str | None]:
    """(overrides, problem): one -c per configured filter driver, emptied.

    A .gitattributes line in the reviewed worktree selects a filter by name,
    but the command behind the name lives in config, which the checkout's
    author cannot write. Emptying every configured driver therefore closes the
    class: a name with no command behind it is a no-op, and required=false
    keeps the no-op from being an error. The realistic abuse needs no attacker
    config at all -- git-lfs registers a required process filter globally on
    most machines, and a hostile attributes file can point any path at it.

    Both directions are emptied. A local-patch diff reads the working tree and
    runs clean; the review tree is a checkout and runs smudge, where git-lfs
    would otherwise go to the network for every pointer the reviewed head
    names. Emptied, a pointer stays a pointer -- which is also what the diff
    shows the passes, so the tree and the patch agree about those files.

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
            f"filter.{name}.smudge=",
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


def checkout_holds(root: str, head: str, overrides: list[str]) -> bool:
    """Whether the checkout at `root` is exactly `head`, with no tracked change.

    A content comparison, not a stat one: diff-index alone reports a file as
    changed whenever its stat information is stale -- touched by an editor or
    a build without being edited -- and a checkout that is merely stat-stale
    would cost a full worktree. Comparing content runs the clean filter, which
    is why the emptied overrides go in front of it, exactly as they do for a
    local-patch diff.

    Untracked files do not count: head does not contain them, and a pass has
    no reason to open a file the diff and the code around it never name.
    Anything but a clean answer -- a difference, or a git that could not say
    -- reads as "not head", the direction that costs a worktree rather than a
    review of the wrong code.
    """
    code, out, _ = git_text(root, "rev-parse", "--verify", "--quiet", "HEAD")
    if code != 0 or out.strip() != head:
        return False
    code, _, _ = git(
        root, *overrides, "diff", "--quiet", "--no-ext-diff", "--no-textconv", "HEAD", "--"
    )
    return code == 0


# The directory in temp that holds every review tree; see trees_dir.
TREES_DIR = "two-pass-review-trees"


def trees_dir() -> str:
    """Where review trees are checked out: temp, not the checkout.

    The opposite choice from reports_dir, for the opposite reason. A review tree
    lasts one run and `--release` removes it, so the sweep that makes temp wrong
    for runs cannot touch one in use. And a full second copy of the source inside
    the user's checkout would be found by every tool that does not read
    .gitignore -- test runners, compilers, linters -- for as long as it stood,
    which on a run that never reached its release is indefinitely.

    Each tree is named exactly as its run directory, which mkdtemp made unique
    in the checkout. That one rule is how release recognises a tree, from its
    path alone, so no second spelling of it exists anywhere to drift.
    """
    return os.path.join(tempfile.gettempdir(), TREES_DIR)


def make_private_dir(path: str) -> str | None:
    """Create one directory readable only by its owner. Returns the problem, or None.

    This is modest housekeeping rather than a defence against a determined
    attacker -- anyone with a shell on the machine has easier targets than a
    code review. It earns its lines on a shared build host, where `gettempdir()`
    is the common `/tmp` and this path is fixed and so is guessable.

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
        return f"cannot create {path}: {error}"

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    except OSError:
        return f"{path} is a symlink or not a directory; refusing to check code out through it"
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid():
            return f"{path} is owned by another user; refusing to check code out into it"
        if created:
            # mkdir's mode is masked by the umask, so set it on the descriptor.
            os.fchmod(descriptor, 0o700)
        elif info.st_mode & 0o077:
            # Somebody chose this mode. Say so rather than silently undoing it.
            return (
                f"{path} is readable by other users. Review trees are checked out here, so "
                "either `chmod 700` it or remove it and let this run recreate it"
            )
    finally:
        os.close(descriptor)
    return None


def review_tree(root: str, head: str, run_dir: str) -> tuple[str | None, str | None]:
    """(worktree, problem): a detached worktree at `head`, when the passes need one.

    The diff is blob to blob, but the passes read files, file_lines.json counts
    files, and validate.py checks every line range against files -- all in
    whatever tree they are pointed at. Pointed at a checkout that holds some
    other commit, they review the wrong code: a pull request that is not
    checked out, a single commit that is not HEAD, or a branch carrying
    uncommitted edits. Nothing on the page would show it, and a correct
    finding can be refused for a range the reviewed file does contain.

    So when the checkout is not exactly `head`, the tree the run reads is a
    worktree checked out at `head` in trees_dir -- private, named after the
    run directory, and still a git checkout, so `git grep` works there. When
    the checkout is exactly `head`, it is read in place, which is the common
    case and costs nothing: a worktree is a full checkout, and on a large
    repository that is real time and disk. (None, None) means that. What in
    place gives up is isolation from edits made after this moment, while the
    passes run; this checks the checkout once, here.

    The checkout runs with every filter driver emptied, for filter_overrides'
    reason, and with hooks switched off. A post-checkout hook is the
    developer's own, but a review that runs `npm install` behind the user's
    back is not one they asked for. This is also where a filter name the -c
    override cannot express refuses a revision range, which the blob-to-blob
    diff alone never would: comparing the checkout and checking one out both
    run the filter.

    Symlinks are written as plain files holding their target, for the same
    reason a filtered pointer stays a pointer: it is what the diff shows. Live,
    a link the reviewed branch points at ~/.aws or anywhere else outside the
    checkout is one the passes are told to open -- and its contents can reach
    an excerpt in a report people forward. confine() already refuses such a
    link for the scripts; this makes the tree agree for the passes. Before the
    review tree existed, a branch that was not checked out was never on disk
    at all, so this is exposure the worktree would otherwise have added. A
    checkout read in place is the user's own, links and all, as before.
    """
    overrides, problem = filter_overrides(root)
    if problem:
        return None, problem
    # Stated for the checker, as in build_diff: the problem returned above.
    assert overrides is not None
    if checkout_holds(root, head, overrides):
        return None, None
    trees = trees_dir()
    problem = make_private_dir(trees)
    if problem:
        return None, problem
    # Named as the run, for trees_dir's reason. os.mkdir keeps what mkdtemp
    # would give: the directory is new, so removing it on a failed checkout
    # removes nothing of anyone else's, and it is 0700 from the start -- a umask
    # can take bits away, never add them. git checks out into an empty
    # directory that already exists.
    path = os.path.join(trees, os.path.basename(run_dir))
    try:
        os.mkdir(path, 0o700)
    except OSError as refused:
        return None, f"cannot create {path} for the review tree: {refused}"
    code, _, error = git(
        root,
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.symlinks=false",
        *overrides,
        "worktree",
        "add",
        "--detach",
        "--quiet",
        path,
        head,
    )
    if code != 0:
        shutil.rmtree(path, ignore_errors=True)
        return None, (
            f"could not check out {head} into a worktree for the passes to read: "
            f"{error or 'git worktree add failed'}. Check it out yourself and run again -- "
            "a checkout that already holds the reviewed head is read in place"
        )
    return path, None


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


def release(run_dir: str) -> int:
    """Remove the run's review tree, if it has one. Safe to run on any run.

    review_tree creates the worktree, so the script that made it owns
    removing it, and the orchestrator runs one command at the end of every
    run -- including one that ends early -- instead of testing `worktree` for
    null and composing a git command of its own. A run read in place has
    nothing to release and exits 0.

    It removes only a directory named exactly as the run directory, inside one
    named TREES_DIR, whatever scope.json says. The file names a path this
    script will hand to `git worktree remove --force`, and a path read from
    disk is not one to delete on trust -- not the user's own worktree, and not
    another run's tree. The check reads the path's shape and never trees_dir()
    itself: that reads TMPDIR in whichever process calls it, and a release run
    with a different TMPDIR from the scope.py call that made the tree would
    refuse the very tree it is for. git refuses, for its part, any path that is
    not a worktree of this checkout. git runs in the
    user's checkout, and removes the registration under its .git/worktrees
    along with the directory -- or the registration alone, when the directory
    was already deleted by hand. Nothing broader: `git worktree prune` would
    also drop the user's own worktrees on a drive that is not mounted.
    Releasing twice is not an error; the second finds nothing left.
    """

    def refuse(message: str, status: int) -> int:
        sys.stderr.write(f"Cannot release the review tree: {message}\n")
        return status

    run_dir = os.path.abspath(os.path.expanduser(run_dir))
    try:
        with open(os.path.join(run_dir, "scope.json"), encoding="utf-8") as handle:
            pinned = json.load(handle)
    except (OSError, ValueError):
        return refuse(f"{run_dir} holds no scope.json written by scope.py", 2)
    worktree = pinned.get("worktree") if isinstance(pinned, dict) else None
    checkout = pinned.get("checkout") if isinstance(pinned, dict) else None
    if worktree is None:
        return 0
    parent, name = (
        os.path.split(os.path.normpath(worktree)) if isinstance(worktree, str) else ("", "")
    )
    if (
        name != os.path.basename(run_dir)
        or os.path.basename(parent) != TREES_DIR
        or not isinstance(checkout, str)
    ):
        return refuse(f"scope.json in {run_dir} does not name this run's own review tree", 2)
    code, _, error = git(checkout, "worktree", "remove", "--force", worktree)
    if code != 0 and os.path.exists(worktree):
        return refuse(f"could not remove {worktree}: {error}", 4)
    return 0


def main(argv: list[str]) -> int:
    # A mode of its own rather than a flag among the others: it takes a run
    # directory and nothing else, and the scope flags are all required.
    if argv[1:2] == ["--release"]:
        if len(argv) != 3:
            sys.stderr.write("Cannot release the review tree: --release takes one run directory\n")
            return 2
        return release(argv[2])

    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--mode", required=True, choices=("revisions", "local-patch"))
    parser.add_argument("--head")
    parser.add_argument("--label")
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

    # Refused here rather than left for the validator, which would only see it
    # after both passes had run on a pinned diff: this is a bad invocation, and
    # the bad-invocation exit is what the caller can still act on. The rule is
    # validate.py's, called rather than restated, so the two cannot drift.
    if args.label is not None:
        problem = validate.label_problem(args.label)
        if problem:
            return fail(f"--label {problem}", 2)

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

    report_dir, problem = reports_dir(root)
    if problem:
        return fail(problem)
    # Stated for the checker: reports_dir returns a path or a problem.
    assert report_dir is not None

    # One instant, formatted twice: the directory prefix, and the `now` printed
    # below for the artifact's `generated_at`. Taking it once means the report's
    # stamp and its run directory can never name different seconds.
    pinned_at = datetime.now(timezone.utc)
    run_dir = new_run(report_dir, pinned_at)

    context = os.path.join(run_dir, "context.diff")
    with open(context, "w", encoding="utf-8") as handle:
        handle.write(patch["text"])

    # The tree every later step reads. `head` is set exactly when the scope
    # mode is 'revisions'; a local patch is the working tree by definition.
    tree = root
    worktree = None
    if head is not None:
        worktree, problem = review_tree(root, head, run_dir)
        if problem:
            return fail(problem)
        if worktree is not None:
            tree = worktree

    # From here until scope.json names the worktree and the run directory is
    # printed, nothing outside this process knows the tree exists. A failure in
    # between -- a full disk, an interrupt -- prints no run_dir for anyone to
    # release, and the worktree would stay registered in the user's repository.
    # So it is removed here, before the failure propagates.
    try:
        headers = diff_paths.file_headers(patch["text"].split("\n"))
        named = [header.new for header in headers if header.new is not None]
        omitted = sum(1 for header in headers if header.deleted)
        lines_path = os.path.join(run_dir, "file_lines.json")
        with open(lines_path, "w", encoding="utf-8") as handle:
            json.dump(file_lines(tree, named), handle, indent=2, sort_keys=True)

        # Every file header is one path or one deliberate omission, so anything left
        # over is a header this parser did not understand. Say so. The alternative is
        # what happened three times in review: a manifest quietly short of the diff it
        # describes, with the scope line beside it claiming the full count. Not fatal,
        # and deliberately not an exit status -- the passes read those files for
        # themselves, exactly as they did before the manifest existed.
        #
        # The subtraction is from build_diff's count, not from len(headers), and the
        # difference is the whole check. Both count `diff --git` lines, so the two
        # agree today; they are written separately so that a file_headers which one
        # day stops opening a block gets noticed. Subtract from len(headers) instead
        # and that parser is being asked to check itself: a block it never opened is
        # absent from both sides of the subtraction, the remainder is zero, and the
        # manifest goes quietly short -- which is the exact failure this line exists
        # to make loud. It only reads a header this parser opened and could not
        # resolve; it does not read one the parser walked past.
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
        }
        # Verbatim, and only when given. It says what the request *meant* -- "working
        # tree since 2026-08-25 00:00 +0300" -- which nothing else in this object
        # records: two runs of "changes made today" differed by 3.5x in files, and
        # only a reader who re-derives the git commands could see why. Nothing here
        # checks it against the range beside it, and nothing could: the resolution
        # happened in the conversation, above this script. It is declared
        # provenance, the page presents it as such, and the resolved base and head
        # remain the checkable record.
        if args.label is not None:
            scope["label"] = args.label
        scope["base"] = base
        scope["head"] = head
        scope["files_changed"] = patch["files"]
        scope["diff_bytes"] = patch["bytes"]
        if patch["untracked"] is not None:
            scope["untracked"] = patch["untracked"]

        printed = (
            json.dumps(
                {
                    # Every later --repo, and the tree the passes read. Absolute,
                    # because --repo here may have named a subdirectory, which this
                    # script resolves to the top of the checkout and the others take
                    # as given -- so a pass handed the directory the user started in
                    # would have correct locations refused as missing files.
                    "repo_root": tree,
                    # The worktree `--release` removes, or null when the checkout
                    # is read in place. See review_tree.
                    "worktree": worktree,
                    # The user's own checkout, which repo_root is not while a
                    # worktree stands and names nothing once it is released --
                    # so anything asked for after the run, like rule derivation,
                    # has a repository to be handed. And release's git runs here.
                    "checkout": root,
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
                indent=2,
            )
            + "\n"
        )
        # One string, written twice, as collect_docs.py does with docs.json: the
        # merge reads this file rather than a model retyping what was printed, and
        # there is nothing to drift between the two.
        with open(os.path.join(run_dir, "scope.json"), "w", encoding="utf-8") as handle:
            handle.write(printed)
        sys.stdout.write(printed)
    except BaseException:
        if worktree is not None:
            git(root, "worktree", "remove", "--force", worktree)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
