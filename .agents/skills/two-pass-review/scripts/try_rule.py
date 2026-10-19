#!/usr/bin/env python3
"""Test one suggested rule on the reviewed code and on the checkout. Stdlib only, 3.10 syntax.

Usage:
    try_rule.py --tool semgrep --config FILE --tree DIR --checkout DIR [--expect FILE ...]
    try_rule.py --tool ruff --config FILE --select CODES --tree DIR --checkout DIR [--expect FILE ...]

Rule derivation's subagent runs this once per rule, and never starts the tool
itself. Every protection a rule test needs used to be prose in rule 4 of the
rule-derivation prompt, for the subagent to type each time -- make a directory,
find the tool, start it by its absolute path, turn off ruff's cache and its
fixes, leave out `extend` -- and this change had already shown what prose costs
here: the review tree was prose once, and the user's post-checkout hook ran.
So the steps are this script's, and the prompt holds one command.

Only semgrep and ruff, because theirs are the configurations that cannot make
the tool run code; a linter's plugins, a type checker's, a build or a test
suite all run what the repository names. The tool is found on PATH with every
entry left out that is empty, relative or inside either directory, and it is
started with that PATH too, so a program it starts by name -- semgrep starts
git -- cannot be one the reviewed branch committed. It is started only when
it is the tool itself, never a version manager's shim, which picks the
program it starts from the directory it runs in. The rule file is taken
only from the temporary directory, where rule 4 has the subagent make one
with mktemp -d, because a tool quotes a line of a file it cannot parse. ruff
runs with its cache and its fixes off, given the subagent's file as its only
configuration, and refuses one with an `extend` key, which makes ruff read
whatever file it names and quote a line of it in its parse error, or with any
backslash, because an escape can spell the key without its letters. semgrep's
settings, log and version files go in a directory of this script's, removed
afterwards, rather than in a home directory a sandbox may not let it write.

Each tool picks the files it reads, and semgrep's picks leave out tests/,
vendor/, build/ and the like by default, and whatever a .semgrepignore in the
reviewed code names. So --expect takes the files the finding is about, relative
to the reviewed code, and each is handed to the tool by name as well, because
both tools read a file named to them whatever their own selection says; a file
still not read is a rule not tested, and the line says which. The reviewed
code chooses those names, so they follow `--` on every command line: a file
called `--fix`, named as a target, would otherwise be read as ruff's flag and
turn its fixes back on, and one called `--autofix` as semgrep's, which applies
a rule's fixes to the files it scans. A name that begins with `-` is refused
besides, since nothing a finding cites needs one.

It prints one line first, the one that goes under the suggestion: "Tested:"
and each place the rule flagged, by file and line, in the reviewed code and in
the checkout as it is now; or "Not tested:" and why. A file name in it comes
from the reviewed code, so a character that would end the line or read as
markdown is escaped. A run that read no files is not tested, because a rule
that flags nothing there says nothing. When the tool could not run the rule,
the lines after the first are its errors, for the subagent to fix the rule
with -- never the tool's report on the code.

Exit status: 0 tested, 1 not tested, 2 bad invocation.
"""

import argparse
import json
import os
import re
import shutil
import signal
import sys
import tempfile
from collections.abc import Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# The whole validator, for confine and within, as scope.py and collect_docs.py
# already import it for confine: so that --expect and the path checks confine
# a path exactly as the validator confines a pass's location. Importing all of
# it costs a run nothing it can notice; moving the two into a module of their
# own would change three scripts that work, and what none of them does.
import validate  # sibling module, same directory
from tools import find, inside, run, shown

# Per directory, the places named before the rest are only counted, so a rule
# that flags hundreds of lines does not bury the line it is for.
LISTED = 20

# How long one run may take. A large repository takes semgrep minutes; one that
# takes longer than this is a run nobody is waiting for.
TIMEOUT = 600

