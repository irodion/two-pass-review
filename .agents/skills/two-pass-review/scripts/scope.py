#!/usr/bin/env python3
"""Resolve the review scope, pin it to disk, and author the artifact's scope block.

Usage:
    scope.py --repo PATH (--base REV | --against REV | --since TIME) --mode revisions --head REV
    scope.py --repo PATH (--base REV | --against REV | --since TIME) --mode local-patch
    scope.py --release RUN_DIR

A base is required and never guessed. A tool that cannot guess a base cannot
be wrong about one -- so where the request does not determine the range, the
model asks the user rather than inferring a default. There is no auto-detection
ladder here, and no `main` fallback. `--against` and `--since` are not
defaults either: each takes what the user decided -- the branch the work
merges into, an instant with its UTC offset -- and only does the git that
turns it into a commit.

Both revision modes take resolved-or-symbolic revisions and record the resolved
SHAs, because a report saying `main...HEAD` is ambiguous the moment `main` moves.

Prints a JSON object describing the run to stdout, and writes the same bytes to
scope.json in the run directory, where merge.py reads them. The run directory
sits in `.two-pass-review/` at the top of the checkout; see runs.reports_dir. Its
`repo_root` is the tree the rest of the run reads -- the checkout, or a
worktree at the reviewed head when the checkout holds something else; see
review_tree in trees.py. `--release` undoes that at the end of the run, and
after a rule derivation, which makes the tree again; see release in runs.py.
Flags are internal surface, invoked by SKILL.md; natural language is what the
user types.

Once the diff is pinned it also lints the changed files at the base and at the
head, with whichever of ruff, Biome and shellcheck is installed and this skill's
own rules, and keeps what the change introduced, for the hint check; see
hints.py. `--no-hints` skips that, and scope.json records which happened.

Exit status: 0 resolved, 2 bad invocation, 3 needs confirmation, 4 unusable scope,
5 the skill's own references/prompts.md cannot be filled.
`--release` exits 0 when nothing of the run's tree is left, and 4 when git
could not remove it.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime, timezone
from typing import TypedDict, cast

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import diff_paths  # sibling module, same directory
import hints
import prompts
import validate
from gitrun import git, git_text, toplevel
from runs import new_run, release, reports_dir, write_record
from trees import HEAD_ADVICE, discard, filter_overrides, review_tree, snapshot

LARGE_BYTES = 500_000
LARGE_FILES = 150


# A memory-safety ceiling on captured git output, distinct from LARGE_BYTES:
# that one is a UX threshold that asks the reviewer to confirm a big diff; this
# is a backstop against a repository forcing a multi-gigabyte textual patch
# (a huge blob under --text) and exhausting the host before any measurement can
# run. No reviewable diff approaches it -- LARGE_BYTES gates real ones at half a
# megabyte -- so it only ever fires on the pathological case. git_capped
# enforces it.
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


def git_capped(repo: str, *arguments: str, max_bytes: int) -> tuple[int, bytes | None, str]:
    """gitrun.git, holding at most `max_bytes` of stdout in memory.

    For the one call whose output size the reviewed repository controls -- the
    diff. Over the bound, git is killed and stdout comes back None, so
    build_diff can refuse rather than buffer the whole patch (and then a
    decoded copy) and risk the host. Every other call's output is a ref, a file
    list or an attribute table, bounded by construction, and goes through
    gitrun.git untouched.
    """
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


def fail(message: str, status: int = 4) -> int:
    sys.stderr.write(f"Cannot resolve the review scope: {message}\n")
    return status


def resolve_commit(repo: str, revision: str) -> str | None:
    code, out, _ = git_text(repo, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
    return out.strip() if code == 0 else None


# What --since accepts: a date, a time and a UTC offset, with nothing left to
# infer. The offset is the point. Which timezone "today" means is the user's
# answer, and a time without one would be read in this machine's zone -- the
# guess #45 kept out of this script, and still keeps out: refused, never
# defaulted. `Z` is accepted because it is an answer, UTC said outright.
# Written out rather than handed to datetime.fromisoformat, which accepts more
# on 3.11 than on 3.10, so the floor and a modern interpreter would disagree
# about what a valid invocation is.
SINCE_CLOCK = r"(?P<date>\d{4}-\d{2}-\d{2})[T ](?P<time>\d{2}:\d{2}(?::\d{2})?)"
SINCE_OFFSET = r"(?P<offset>Z|[+-]\d{2}:?\d{2})"
SINCE = re.compile(SINCE_CLOCK + " ?" + SINCE_OFFSET)

# The shape, never an example. A literal-copying caller handed an example
# offset fixes its command by copying it, which is the timezone guess this
# flag exists to refuse.
SINCE_SHAPE = "YYYY-MM-DDTHH:MM+HH:MM, with the user's own offset"


def parse_since(text: str) -> tuple[datetime | None, str | None]:
    """The instant --since names, or why it names none.

    Anything that does not end in an offset -- a bare date as much as a date
    and a time -- is refused as offsetless, so every refusal that could be
    "fixed" by inventing a timezone says to ask instead.
    """
    match = SINCE.fullmatch(text)
    if match is None:
        if not re.search(SINCE_OFFSET + "$", text):
            return None, (
                f"{text!r} has no UTC offset. Which timezone the date means is the user's answer "
                f"-- ask them, never infer it -- and pass it as {SINCE_SHAPE}"
            )
        return None, f"{text!r} is not a date, a time and a UTC offset, written {SINCE_SHAPE}"
    # RFC 3339 spells "the offset is unknown" -00:00, and %z would read it as
    # UTC -- an unanswered question recorded as an answered one.
    if match["offset"].replace(":", "") == "-0000":
        return None, (
            f"{text!r} ends in -00:00, which means the offset is unknown. Which timezone the date "
            "means is the user's answer -- ask them; +00:00 or Z is UTC"
        )
    clock = match["time"] if match["time"].count(":") == 2 else match["time"] + ":00"
    # %z takes Z, +03:00 and +0300 alike, on the floor as on a modern
    # interpreter, and refuses a minute past 59 or an offset of a day.
    try:
        instant = datetime.strptime(
            f"{match['date']} {clock}{match['offset']}", "%Y-%m-%d %H:%M:%S%z"
        )
    except ValueError:
        return None, f"{text!r} is not a real date, time and UTC offset"
    return instant, None


def shallow(root: str) -> bool:
    """Whether this clone stops short of its history's roots.

    Asked only once a search for a base has come back empty, because then the
    answer changes what the empty result means: a commit nobody fetched, not
    one that does not exist.
    """
    _, out, _ = git_text(root, "rev-parse", "--is-shallow-repository")
    return out.strip() == "true"


def base_since(root: str, tip: str, since: datetime) -> tuple[str | None, str | None]:
    """The commit a review of everything since `since` diffs against, or why none.

    Two rules, both easy to drop from a command typed by hand.

    The cutoff is the second before the instant. `--before` is inclusive, so
    a cutoff of exactly midnight selects a commit made at midnight *as the
    base*, and a range excludes its base -- that commit drops out of the review
    with nothing saying so. A nightly job commits at exactly midnight every
    night. Committer dates are whole seconds, so no commit sits between the two
    and the ranges are the same set.

    `--first-parent` keeps the search on the reviewed branch's own line.
    Without it rev-list returns the newest commit before the cutoff anywhere
    reachable, so a branch that merged an older side branch since then takes
    that side branch's tip as its base: the range picks up commits from before
    the cutoff and drops the side branch's files, which arrived today. With it,
    work merged in counts by its merge commit's date -- which is what "changes
    since" means.

    What git records is when a commit was made, never when it reached a
    branch, so that holds only where a merge commit marks the arrival. A
    fast-forward makes none: commits fast-forwarded in today keep yesterday's
    dates, sit on the first-parent line, and count as yesterday's. Nothing in
    the repository says otherwise, so nothing here can.

    The date reaches git in its object-header form, `@<seconds> +0000`, which
    git reads exactly. A bare `@<seconds>` goes to its fuzzy date parser, which
    happens to read a ten-digit number right and reads a short one as something
    else entirely.
    """
    cutoff = int(since.timestamp()) - 1
    if cutoff < 0:
        return None, f"{since.isoformat()} predates every commit git can record"
    code, out, error = git_text(
        root, "rev-list", "-1", "--first-parent", f"--before=@{cutoff} +0000", tip
    )
    if code != 0:
        return None, error or "git could not search the history for a base"
    if not out.strip():
        return None, "nothing on {}'s first-parent line was committed before {}{}".format(
            tip[:12],
            since.isoformat(),
            " -- this clone is shallow, so the commit before it may never have been fetched"
            if shallow(root)
            else ", so there is no commit to take as the base",
        )
    return out.strip(), None


def base_against(root: str, against: str, tip: str) -> tuple[str | None, str | None]:
    """The merge-base of `tip` with the commit `against` names, or why none."""
    other = resolve_commit(root, against)
    if other is None:
        return None, f"{against!r} does not name a commit in this repository"
    code, out, error = git_text(root, "merge-base", other, tip)
    # git says nothing and exits 1 when two histories share no commit it has.
    if code == 1 and not out.strip():
        if shallow(root):
            return None, (
                f"{against!r} and {tip[:12]} share no commit this shallow clone has fetched -- "
                "deepen it (git fetch --unshallow) and run again"
            )
        return None, f"{against!r} and {tip[:12]} share no history, so they have no merge-base"
    if code != 0:
        return None, error or "git could not find the merge-base"
    return out.strip(), None


def behind_remote(root: str, against: str, tip: str) -> str | None:
    """Why `against`, a local branch its remote's copy has moved past, is refused.

    The base is meant to be the remote's branch, fetched -- the one the work
    will merge into. A local `main` nobody has pulled lags it, and a merge-base
    taken from that reaches back past work already merged, so the passes review
    it again: one run of this skill on its own branch reviewed nine commits
    where three were new. Refused before anything is made, so a re-run leaves
    no run and no review tree behind. A local branch only *ahead* of its
    remote's copy is no such risk -- its merge-base is at least as recent --
    and is taken as given. A caller who does mean a lagging local branch says
    so with --base, which takes any commit, this one included.

    The remote's copy is the branch's upstream, read through for-each-ref:
    `refs/heads/main@{upstream}` is no revision git accepts, while `main@{u}`
    would read a tag of the same name first. Where no upstream is set, it is
    origin's branch of the same name, which is what SKILL.md tells callers to
    fetch.
    """
    code, full, _ = git_text(root, "rev-parse", "--symbolic-full-name", against)
    full = full.strip()
    if code != 0 or not full.startswith("refs/heads/"):
        return None
    _, remote, _ = git_text(root, "for-each-ref", "--format=%(upstream)", full)
    remote = remote.strip()
    if not remote.startswith("refs/remotes/"):
        remote = "refs/remotes/origin/" + full[len("refs/heads/") :]
    if resolve_commit(root, remote) is None:
        return None
    code, behind, _ = git_text(root, "rev-list", "--count", f"{full}..{remote}")
    if code != 0 or behind.strip() == "0":
        return None
    name = remote[len("refs/remotes/") :]
    return (
        f"--against {against} is a local branch {behind.strip()} commit(s) behind {name}, so a "
        f"base taken from it reaches back past work {name} already has, and the passes would "
        f"review that again. Fetch it and pass --against {name}. If the user asked for the local "
        f'branch itself, pass --base "$(git merge-base {against} {tip})" instead'
    )


# The built-in attributes that rewrite worktree content on the way into a
# diff. Unlike filters these run no configured command, so the overrides that
# trees.filter_overrides builds cannot reach them and there is no flag to
# refuse them; they are detected and the run stops instead. text/eol (CRLF) is
# deliberately absent: it is on nearly every repository, and the only change it
# hides is one of line endings, which the review does not examine -- listing it
# would refuse honest repos for no gain.
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
    overrides: list[str]
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
    code, raw, error = git_capped(
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
    """path -> its line count on disk, or null where there is no count to give.

    Confinement is validate.confine, called rather than copied: this manifest is
    a prediction of what that validator will say, so a path the two resolve
    differently is the one path it must not carry a number for, and two
    implementations of "inside the checkout" is how that happens.

    Null is not padding, and it is not "no file": it says there is no count. A
    path that leaves the checkout or names no regular file is one a range over
    would be rejected however it was arrived at, and an unreadable file is one
    the pass will find unreadable too. A file over COUNT_CEILING is different:
    it is readable, the validator counts it with no ceiling and judges a range
    over it against its true length, and the pass reads it itself, as it did
    before the manifest existed. A file the diff deletes is none of these and
    gets no entry at all -- git writes its post-image as /dev/null, and the
    patch the pass is reading says so on the same line.
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


