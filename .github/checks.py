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
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from typing import IO

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


def sanitiser_holds(problems: list[str]) -> None:
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
        problems.append(f"cannot import markdown_subset: {error}")
        return

    renderer = Markdown(known_ids=set())
    for source in HOSTILE:
        rendered = renderer.render(source)
        lowered = rendered.lower()
        for tag in ("<script", "<img", "<iframe", "<svg"):
            if tag in lowered:
                problems.append(f"markdown_subset: {tag!r} survived {source!r} unescaped")
        for url in HREF.findall(rendered):
            if not url.lower().startswith(SAFE_PREFIXES):
                problems.append(f"markdown_subset: emitted href={url!r} from {source!r}")

    # The opposite failure -- a sanitiser that strips everything -- would satisfy
    # every assertion above while making the report's cross-references dead text.
    safe = renderer.render("[ok](https://example.com)")
    if 'href="https://example.com"' not in safe:
        problems.append("markdown_subset: a safe https link no longer renders as a link")


# What every payload below tries to plant. Names nothing the page ever uses, so
# one appearing as a tag or an attribute can only have come from a payload.
PLANTED = "pwn"


def _payload(field: str, block: bool) -> str:
    """Text built to break out of every context the page puts text in.

    It opens with a token naming its field, so the check can tell a field the
    page escaped from one it never rendered -- a field that does not reach the
    page passes every escaping test vacuously. Then a tag, a double- and a
    single-quoted attribute breakout for attribute values, closers for the
    elements whose content is text rather than markup -- `<title>` among them,
    where a tag is inert but a closer is not -- a script link for the markdown
    sanitiser, and character
    references that must arrive as written. A block field adds the markdown
    structures that build tags of their own, with a payload inside each.
    """
    text = (
        f"tok-{field} <{PLANTED}-tag></{PLANTED}-tag> \" {PLANTED}-dq=\"1 ' {PLANTED}-sq='1 "
        f"</title></textarea></style></script><script>{PLANTED}()</script> "
        f"[x](javascript:{PLANTED}()) <img src=x onerror={PLANTED}()> &amp; &lt; &"
    )
    if block:
        text += (
            f"\u2028\n\n- <{PLANTED}-tag> in a list\n\n> <{PLANTED}-tag> in a quote\n\n"
            f'```html\n</code></pre><{PLANTED}-tag {PLANTED}-dq="1">\n```\n\n'
            f"`<{PLANTED}-tag>` and **<{PLANTED}-tag>**, beside qa-1"
        )
    return text


def _short(field: str) -> str:
    """The same attack within the one-line, 64-character fields."""
    return f"tok-{field} <{PLANTED}-tag>\" {PLANTED}-dq=\"1 ' {PLANTED}-sq='1 </script>"


def _hostile_artifact(version: int, benign: bool = False) -> dict[str, object]:
    """A valid merged artifact with a payload in every field that carries text.

    Every field the page escapes rather than looks up: what a pass wrote, what
    the orchestrator passed in, and what the reviewed repository named -- a
    path, a document, the repository itself. Enums, ids, counts and timestamps
    are left out because the validator refuses any value of those it does not
    already know, so no artifact the renderer accepts can carry one.

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

    findings: list[dict[str, object]] = [
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
    security: dict[str, object] = {
        "producer": "security",
        "what_holds_up_md": block("holds"),
        "closing_md": block("closing"),
        "empty_reason_md": None,
        "requested_model": short("model"),
        "requested_effort": short("effort"),
    }
    quality: dict[str, object] = {
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

    artifact: dict[str, object] = {
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
            "examined": [text("doc")],
            "skipped": [{"path": text("skipped-doc"), "reason": text("skip-reason")}],
            "notes": [
                {
                    "path": text("doc"),
                    "kind": "stale",
                    "claim_md": block("claim"),
                    "why_md": block("why"),
                    "owed_md": block("owed"),
                }
            ],
        },
    }
    if version >= 4:
        artifact["self_check"] = [
            {
                "question": "Does sec-1 " + text("question"),
                "answer_md": block("answer"),
                "anchors": ["sec-1"],
            }
        ]
    return artifact


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


class _Collector(HTMLParser):
    """What a browser would build from the page: tags, attributes, and decoded text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, attrs))
        self.text.extend(value for _, value in attrs if value)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _parse(page: str) -> _Collector:
    collector = _Collector()
    collector.feed(page)
    collector.close()
    return collector


