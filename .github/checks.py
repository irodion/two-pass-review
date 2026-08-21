#!/usr/bin/env python3
"""The mechanical floor: the two non-negotiable constraints, plus link rot.

Usage:
    python3 .github/checks.py

This is the only automated verification in the repository, and it deliberately
checks nothing about *review quality*. What a pass finds, how a page reads, and
whether the renderer is right are settled by running the skill and reading the
report -- see AGENTS.md. Nothing here substitutes for that.

Needs Python 3.10+ for sys.stdlib_module_names, so it runs on the modern
interpreter only. That is fine: it is a check *about* the scripts, not one of
them, and nothing ships it to a user. The scripts themselves still have to
compile on 3.10, which is a separate job.
"""

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, ".agents", "skills", "two-pass-review")
SCRIPTS = os.path.join(SKILL, "scripts")

# The scripts import each other by bare name, because they are run as files from
# a directory the skill does not control and sys.path[0] is the only thing that
# reliably points at their siblings.
SIBLINGS = {"validate", "page", "markdown_subset", "render", "scope", "collect_docs"}


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


def page_script_parses(problems):
    """The page's one script lives inside a Python string, so nothing on the
    Python side ever looks at it. `py_compile` sees a string literal; the 3.10 and
    3.13 jobs see a string literal. A typo in it therefore ships a page that
    renders perfectly and a button that silently does nothing.

    `node --check` parses without executing, which is the whole of what is wanted
    here -- this is a syntax check, not a linter. It catches a typo. It cannot
    catch a *mistake*: misspell the `data-copy` attribute or get the selector
    wrong and this passes while the button stays dead. That is still settled by
    opening the report and clicking it, per AGENTS.md.

    Skips where node is absent rather than failing. ubuntu-latest ships node, so
    CI always runs it; a contributor without node loses the check and is told so,
    which is the same bargain stdlib_only strikes on Python 3.10."""
    path = os.path.join(SCRIPTS, "page.py")
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)

    source = None
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)):
            continue
        if any(isinstance(t, ast.Name) and t.id == "SCRIPT" for t in node.targets):
            source = node.value.value

    # Not "nothing to do": the constant being gone means either the page stopped
    # carrying a script, or it started building one some other way. Both want a
    # human, so neither is allowed to pass quietly.
    if not isinstance(source, str):
        problems.append("page.py: no SCRIPT string constant, so its JavaScript was not checked")
        return

    node_bin = shutil.which("node")
    if node_bin is None:
        sys.stdout.write("  skipped: no node on PATH, so page.py's SCRIPT was not parsed\n")
        return

    handle = tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False)
    try:
        handle.write(source)
        handle.close()
        result = subprocess.run(
            [node_bin, "--check", handle.name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        if result.returncode != 0:
            # The temp path is in node's message and means nothing to a reader, so
            # it is swapped for the name of the thing they would actually edit.
            # Both spellings, longest first: on macOS tempfile hands back
            # /var/folders/... while node reports the resolved
            # /private/var/folders/..., and substituting the short one first
            # leaves a severed "/private" behind.
            detail = result.stdout
            for name in sorted({handle.name, os.path.realpath(handle.name)}, key=len, reverse=True):
                detail = detail.replace(name, "page.py:SCRIPT")
            detail = detail.strip()
            problems.append("page.py: SCRIPT is not valid JavaScript --\n    {}".format(detail))
    finally:
        os.unlink(handle.name)


HOSTILE = (
    "[x](javascript:alert(1))",
    "[x](JaVaScRiPt:alert(1))",
    "[x](data:text/html,<script>alert(1)</script>)",
    "[x](vbscript:msgbox)",
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "<IMG SRC=x ONERROR=alert(1)>",
)
HREF = re.compile(r'href="([^"]*)"', re.IGNORECASE)

# Restated here rather than imported from markdown_subset. Judging the output
# with the module's own is_safe_url makes the oracle regress along with the
# thing it is judging: flip that function to `return True` and every assertion
# below still passes, which is exactly the regression this exists to catch.
SAFE_PREFIXES = ("http://", "https://", "mailto:")


def sanitiser_holds(problems):
    """Constraint 2 again, from the other end -- and the only check here that runs
    the code rather than reading it.

    The static scan above looks at page.py, so a regression in the URL sanitiser
    would walk straight past it: markdown_subset.py is where a scheme becomes an
    href, and that is the one place a link in a finding can turn into script. So
    the sanitiser is exercised on input written to get through it.

    Escaped text is not a finding. '&lt;img src=x onerror=alert(1)&gt;' in the
    output is the sanitiser working, which is why this asserts on tags and href
    values rather than grepping for 'onerror'."""
    # This is the one check that imports from the tree it is checking, and an
    # import writes __pycache__/ next to the scripts. A check has no business
    # leaving anything behind in the working copy -- it got committed once.
    sys.dont_write_bytecode = True
    sys.path.insert(0, SCRIPTS)
    try:
        from markdown_subset import Markdown
    except ImportError as error:  # pragma: no cover - a broken import is the floor job's problem
        problems.append("cannot import markdown_subset: {}".format(error))
        return

    renderer = Markdown(known_ids=set())
    for source in HOSTILE:
        rendered = renderer.render(source)
        lowered = rendered.lower()
        for tag in ("<script", "<img", "<iframe", "<svg"):
            if tag in lowered:
                problems.append("markdown_subset: {!r} survived {!r} unescaped".format(tag, source))
        for url in HREF.findall(rendered):
            if not url.lower().startswith(SAFE_PREFIXES):
                problems.append("markdown_subset: emitted href={!r} from {!r}".format(url, source))

    # The opposite failure -- a sanitiser that strips everything -- would satisfy
    # every assertion above while making the report's cross-references dead text.
    safe = renderer.render("[ok](https://example.com)")
    if 'href="https://example.com"' not in safe:
        problems.append("markdown_subset: a safe https link no longer renders as a link")


def _git(problems, *args):
    """Run git, or record why it could not run and return None.

    Both callers read git's output to decide whether something is absent, and at
    that level empty output and a failed command are indistinguishable. Ignoring
    the exit status therefore reports success when git is missing, when the tree
    is not a repository, or when the index is locked -- the check passes loudest
    exactly when it saw nothing. Returning None makes the caller choose."""
    result = subprocess.run(
        ["git"] + list(args), cwd=ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        problems.append(
            "git {} failed ({}): {}".format(
                " ".join(args), result.returncode, result.stderr.strip() or "no output"
            )
        )
        return None
    return result.stdout


def committed_symlink(problems):
    """The trap that is invisible locally: an absolute symlink works for whoever
    wrote it and is broken for everyone who clones, and git shows nothing wrong
    because it stores the path as the file's contents."""
    rel = os.path.join(".claude", "skills", "two-pass-review")
    listing = _git(problems, "ls-files", "-s", rel)
    if listing is None:
        return
    out = listing.strip()
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


def no_build_artifacts(problems):
    """Nothing generated by running the code belongs in the tree.

    This one is here because it happened: a .pyc was committed, written by the
    sanitiser check above importing the module it exercises, and picked up by a
    `git add -A`. The skill directory is copied wholesale into other people's
    repositories, so a stray .pyc does not just sit there -- it travels, stale
    and for the wrong interpreter."""
    # -z, because git quotes any path it thinks unusual: a tracked 'ünïcode.pyc'
    # prints as "\303\274n\303\257code.pyc", trailing quote and all, so
    # endswith('.pyc') is false and the file walks straight through. Confirmed,
    # not assumed. Null-delimited output is never quoted, and does not split on
    # the spaces in a path either.
    listing = _git(problems, "ls-files", "-z")
    if listing is None:
        return
    for path in listing.split("\0"):
        if not path:
            continue
        if "__pycache__" in path or path.endswith((".pyc", ".pyo")):
            problems.append("{}: build artifact is tracked".format(path))


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
    checks = (
        stdlib_only,
        page_script_parses,
        sanitiser_holds,
        committed_symlink,
        no_build_artifacts,
        links_resolve,
    )
    for check in checks:
        check(problems)
    for problem in problems:
        sys.stderr.write("  {}\n".format(problem))
    if problems:
        sys.stderr.write("\n{} problem(s).\n".format(len(problems)))
        return 1
    # Says what it checked, not what it hopes. It parsed the page's script; it did
    # not click the button, and it did not open a report. Saying more than that is
    # how a green tick starts standing in for the thing it cannot do.
    sys.stdout.write(
        "stdlib-only; page SCRIPT parses; sanitiser rejects unsafe schemes; "
        "symlink relative; no build artifacts tracked; links resolve.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
