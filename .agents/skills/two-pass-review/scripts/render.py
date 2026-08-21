#!/usr/bin/env python3
"""Render a merged two-pass-review artifact as one self-contained HTML report.

Usage:
    render.py FINDINGS_JSON

Stdlib only, Python 3.10 syntax. The page carries no embedded JSON, no network
requests and no sibling assets -- everything it needs is in the single file it
writes. Its script is fixed -- a clipboard handler behind the copy buttons and a
class toggle behind `Mark dealt with` -- and nothing a pass wrote is ever
interpolated into it.

Internal flags (not user-facing surface):
    --no-open        write and print the path without launching a browser
    --latest PATH    also refresh a stable copy of the report at PATH
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # sibling script, same directory
from page import render_page


# --- delivery ----------------------------------------------------------------


# Long enough for a cold powershell.exe start, short enough that a wedged
# opener cannot make the render look like it died. The open is a convenience;
# no convenience is worth stalling the mechanism.
OPENER_TIMEOUT = 10


def launch(command, cwd=None):
    """Run an opener to completion. None if it never started at all.

    Failing to start and starting badly are different answers, and the bottom
    of the WSL ladder needs to tell them apart: explorer.exe's exit code is not
    evidence either way, but a binary that could not be executed has certainly
    opened nothing.

    A rung that outlives the timeout gets killed and reported as success, not
    failure, because it certainly started and may well have opened the report
    before wedging -- xdg-open blocks until the application it launched exits
    on some desktops. Calling that a failure would fire the rung below and
    risk a second window, the exact double-open the ladder is built to avoid.
    """
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            timeout=OPENER_TIMEOUT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    except subprocess.TimeoutExpired:
        return 0
    return completed.returncode


def ran_ok(command, cwd=None):
    """Whether the opener reported success, not merely that it started.

    The distinction only matters under WSL, where openers are tried in order:
    a helper that returns True for any process that launched makes every rung
    below the first unreachable, and the ladder becomes decoration.
    """
    return launch(command, cwd=cwd) == 0


def is_wsl():
    """WSL reports itself as Linux, and the browser it can reach is Windows'.

    Either interop variable is conclusive; /proc/version is the fallback for a
    shell that inherited a scrubbed environment. Only ever reached from the
    Linux branch, so no other platform stats /proc.
    """
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/version", encoding="utf-8") as handle:
            return "microsoft" in handle.read().lower()
    except OSError:
        return False


def translate_path(*arguments):
    """Ask wslpath, in either direction. Nothing here composes a path by hand.

    Composing \\\\wsl.localhost\\<distro>\\... means guessing the distro when
    WSL_DISTRO_NAME is unset, and it silently corrupts a path that is already
    on a Windows drive, rewriting it to a UNC path that names a different file.
    Composing the other direction is worse: [automount] root in /etc/wsl.conf
    moves where the drives are mounted, so /mnt/c is a default and not a fact.
    wslpath knows both, and Microsoft's interop guidance is to ask it.

    Decoded explicitly rather than with text=True, which follows the locale --
    and a POSIX-locale shell is exactly where a repository slug outside ASCII
    would come back mangled.

    Timed out like the openers, and for the same reason: wslpath answers
    instantly or the interop channel is wedged, and a wedged channel hanging
    here would stall the render before any opener even ran. No translation
    means falling through, which is the honest outcome of no answer.
    """
    try:
        completed = subprocess.run(
            ["wslpath", *arguments],
            check=False,
            timeout=OPENER_TIMEOUT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    except subprocess.TimeoutExpired:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", "replace").strip() or None


def open_in_wsl(path):
    """Hand the report to Windows, which owns the browser on this host.

    A WSL box usually has no Linux browser and often no xdg-open, so the Linux
    opener finds nothing and the report never appears -- which is the failure
    this exists to fix.
    """
    if shutil.which("wslview") and ran_ok(["wslview", path]):
        return True

    target = translate_path("-w", path)
    if target is None:
        return False

    # A Windows binary inherits this process's directory, and a Linux directory
    # reaches Windows as \\wsl.localhost\... -- which start and explorer refuse
    # outright with "UNC paths are not supported". Anywhere on a drive fixes it,
    # and the file being opened is named absolutely either way.
    #
    # Asked for rather than assumed to be /mnt/c, because [automount] root is
    # configurable. Checked with isdir because wslpath translates the syntax
    # without caring whether anything is mounted there, and a cwd that does not
    # exist fails the launch outright -- worse than the UNC directory it was
    # meant to avoid.
    from_drive = translate_path("-u", "C:\\")
    if from_drive is not None and not os.path.isdir(from_drive):
        from_drive = None

    if shutil.which("powershell.exe"):
        # Single-quoted, with any quote in the path doubled, because this is a
        # PowerShell command line rather than an argument vector. -FilePath is
        # the parameter Start-Process actually has -- it is the positional one,
        # so this is the invocation the failing run confirmed by hand -- and it
        # takes the path literally, so a [ or ] in a repository slug stays a
        # character rather than becoming a wildcard.
        command = "Start-Process -FilePath '{}'".format(target.replace("'", "''"))
        if ran_ok(["powershell.exe", "-NoProfile", "-Command", command], cwd=from_drive):
            return True

    # Last, and its exit code ignored, because explorer.exe exits 1 even when
    # it succeeds. Higher up the ladder it would open the report, report
    # failure, and have the rung below it open the report a second time.
    #
    # Whether it started is still worth knowing. Interop can be turned off
    # while the Windows directories stay on PATH, so the binary is findable and
    # unrunnable at once -- and answering True there would suppress the
    # xdg-open the caller would otherwise fall through to.
    if shutil.which("explorer.exe"):
        return launch(["explorer.exe", target], cwd=from_drive) is not None
    return False


def open_in_browser(path):
    """Best effort, never fatal. The printed path is always the real mechanism."""
    for variable in ("CODEX_SANDBOX", "CI", "SSH_CONNECTION"):
        if os.environ.get(variable):
            return False
    system = platform.system()
    if system == "Darwin":
        opener = ["open"]
    elif system == "Linux":
        if is_wsl() and open_in_wsl(path):
            return True
        # Falling through rather than returning: a box with interop disabled in
        # /etc/wsl.conf can still have WSLg and a Linux browser, and opening the
        # report there beats not opening it at all.
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            return False
        opener = ["xdg-open"]
    elif system == "Windows":
        opener = ["cmd", "/c", "start", ""]
    else:
        return False
    if not shutil.which(opener[0]):
        return False
    # A plain path, never a file:// URL -- a '#' in the path truncates the URL.
    return ran_ok([*opener, path])


def main(argv):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("findings", metavar="FINDINGS_JSON")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--latest")
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit:
        return 2

    source = os.path.abspath(args.findings)
    problems = validate.validate_paths([source])
    if problems:
        sys.stderr.write("Refusing to render an invalid artifact:\n\n")
        for problem in problems:
            sys.stderr.write(f"  {problem}\n")
        sys.stderr.write("\nRepair the artifact and render again.\n")
        return 1

    with open(source, encoding="utf-8") as handle:
        merged = json.load(handle)

    target = os.path.join(os.path.dirname(source), "report.html")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(render_page(merged))

    if args.latest:
        shutil.copyfile(target, os.path.abspath(args.latest))

    # The path goes out before any opener runs, flushed past stdout's buffer,
    # because the printed path is the mechanism and the open is the
    # convenience: a harness that kills a stalled render must still find the
    # report named in what already reached it.
    sys.stdout.write(f"{target}\n")
    sys.stdout.write(f"file://{target}\n")
    sys.stdout.flush()

    if not args.no_open:
        open_in_browser(target)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
