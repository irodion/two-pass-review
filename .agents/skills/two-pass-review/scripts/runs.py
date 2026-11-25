#!/usr/bin/env python3
"""A run: its directory, its record scope.json, and the review tree it names. Stdlib only, 3.10 syntax.

The directory is made, named and pruned here, and its record is read and
written here. scope.py writes the record when it pins a run, rule derivation
when it makes the run's tree anew, and release when it removes that tree;
prune, the merge, the rule-derivation prompt and `render.py --show` read it. All of them go
through read_record and write_record, and the fields have one type, Record,
so a field added or renamed is one change in one place, and a reader that
spells it wrong is mypy's error.

trees.py owns where a review tree is: it names each one as its run, and
recognises one from its path alone. What is here is the record that names a
tree -- which one, in which checkout -- and the release that removes exactly
that. Moved out of scope.py, which kept the scope, the diff and main.

One module on purpose, not one per layer. The three are one life: prune keeps
a run while the tree its record names stands, rules_tree records a tree so
that prune and release find it, and release writes the record back. Split by
layer -- the record in a module of its own, the tree's life in trees.py --
each of those rules would have its two halves in two files. What it costs is
that merge.py, to read a record, imports the git helpers as well: a few
imports, and no git runs.
"""

import contextlib
import json
import os
import posixpath
import re
import shutil
import stat
import sys
import tempfile
from datetime import datetime
from types import UnionType
from typing import Any, TypedDict, Union, cast, get_args, get_origin, get_type_hints

from gitrun import git, git_text, toplevel
from trees import HEAD_ADVICE, discard, filter_overrides, is_run_tree, review_tree


# Where runs are kept: one directory at the top of the user's checkout, holding
# a run directory per run and the stable latest.html. See reports_dir.
REPORTS_DIR = ".two-pass-review"

# The whole of what makes REPORTS_DIR invisible to git. A `.gitignore` applies
# to its own directory, and `*` matches the file itself too, so nothing under
# REPORTS_DIR is ever untracked, staged, or diffed. Git only: the comment says
# so, because a tool that reads just its own ignore file still walks it.
SELF_IGNORE = (
    "# Written by two-pass-review, so that git ignores review runs. Tools that read\n"
    "# only their own ignore file, like docker build and prettier, need it there.\n"
    "*\n"
)

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
    appears in `git status`, in a local patch, or in its untracked count. That
    is git alone. A tool that reads only its own ignore file -- `docker build`,
    prettier -- still walks the directory, and the README tells users to list
    it there; nothing here edits those files, which are the reviewed code's.

    A repository that tracks anything under the path is refused, because there
    the self-ignore cannot hold -- git never ignores a tracked file -- and runs
    would land among the reviewed code's own files. A symlink or a file at the
    path is refused as trees.make_private_dir refuses one: `O_NOFOLLOW` makes it an
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
    target = os.path.join(path, ".gitignore")
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        pass
    except OSError as error:
        return None, f"cannot write {target}: {error}"
    else:
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(SELF_IGNORE)
        except OSError as error:
            # Not left half-written: an empty file here would be kept by every
            # later run, under the O_EXCL rule above, until a person removed it.
            with contextlib.suppress(OSError):
                os.unlink(target)
            return None, f"cannot write {target}: {error}"
    # Whatever the file holds, git is asked rather than the file trusted. One
    # left empty by an interrupted run, or edited by the user, would otherwise
    # pass for proof that runs are hidden while every later run showed in `git
    # status`. The probe names a file that need not exist: check-ignore matches
    # patterns, not files.
    code, _, _ = git(root, "check-ignore", "-q", "--", f"{REPORTS_DIR}/probe")
    if code != 0:
        return None, (
            f"git does not ignore {path}, so every run there would show in `git status`. Its "
            ".gitignore should hold the single pattern `*`: delete that file and run again, and "
            "a new one is written"
        )
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
    `--release` needs it. So is a run whose scope.json read_record refuses,
    which only a hand edit makes: the edit may have left `worktree` naming a
    standing tree, and deleting the record would leave that tree in temp and in
    `git worktree list` with nothing to release it.

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
        record = read_record(path)
        if record is None and os.path.lexists(os.path.join(path, "scope.json")):
            continue
        worktree = record.get("worktree") if record else None
        if is_run_tree(worktree, path) and os.path.exists(worktree):
            continue
        shutil.rmtree(path, ignore_errors=True)


