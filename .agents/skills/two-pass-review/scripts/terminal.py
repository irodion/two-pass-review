#!/usr/bin/env python3
"""What a path or a command must be to reach a terminal, or a shell, as it is. Stdlib only, 3.10 syntax.

`render.py --agent` and `--show` print to a terminal or a harness that shows
one, and print commands an orchestrator copies into bash, zsh, Git Bash or
PowerShell. What may be printed raw, what a path must hold to be printed at
all, and how a word is quoted so that every one of those shells reads it alike
are stated here once, for render.py, runs.reviewed_code, and the scope.py and
prompts.py refusals of a checkout no command could name. Nothing here reads a
run or runs git.
"""

import os
import re
from typing import cast

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


# What a path printable_path refuses holds, for each refusal to say in the
# same words: a rule that grows -- the backtick did -- grows here.
UNPRINTABLE_PATH = "a line break, a control character, a backtick or a byte that is not UTF-8"


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
