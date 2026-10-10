#!/usr/bin/env python3
"""The mechanical floor: the two non-negotiable constraints, plus drift a clone would trip on.

Link rot, the committed symlink, build artifacts in the tree, flags the
documents name that no script takes, the two output contracts' shared
paragraphs drifting apart, and a change to either forked rubric.

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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from collections.abc import Callable
from typing import IO, Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, ".agents", "skills", "two-pass-review")
SCRIPTS = os.path.join(SKILL, "scripts")

# The scripts import each other by bare name, because they are run as files from
# a directory the skill does not control and sys.path[0] is the only thing that
# reliably points at their siblings. Read off the directory rather than kept by
# hand: a new script is a sibling the moment it exists, with no list to update.
SIBLINGS = {name[: -len(".py")] for name in os.listdir(SCRIPTS) if name.endswith(".py")}


def stdlib_only(problems: list[str]) -> None:
    """Constraint 1. The skill is copied into repositories we never see, so a
    dependency is a thing that will be missing rather than a thing to install."""
    if not hasattr(sys, "stdlib_module_names"):
        problems.append("checks.py needs Python 3.10+ to know what the stdlib contains")
        return
    for name in sorted(os.listdir(SCRIPTS)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(SCRIPTS, name)
        with open(path, encoding="utf-8") as handle:
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
                problems.append(f"{name}: imports {top!r}, which is not in the stdlib")


def page_script_parses(problems: list[str]) -> None:
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
    handle: IO[str]
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)

    source: object = None
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

    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(source)
    try:
        result = subprocess.run(
            [node_bin, "--check", handle.name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
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
            problems.append(f"page.py: SCRIPT is not valid JavaScript --\n    {detail}")
    finally:
        os.unlink(handle.name)


# Restated here rather than imported from markdown_subset. Judging the page with
# that module's own is_safe_url makes the oracle regress along with the thing it
# is judging: flip that function to `return True` and every link assertion below
# still passes, which is exactly the regression they exist to catch.
SAFE_PREFIXES = ("http://", "https://", "mailto:")


# What every payload below tries to plant. Names nothing the page ever uses, so
# one appearing as a tag or an attribute can only have come from a payload.
PLANTED = "pwn"


def _head(field: str) -> str:
    """How every payload opens, and what a reader must see of it, verbatim.

    A token naming the field, a tag, and character references. Found in the
    page's visible text, it proves three things at once: the field rendered at
    all -- one that never reaches the page passes every escaping test
    vacuously -- its tag arrived as text rather than being deleted, which is
    the failure escaping must never turn into, and `&amp;` and `&lt;` read as
    written rather than as the characters they would decode to.
    """
    return f"tok-{field} <{PLANTED}-tag> &amp;&lt;&"


def _payload(field: str, block: bool) -> str:
    """Text built to break out of every context the page puts text in.

    After the head: a double- and a single-quoted attribute breakout, closers
    for the elements whose content is text rather than markup -- `<title>`
    among them, where a tag is inert but a closer is not -- a script link for
    the sanitiser, and a safe-scheme link carrying an attribute breakout, which
    the sanitiser lets through and only escaping stops. A block field adds the
    markdown structures that build tags of their own, with a payload inside
    each, and the unsafe schemes in the spellings a sanitiser has to refuse
    whatever their case -- `JaVaScRiPt:`, `data:`, `vbscript:`.
    """
    text = (
        _head(field) + f" </{PLANTED}-tag> \" {PLANTED}-dq=\"1 ' {PLANTED}-sq='1 "
        f"</title></textarea></style></script><script>{PLANTED}()</script> "
        f'[x](javascript:{PLANTED}()) [y](https://e/"{PLANTED}-dq="1) '
        f"<img src=x onerror={PLANTED}()>"
    )
    if block:
        text += (
            f"\u2028\n\n- <{PLANTED}-tag> in a list\n\n> <{PLANTED}-tag> in a quote\n\n"
            f"- [a](JaVaScRiPt:{PLANTED}()) [b](data:text/html,<script>{PLANTED}()</script>) "
            f"[c](vbscript:{PLANTED}) <IMG SRC=x ONERROR={PLANTED}()>\n\n"
            f'```html"{PLANTED}-dq="1\n</code></pre><{PLANTED}-tag {PLANTED}-dq="1">\n```\n\n'
            f"`<{PLANTED}-tag>` and **<{PLANTED}-tag>**, beside qa-1"
        )
    return text


def _short(field: str) -> str:
    """The same attack within the one-line, 64-character fields."""
    return _head(field) + f"\" {PLANTED}-dq=\"1 ' {PLANTED}-sq='1"


def _hostile_artifact(version: int, benign: bool = False) -> dict[str, Any]:
    """A valid merged artifact with a payload in every field that carries text.

    Built here rather than kept as a file, and judged against invariants
    rather than a stored page: there is no expected output anywhere, which is
    what keeps it from being a test corpus.

    Every field the page escapes rather than looks up: what a pass wrote, what
    the orchestrator passed in, and what the reviewed repository named -- a
    path, a document, the repository itself. Enums, ids, counts and timestamps
    are left out because the validator refuses any value of those it does not
    already know, so no artifact the renderer accepts can carry one. Each field
    gets its own token, so no field can stand in for another: two examined
    documents, because a doc note's path has to be one of them and would
    otherwise be the only proof the examined list renders.

    Version 4 carries corroboration, a contest, doc notes and a self-check;
    version 3 carries a withdrawn finding and a pass that found nothing, the
    two render paths a version-4 artifact never reaches. `benign` swaps every
    payload for its bare token, which gives the page's own structure to compare
    against.
    """

    def text(field: str) -> str:
        return f"tok-{field}" if benign else _payload(field, block=False)

    def block(field: str) -> str:
        return f"tok-{field}, beside qa-1" if benign else _payload(field, block=True)

    def short(field: str) -> str:
        return f"tok-{field}" if benign else _short(field)

    findings: list[dict[str, Any]] = [
        {
            "id": "sec-1",
            "producer": "security",
            "disposition": "blocking",
            "severity": "high",
            "title": text("title"),
            "locations": [{"path": text("path"), "start_line": 3, "end_line": 9}],
            "body_md": block("body"),
            "confidence": "low",
            "confidence_rationale": text("rationale"),
        },
        {
            "id": "sec-2",
            "producer": "security",
            "disposition": "follow-up",
            "severity": "low",
            "title": text("second-title"),
            "locations": [{"path": text("second-path")}],
            "body_md": block("second-body"),
        },
    ]
    security: dict[str, Any] = {
        "producer": "security",
        "what_holds_up_md": block("holds"),
        "closing_md": block("closing"),
        "empty_reason_md": None,
        "requested_model": short("model"),
        "requested_effort": short("effort"),
    }
    quality: dict[str, Any] = {
        "producer": "quality",
        "what_holds_up_md": None,
        "closing_md": None,
        "empty_reason_md": None,
    }
    if version >= 4:
        findings[1]["contested_md"] = block("contest")
        findings[0]["corroborated_by"] = ["qa-1"]
        findings.append(
            {
                "id": "qa-1",
                "producer": "quality",
                "disposition": "blocking",
                "category": "legibility",
                "title": text("qa-title"),
                "locations": [{"path": text("qa-path"), "start_line": 1}],
                "body_md": block("qa-body"),
                "corroborated_by": ["sec-1"],
            }
        )
    else:
        findings[1]["falsified"] = True
        quality["empty_reason_md"] = block("empty")

    artifact: dict[str, Any] = {
        "schema_version": version,
        "kind": "merged",
        "verdict": "blocked",
        "run": {
            "mode": "parallel",
            "falsification": "ran",
            "docs_check": "ran",
            "generated_at": "2026-10-03T00:00:00Z",
            "scope": {
                "repo": text("repo"),
                "mode": "revisions",
                "label": short("label"),
                "base": text("base"),
                "head": text("head"),
                "files_changed": 1,
                "diff_bytes": 1,
            },
        },
        "passes": [security, quality],
        "findings": findings,
        "docs_check": {
            "examined": [text("doc"), text("noted-doc")],
            "skipped": [{"path": text("skipped-doc"), "reason": text("skip-reason")}],
            "notes": [
                {
                    "path": text("noted-doc"),
                    "kind": "stale",
                    "claim_md": block("claim"),
                    "why_md": block("why"),
                    "owed_md": block("owed"),
                }
            ],
        },
    }
    # A scope finds its base one way or the other, never both, so the two
    # versions split them. `since` has one shape, so it carries no payload, but
    # it is carried all the same: that is what puts its row under this check.
    scope = artifact["run"]["scope"]
    if version >= 4:
        artifact["self_check"] = [
            {
                "question": "Does sec-1 " + text("question"),
                "answer_md": block("answer"),
                "anchors": ["sec-1"],
            }
        ]
        scope["against"] = short("against")
    else:
        scope["since"] = "2026-10-03T00:00:00+03:00"
    return artifact


# Where each object validate.py closes a field set over sits in an artifact, by
# the name of that field set. Compared object by object rather than as one pool
# of names: a field added to one object under a name another object already
# uses would otherwise count as carried. A field set validate.py declares and
# this table does not place fails the check too, so a new kind of object cannot
# arrive untested either.
OBJECTS: dict[str, Callable[[dict[str, Any]], list[Any]]] = {
    "MERGED_FIELDS": lambda a: [a],
    "RUN_FIELDS": lambda a: [a["run"]],
    "SCOPE_FIELDS": lambda a: [a["run"]["scope"]],
    "EMBEDDED_PASS_FIELDS": lambda a: a["passes"],
    "FINDING_FIELDS": lambda a: a["findings"],
    "LOCATION_FIELDS": lambda a: [loc for f in a["findings"] for loc in f["locations"]],
    "DOCS_CHECK_FIELDS": lambda a: [a["docs_check"]],
    "DOC_NOTE_FIELDS": lambda a: a["docs_check"]["notes"],
    "DOC_SKIP_FIELDS": lambda a: a["docs_check"]["skipped"],
    "SELF_CHECK_FIELDS": lambda a: a.get("self_check", []),
}
# A standalone pass file's envelope. Inside a merged artifact the same object
# is EMBEDDED_PASS_FIELDS, placed above.
NOT_IN_ARTIFACT = frozenset(["PASS_FIELDS"])
# Fields that cannot carry text, by object.
NOT_TEXT = {"SCOPE_FIELDS": frozenset(["untracked"])}

# Elements a payload would plant to run code, load something, or restyle the
# page. The page uses some of them itself -- its one script, its one stylesheet,
# its filter inputs, its icons -- so they are counted against a benign render of
# the same artifact rather than forbidden.
COUNTED = (
    "script",
    "style",
    "img",
    "iframe",
    "svg",
    "object",
    "embed",
    "link",
    "meta",
    "base",
    "form",
    "input",
    "textarea",
    "button",
)

# What a reader sees without opening anything: text, and the tooltips `title`
# shows on hover. Not `data-copy`, which carries every field of its finding
# verbatim whether or not the card renders it.
VISIBLE_ATTRIBUTES = frozenset(["title"])


class _Collector(HTMLParser):
    """What a browser would build from the page: tags, attributes, visible text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []
        self.visible: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, attrs))
        self.visible.extend(v for k, v in attrs if k in VISIBLE_ATTRIBUTES and v)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        self.visible.append(data)