RELEASE_HELP = (
    "removes the review tree of RUN_DIR, the run_dir scope.py printed, and does nothing when the "
    "run read the checkout in place. Safe on every run, including one that ended early."
)


def main(argv: list[str]) -> int:
    # A mode of its own rather than a flag among the others: it takes a run
    # directory and nothing else, and the scope flags are all required.
    if argv[1:2] == ["--release"]:
        # Help asked of this mode, which would otherwise be read as a run
        # directory named `--help` -- and, with no tree to remove, exit 0.
        if argv[2:] in (["-h"], ["--help"]):
            sys.stdout.write(f"usage: scope.py --release RUN_DIR\n\n{RELEASE_HELP}\n")
            return 0
        if len(argv) != 3:
            sys.stderr.write("Cannot release the review tree: --release takes one run directory\n")
            return 2
        return release(argv[2])

    # Named in the epilog because argparse never sees it, and --help is where
    # a reader looks for the modes.
    parser = argparse.ArgumentParser(
        add_help=True, epilog=f"scope.py --release RUN_DIR, given alone: {RELEASE_HELP}"
    )
    parser.add_argument("--repo", required=True)
    # Three ways to name one base, exactly one of them required. The latter two
    # resolve it here, from facts the caller has already settled with the user
    # -- which branch, which instant in which timezone -- because the rules for
    # turning those into a commit were rules a caller applied by hand, and the
    # caller is the one most likely to drop a flag. See base_against and
    # base_since. None of them guesses: each names its base outright.
    bases = parser.add_mutually_exclusive_group(required=True)
    bases.add_argument("--base")
    bases.add_argument("--against")
    bases.add_argument("--since")
    parser.add_argument("--mode", required=True, choices=("revisions", "local-patch"))
    parser.add_argument("--head")
    parser.add_argument("--label")
    parser.add_argument("--confirm-large", action="store_true")
    parser.add_argument("--no-hints", action="store_true")
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit:
        return 2

    def stop(signum: int, frame: object) -> None:
        raise SystemExit(128 + signum)

    # A terminate or a hangup arrives as SystemExit, as an interrupt arrives as
    # KeyboardInterrupt, so the cleanup below runs for each: the review tree is
    # discarded, and a linter the hints step started goes down with its process
    # group -- see tools.run. Without it the terminate an orchestrator sends
    # when a command's time runs out left both behind.
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop)

    repo = os.path.abspath(os.path.expanduser(args.repo))
    if not os.path.isdir(repo):
        return fail(f"{repo} is not a directory")
    root = toplevel(repo)
    if root is None:
        return fail(f"{repo} is not inside a git repository")

    # Refused here rather than left for the validator, which would only see it
    # after both passes had run on a pinned diff: this is a bad invocation, and
    # the bad-invocation exit is what the caller can still act on. The rule is
    # validate.py's, called rather than restated, so the two cannot drift.
    if args.label is not None:
        problem = validate.label_problem(args.label)
        if problem:
            return fail(f"--label {problem}", 2)
    if args.against is not None:
        problem = validate.against_problem(args.against)
        if problem:
            return fail(f"--against {problem}", 2)
    since = None
    if args.since is not None:
        since, problem = parse_since(args.since)
        if problem:
            return fail(f"--since {problem}", 2)

    if args.mode == "revisions" and not args.head:
        return fail("--head is required under scope mode 'revisions'", 2)
    if args.mode == "local-patch" and args.head:
        return fail("--head does not apply to a local working patch", 2)

    # Read before anything is made. A prompts.md this cannot fill is a defect in
    # the skill, and finding it once the run directory and the review tree exist
    # would leave both behind. Its own status, because exit 4 says the range is
    # the problem, and here the user's repository is not.
    blocks, problem = prompts.read()
    if blocks is None:
        return fail(
            f"references/prompts.md cannot be filled: {problem}. That is a defect in the skill, "
            "not in this repository",
            5,
        )

    head = None
    if args.mode == "revisions":
        head = resolve_commit(root, args.head)
        if head is None:
            return fail(f"{args.head!r} does not name a commit in this repository")

    if args.base is not None:
        base = resolve_commit(root, args.base)
        if base is None:
            return fail(f"{args.base!r} does not name a commit in this repository")
    else:
        # Where the search for the base starts: the reviewed head, or for a
        # local patch the commit the working tree sits on.
        tip = head or resolve_commit(root, "HEAD")
        if tip is None:
            return fail("HEAD names no commit yet, so there is no history to take a base from")
        if since is not None:
            base, problem = base_since(root, tip, since)
        else:
            base, problem = base_against(root, args.against, tip)
        if base is None:
            return fail(problem or "git could not resolve a base")
        if args.against is not None:
            problem = behind_remote(root, args.against, args.head or "HEAD")
            if problem:
                return fail(problem, 2)

    patch, error = build_diff(root, args.mode, base, head)
    if patch is None:
        return fail(error or "git could not produce the diff")
    if not patch["files"]:
        if args.mode == "local-patch":
            return fail(
                "the working tree matches {} -- there is nothing to review. The {} untracked file(s) "
                "here are invisible to git diff; stage or commit them to bring them in".format(
                    args.base or base[:12],
                    patch["untracked"] if patch["untracked"] is not None else 0,
                )
            )
        if since is not None and base == head:
            return fail(
                f"nothing reached {head[:12]}'s first-parent line at or after "
                f"{since.isoformat()} -- there is nothing to review"
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

    # A local patch's code, held as a commit for rule derivation, which is
    # asked for once the working tree has moved on; see trees.snapshot. Taken
    # as soon as the diff is known to be wanted, because the two are two reads
    # of a working tree the user may still be editing. The diff stays the
    # working tree's own rather than this commit's: a commit cannot record a
    # dirty submodule, which the working-tree diff shows the passes as -dirty.
    # A commit that cannot be made costs the derivation, never the review.
    held = None
    if args.mode == "local-patch":
        held, problem = snapshot(root)
        if held is None:
            sys.stderr.write(
                f"Warning: could not record the working tree as a commit: {problem}.\n"
                "The review is unaffected, but rule derivation on this run will refuse, because "
                "nothing else holds the reviewed code to test rules on.\n"
            )

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
        worktree, problem = review_tree(root, head, run_dir, advice=HEAD_ADVICE)
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
        headers = diff_paths.file_headers(patch["text"].split("\n"), changes=True)
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

        # Linted here, once, rather than left to a pass that might: passes
        # told the linters were installed used them in 2 runs of 20. Never
        # fatal -- a linter that is missing or fails is recorded and skipped.
        # A local patch's head is the commit its working tree was recorded as.
        hinted = (
            hints.disabled()
            if args.no_hints
            else hints.collect(root, tree, base, head or held, headers, run_dir)
        )

        # Every prompt the run sends, filled with this run's paths and written
        # beside the files it names. What is handed over is one line pointing
        # at the file, so the orchestrator retypes no path and paraphrases no
        # rule. The hint check's only when there are leads for it to check.
        # Rule derivation is not among them: it is asked for later, and filled
        # then.
        handed = prompts.write_run(
            blocks,
            run_dir,
            prompts.run_values(
                repo_root=tree,
                checkout=root,
                run_dir=run_dir,
                context_diff=context,
                # Counted as the validator counts, so the number agrees with
                # the line a reader's tool numbers last, or falls one short of
                # a tool that numbers the empty line after the final newline.
                context_diff_lines=validate.line_count(context),
                file_lines=lines_path,
            ),
            hints=hinted["leads"] > 0,
        )

        scope: dict[str, str | int | list[dict[str, str | int]] | None] = {
            "repo": os.path.basename(root),
            "mode": args.mode,
        }
        # Verbatim, and only when given. It says what the request named in words
        # nothing else in this object records -- "pull request #54". It began as
        # the record of a date's resolution, after two runs of "changes made
        # today" differed by 3.5x in files and only a reader who re-derived the
        # git commands could see why; `since` below records that now, and records
        # it checked. Nothing here checks the label against the range beside it,
        # and nothing could: what the words meant was settled in the
        # conversation, above this script. It is declared provenance, the page
        # presents it as such, and the resolved base and head remain the
        # checkable record.
        if args.label is not None:
            scope["label"] = args.label
        # How the base was found, when this script found it. Unlike the label
        # these are checked: the base beside them was derived from them, here,
        # so the page states them without the label's caveat. What they cannot
        # say is whether the user meant that branch or that instant -- that
        # answer was given in the conversation, as it always was.
        if args.against is not None:
            scope["against"] = args.against
        if since is not None:
            scope["since"] = since.isoformat()
        scope["base"] = base
        scope["head"] = head
        # Each changed file and the lines its blocks add and remove, for the
        # report's map, which draws every changed file as a row. Read from the
        # pinned patch, as the block count is and for its reason: a second `git
        # diff --numstat` could disagree with context.diff. Header.path says
        # which path names a block, a deletion's included. A header whose path
        # would not decode has none and is not listed: the warning above counts
        # it, except for a deletion whose old name would not decode, which that
        # warning rightly omits -- file_lines never wanted a deleted file -- so
        # the map has no row for it and says nothing. files_changed still counts
        # it, which is why the map heads its rows with its own count.
        #
        # One entry per path, not per block. A file whose type changes -- one
        # replaced by a symlink -- is two blocks under one path, a deletion and
        # an addition, and one file; listed twice, the map kept one block's
        # churn and dropped the other's. Summed, the entry says what numstat says.
        churn: dict[str, tuple[int, int]] = {}
        for header in headers:
            if header.path is not None:
                added, removed = churn.get(header.path, (0, 0))
                churn[header.path] = (added + header.added, removed + header.removed)
        # Files, for the same reason: the report's run facts say "files
        # changed", and the patch's `diff --git` count says one more for every
        # file whose type changed. Each block whose path an earlier block already
        # named is taken off; a header that named nothing readable still counts
        # as the file it was, as it always has. The block count itself stays
        # what the gate above and the warning measure, because those are about
        # the patch.
        repeats = sum(1 for header in headers if header.path is not None) - len(churn)
        scope["files_changed"] = patch["files"] - repeats
        scope["files"] = [
            {"path": path, "added": added, "removed": removed}
            for path, (added, removed) in churn.items()
        ]
        scope["diff_bytes"] = patch["bytes"]
        if patch["untracked"] is not None:
            scope["untracked"] = patch["untracked"]

        # One string, written twice, as collect_docs.py does with docs.json: the
        # merge reads the record rather than a model retyping what was printed,
        # and there is nothing to drift between the two.
        printed = write_record(
            run_dir,
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
                # The commit that holds a local patch's working tree as this
                # run read it, for rule derivation; null under scope mode
                # 'revisions', whose head already is one. Outside `scope`, as
                # `now` is, because the validator closes that object and this
                # is no fact about the range.
                "snapshot": held,
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
                # Each subagent's whole prompt, by name: security, quality,
                # hints when there are leads, docs, falsification. Sent
                # exactly as printed.
                "prompts": handed,
                # What the linters found new in the change, or why none ran:
                # status, the number of leads, hints.md when there are any,
                # and each linter's version, rules and files. Outside `scope`,
                # as `now` is: it is no fact about the range.
                "hints": dict(hinted),
                "scope": scope,
            },
        )
        sys.stdout.write(printed)
    except BaseException:
        if worktree is not None:
            discard(root, worktree)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