class Record(TypedDict, total=False):
    """scope.json: what scope.py printed when it pinned the run, kept up to date since.

    Not total, because a run pinned before a field existed has none of it, and
    each reader says what it does then. Typed, so that a field read under a
    name nothing writes is mypy's error rather than a None at run time.
    """

    repo_root: str
    worktree: str | None
    checkout: str
    snapshot: str | None
    stood_on: str | None
    branch: str | None
    run_dir: str
    report_dir: str
    now: str
    context_diff: str
    file_lines: str
    latest: str
    prompts: dict[str, str]
    hints: dict[str, Any]
    scope: dict[str, Any]


def kinds(hint: object) -> tuple[type, ...]:
    """What isinstance takes for one field's annotation: a union's members, or the bare type."""
    members = get_args(hint) if get_origin(hint) in (Union, UnionType) else (hint,)
    return tuple(cast(type, get_origin(member) or member) for member in members)


# Each field's type, read off Record rather than written out a second time. A
# list kept by hand beside the class was tried, and a field retyped in one and
# not the other makes read_record refuse every new record, with a message that
# says the file is missing. Every field, not only those read today, because the
# readers rely on this rather than checking for themselves -- prompts.py's
# `if not checkout`, merge.py's `scope is None`, rules_tree's and release's
# `is None` -- and a field that gains a reader then needs no change here. It is
# shallow on purpose: a field that is a dict is checked as a dict, never what
# it holds. What is inside `scope` is the validator's to judge, so a reader of
# a field in there, like reviewed_revision's head, still checks it, and
# `prompts` is printed by scope.py and read by nothing.
FIELD_KINDS = {field: kinds(hint) for field, hint in get_type_hints(Record).items()}


# What a reader says when read_record returns None, in one place for all of
# them. None is a file missing, one that is not JSON, or one with a field of a
# type scope.py never writes -- a hand edit -- and the words have to cover the
# last as well, or they send the user looking for a file that is there.
NO_RECORD = "holds no scope.json written by scope.py, or one edited since"