# ruff's selectors: codes, prefixes and ALL, comma-separated. Held to this so a
# value can never be read as another option.
SELECTORS = re.compile(r"[A-Z][A-Z0-9]*(,[A-Z][A-Z0-9]*)*")

# A ruff rule code. Its JSON also carries syntax errors, under a code that is
# not one ("invalid-syntax", or null in older versions), and those are no
# rule's.
RULE_CODE = re.compile(r"[A-Z]+[0-9]+")

# An `extend` key, wherever TOML lets a key stand: at the start of a line, after
# the dot of a dotted key such as `ruff.extend`, or inside an inline table after
# `{` or `,` -- bare or quoted, with any spaces TOML allows around it.
EXTEND = re.compile(r"""(?<![\w-])["']?extend["']?\s*=""")

# No escape is recognised, because every escape is refused: any backslash in a
# ruff configuration. An escape is how a quoted key says `extend` without its
# six letters, and the set of escapes is the parser's, not this file's to list --
# \u and \U in TOML 1.0, and \x in the TOML 1.1 that newer ruff reads, which
# got past a check for the first two alone. A pattern that needs one is written
# without it: [0-9] for \d.

# A tool's run: places flagged or None when it failed, files read, and errors.
Outcome = tuple[list[tuple[str, int]] | None, set[str], list[str]]


def places(found: list[tuple[str, int]]) -> str:
    """`a.py:3, b.py:7`, at most LISTED of them, or `nothing`."""
    if not found:
        return "nothing"
    named = [f"{shown(path)}:{line}" for path, line in sorted(set(found))]
    listed = ", ".join(named[:LISTED])
    return listed if len(named) <= LISTED else f"{listed}, and {len(named) - LISTED} more"


def tail(text: str) -> list[str]:
    """The last lines of a tool's error output, each cut short."""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return [line[:300] for line in lines[-15:]]


def semgrep_problem(config: str, select: str | None) -> str | None:
    """Why this semgrep invocation cannot be tested, or None."""
    if select is not None:
        return "--select is ruff's; a semgrep rule file names its own rules"
    return None


def semgrep(
    tool: str,
    config: str,
    select: str | None,
    directory: str,
    targets: list[str],
    env: dict[str, str],
) -> Outcome:
    """One semgrep run in `directory`. `select` is ruff's, and semgrep_problem refused it."""
    code, out, err = run(
        [
            tool,
            "scan",
            "--json",
            "--metrics=off",
            "--disable-version-check",
            f"--config={config}",
            "--",
            *targets,
        ],
        directory,
        env,
        TIMEOUT,
    )
    if code is None:
        return None, set(), [f"semgrep did not finish within {TIMEOUT} seconds"]
    try:
        report = json.loads(out)
        found = [
            (os.path.normpath(result["path"]), int(result["start"]["line"]))
            for result in report["results"]
        ]
        read = {os.path.normpath(path) for path in report["paths"]["scanned"]}
        errors = [error for error in report.get("errors", []) if error.get("level") == "error"]
    except (ValueError, KeyError, TypeError):
        return None, set(), tail(err) or [f"semgrep exited {code} and printed no report"]
    if errors or code not in (0, 1):
        lines = []
        for error in errors:
            what = error.get("long_msg") or error.get("message") or error.get("short_msg") or ""
            where = [
                f"{span.get('file')}:{span.get('start', {}).get('line')}"
                for span in error.get("spans") or []
                if span.get("file") == config
            ]
            lines.append(
                f"{error.get('type')}: {what}" + (f" (at {', '.join(where)})" if where else "")
            )
        return None, set(), [line[:300] for line in lines] or tail(err)
    return found, read, []