def _parse(page: str) -> _Collector:
    collector = _Collector()
    collector.feed(page)
    collector.close()
    return collector


def page_escapes_everything(problems: list[str]) -> None:
    """Constraint 2 across the whole page, the markdown sanitiser included.

    Every field of a merged artifact that carries text gets a payload, the page
    is rendered, and what a browser would build from it is checked: no tag or
    attribute a payload planted, no inline event handler, no link to anything
    but an anchor or a safe scheme, and no more scripts, styles, images or
    frames than the same artifact renders with harmless text. Each field's
    head has to be in the visible text, as written.

    What counts as markup is the HTML parser's call, never a rule imported from
    page.py, so a page.py that stops escaping a field fails here whichever
    function it stopped in. The copy buttons are the one comparison with
    page.py: their attribute has to decode back to exactly what copy_texts
    returns -- or report_markdown, for the report's own -- which tests the
    escaping and says nothing about what either chose to include. So what a
    finding's payload must carry -- its body, its contest, its paths -- is
    looked for verbatim as well, independently."""
    # The one check that imports from the tree it is checking, and an import
    # writes __pycache__/ next to the scripts. A check has no business leaving
    # anything behind in the working copy -- a .pyc from one got committed once.
    sys.dont_write_bytecode = True
    sys.path.insert(0, SCRIPTS)
    try:
        import page
        import validate
    except ImportError as error:  # pragma: no cover - a broken import is the floor job's problem
        problems.append(f"cannot import page or validate: {error}")
        return

    artifacts = {version: _hostile_artifact(version) for version in (4, 3)}

    declared = {name for name in vars(validate) if name.endswith("_FIELDS")}
    for name in sorted(declared - OBJECTS.keys() - NOT_IN_ARTIFACT):
        problems.append(
            f"checks.py: validate.{name} describes an object _hostile_artifact does not place"
        )
    for name, place in OBJECTS.items():
        carried: set[str] = set()
        for artifact in artifacts.values():
            for obj in place(artifact):
                carried.update(obj)
        fields: frozenset[str] = getattr(validate, name, frozenset())
        for missing in sorted(fields - carried - NOT_TEXT.get(name, frozenset())):
            problems.append(
                f"checks.py: _hostile_artifact carries no {missing!r} in {name}, "
                "so it is never tested"
            )

    for version, artifact in artifacts.items():
        where = f"rendered page (v{version} artifact)"
        # Named, never a traceback: a page.py that raises on hostile text is a
        # finding about page.py, and the checks after this one still owe their
        # answers. The exceptions a render can raise on unexpected data, not a
        # blanket catch -- an interrupt is not a problem with the page.
        try:
            # The payloads only prove anything about artifacts the renderer
            # accepts, so the hostile artifact is held to the validator first. A
            # schema change that breaks it fails here, by name, instead of
            # quietly testing nothing.
            with tempfile.TemporaryDirectory() as scratch:
                path = os.path.join(scratch, "findings.json")
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(artifact, handle)
                refused = validate.validate_paths([path])
            if refused:
                problems.append(
                    f"checks.py: the hostile v{version} artifact no longer validates -- "
                    + "; ".join(refused)
                )
                continue
            hostile = _parse(page.render_page(artifact))
            baseline = _parse(page.render_page(_hostile_artifact(version, benign=True)))
        except (LookupError, TypeError, ValueError, AttributeError) as error:
            problems.append(f"{where}: raised {type(error).__name__} on hostile text: {error}")
            continue

        for tag, attrs in hostile.tags:
            if tag.startswith(PLANTED):
                problems.append(f"{where}: a payload planted a <{tag}> element")
            for name, value in attrs:
                if name.startswith(PLANTED):
                    problems.append(f"{where}: a payload planted a {name!r} attribute on <{tag}>")
                elif name.startswith("on"):
                    problems.append(f"{where}: <{tag}> carries an inline handler, {name!r}")
                elif (
                    name in ("href", "src", "action", "formaction")
                    and value is not None
                    and not value.startswith("#")
                    and not value.lower().startswith(SAFE_PREFIXES)
                ):
                    problems.append(f"{where}: <{tag}> has {name}={value!r}")

        # The opposite failure: a sanitiser that refuses every link would pass
        # everything above while turning the report's links into dead text. The
        # payload's safe link has to arrive as a link, its breakout still inside
        # the value where escaping put it.
        safe = f'https://e/"{PLANTED}-dq="1'
        if not any(t == "a" and ("href", safe) in attrs for t, attrs in hostile.tags):
            problems.append(f"{where}: a safe https link no longer renders as a link")

        for tag in COUNTED:
            planted = sum(1 for t, _ in hostile.tags if t == tag)
            own = sum(1 for t, _ in baseline.tags if t == tag)
            if planted != own:
                problems.append(
                    f"{where}: {planted} <{tag}> element(s) with payloads, {own} without"
                )

        visible = "\n".join(hostile.visible)
        for field in sorted(set(re.findall(r"tok-([a-z-]+)", json.dumps(artifact)))):
            if _head(field) not in visible:
                problems.append(
                    f"{where}: {field!r} does not show as written -- missing, deleted, or "
                    "decoded once too often"
                )

        copied = [
            value for _, attrs in hostile.tags for name, value in attrs if name == "data-copy"
        ]
        # The report's own button carries every finding, so it would answer for
        # any one of them below. Only the cards' buttons may.
        report = page.report_markdown(artifact)
        cards = [value for value in copied if value != report]
        live = {f["id"]: f for f in artifact["findings"] if f.get("falsified") is not True}
        for finding in live.values():
            for expected in page.copy_texts(finding, page.partners_of(finding, live)):
                if expected not in copied:
                    problems.append(
                        f"{where}: {finding['id']}'s copy button does not decode to its payload"
                    )
            owed = [finding["body_md"], *(loc["path"] for loc in finding["locations"])]
            if finding.get("confidence_rationale"):
                owed.append(finding["confidence_rationale"])
            # Quoted into the payload a line at a time, so it is owed a line at a time.
            owed += finding.get("contested_md", "").split("\n")
            if not any(all(part in value for part in owed) for value in cards if value):
                problems.append(
                    f"{where}: no copy button carries {finding['id']}'s body, paths, "
                    "rationale and contest verbatim"
                )
        # The one payload composed from every finding, carried whole like the
        # rest: the same comparison, against report_markdown.
        if report not in copied:
            problems.append(
                f"{where}: the report-as-markdown button does not decode to its payload"
            )


