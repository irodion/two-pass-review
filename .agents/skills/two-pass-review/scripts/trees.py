#!/usr/bin/env python3
"""The review trees checked out of the user's repository. Stdlib only, 3.10 syntax.

A module of its own because two places make a review tree, and the one that
came second cannot import the first. scope.py makes it for the passes, and
runs.rules_tree makes it again for rule derivation, which prompts.py asks for
long after the run released its own. scope.py imports runs.py, so the tree
cannot live in scope.py without a cycle: runs.py would have to import scope.py
back. escaping.py moved out of page.py for the same reason.

The tree is the place to have one copy: every override it runs with is there
because the reviewed code can be a stranger's, and a second spelling of the
checkout is a second place for one of them to go missing. The first version of
rule derivation spelled it again, in prose, for its subagent to type, and a
review of it ran the user's post-checkout hook on the reviewed head's own
configuration.
"""

import contextlib
import os
import shutil
import tempfile
from typing import TypeGuard

from gitrun import git, git_text


def filter_overrides(repo: str) -> tuple[list[str], str | None]:
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

    A name the -c syntax cannot express fails the run instead, as a problem
    beside an empty list, so a caller that returns on the problem holds a list
    either way and asserts nothing. -c splits its argument at the first equals
    sign, so a driver named with one -- legal in config, selectable from
    .gitattributes -- would take the override as a different variable and keep
    its command. GIT_CONFIG_KEY_n would express it, but older gits ignore those
    variables silently, which turns one unrepresentable name into no
    neutralization at all. No real tool names a filter that way, so refusing is
    a message to an attacker, not a user.
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
            return [], (
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
    path alone, so check_out, which names a tree, and is_run_tree, which
    recognises one, both live here, where a change to one is a change beside
    the other.
    """
    return os.path.join(tempfile.gettempdir(), TREES_DIR)


def is_run_tree(worktree: str | None, run_dir: str) -> TypeGuard[str]:
    """Whether `worktree` names the run's own review tree: TREES_DIR/<the run's name>.

    A path read from scope.json is not one to trust -- release hands it to
    `git worktree remove --force`, and rules_tree hands it to a subagent as the
    reviewed code -- and it must never be the user's own worktree, or another
    run's tree. So both hold it to the shape check_out makes a tree in. The
    check reads the path's shape and never trees_dir() itself: that reads
    TMPDIR in whichever process calls it, and a release run with a different
    TMPDIR from the call that made the tree would refuse the very tree it is for.
    """
    if worktree is None:
        return False
    parent, name = os.path.split(os.path.normpath(worktree))
    return name == os.path.basename(run_dir) and os.path.basename(parent) == TREES_DIR


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


def check_out(
    root: str, revision: str, run_dir: str, overrides: list[str]
) -> tuple[str | None, str | None]:
    """(tree, why not): `revision` checked out into a new review tree named as the run.

    Hooks off, symlinks written as plain files, and every filter driver
    emptied -- review_tree says why each. The reason it failed comes back
    bare, for the caller to say what it was checking out.

    The directory is named as the run, for trees_dir's reason. os.mkdir keeps
    what mkdtemp would give: the directory is new, so removing it on a failed
    checkout removes nothing of anyone else's, and it is 0700 from the start
    -- a umask can take bits away, never add them. git checks out into an
    empty directory that already exists.
    """
    trees = trees_dir()
    problem = make_private_dir(trees)
    if problem:
        return None, problem
    path = os.path.join(trees, os.path.basename(run_dir))
    try:
        os.mkdir(path, 0o700)
    except OSError as refused:
        return None, f"cannot create {path}: {refused}"
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
        revision,
    )
    if code != 0:
        shutil.rmtree(path, ignore_errors=True)
        return None, error or "git worktree add failed"
    return path, None


def discard(root: str, tree: str) -> tuple[int, str]:
    """Remove a review tree and its registration in `root`. Returns git's status and error.

    The one spelling of the removal, for release and for every failure that
    must not leave a tree behind. --force, because nothing in a review tree is
    anyone's work: a file a tool wrote there while a rule was tested -- a
    cache it was told not to write -- would otherwise make git refuse to
    remove it.
    """
    code, _, error = git(root, "worktree", "remove", "--force", tree)
    return code, error


# What a checkout that fails tells the user to do, when the commit is one they
# can check out themselves: a head they asked to have reviewed.
HEAD_ADVICE = (
    "Check it out yourself and run again -- a checkout that already holds the reviewed head is "
    "read in place"
)


def review_tree(
    root: str, head: str, run_dir: str, *, advice: str
) -> tuple[str | None, str | None]:
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

    What a failed checkout tells the user to do is `advice`, the caller's to
    give, because the two callers check out different things: scope.py a head
    the user named, which they can check out themselves -- HEAD_ADVICE -- and
    rule derivation, for a local patch, a commit they have never seen.
    """
    overrides, problem = filter_overrides(root)
    if problem:
        return None, problem
    if checkout_holds(root, head, overrides):
        return None, None
    path, reason = check_out(root, head, run_dir, overrides)
    if path is None:
        return None, f"could not check out {head} into a review tree: {reason}. {advice}"
    return path, None


def snapshot(root: str) -> tuple[str | None, str | None]:
    """(commit, problem): the working tree's tracked files as one commit, which no ref names.

    A local patch is the working tree, which no commit holds and the user goes
    on editing, so rule derivation, asked for long after the run, would have
    no reviewed code to check out. scope.py makes this commit when it pins a
    local patch, and rules_tree checks it out as review_tree checks out a
    head: every byte as git read it, binary or not, and links as files holding
    their target.

    Built on a copy of the index, never the index itself: `git add --update`
    on the copy stages every tracked file as it stands, and the user's own
    staging is left exactly as it was. git stash create does this in one
    command, and refuses two states a local patch reviews: a file added with
    `git add -N`, and a merge in conflict. The copy keeps the index's mtime,
    because git compares it with each file's to catch an edit made in the same
    second the index was written; a copy stamped now would pass a same-size
    edit over as unchanged, and the commit would hold the file before the edit
    the diff shows. Filters are emptied, as for the diff, so the commit holds
    the bytes the diff read; hooks are off, as for every git call here that
    writes. The split index is off so the copy is written whole, and nothing
    but objects lands in the repository's git directory. The commit carries an
    identity of its own, because on a machine with none configured -- CI is
    one -- git would refuse to make it.

    Nothing names the commit, so `git gc` prunes it once it is older than
    gc.pruneExpire, two weeks by default. A derivation after that refuses, as
    one does when a reviewed head is gone.
    """
    overrides, problem = filter_overrides(root)
    if problem:
        return None, problem
    code, out, error = git_text(root, "rev-parse", "--git-path", "index")
    if code != 0:
        return None, error or "git could not say where the index is"
    descriptor, copy = tempfile.mkstemp(prefix="two-pass-review-index-")
    os.close(descriptor)
    try:
        index = os.path.join(root, out.strip())
        shutil.copyfile(index, copy)
        # The times alone: copy2 would also copy the mode, and mkstemp's 0600
        # keeps the file list of a private repository out of a shared /tmp.
        stamp = os.stat(index)
        os.utime(copy, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        flags = ["-c", "core.hooksPath=/dev/null", "-c", "core.splitIndex=false", *overrides]
        code, _, error = git(root, *flags, "add", "--update", env={"GIT_INDEX_FILE": copy})
        if code != 0:
            return None, error or "git add --update failed"
        code, tree, error = git_text(root, *flags, "write-tree", env={"GIT_INDEX_FILE": copy})
        if code != 0:
            return None, error or "git write-tree failed"
    except OSError as unreadable:
        return None, f"cannot copy the index: {unreadable}"
    finally:
        with contextlib.suppress(OSError):
            os.remove(copy)
    code, commit, error = git_text(
        root,
        "-c",
        "user.name=two-pass-review",
        "-c",
        "user.email=two-pass-review@localhost",
        "commit-tree",
        "-m",
        "two-pass-review: the working tree, as a local-patch run read it",
        tree.strip(),
    )
    if code != 0:
        return None, error or "git commit-tree failed"
    return commit.strip(), None
