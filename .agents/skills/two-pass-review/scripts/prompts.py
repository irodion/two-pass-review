#!/usr/bin/env python3
"""Fill the subagent prompts in references/prompts.md with one run's values. Stdlib only, 3.10 syntax.

Usage:
    prompts.py --rules RUN_DIR [--checkout PATH]

Mostly a library: `scope.py` calls it to fill the four prompts every run sends,
write each into the run directory, and print the one line that hands it over.
The command line is the fifth, the rule-derivation prompt, which is asked for
long after a run and so is filled when it is asked for, from the prompts.md the
skill holds then -- never from a copy saved when the run was pinned, which
would replay old wording and paths that may no longer exist.

The orchestrator used to copy each block out of prompts.md and replace five or
six absolute paths by hand, and a weak orchestrator types a temp path wrong:
one run sent a pass `_303jvh` for `_393jvh`, which would have cost it the line
manifest without a word (#41). A script that printed prompts was refused once,
as SKILL.md duplicated in code that would drift from it. This holds no prompt
text at all: prompts.md stays the one place a prompt is written, and what is
filled here is only what scope.py resolved.

Exit status of the command line: 0 filled, 1 refused, 2 bad invocation.
"""

import argparse
import json
import os
import re
import shlex
import sys

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(SKILL_DIR, "references", "prompts.md")

# The prompts every run sends, by heading in prompts.md, and the name a run
# gives each: the file prompt.<name>.md in the run directory, and the key under
# `prompts` in what scope.py prints.
RUN_SECTIONS = {
    "Security pass": "security",
    "Quality pass": "quality",
    "Docs check": "docs",
    "Falsification check": "falsification",
}
RULES_SECTION = "Rule derivation"

# Angle-bracketed words a block says to the subagent rather than to this file:
# the rule-derivation prompt asks for titles that end "(from <ids>)".
LITERAL = frozenset(["ids"])

# Wide on purpose: a mistyped placeholder -- a capital, a digit -- must be
# refused as unknown, not shipped as text because it did not look like one.
PLACEHOLDER = re.compile(r"<([A-Za-z][A-Za-z0-9_-]*)>")
FENCE = re.compile(r"^(`{3,})(.*)$")


def run_values(
    *, repo_root: str, checkout: str, run_dir: str, context_diff: str, file_lines: str
) -> dict[str, str]:
    """What the four run prompts may name. The one statement of their placeholders."""
    return {
        "repo_root": repo_root,
        "checkout": checkout,
        "run_dir": run_dir,
        "context_diff": context_diff,
        "file_lines": file_lines,
        "skill-dir": SKILL_DIR,
    }


def rules_values(*, checkout: str, run_dir: str) -> dict[str, str]:
    """What the rule-derivation prompt may name, all of it read off the finished run."""
    return {
        "checkout": checkout,
        "run_dir": run_dir,
        "context_diff": os.path.join(run_dir, "context.diff"),
        "findings_json": os.path.join(run_dir, "findings.json"),
        "skill-dir": SKILL_DIR,
    }


# Derived from the functions above rather than listed beside them, so a
# placeholder exists in one place: load() refuses a block naming anything else,
# before scope.py has made a run directory for a fill to fail inside.
RUN_KEYS = frozenset(
    run_values(repo_root="", checkout="", run_dir="", context_diff="", file_lines="")
)
RULES_KEYS = frozenset(rules_values(checkout="", run_dir=""))


def _blocks(source: str) -> tuple[dict[str, list[str]] | None, str | None]:
    """(heading -> its ```text blocks, problem), reading fences the way markdown does.

    Line by line, never by pattern across the file: a `## ` line inside a
    fence is text, not a heading, and a fence opened inside a ```text block
    with as many backticks would close that block early in any markdown
    reader -- so it is refused by name instead of cutting the prompt short.
    A heading that appears twice is refused too: the later section would
    otherwise replace the earlier one without a word.
    """
    found: dict[str, list[str]] = {}
    heading: str | None = None
    fence = 0
    text: list[str] | None = None
    for line in source.split("\n"):
        if fence:
            if re.fullmatch(rf"`{{{fence},}}\s*", line):
                if text is not None and heading is not None:
                    found[heading].append("".join(part + "\n" for part in text))
                fence, text = 0, None
            elif text is not None:
                opener = FENCE.match(line)
                if opener and len(opener.group(1)) >= fence and opener.group(2).strip():
                    return None, (
                        f"'{heading}' opens a fence inside its text block, which would end the "
                        f"block early -- open the block with {'`' * (fence + 1)}text to nest one"
                    )
                text.append(line)
            continue
        if line.startswith("## "):
            heading = line[3:].strip()
            if heading in found:
                return None, f"'{heading}' appears twice"
            found[heading] = []
            continue
        opener = FENCE.match(line)
        if opener:
            fence = len(opener.group(1))
            text = [] if opener.group(2).strip() == "text" and heading is not None else None
    if fence:
        return None, f"a fence under '{heading}' is never closed"
    return found, None