def read_record(run_dir: str) -> Record | None:
    """The run's scope.json, or None where it holds none a script wrote.

    A field of the wrong type is a file no script wrote -- a hand edit, or
    another program's -- and is refused whole, as one that is not JSON is.
    """
    try:
        with open(os.path.join(run_dir, "scope.json"), encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    for field, allowed in FIELD_KINDS.items():
        if field in record and not isinstance(record[field], allowed):
            return None
    return cast(Record, record)


def write_record(run_dir: str, record: Record) -> str:
    """Write the run's scope.json whole, and return the text written.

    Through a temporary file and a rename, so a failed write cannot leave a
    record half-written: release needs it whole to find the tree it names. The
    text comes back because scope.py prints the bytes it writes.
    """
    text = json.dumps(record, indent=2) + "\n"
    path = os.path.join(run_dir, "scope.json")
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(path + ".tmp", path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(path + ".tmp")
        raise
    return text


def reviewed_revision(
    run_dir: str, pinned: Record, checkout: str
) -> tuple[tuple[str, str] | None, str | None]:
    """((revision, advice), problem): the commit holding the run's reviewed code, or why none does.

    The head under scope mode 'revisions'; under 'local-patch', the commit
    scope.py made of the working tree when it pinned the run. The advice is
    what a checkout of it that fails tells the user, and the two differ: a
    head is a commit they can check out themselves, the snapshot one they have
    never seen.
    """
    scope = pinned.get("scope") or {}
    if scope.get("mode") == "revisions" and isinstance(scope.get("head"), str):
        return (scope["head"], HEAD_ADVICE), None
    if scope.get("mode") != "local-patch":
        return None, f"{run_dir}/scope.json records no scope to rebuild the reviewed code from"
    snapshot = pinned.get("snapshot")
    if snapshot is None:
        return None, (
            f"{run_dir}/scope.json records no commit holding the reviewed working tree: "
            "the run predates it, or scope.py could not make one when it pinned the run. "
            "Review the working tree again to derive rules from it"
        )
    # Asked here, because only here is the likely reason for its absence known.
    code, _, _ = git_text(checkout, "cat-file", "-e", f"{snapshot}^{{commit}}")
    if code != 0:
        return None, (
            f"the commit that held the reviewed working tree, {snapshot[:12]}, is not in "
            f"{checkout}: git gc prunes it once it is two weeks old, by default, and "
            "another clone never had it. Review the working tree again to derive rules from it"
        )
    advice = (
        "That commit holds the working tree as the run read it, which no checkout of yours "
        "can stand in for: fix what stopped the checkout and ask again"
    )
    return (snapshot, advice), None


# What a word can hold and stay unquoted for bash and PowerShell alike. Narrower
# than shlex.quote's: PowerShell splats a leading @ and reads a comma as an array.
PLAIN_WORD = re.compile(r"[\w./:=+-]+", re.ASCII)
# What ends PowerShell's single-quoted string, and what its double-quoted one
# ends at or expands, beside bash's: it takes the typographic quotes for the
# ASCII ones.
ENDS_SINGLE = re.compile("['\u2018-\u201b]")
ENDS_DOUBLE = re.compile('[$`"\\\\\u201c-\u201e]')


# What `render.py` spells out as text rather than print: every control
# character but newline and tab -- C0, DEL and C1 -- and the characters that
# reorder or break what a terminal shows without changing what is there: the
# bidirectional marks, embeddings, overrides and isolates, and the Unicode line
# and paragraph separators. A finding quotes the reviewed repository, which can
# hold an escape sequence that sets a terminal's clipboard or erases a line;
# the page never minded, being HTML, but `--agent` and `--show` write to a
# terminal or a harness that shows one. Here because printable_path asks the
# same question of a path, and two lists would drift.
UNPRINTED = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)


def printable_path(path: str) -> bool:
    """Whether `path` can be printed for an orchestrator to copy as it is.

    One line, so it cannot stand a forged block opener on a line of its own;
    no backtick, which would end the code span a printed command sits in;
    nothing UNPRINTED, which `render.py` would spell out as text and so name
    another directory; and UTF-8, which every file a run writes a path into is,
    where a byte that is not would be a crash halfway through making the run.
    scope.py refuses a checkout that fails this before any pass runs, and
    `render.py --show` will not print one a record holds.
    """
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    # And no backtick: the commands printed for a reader sit in a code span,
    # which a backtick inside one would end early.
    return path.splitlines() == [path] and not UNPRINTED.search(path) and "`" not in path


# A full object id, which is how scope.py records every revision.
OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def quote_word(word: str) -> str | None:
    """`word` quoted so that bash and PowerShell read it alike, where any form does.

    Single quotes first: both take everything inside them literally. Neither
    holds a single quote there the way the other does -- bash closes and
    reopens, PowerShell doubles it -- so a word with one goes in double quotes,
    which both also read literally while the word holds none of what either
    expands inside them: $, a backtick, a double quote or a backslash. A word
    with an apostrophe and one of those has no form both read alike, and gets
    None: bash's form would hand PowerShell a typographic quote that ends its
    string, and the $ after it would run.
    """
    if PLAIN_WORD.fullmatch(word):
        return word
    if not ENDS_SINGLE.search(word):
        return f"'{word}'"
    if not ENDS_DOUBLE.search(word):
        return f'"{word}"'
    return None


def command_line(words: list[str], *rest: str) -> str | None:
    """`words` as one command a harness's shell runs as copied, then `rest`
    unquoted: placeholders such as `<path>` for the reader to fill in.

    Quoted by quote_word, for the shells agent harnesses run -- bash and zsh,
    Git Bash on Windows, PowerShell. The first word is a program's name, never
    a path, so it is never quoted: PowerShell reads a quoted first word as a
    string, not a command. On Windows the separators become forward slashes,
    which git and Python both take, because Git Bash drops a backslash from an
    unquoted word. None where a word has no form both shells read alike: the
    caller says what to run another way rather than print a command that runs
    something else in one of them.
    """
    if not PLAIN_WORD.fullmatch(words[0]):
        raise ValueError(f"a command's first word is a program's name: {words[0]!r}")
    if os.name == "nt":
        words = [word.replace("\\", "/") for word in words]
    quoted = [quote_word(word) for word in words]
    if None in quoted:
        return None
    return " ".join([*cast(list[str], quoted), *rest])


def mode_on_disk(checkout: str, path: str, mode: str) -> bool:
    """Whether `path` in `checkout` is, on disk, what git records as `mode`.

    Where core.fileMode is off -- Windows, and filesystems without an execute
    bit -- git keeps the executable bit it had and the disk cannot say, so
    there any regular file is either regular mode.
    """
    try:
        status = os.lstat(os.path.join(checkout, path))
    except OSError:
        return False
    if stat.S_ISLNK(status.st_mode):
        return mode == "120000"
    if not stat.S_ISREG(status.st_mode) or mode not in ("100644", "100755"):
        return False
    code, out, _ = git_text(checkout, "config", "--type=bool", "core.fileMode")
    if code == 0 and out.strip() == "false":
        return True
    return mode == ("100755" if status.st_mode & 0o111 else "100644")


def reviewed_code(run_dir: str, cited: list[str]) -> list[str]:
    """Where a finding's reviewed code is now, as sentences `render.py --show`
    prints after the finding.

    A finding's line numbers are the reviewed revision's, and the checkout the
    orchestrator would fix may not hold it: a pull request reviewed without
    checking it out, work edited since, a local patch's snapshot git has
    pruned, a file the change deleted. Each of those was once a git command
    SKILL.md described for a model to run and read -- and read wrong, where git
    printed something other than what the prose named. So this asks git, and
    SKILL.md says what to do about each answer. Each sentence opens with the
    words SKILL.md matches on.

    `cited` is the path of each of the finding's locations, in its order. Its
    producer wrote them, and these lines sit outside the finding's block, so
    no sentence repeats one: a file is named by the number of its location,
    counted from one in the order the block lists them.

    Worded here rather than taken from reviewed_revision, whose words are rule
    derivation's: they end by advising a review to derive rules from.
    """
    pinned = read_record(run_dir)
    if pinned is None:
        return [f"Reviewed code unknown: {run_dir} {NO_RECORD}."]
    # What the sentences below print beside the block comes from scope.json,
    # which a hand or another program can edit, so each value is held to the
    # shape scope.py writes before any of it is printed: a checkout that is a
    # printable path and the top of its repository, as git says -- not any
    # directory inside one, whose name could be a sentence -- and object ids.
    # Until the checkout passes, no answer prints it.
    record = f"{run_dir}/scope.json"
    checkout = pinned.get("checkout")
    if not isinstance(checkout, str):
        return [f"Reviewed code unknown: {record} records no checkout."]
    if not printable_path(checkout):
        return [f"Reviewed code unknown: {record} records a checkout no script wrote."]
    if not os.path.isdir(checkout):
        return [f"Reviewed code unknown: the checkout {record} records is not there."]
    # Asked before anything is read from it, so a checkout git will not work in
    # -- no longer a repository, or refused by safe.directory -- is not taken
    # for one that has lost the reviewed commit.
    top = toplevel(checkout)
    if top is None:
        return [f"Reviewed code unknown: git cannot read the checkout {record} records."]
    if top != checkout:
        return [f"Reviewed code unknown: {record} records a checkout no script wrote."]
    scope = pinned.get("scope") or {}
    local = scope.get("mode") == "local-patch"
    revision = pinned.get("snapshot") if local else scope.get("head")
    if not isinstance(revision, str):
        if local:
            return ["Reviewed code gone: the run made no commit of the working tree it reviewed."]
        return [f"Reviewed code unknown: {run_dir}/scope.json records no reviewed revision."]
    stood = pinned.get("stood_on")
    anchor = (stood or scope.get("base")) if local else revision
    if not all(isinstance(v, str) and OBJECT_ID.fullmatch(v) for v in (revision, anchor)):
        return [f"Reviewed code unknown: {run_dir}/scope.json records a revision no script wrote."]
    if git_text(checkout, "cat-file", "-e", f"{revision}^{{commit}}")[0] != 0:
        pruned = " -- git prunes the commit of a reviewed working tree about two weeks on"
        return [f"Reviewed code gone: {revision} is not in {checkout}{pruned if local else ''}."]
    # Named on the first line, so that every answer below -- not only the
    # one that says the checkout is elsewhere -- has a way to read the code.
    # Without -C where the checkout's path has no form every shell reads
    # alike: the revision is an object id, so this one always prints.
    show = command_line(["git", "-C", checkout, "show"], f"{revision}:<path>")
    reads = f"`{show}`" if show else f"`git show {revision}:<path>`, run in it,"
    lines = [f"Reviewed revision: {revision}, in {checkout}. {reads} reads a file as reviewed."]

    # One file however a pass spelled it -- `./a.py` is `a.py` -- as the
    # validator and merge.overlap count files. posixpath, as validate.path_key
    # uses: these become git tree names, which os.path on Windows would turn
    # into backslashes that no tree holds.
    cited = [posixpath.normpath(path) for path in cited]
    paths = list(dict.fromkeys(cited))

    def files(some: list[str]) -> str:
        if len(paths) == 1:
            return "the one file it cites"
        places = [str(n) for n, path in enumerate(cited, 1) if path in some]
        listed = places[0] if len(places) == 1 else ", ".join(places[:-1]) + " and " + places[-1]
        return "the {} at location{} {}".format(
            "file" if len(some) == 1 else "files", "s" if len(places) > 1 else "", listed
        )

    # Not found rather than deleted: a path the validator opened on a
    # case-insensitive filesystem can be one git's tree spells otherwise.
    missing = [p for p in paths if git_text(checkout, "cat-file", "-e", f"{revision}:{p}")[0]]
    if missing:
        lines.append(
            f"Not found in the reviewed revision: {files(missing)} -- a file the change deleted, "
            "one a remedy proposes, or a path git spells otherwise."
        )
    present = [p for p in paths if p not in missing]
    # Whether the checkout stands where the review did. A head the checked-out
    # branch does not contain is a pull request reviewed without checking it
    # out, or a branch rebased since -- every branch here, once GitHub's rebase
    # merge has minted new commits for it. A local patch's snapshot is never
    # an ancestor, made of the working tree with no parent, so what is asked of
    # it is the branch that working tree was on: on it, the checkout stands
    # there however its commits were amended or rebased since; on any other,
    # it does not, whatever that branch contains. The branch is only compared,
    # never printed. Only a HEAD on no branch, then or now, is asked about the
    # commit the working tree stood on; and a run pinned before scope.py
    # recorded either, about the range's base, which a branch cut from the same
    # base still contains -- a weaker question, named as the one it is.
    branch = pinned.get("branch")
    code, out, _ = git_text(checkout, "symbolic-ref", "--quiet", "HEAD")
    current = out.removesuffix("\n") if code == 0 else None
    if local and isinstance(branch, str) and current is not None:
        on_history = current == branch
        where = f"{checkout} is not on the branch the reviewed working tree was on"
    else:
        on_history = git_text(checkout, "merge-base", "--is-ancestor", str(anchor), "HEAD")[0] == 0
        what = ""
        if local:
            what = (
                ", the commit the reviewed working tree stood on"
                if stood
                else ", the base of the reviewed range"
            )
        where = f"HEAD in {checkout} does not contain {str(anchor)[:12]}{what}"
    if not on_history:
        lines.append(f"Not checked out: {where}.")
    # Literal pathspecs, because a cited path is a file's name and not a
    # pattern: `app/[id]/page.tsx` would otherwise match `app/i/page.tsx`.
    # Against the working tree, so an uncommitted edit counts as a difference.
    # A local patch's snapshot was committed with every filter driver emptied,
    # and is compared the same way, or a file under git-lfs would read as
    # changed by its own clean filter; a head was committed through the
    # filters, and is compared through them.
    overrides: list[str] = []
    if local:
        overrides, problem = filter_overrides(checkout)
        if problem:
            return [*lines, f"Not compared: {problem}."]
    # And with no external driver, textconv or submodule setting to answer for
    # git: an attribute the reviewed repository writes can select a driver
    # whose exit code says "the same". The scope's own diff refuses them alike.
    plain = ["--no-ext-diff", "--no-textconv", "--ignore-submodules=none"]
    diff = ["--literal-pathspecs", *overrides, "diff", *plain, "--quiet", revision, "--"]
    answers = {p: git_text(checkout, *diff, p)[0] for p in present}
    # A local patch's file under a filter can read as different to that diff
    # with its bytes untouched. The snapshot holds a file the review found
    # modified as its own bytes, and any other as the index's blob, which went
    # through the filter; the diff compares whichever the index has now. So
    # where the mode is unchanged -- the raw diff says, and a mode change is a
    # change -- the file is hashed both ways, and either matching the
    # snapshot's blob means nothing changed.
    if local:
        for path, code in answers.items():
            if code != 1:
                continue
            raw = git_text(checkout, *diff[:-3], "--raw", revision, "--", path)[1].split()
            # A mode change is a change. The all-zero mode is a path the index
            # no longer tracks -- a new file staged for the review and unstaged
            # since, its bytes still on disk -- so its mode is the disk's.
            if len(raw) < 2:
                continue
            held_mode, now = raw[0].lstrip(":"), raw[1]
            if now == "000000" and not mode_on_disk(checkout, path, held_mode):
                continue
            if now not in ("000000", held_mode):
                continue
            held = git_text(checkout, "rev-parse", "--verify", "--quiet", f"{revision}:{path}")[1]
            # The second way runs the checkout's own clean filter, as `git add`
            # would on this working tree: the user's configuration on the
            # user's files, never the review tree's, and only once the
            # filter-free answer has come back different.
            for way in (["--no-filters"], []):
                ours = git_text(checkout, "hash-object", *way, "--", path)
                if ours[0] == 0 and held.strip() and ours[1].strip() == held.strip():
                    answers[path] = 0
                    break
    changed = [p for p, code in answers.items() if code == 1]
    failed = [p for p, code in answers.items() if code not in (0, 1)]
    # With the overrides the comparison used, so that for a file under a filter
    # the command shows the difference that was found, not the filter's form.
    how = command_line(
        ["git", *overrides, "--literal-pathspecs", "-C", checkout, "diff", *plain, revision, "--"],
        "<path>",
    )
    shows = f"`{how}` shows how"
    if how is None:
        # Without -C, as for `show` above; the overrides name drivers from the
        # user's config, so even this can find no form, and then says less.
        here = command_line(
            ["git", *overrides, "--literal-pathspecs", "diff", *plain, revision, "--"], "<path>"
        )
        shows = (
            f"`{here}`, run in the checkout, shows how"
            if here
            else "git diff in the checkout shows how"
        )
    # Only a local patch's snapshot is the working tree as the review read it,
    # so only there is a difference one made since -- and only where the
    # checkout is known to stand where it stood, which a run recorded before
    # stood_on cannot say. A head was read in a tree of its own, beside
    # whatever the working tree held then, so a difference from it may predate
    # the review, and is said without saying when.
    since = local and on_history and stood is not None
    if changed:
        differs = (
            "Changed in the checkout since the review" if since else "Different in the checkout"
        )
        lines.append(f"{differs}: {files(changed)} -- {shows}.")
    if failed:
        lines.append(f"Not compared: git could not diff {files(failed)}.")
    same = [p for p, code in answers.items() if code == 0]
    if same:
        rest = "every file it cites" if len(same) == len(paths) else files(same)
        unchanged = (
            "Unchanged in the checkout since the review" if since else "The same in the checkout"
        )
        lines.append(f"{unchanged}: {rest}.")
    return lines


def rules_tree(run_dir: str, pinned: Record, checkout: str) -> tuple[str | None, str | None]:
    """(tree, problem): the reviewed code, for rule derivation.

    Rule derivation tests each rule it suggests on the code that was reviewed,
    and long after the run that code is on disk nowhere: release removed the
    review tree, and a local patch never had one. So this checks it out again
    through review_tree -- at the head under scope mode 'revisions', which is
    the checkout itself when it still holds the head, and under 'local-patch'
    at the commit scope.py made of the working tree when it pinned the run;
    see trees.snapshot -- because checking it out is the step that would
    otherwise run the user's hooks and filters on the reviewed code, and that
    code can be a stranger's pull request.

    A tree is recorded in scope.json as `worktree`, the field release and prune
    already read, so `scope.py --release` removes it as it removes the run's
    own, and prune keeps the run while it stands; there is no second record to
    keep in step. One that already stands -- a run whose release was skipped,
    since release writes `worktree` back as null -- is never used again. It is
    removed from the checkout, where git runs, and checked out anew, because
    whatever is in it has been within reach of every subagent and tool that
    ran there since: git started inside it would read a repository its `.git`
    file names, and a tree a temp cleaner half swept would be handed over as
    the reviewed code.
    """
    named, problem = reviewed_revision(run_dir, pinned, checkout)
    if named is None:
        return None, problem
    revision, advice = named
    recorded = pinned.get("worktree")
    if is_run_tree(recorded, run_dir):
        # git will not check a tree out where it still lists one, so the
        # listing goes first, as release drops it. A directory still standing
        # after that is one git could not remove, and it is never handed over
        # as the reviewed code.
        #
        # Not through release, whose question differs: whether the tree the
        # record names is gone, from the checkout recorded, refusing one not
        # the run's. Here a new tree goes at this one path, so anything at it
        # -- a dangling link too, hence lexists -- stops the derivation; git
        # runs in the checkout this derivation was given, the only one a run
        # from before `checkout` was recorded has; and a value not the run's
        # tree is left alone, as release leaves it, and recorded over. A
        # record still naming this tree, when the checkout is then read in
        # place, is cleared by the release that ends every derivation.
        discard(checkout, recorded)
        if os.path.lexists(recorded):
            return None, (
                f"the review tree {recorded} from an earlier derivation could not be removed, "
                "and it is not handed over as the reviewed code. Remove that directory and ask "
                "again"
            )
    made, problem = review_tree(checkout, revision, run_dir, advice=advice)
    if problem:
        return None, problem
    if made is None:
        return checkout, None
    # And the repository the tree is registered in, which is where release
    # runs git: the same value scope.py recorded, unless this run predates the
    # field or the user named another with --checkout -- and then the record
    # has to follow, or release would refuse the tree it is asked to remove.
    # Until the record is written, nothing but this process knows the tree
    # stands, so any failure removes it.
    record = pinned.copy()
    record["worktree"] = made
    record["checkout"] = checkout
    try:
        write_record(run_dir, record)
    except OSError as error:
        discard(checkout, made)
        return None, f"cannot record the review tree in {run_dir}/scope.json: {error}"
    except BaseException:
        discard(checkout, made)
        raise
    return made, None


def release(run_dir: str) -> int:
    """Remove the run's review tree, if it has one. Safe to run on any run.

    review_tree creates the worktree, and rules_tree makes it again for rule
    derivation, so the script that made it owns removing it, and the
    orchestrator runs one command at the end of every run -- including one
    that ends early -- and after every rule derivation, instead of testing
    `worktree` for null and composing a git command of its own. A run read in
    place has nothing to release and exits 0.

    It removes only a directory named exactly as the run directory, inside one
    named TREES_DIR, whatever scope.json says -- see trees.is_run_tree. git refuses,
    for its part, any path that is not a worktree of this checkout. git runs in
    the user's checkout, and removes the registration under its .git/worktrees
    along with the directory -- or the registration alone, when the directory
    was already deleted by hand. Nothing broader: `git worktree prune` would
    also drop the user's own worktrees on a drive that is not mounted.
    Releasing twice is not an error; the second finds nothing left.
    """

    def refuse(message: str, status: int) -> int:
        sys.stderr.write(f"Cannot release the review tree: {message}\n")
        return status

    run_dir = os.path.abspath(os.path.expanduser(run_dir))
    record = read_record(run_dir)
    if record is None:
        return refuse(f"{run_dir} {NO_RECORD}", 2)
    worktree = record.get("worktree")
    checkout = record.get("checkout")
    if worktree is None:
        return 0
    if not is_run_tree(worktree, run_dir) or checkout is None:
        return refuse(f"scope.json in {run_dir} does not name this run's own review tree", 2)
    code, error = discard(checkout, worktree)
    if code != 0 and os.path.exists(worktree):
        return refuse(f"could not remove {worktree}: {error}", 4)
    # And the record says so, so it never names a directory that is gone: a
    # derivation after a release then makes its tree without first asking git
    # to remove one that is not there. Best effort -- a record left naming the
    # tree is one rules_tree already copes with.
    record["worktree"] = None
    with contextlib.suppress(OSError):
        write_record(run_dir, record)
    return 0
