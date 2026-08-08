#!/usr/bin/env python3
"""The mechanical floor: the three non-negotiable constraints, plus link rot.

Usage:
    python3 .github/checks.py

This is the only automated verification in the repository, and it deliberately
checks nothing about *review quality*. What a pass finds, how a page reads, and
whether the renderer is right are settled by running the skill and reading the
report -- see AGENTS.md. Nothing here substitutes for that.

Needs Python 3.10+ for sys.stdlib_module_names, so it runs on the modern
interpreter only. That is fine: it is a check *about* the scripts, not one of
them, and nothing ships it to a user. The scripts themselves still have to
compile on 3.9, which is a separate job.
"""

import ast
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, ".agents", "skills", "two-pass-review")
SCRIPTS = os.path.join(SKILL, "scripts")

# The scripts import each other by bare name, because they are run as files from
# a directory the skill does not control and sys.path[0] is the only thing that
# reliably points at their siblings.
SIBLINGS = {"validate", "page", "markdown_subset", "render", "scope"}


def stdlib_only(problems):
    """Constraint 1. The skill is copied into repositories we never see, so a
    dependency is a thing that will be missing rather than a thing to install."""
    if not hasattr(sys, "stdlib_module_names"):
        problems.append("checks.py needs Python 3.10+ to know what the stdlib contains")
        return
    for name in sorted(os.listdir(SCRIPTS)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(SCRIPTS, name)
        with open(path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # A relative import has no module name to check.
                imported = [node.module] if node.level == 0 and node.module else []
            else:
                continue
            for module in imported:
                top = module.split(".")[0]
                if top in SIBLINGS or top in sys.stdlib_module_names:
                    continue
                problems.append("{}: imports {!r}, which is not in the stdlib".format(name, top))


# Handlers are named one by one rather than matched as on[a-z]+=, because page.py
# is Python: that pattern also fires on "only =", "once =" and "ongoing=1", and a
# check that fails on a variable name teaches people to stop reading it. The list
# is therefore knowingly incomplete -- it is a tripwire on the way an inline
# handler would actually arrive, not a proof that the page has none. The proof is
# reading a rendered report, which is what AGENTS.md sends you to do.
HANDLERS = (
    "onload", "onerror", "onclick", "onmouseover", "onmouseenter", "onfocus",
    "onblur", "onsubmit", "onchange", "onkeydown", "onkeyup", "ontoggle",
    "onanimationstart", "onanimationend", "ontransitionend", "onscroll",
)
NO_JS = re.compile(
    r"<script\b|javascript\s*:|\b(" + "|".join(HANDLERS) + r")\s*=", re.IGNORECASE
)


def no_javascript(problems):
    """Constraint 2. Only page.py is examined: the '<script' in markdown_subset.py
    is the sanitiser naming what it strips."""
    path = os.path.join(SCRIPTS, "page.py")
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    found = NO_JS.search(source)
    if found:
        problems.append(
            "page.py: contains {!r}; the page carries no JavaScript".format(found.group(0))
        )


def committed_symlink(problems):
    """The trap that is invisible locally: an absolute symlink works for whoever
    wrote it and is broken for everyone who clones, and git shows nothing wrong
    because it stores the path as the file's contents."""
    rel = os.path.join(".claude", "skills", "two-pass-review")
    out = subprocess.run(
        ["git", "ls-files", "-s", rel], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()
    if not out:
        problems.append("{} is not tracked".format(rel))
        return
    if not out.startswith("120000"):
        problems.append("{} is committed as a regular file, not a symlink".format(rel))
        return
    target = os.readlink(os.path.join(ROOT, rel))
    if os.path.isabs(target):
        problems.append("{} points at an absolute path ({})".format(rel, target))
    if not os.path.isdir(os.path.join(ROOT, rel)):
        problems.append("{} does not resolve to a directory".format(rel))


def links_resolve(problems):
    """A clone has to contain everything the docs point at. This has shipped
    broken once already -- see 1c60e38."""
    docs = [
        os.path.join(ROOT, "README.md"),
        os.path.join(ROOT, "AGENTS.md"),
        os.path.join(ROOT, "CONTEXT.md"),
        os.path.join(ROOT, "CODE_OF_CONDUCT.md"),
        os.path.join(SKILL, "SKILL.md"),
        os.path.join(SKILL, "NOTICE.md"),
    ]
    for doc in docs:
        with open(doc, "r", encoding="utf-8") as handle:
            text = handle.read()
        for target in _markdown_targets(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            # exists(), not isfile(): NOTICE.md links at references/ as a
            # directory, which GitHub renders as a browsable listing.
            path = os.path.normpath(os.path.join(os.path.dirname(doc), target.split("#")[0]))
            if not os.path.exists(path):
                problems.append(
                    "{}: links to {!r}, which a clone does not have".format(
                        os.path.relpath(doc, ROOT), target
                    )
                )


def _markdown_targets(text):
    """Inline links only. Enough for these files, and a real parser would be a
    dependency, which is the one thing this repository cannot have."""
    targets, index = [], 0
    while True:
        open_paren = text.find("](", index)
        if open_paren == -1:
            return targets
        close = text.find(")", open_paren)
        if close == -1:
            return targets
        targets.append(text[open_paren + 2 : close].strip())
        index = close + 1


def main():
    problems = []
    for check in (stdlib_only, no_javascript, committed_symlink, links_resolve):
        check(problems)
    for problem in problems:
        sys.stderr.write("  {}\n".format(problem))
    if problems:
        sys.stderr.write("\n{} problem(s).\n".format(len(problems)))
        return 1
    sys.stdout.write("stdlib-only, no JavaScript, symlink relative, links resolve.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