def _git(problems: list[str], *args: str) -> str | None:
    """Run git, or record why it could not run and return None.

    Both callers read git's output to decide whether something is absent, and at
    that level empty output and a failed command are indistinguishable. Ignoring
    the exit status therefore reports success when git is missing, when the tree
    is not a repository, or when the index is locked -- the check passes loudest
    exactly when it saw nothing. Returning None makes the caller choose."""
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        problems.append(
            "git {} failed ({}): {}".format(
                " ".join(args), result.returncode, result.stderr.strip() or "no output"
            )
        )
        return None
    return result.stdout


def committed_symlink(problems: list[str]) -> None:
    """The trap that is invisible locally: an absolute symlink works for whoever
    wrote it and is broken for everyone who clones, and git shows nothing wrong
    because it stores the path as the file's contents."""
    rel = os.path.join(".claude", "skills", "two-pass-review")
    listing = _git(problems, "ls-files", "-s", rel)
    if listing is None:
        return
    out = listing.strip()
    if not out:
        problems.append(f"{rel} is not tracked")
        return
    if not out.startswith("120000"):
        problems.append(f"{rel} is committed as a regular file, not a symlink")
        return
    target = os.readlink(os.path.join(ROOT, rel))
    if os.path.isabs(target):
        problems.append(f"{rel} points at an absolute path ({target})")
    if not os.path.isdir(os.path.join(ROOT, rel)):
        problems.append(f"{rel} does not resolve to a directory")