def page_escapes_everything(problems: list[str]) -> None:
    """Constraint 2 across the whole page, where sanitiser_holds covers markdown.

    Every field of a merged artifact that carries text gets a payload, the page
    is rendered, and what a browser would build from it is checked: no tag or
    attribute a payload planted, no inline event handler, no link to anything
    but an anchor or a safe scheme, and no more scripts, styles, images or
    frames than the same artifact renders with harmless text. The copy buttons
    are checked from the other end: the attribute has to decode back to exactly
    the payload copy_payload built, or the reader's clipboard gets something
    other than the finding.

    It escapes nothing itself and imports no rule from page.py: the oracle is
    an HTML parser, so a page.py that stopped escaping a field fails here
    whichever function it stopped in."""
    sys.dont_write_bytecode = True
    sys.path.insert(0, SCRIPTS)
    try:
        import page
        import validate
    except ImportError as error:  # pragma: no cover - a broken import is the floor job's problem
        problems.append(f"cannot import page or validate: {error}")
        return

    # Every field the schema knows has to be in the fixture, or named here as one
    # that cannot carry text. A field added to validate.py and not to
    # _hostile_artifact would otherwise be the one field this never tests.
    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {k for v in value.values() for k in keys(v)}
        if isinstance(value, list):
            return {k for v in value for k in keys(v)}
        return set()

    carried = keys(_hostile_artifact(4)) | keys(_hostile_artifact(3))
    not_text = {"untracked"}
    schema = (
        validate.MERGED_FIELDS,
        validate.RUN_FIELDS,
        validate.SCOPE_FIELDS,
        validate.EMBEDDED_PASS_FIELDS,
        validate.FINDING_FIELDS,
        validate.LOCATION_FIELDS,
        validate.DOCS_CHECK_FIELDS,
        validate.DOC_NOTE_FIELDS,
        validate.DOC_SKIP_FIELDS,
        validate.SELF_CHECK_FIELDS,
    )
    for missing in sorted(frozenset().union(*schema) - carried - not_text):
        problems.append(
            f"checks.py: _hostile_artifact carries no {missing!r}, so it is never tested"
        )

    for version in (4, 3):
        artifact = _hostile_artifact(version)
        # The payloads only prove anything about artifacts the renderer accepts,
        # so the fixture is held to the validator first. A schema change that
        # breaks it fails here, by name, instead of quietly testing nothing.
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

        where = f"page.py (v{version} artifact)"
        hostile = _parse(page.render_page(artifact))
        baseline = _parse(page.render_page(_hostile_artifact(version, benign=True)))

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

        for tag in COUNTED:
            planted = sum(1 for t, _ in hostile.tags if t == tag)
            own = sum(1 for t, _ in baseline.tags if t == tag)
            if planted != own:
                problems.append(
                    f"{where}: {planted} <{tag}> element(s) with payloads, {own} without"
                )

        # Rendered, not dropped: an escaping check passes vacuously on a field
        # that never reaches the page, and a sanitiser that deletes text is the
        # opposite failure, which escaping is not allowed to turn into.
        decoded = "\n".join(hostile.text)
        for token in sorted(set(re.findall(r"tok-[a-z-]+", json.dumps(artifact)))):
            if token not in decoded:
                problems.append(f"{where}: {token[4:]!r} never reaches the page")
        if f"<{PLANTED}-tag>" not in decoded:
            problems.append(f"{where}: the payloads' markup was removed rather than escaped")

        findings = artifact["findings"]
        assert isinstance(findings, list)
        live = {f["id"]: f for f in findings if f.get("falsified") is not True}
        copied = [
            value for _, attrs in hostile.tags for name, value in attrs if name == "data-copy"
        ]
        for finding in live.values():
            partners = [live[i] for i in finding.get("corroborated_by", []) if i in live]
            payload = page.copy_payload(finding, partners)
            for expected in (payload, payload + "\n\n" + page.PROMPT_WRAPPER):
                if expected not in copied:
                    problems.append(
                        f"{where}: {finding['id']}'s copy button does not decode to its payload"
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
            problems.append(f"{path}: build artifact is tracked")


def links_resolve(problems: list[str]) -> None:
    """A clone has to contain everything the docs point at. This has shipped
    broken once already -- see 1c60e38."""
    docs = [
        os.path.join(ROOT, "README.md"),
        os.path.join(ROOT, "AGENTS.md"),
        os.path.join(ROOT, "CONTEXT.md"),
        os.path.join(ROOT, "CODE_OF_CONDUCT.md"),
        os.path.join(SKILL, "SKILL.md"),
        os.path.join(SKILL, "NOTICE.md"),
        os.path.join(SKILL, "references", "prompts.md"),
    ]
    for doc in docs:
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


def main() -> int:
    problems: list[str] = []
    checks = (
        stdlib_only,
        page_script_parses,
        sanitiser_holds,
        page_escapes_everything,
        committed_symlink,
        no_build_artifacts,
        links_resolve,
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
        "stdlib-only; page SCRIPT parses; sanitiser rejects unsafe schemes; "
        "page escapes every text field; symlink relative; no build artifacts tracked; "
        "links resolve.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