def load(source: str) -> tuple[dict[str, str] | None, str | None]:
    """(blocks, problem): every prompt's one text block, unfilled, keyed by name.

    Checked whole, before scope.py makes anything: a missing section, a second
    block under one heading, or a placeholder nothing fills is a defect in the
    skill, and finding it after the run directory and the review tree exist
    would leave them behind.
    """
    sections, problem = _blocks(source)
    if sections is None:
        return None, problem
    wanted = {**RUN_SECTIONS, RULES_SECTION: "rules"}
    blocks: dict[str, str] = {}
    for heading, name in wanted.items():
        found = sections.get(heading)
        if found is None:
            return None, f"no section '{heading}'"
        if len(found) != 1:
            return None, f"'{heading}' holds {len(found)} text blocks, not one"
        allowed = RULES_KEYS if name == "rules" else RUN_KEYS
        unknown = sorted(set(PLACEHOLDER.findall(found[0])) - allowed - LITERAL)
        if unknown:
            return None, "'{}' uses placeholders nothing fills: {}".format(
                heading, ", ".join(f"<{u}>" for u in unknown)
            )
        blocks[name] = found[0]
    return blocks, None


def read() -> tuple[dict[str, str] | None, str | None]:
    """load(), on the prompts.md this skill ships.

    A file that is not UTF-8 is as broken as one that is missing, and it gets
    the same refusal rather than a traceback where scope.py promised exit 5.
    """
    try:
        with open(SOURCE, encoding="utf-8") as handle:
            return load(handle.read())
    except (OSError, UnicodeDecodeError) as error:
        return None, f"cannot read {SOURCE}: {error}"


def fill(block: str, values: dict[str, str]) -> str:
    """The block with every placeholder replaced by its value, shell-quoted.

    Quoted because a block hands its subagent commands -- the validator's,
    with --repo and two file paths -- and a checkout under `~/My Projects`
    would otherwise split into two arguments. shlex.quote leaves an ordinary
    path exactly as it was, so only a path that needs it changes, and in
    prose the quotes still read as one path.
    """
    return PLACEHOLDER.sub(
        lambda m: shlex.quote(values[m.group(1)]) if m.group(1) in values else m.group(0), block
    )


def handover(path: str) -> str:
    """The whole of what the orchestrator sends a subagent: where its prompt is.

    One line, printed whole, so nothing in it is retyped. The last sentence is
    what makes a mistyped path fail where it can be seen: without it, a
    subagent that cannot find its file may go looking, and find a
    prompt.<name>.md of the same name in an older run beside this one.
    """
    return (
        f"Read {shlex.quote(path)} from start to end and do what it says. It is your task, in "
        "full. If that file does not exist, reply only that it is missing, and do nothing else."
    )


def write_prompt(path: str, text: str) -> str:
    """Write one filled prompt and return the line that hands it over.

    The one place a prompt file is written, for the run's four and for rule
    derivation alike. An OSError is the caller's: scope.py lets it reach the
    block that removes the review tree, and the command line refuses with it.
    """
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return handover(path)


def write_run(blocks: dict[str, str], run_dir: str, values: dict[str, str]) -> dict[str, str]:
    """Write the four run prompts into the run directory. Returns name -> handover line."""
    return {
        name: write_prompt(os.path.join(run_dir, f"prompt.{name}.md"), fill(blocks[name], values))
        for name in RUN_SECTIONS.values()
    }


def write_rules(blocks: dict[str, str], run_dir: str, checkout: str) -> str:
    """Write the rule-derivation prompt into the run directory. Returns its handover line."""
    values = rules_values(checkout=checkout, run_dir=run_dir)
    return write_prompt(os.path.join(run_dir, "prompt.rules.md"), fill(blocks["rules"], values))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--rules", required=True, metavar="RUN_DIR")
    parser.add_argument("--checkout")
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit:
        return 2

    def refuse(message: str) -> int:
        sys.stderr.write(f"Cannot fill the rule-derivation prompt: {message}\n")
        return 1

    run_dir = os.path.abspath(os.path.expanduser(args.rules))
    try:
        with open(os.path.join(run_dir, "scope.json"), encoding="utf-8") as handle:
            pinned = json.load(handle)
    except (OSError, ValueError):
        return refuse(f"{run_dir} holds no scope.json written by scope.py")
    # The user's own repository, never the review tree, which --release removed
    # when the run ended. A run from before `checkout` was recorded has none,
    # and only the person asking knows where their repository is.
    checkout = args.checkout or (pinned.get("checkout") if isinstance(pinned, dict) else None)
    if not isinstance(checkout, str) or not checkout:
        return refuse(
            f"{run_dir}/scope.json records no checkout -- pass --checkout with the user's repository"
        )
    blocks, problem = read()
    if blocks is None:
        return refuse(f"references/prompts.md cannot be filled: {problem}")
    try:
        line = write_rules(blocks, run_dir, checkout)
    except OSError as error:
        return refuse(f"cannot write the prompt: {error}")
    sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