def ruff_problem(config: str, select: str | None) -> str | None:
    """Why this ruff invocation cannot be tested, or None."""
    if select is None or not SELECTORS.fullmatch(select):
        return "--tool ruff takes --select with the rule's codes, such as TID251, comma-separated"
    try:
        with open(config, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as error:
        return f"cannot read --config {config}: {error}"
    if EXTEND.search(text):
        return (
            "the ruff configuration has an extend key, which makes ruff read whatever file it "
            "names. Copy what you need from that file into this one instead"
        )
    if "\\" in text:
        return (
            "the ruff configuration has a backslash, and an escape can spell an extend key. "
            "Write it without one -- [0-9] rather than \\d in a pattern"
        )
    return None


def ruff(
    tool: str,
    config: str,
    select: str | None,
    directory: str,
    targets: list[str],
    env: dict[str, str],
) -> Outcome:
    """One ruff run in `directory`, with the codes ruff_problem held to its selector syntax."""
    common = ["check", "--no-cache", f"--config={config}"]
    code, out, err = run(
        [
            tool,
            *common,
            "--no-fix",
            "--no-fix-only",
            "--output-format=json",
            f"--select={select}",
            "--",
            *targets,
        ],
        directory,
        env,
        TIMEOUT,
    )
    if code is None:
        return None, set(), [f"ruff did not finish within {TIMEOUT} seconds"]
    if code not in (0, 1):
        return None, set(), tail(err) or [f"ruff exited {code}"]
    root = os.path.realpath(directory)
    try:
        diagnostics = json.loads(out)
        found = [
            (
                os.path.relpath(os.path.realpath(entry["filename"]), root),
                int(entry["location"]["row"]),
            )
            for entry in diagnostics
            if isinstance(entry.get("code"), str) and RULE_CODE.fullmatch(entry["code"])
        ]
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, set(), tail(err) or ["ruff printed no report"]
    # The files a run reads, which its JSON does not say: --show-files lists
    # them with the same configuration and targets, and lints nothing.
    code, out, err = run([tool, *common, "--show-files", "--", *targets], directory, env, TIMEOUT)
    if code != 0:
        return None, set(), tail(err) or [f"ruff --show-files exited {code}"]
    read = {
        os.path.relpath(os.path.realpath(line), root) for line in out.splitlines() if line.strip()
    }
    return found, read, []


# Each tool: what refuses its invocation, and what runs it. One table, so that
# what a tool needs is stated beside the tool and nowhere else, and main holds
# no branch on which tool it has -- the checks were once three such branches,
# far apart in main. Both entries take ruff's arguments, so semgrep's check
# ignores `config` and its runner `select`: that is the price of one shape,
# and a new tool is one row with no change to main. So is ruff's `select`
# typed optional, the str ruff_problem has made it hidden from mypy; a runner
# handed None anyway would pass --select=None, which ruff refuses, and the
# line would say not tested.
Problem = Callable[[str, str | None], str | None]
Runner = Callable[[str, str, str | None, str, list[str], dict[str, str]], Outcome]
TOOLS: dict[str, tuple[Problem, Runner]] = {
    "semgrep": (semgrep_problem, semgrep),
    "ruff": (ruff_problem, ruff),
}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--tool", required=True, choices=tuple(TOOLS))
    parser.add_argument("--config", required=True, metavar="FILE")
    parser.add_argument("--select", metavar="CODES")
    parser.add_argument("--tree", required=True, metavar="DIR")
    parser.add_argument("--checkout", required=True, metavar="DIR")
    parser.add_argument("--expect", action="append", default=[], metavar="FILE")
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit:
        return 2

    def refuse(message: str) -> int:
        sys.stderr.write(f"Cannot test the rule: {message}\n")
        return 2

    def untested(reason: str, errors: tuple[str, ...] = ()) -> int:
        sys.stdout.write("".join(f"{line}\n" for line in (f"Not tested: {reason}", *errors)))
        return 1

    config, tree, checkout = (
        os.path.abspath(os.path.expanduser(path))
        for path in (args.config, args.tree, args.checkout)
    )
    for name, directory in (("--tree", tree), ("--checkout", checkout)):
        if not os.path.isdir(directory):
            return refuse(f"{name} {directory} is not a directory")
    if not os.path.isfile(config):
        return refuse(f"--config {config} is not a file")
    # A file the tool cannot parse comes back quoted, a line of it among the
    # errors passed on, so a --config taken from anywhere would read out a line
    # of any file the user can read: a key, another project's .env. Rule 4 has
    # the subagent write its file in a directory made with mktemp -d, so that
    # is the one place a file is taken from -- mktemp's, $TMPDIR or /tmp, and
    # never tempfile.gettempdir()'s, which is the working directory when no
    # temporary directory can be written.
    temp = os.environ.get("TMPDIR") or "/tmp"
    if not inside(temp, config):
        return refuse(
            f"--config {config} is outside the temporary directory {temp}; write it in a "
            "directory of your own, made with mktemp -d"
        )
    if inside(tree, config) or inside(checkout, config):
        return refuse(
            f"--config {config} is inside the reviewed code or the checkout; write it in a "
            "directory of your own, made with mktemp -d"
        )
    problem_of, runner = TOOLS[args.tool]
    problem = problem_of(config, args.select)
    if problem:
        return refuse(problem)
    # Confined as a pass's location is, so a name cannot hand the tool a file
    # outside the directory it checks, and never one that begins with `-`,
    # which the `--` before the targets already keeps from being read as a
    # flag -- refused as well, so that one slip in a command line is not the
    # whole of the defence.
    expected = []
    for path in args.expect:
        if path.startswith("-"):
            return refuse(
                f"--expect {path} begins with -, which a file a finding cites never needs"
            )
        resolved = validate.confine(tree, path)
        if resolved is None or not os.path.isfile(resolved):
            return refuse(f"--expect {path} names no file in the reviewed code, relative to it")
        expected.append(os.path.normpath(path))

    directories = [tree, checkout]
    tool, path, reason = find(args.tool, directories)
    if tool is None:
        return untested(reason)

    same = os.path.realpath(tree) == os.path.realpath(checkout)
    named = (
        [("the reviewed code, which is the checkout as it is now", tree, True)]
        if same
        else [("the reviewed code", tree, True), ("the checkout now", checkout, False)]
    )
    state = tempfile.mkdtemp(prefix="two-pass-review-rule-")
    env = {
        **os.environ,
        "PATH": path,
        "SEMGREP_SETTINGS_FILE": os.path.join(state, "settings.yml"),
        "SEMGREP_LOG_FILE": os.path.join(state, "last.log"),
        "SEMGREP_VERSION_CACHE_PATH": os.path.join(state, "version"),
    }
    said: list[str] = []

    def stop(signum: int, frame: object) -> None:
        raise SystemExit(128 + signum)

    # So that run's cleanup sees a terminate or a hangup, as it sees an
    # interrupt, and the tool's group goes with this script; see run.
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop)
    try:
        for where, directory, reviewed in named:
            # The checkout as it is now may have lost an expected file since.
            here = [
                name
                for name in expected
                if (resolved := validate.confine(directory, name)) and os.path.isfile(resolved)
            ]
            found, read, errors = runner(tool, config, args.select, directory, [".", *here], env)
            if found is None:
                return untested(
                    f"{args.tool} could not run the rule in {where}. Its errors follow, for you "
                    "to fix the rule with; never copy them into your reply.",
                    tuple(errors),
                )
            if not read:
                return untested(f"{args.tool} read no files in {where}.")
            missed = [name for name in expected if name not in read] if reviewed else []
            if missed:
                return untested(
                    f"{args.tool} did not read {', '.join(shown(name) for name in missed)} in "
                    f"{where}, even named, so a rule for it cannot be shown to flag it."
                )
            count = len(read)
            said.append(
                f"in {where} ({count} file{'s' if count != 1 else ''} read), {places(found)}"
            )
    finally:
        shutil.rmtree(state, ignore_errors=True)
    sys.stdout.write(f"Tested: {'; '.join(said)}.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
