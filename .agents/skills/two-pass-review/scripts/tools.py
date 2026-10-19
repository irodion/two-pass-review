#!/usr/bin/env python3
"""Find a third-party tool on PATH and start it safely. Stdlib only, 3.10 syntax.

Not a script -- a library two of them share. `try_rule.py` tests a suggested
rule with semgrep or ruff; `hints.py` lints the changed files with ruff, Biome
and shellcheck. Both run a program the user installed against code the
reviewed branch controls, and every protection that takes was worked out once,
for try_rule.py: the tool is found on a PATH with every entry left out that the
reviewed code could put a program in, it is started only when it is the tool
itself and never a version manager's shim, and it runs in a process group of
its own that a timeout or an interrupt takes down whole. Written twice, a fix
to one would not reach the other.

What the tool is given -- its configuration, its flags, the files it reads --
stays each caller's business, because that is where the two differ.
"""

import contextlib
import os
import re
import shutil
import signal
import subprocess

# The whole validator, for within, as try_rule.py imports it for confine. Bare,
# with no sys.path mutation, for page.py's reason: the entrypoint that imported
# this put the directory on the path.
import validate

# What a file name could use to break the Tested line or be read as markdown:
# the escape character itself, code spans, links, and raw HTML.
MARKUP = "\\`[]<>"

# How a compiled program begins: ELF, or Mach-O, thin in either byte order or
# fat.
COMPILED = (
    b"\x7fELF",
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xfe\xed\xfa\xce",
    b"\xca\xfe\xba\xbe",
    b"\xca\xfe\xba\xbf",
)

# A python interpreter's name once its links are followed, as the #! line of
# a script pip or uv installs names it: python3.13, or python3.13t.
PYTHON = re.compile(r"python[0-9.]*t?")


def safe_path(directories: list[str]) -> str:
    """PATH without an entry the reviewed code could put a program in.

    An empty entry and `.` mean the current directory, and a relative one is
    read from it, so wherever the tool or a program it starts stands, any of
    them can find a file the reviewed branch committed. An absolute entry
    inside either directory is the same file by another name.
    """
    entries = [
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if os.path.isabs(entry) and not any(inside(d, entry) for d in directories)
    ]
    return os.pathsep.join(entries)


def inside(directory: str, path: str) -> bool:
    """validate.within, for two paths not yet resolved."""
    return validate.within(os.path.realpath(directory), os.path.realpath(path))


def head(path: str) -> bytes:
    """A program's first bytes, as much as Linux reads of a #! line; nothing when unreadable."""
    try:
        with open(path, "rb") as handle:
            return handle.read(256)
    except OSError:
        return b""


def shim(name: str, found: str, directories: list[str]) -> str | None:
    """Why `found`, first on PATH for `name`, may not be the tool itself, or None when it is.

    A version manager puts a shim on PATH in the tool's place, and the shim
    picks the program it starts from a file in the directory it runs in, which
    here is the reviewed code: a branch that commits a .tool-versions saying
    `ruff path:evil` has asdf start the branch's own evil/bin/ruff, and the
    line reads tested. asdf, pyenv and rbenv write their shims as shell
    scripts; mise, aqua and Volta link every name to one program that goes by
    the name it was started under. So the tool is started only as itself: a
    compiled program of its own name once its links are followed, or a script
    whose #! line names a compiled python by absolute path, which is what pip
    and uv install semgrep as. That refuses a tool someone wrapped by hand,
    and pip's /bin/sh launcher for an interpreter whose path is too long for
    a #! line; both get a line saying why.

    Not the tool started somewhere else instead: ruff resolves the relative
    paths of a --config file against the directory it runs in, and the file
    is the repository's own settings, whose per-file-ignores would then match
    nothing. Nor a later entry on PATH when the first is a shim: semgrep
    starts pysemgrep by name, and on that PATH would find the shim's first. A
    program the tool starts by name -- git, for semgrep -- is not judged here,
    since that would take a version manager's git.
    """
    tool = os.path.realpath(found)
    if os.path.basename(tool) != name:
        return (
            f"{name} on PATH is a link to {shown(tool)}, which goes by the name it was started "
            "under, as a version manager's shim does, and a shim picks the program it starts from "
            f"the directory it runs in, here the reviewed code. Put a {name} that is the tool "
            "itself first on PATH."
        )
    start = head(tool)
    if start.startswith(COMPILED):
        return None
    words = start[2:].split(b"\n", 1)[0].split() if start.startswith(b"#!") else []
    named = os.fsdecode(words[0]) if words else ""
    interpreter = os.path.realpath(named)
    if (
        os.path.isabs(named)
        and PYTHON.fullmatch(os.path.basename(interpreter))
        and head(interpreter).startswith(COMPILED)
        and not any(inside(directory, interpreter) for directory in directories)
    ):
        return None
    return (
        f"{name} on PATH is {shown(tool)}, which is not {name} itself but a program that starts "
        "it, as a version manager's shim does, and a shim picks the program it starts from the "
        f"directory it runs in, here the reviewed code. Put a {name} that is the tool itself "
        "first on PATH."
    )


def shown(path: str) -> str:
    """A file name as the Tested line prints it: anything that is not plain text escaped."""
    return "".join(
        f"\\{c}" if c in MARKUP else c if c.isprintable() else ascii(c)[1:-1] for c in path
    )


def run(
    command: list[str], directory: str, env: dict[str, str], timeout: int
) -> tuple[int | None, str, str]:
    """(exit status or None when it did not finish, stdout, stderr) of one run in `directory`.

    The tool runs in a process group of its own, and a run that times out, or
    that this script is interrupted in, takes the whole group with it. semgrep
    becomes pysemgrep, which starts semgrep-core as a child, and killing the
    process it started -- all subprocess.run's timeout does -- left the engine
    scanning on after the line said not tested. A group of its own also keeps
    a signal meant for this script's group from reaching it -- the terminal's
    interrupt, or the terminate a caller sends when its own timeout runs out --
    so each is passed on here, as the kill: an interrupt arrives as
    KeyboardInterrupt, and main makes a terminate or a hangup arrive as
    SystemExit.
    """
    with subprocess.Popen(
        command,
        cwd=directory,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as process:
        try:
            out, err = process.communicate(timeout=timeout)
        except BaseException as stopped:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            if isinstance(stopped, subprocess.TimeoutExpired):
                return None, "", ""
            raise
    return process.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def find(name: str, directories: list[str]) -> tuple[str | None, str, str]:
    """(the tool's own path or None, the PATH to start it with, why there is no tool).

    `directories` are the ones the reviewed branch controls: the reviewed code,
    the checkout, and anything a caller copied out of either. The PATH comes
    back whether or not the tool was found, because it is also the PATH every
    program the tool starts by name is found on.
    """
    path = safe_path(directories)
    tool = shutil.which(name, path=path)
    if tool is None:
        return None, path, f"{name} is not on PATH."
    if any(inside(directory, tool) for directory in directories):
        return None, path, f"the only {name} on PATH is inside the reviewed code or the checkout."
    reason = shim(name, tool, directories)
    if reason:
        return None, path, reason
    return os.path.realpath(tool), path, ""
