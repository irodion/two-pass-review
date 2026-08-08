#!/usr/bin/env python3
"""Render a merged atomic-review artifact as one self-contained HTML report.

Usage:
    render.py FINDINGS_JSON

Stdlib only, Python 3.9 syntax. The page carries no JavaScript, no embedded
JSON, no network requests and no sibling assets -- everything it needs is in
the single file it writes.

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
import validate  # noqa: E402  (sibling script, same directory)
from page import render_page  # noqa: E402


# --- delivery ----------------------------------------------------------------


def open_in_browser(path):
    """Best effort, never fatal. The printed path is always the real mechanism."""
    for variable in ("CODEX_SANDBOX", "CI", "SSH_CONNECTION"):
        if os.environ.get(variable):
            return False
    system = platform.system()
    if system == "Darwin":
        opener = ["open"]
    elif system == "Linux":
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            return False
        opener = ["xdg-open"]
    elif system == "Windows":
        opener = ["cmd", "/c", "start", ""]
    else:
        return False
    if not shutil.which(opener[0]):
        return False
    try:
        # A plain path, never a file:// URL -- a '#' in the path truncates the URL.
        subprocess.run(opener + [path], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


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
            sys.stderr.write("  {}\n".format(problem))
        sys.stderr.write("\nRepair the artifact and render again.\n")
        return 1

    with open(source, "r", encoding="utf-8") as handle:
        merged = json.load(handle)

    target = os.path.join(os.path.dirname(source), "report.html")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(render_page(merged))

    if args.latest:
        shutil.copyfile(target, os.path.abspath(args.latest))

    if not args.no_open:
        open_in_browser(target)

    sys.stdout.write("{}\n".format(target))
    sys.stdout.write("file://{}\n".format(target))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