def no_build_artifacts(problems: list[str]) -> None:
    """Nothing generated by running the code belongs in the tree.

    This one is here because it happened: a .pyc was committed, written by a
    check here importing the module it exercises, and picked up by a
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
            problems.append(f"{path}: build artifact is tracked")


def links_resolve(problems: list[str]) -> None:
    """A clone has to contain everything the docs point at. This has shipped
    broken once already -- see 1c60e38."""
    for doc in DOCS:
        with open(doc, encoding="utf-8") as handle:
            text = handle.read()
        for target in _markdown_targets(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            # exists(), not isfile(): NOTICE.md links at references/ as a
            # directory, which GitHub renders as a browsable listing.
            path = os.path.normpath(os.path.join(os.path.dirname(doc), target.split("#")[0]))
            if not os.path.exists(path):
                problems.append(
                    f"{os.path.relpath(doc, ROOT)}: links to {target!r}, which a clone does not have"
                )


def _markdown_targets(text: str) -> list[str]:
    """Inline links only. Enough for these files, and a real parser would be a
    dependency, which is the one thing this repository cannot have."""
    targets: list[str] = []
    index = 0
    while True:
        open_paren = text.find("](", index)
        if open_paren == -1:
            return targets
        close = text.find(")", open_paren)
        if close == -1:
            return targets
        targets.append(text[open_paren + 2 : close].strip())
        index = close + 1


# Where an orchestrator copies commands from. A flag in these is checked even
# where no script stands beside it -- SKILL.md says `--confirm-large` in a
# sentence, and a weak orchestrator types it as written.
COMMAND_DOCS = (
    os.path.join(SKILL, "SKILL.md"),
    os.path.join(SKILL, "references", "prompts.md"),
)
# Every document a clone ships, all of them link-checked. The ones past the
# first two teach other tools' flags too, so only a command naming one of this
# skill's scripts is held to anything there.
DOCS = (
    *COMMAND_DOCS,
    os.path.join(ROOT, "README.md"),
    os.path.join(ROOT, "AGENTS.md"),
    os.path.join(ROOT, "CONTEXT.md"),
    os.path.join(ROOT, "CODE_OF_CONDUCT.md"),
    os.path.join(SKILL, "NOTICE.md"),
)
FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")
SCRIPT = re.compile(r"\b(\w+)\.py\b")
# Where one shell command ends and the next begins, so that `a.py && git ...`
# does not hand git's flags to a.py.
OPERATOR = re.compile(r"&&|\|\||;|\|")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# A flag a script reads off argv[1] before argparse runs, as a mode of its own:
# scope.py's `--release RUN_DIR`. It is honoured only as the one flag given.
SOLO = re.compile(r"""argv\[1:2\]\s*==\s*\[["'](--[a-z][a-z0-9-]*)["']\]""")


def _script_flags(name: str) -> tuple[set[str], set[str]] | str:
    """(every flag, the solo flags) a script takes, or why they could not be read.

    What --help prints, because merge.py builds `--security-model` and its
    siblings in a loop, and the SOLO pattern, because --release is read before
    argparse and so never appears in --help. Colour is turned off and stripped
    anyway: Python 3.14 colours help when FORCE_COLOR is set, and an escape
    code ending in `m` stands where the lookbehind wants a space.
    """
    path = os.path.join(SCRIPTS, f"{name}.py")
    try:
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
    except OSError as error:
        return f"cannot read {path}: {error}"
    solo = set(SOLO.findall(source))
    flags = set(solo)
    if 'if __name__ == "__main__":' in source:
        env = {k: v for k, v in os.environ.items() if k != "FORCE_COLOR"}
        env |= {"PYTHON_COLORS": "0", "NO_COLOR": "1"}
        try:
            shown = subprocess.run(
                [sys.executable, "-B", path, "--help"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return f"{name}.py --help did not finish within 60 seconds"
        # Judged by what it printed, never by its exit status: every script here
        # catches argparse's SystemExit and returns 2, so a perfect --help exits
        # 2 too. One that raised while importing or building its parser prints
        # no usage at all, and reading flags off that would blame the documents.
        shown_text = ANSI.sub("", shown.stdout)
        if "usage:" not in shown_text:
            detail = (shown.stderr.strip().splitlines() or ["no output"])[-1]
            return f"{name}.py --help printed no usage (exit {shown.returncode}): {detail}"
        flags |= set(FLAG.findall(shown_text))
    return flags, solo


def _subshells(command: str) -> tuple[str, list[str]]:
    """The command with each `$( ... )` taken out, and what was inside each.

    Taken out so a git call's flags are not read as the script's around it,
    and kept so a script called inside one -- `run_dir=$(python3 scope.py ...)`
    -- is checked as a command of its own.
    """
    inner: list[str] = []
    while (start := command.find("$(")) != -1:
        depth, end = 0, start + 1
        while end < len(command):
            depth += {"(": 1, ")": -1}.get(command[end], 0)
            if depth == 0:
                break
            end += 1
        inner.append(command[start + 2 : end])
        command = command[:start] + " " + command[end + 1 :]
    return command, inner


def _code(text: str) -> list[str]:
    """Where commands are written: each fenced block's logical lines, and each inline span.

    An inline span may wrap onto the next line -- the docs are hard-wrapped,
    and `scope.py\n--release` is one span -- but never across a blank line, and
    it closes only on a backtick run as long as the one that opened it.
    """
    found: list[str] = []

    def fenced(match: re.Match[str]) -> str:
        found.extend(match.group(1).replace("\\\n", " ").split("\n"))
        return "\n\n"

    rest = re.sub(r"(?ms)^[ \t]*```[^\n]*\n(.*?)^[ \t]*```[ \t]*$", fenced, text)
    for paragraph in re.split(r"\n[ \t]*\n", rest):
        index = 0
        while (opener := re.search(r"`+", paragraph[index:])) is not None:
            start = index + opener.end()
            run = opener.group(0)
            closer = re.search(rf"(?<!`){run}(?!`)", paragraph[start:])
            if closer is None:
                index = start
                continue
            found.append(paragraph[start : start + closer.start()].replace("\n", " "))
            index = start + closer.end()
    return found


def _check_command(
    command: str,
    held: bool,
    flags: dict[str, tuple[set[str], set[str]]],
    unread: set[str],
    every: set[str],
    where: str,
    problems: list[str],
) -> None:
    """Hold one command's flags to the scripts it names; `held` holds the rest to any script's."""
    command, inner = _subshells(command)
    for nested in inner:
        _check_command(nested, held, flags, unread, every, where, problems)
    for simple in OPERATOR.split(command):
        # A git command's flags are git's to judge -- `git fetch origin
        # pull/<number>/head` is SKILL.md teaching git, not a script.
        if simple.split()[:1] == ["git"]:
            continue
        named = [m for m in SCRIPT.finditer(simple) if m.group(1) in flags or m.group(1) in unread]
        stray = FLAG.findall(simple[: named[0].start()] if named else simple)
        if held:
            for flag in stray:
                if flag not in every:
                    problems.append(f"{where}: `{flag}` is a flag of no script here")
        for index, this in enumerate(named):
            after = named[index + 1] if index + 1 < len(named) else None
            script = this.group(1)
            if script in unread:
                continue
            known, solo = flags[script]
            given = FLAG.findall(simple[this.end() : after.start() if after else len(simple)])
            for flag in given:
                if flag not in known:
                    problems.append(
                        f"{where}: `{script}.py {flag}` -- {script}.py takes no such flag"
                    )
                elif flag in solo and given != [flag]:
                    problems.append(
                        f"{where}: `{script}.py {flag}` comes with other flags, and {script}.py "
                        f"honours {flag} only as the one flag given"
                    )


def docs_name_real_flags(problems: list[str]) -> None:
    """Every flag the documents hand an orchestrator is one a script takes, where it takes it.

    The orchestrators this skill is mostly driven by copy a command as written,
    so a flag renamed in a script and not in SKILL.md is a run that dies on its
    first step -- and nothing else here would notice, because CI exercises the
    scripts with the flags it spells itself, not the ones the documents do.
    """
    flags: dict[str, tuple[set[str], set[str]]] = {}
    unread: set[str] = set()
    for name in sorted(SIBLINGS):
        read = _script_flags(name)
        if isinstance(read, str):
            problems.append(f"documented flags: {read}")
            unread.add(name)
            continue
        flags[name] = read
    every = set().union(*(known for known, _ in flags.values()))
    for doc in DOCS:
        where = os.path.relpath(doc, ROOT)
        try:
            with open(doc, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as error:
            problems.append(f"{where}: cannot be read for its flags: {error}")
            continue
        for command in _code(text):
            # A script whose flags could not be read has its own problem above;
            # checking the documents against a guess would only bury it.
            _check_command(
                command, doc in COMMAND_DOCS and not unread, flags, unread, every, where, problems
            )


# The two output contracts tell two passes one set of rules, so most of their
# paragraphs are the same paragraph twice -- and an edit that reaches one copy
# is a rule the passes now disagree about. It happened: 2815638 had to put one
# fix into both by hand. So every paragraph is paired across the contracts by
# how it opens, once each file's own pass name is taken out, and a pair must
# read the same. The three lists are the exceptions, each a deliberate choice:
# a pair that differs on purpose, and a paragraph only one pass is told. One
# added to a contract without being added to the other or to its list fails,
# and so does a list entry that no longer names anything.
OPENING = 40
DIFFERS: tuple[str, ...] = (
    "You are the **PASS** pass. The orchestra",
    '```json {"id": "ID-1", "producer": "PASS',
    "| field | required | value | |---|---|--",
    "**Cross-reference your own findings by i",
    "The merged report is **one list ordered ",
    "- `blocking` — this change should not me",
    "Argue the finding and say what to do abo",
    "**Open with the evidence.** The body's f",
    "**Write for a reader who is skimming.** ",
)
SECURITY_ONLY: tuple[str, ...] = (
    "1. **Is the defect in code this diff add",
    "## Sweep the callers before you close",
    "The breakage this rubric weights most he",
    "1. **List what the diff changed the shap",
    "A `note` carries **no severity**. `note`",
    "## Severity, by worked example",
    "Calibrate against these, drawn from what",
    "- **`critical`** — exploitable with no a",
    "If you are choosing between two levels, ",
)
QUALITY_ONLY: tuple[str, ...] = (
    "1. **Is the problem in code this diff ad",
    "## Sweep the repository before you close",
    "The claims this rubric wants are claims ",
    "1. **For every helper or pattern the dif",
    "`category` is the **Output Expectations*",
    "| tier | slug | |---|---| | 1 | `structu",
    "You emit no severity. Rank is what the t",
    "Choose by naming the cost: who pays if t",
)
# Each file has only its own pass name taken out: a paragraph copied from the
# other contract and not renamed -- the security pass told to write
# findings.quality.jsonl -- must not compare equal.
CONTRACTS = (
    ("security.md", "security", "sec", SECURITY_ONLY),
    ("code-quality.md", "quality", "qa", QUALITY_ONLY),
)


# The line that divides a rubric file: forked rubric above, this repository's
# output contract below. Matched as a whole line, and found one way only --
# two ways of finding it were two different splits of one file.
DIVIDER = re.compile(rb"(?m)^# Output contract[ \t]*\r?$")


def _split_rubric(name: str) -> tuple[bytes, bytes] | str:
    """(rubric half, contract half) of a rubric file; or what is wrong with it."""
    path = os.path.join(SKILL, "references", name)
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as error:
        return f"cannot be read: {error}"
    found = list(DIVIDER.finditer(data))
    if len(found) != 1:
        return f"holds {len(found)} '# Output contract' lines, where it needs exactly one"
    return data[: found[0].start()], data[found[0].start() :]


def _contract_paragraphs(name: str, own: str, prefix: str) -> dict[str, str] | str:
    """A contract's output-contract half, paragraph by paragraph, keyed by opening; or what is wrong."""
    split = _split_rubric(name)
    if isinstance(split, str):
        return f"{name} {split}"
    text = split[1].decode("utf-8", "replace")
    text = re.sub(rf"\b{own}\b", "PASS", text)
    text = re.sub(rf"\b{prefix}-(\d+|<n>)", r"ID-\1", text)
    paragraphs: dict[str, str] = {}
    for block in re.split(r"\n\s*\n", text):
        paragraph = " ".join(line.strip() for line in block.strip().split("\n"))
        if not paragraph:
            continue
        opening = paragraph[:OPENING]
        if opening in paragraphs:
            return f"{name} has two paragraphs opening {opening!r}, which cannot be paired"
        paragraphs[opening] = paragraph
    return paragraphs


def contracts_mirror(problems: list[str]) -> None:
    """The contracts' shared paragraphs read the same, and every difference is a listed one."""
    sides: dict[str, tuple[dict[str, str], tuple[str, ...]]] = {}
    for name, own, prefix, only in CONTRACTS:
        read = _contract_paragraphs(name, own, prefix)
        if isinstance(read, str):
            problems.append(f"contracts: {read}")
            return
        sides[name] = (read, only)
    (sec, sec_only), (qa, qa_only) = sides["security.md"], sides["code-quality.md"]
    for opening in sorted(set(sec) | set(qa)):
        if opening in sec and opening in qa:
            left, right = sec[opening], qa[opening]
            if left != right and opening not in DIFFERS:
                at = len(os.path.commonprefix([left, right]))
                problems.append(
                    f"contracts: the paragraph opening {opening!r} differs -- security.md has "
                    f"{left[max(0, at - 20) : at + 40]!r}, code-quality.md has "
                    f"{right[max(0, at - 20) : at + 40]!r}. An edit reached one copy, or the "
                    "difference is meant and belongs in DIFFERS"
                )
            elif left == right and opening in DIFFERS:
                problems.append(
                    f"contracts: DIFFERS lists {opening!r}, which now reads the same in both -- "
                    "take it out of DIFFERS"
                )
        else:
            name, only = (
                ("security.md", sec_only) if opening in sec else ("code-quality.md", qa_only)
            )
            if opening not in only:
                problems.append(
                    f"contracts: only {name} has a paragraph opening {opening!r} -- add it to the "
                    "other contract too, or list it as one pass's alone"
                )
    for listed, present, label in (
        (DIFFERS, set(sec) & set(qa), "DIFFERS"),
        (SECURITY_ONLY, set(sec) - set(qa), "SECURITY_ONLY"),
        (QUALITY_ONLY, set(qa) - set(sec), "QUALITY_ONLY"),
    ):
        for opening in listed:
            if opening not in present:
                problems.append(
                    f"contracts: {label} lists {opening!r}, which names no such paragraph now"
                )


# Where the pins live: a table in NOTICE.md, beside the list of authorised
# edits, so that changing a pin is an edit to that file -- the one a reviewer
# reads to see whether the change was authorised.
NOTICE = os.path.join(SKILL, "NOTICE.md")
PIN_ROW = re.compile(
    r"^\|\s*`references/(?P<name>[\w.-]+)`\s*\|\s*`(?P<pin>[0-9a-f]{64})`\s*\|\s*$", re.M
)
PINNED = ("security.md", "code-quality.md")


def rubric_hash(name: str) -> str:
    """The SHA-256 of a rubric file's forked text; or what is wrong with the file.

    The forked text is the rubric half less the provenance comment that opens
    it: that comment is this repository's, like the contract below, and a
    pointer in it must be fixable without touching the pin. LF line endings,
    so a checkout with core.autocrlf on hashes what CI hashes.

    A maintainer re-pinning after an edit NOTICE.md authorises prints this
    with: python3 -c 'import sys; sys.path.insert(0, ".github"); import checks;
    print(checks.rubric_hash("security.md"))'
    """
    split = _split_rubric(name)
    if isinstance(split, str):
        return split
    rubric = split[0]
    if rubric.lstrip().startswith(b"<!--") and b"-->" in rubric:
        rubric = rubric[rubric.index(b"-->") + 3 :].lstrip(b"\r\n")
    return hashlib.sha256(rubric.replace(b"\r\n", b"\n")).hexdigest()


def rubrics_frozen(problems: list[str]) -> None:
    """Each rubric's forked text is the bytes NOTICE.md pins.

    The rubrics are forked text and frozen (AGENTS.md), and the likeliest edit
    is a coding agent improving a sentence it was never meant to touch. So the
    failure says to revert, and never prints the hash that would silence it: a
    message is the one thing an agent is sure to read and act on, and a
    re-pin is a person's decision, made in NOTICE.md.
    """
    try:
        with open(NOTICE, encoding="utf-8") as handle:
            pins = {m.group("name"): m.group("pin") for m in PIN_ROW.finditer(handle.read())}
    except OSError as error:
        problems.append(
            f"{os.path.relpath(NOTICE, ROOT)}: cannot be read for the rubric pins: {error}"
        )
        return
    for name in PINNED:
        where = os.path.relpath(os.path.join(SKILL, "references", name), ROOT)
        actual = rubric_hash(name)
        if len(actual) != 64:
            problems.append(f"{where}: {actual}")
        elif name not in pins:
            problems.append(f"{where}: NOTICE.md pins no hash for it")
        elif actual != pins[name]:
            problems.append(
                f"{where}: the forked rubric text above '# Output contract' has changed. It is "
                "frozen (AGENTS.md) -- revert the edit. Only a change NOTICE.md lists as authorised "
                "may land, and whoever authorises it updates the pin there."
            )


def main() -> int:
    problems: list[str] = []
    checks = (
        stdlib_only,
        page_script_parses,
        page_escapes_everything,
        committed_symlink,
        no_build_artifacts,
        links_resolve,
        docs_name_real_flags,
        contracts_mirror,
        rubrics_frozen,
    )
    for check in checks:
        check(problems)
    for problem in problems:
        sys.stderr.write(f"  {problem}\n")
    if problems:
        sys.stderr.write(f"\n{len(problems)} problem(s).\n")
        return 1
    # Says what it checked, not what it hopes. It parsed the page's script; it did
    # not click the button, and it did not open a report. Saying more than that is
    # how a green tick starts standing in for the thing it cannot do.
    sys.stdout.write(
        "stdlib-only; page SCRIPT parses; a hostile payload in each text field renders "
        "as text and no unsafe link; symlink relative; no build artifacts tracked; "
        "links resolve; documented flags exist; mirrored contract paragraphs agree; "
        "forked rubric text matches NOTICE.md's pins.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
