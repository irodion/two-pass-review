#!/usr/bin/env python3
"""Run git, and read what it says as bytes or as text. Stdlib only, 3.10 syntax.

The one runner for every script that asks git anything -- scope.py, trees.py,
runs.py and prompts.py -- so every call decodes its output and reports failure
the same way. It cannot live in scope.py, which the others would then import
in a cycle, since scope.py imports prompts.py. The diff is read elsewhere:
only scope.py reads it, under a byte ceiling, so git_capped sits there beside
the ceiling it enforces.
"""

import os
import subprocess


def git(
    repo: str, *arguments: str, input: bytes | None = None, env: dict[str, str] | None = None
) -> tuple[int, bytes, str]:
    """Raw bytes out.

    Git's output is not guaranteed to be UTF-8. `git diff` calls a file text on
    a heuristic, so a Latin-1 comment or a mis-encoded fixture arrives as bytes
    a strict decoder rejects -- and a strict decode here would kill the whole
    review in step one, on a repository doing nothing unusual.

    `input` is forwarded to subprocess for the one caller that pipes a payload
    to git -- check-attr --stdin -- so that caller stays on this helper rather
    than rebuilding the argument vector and its own error handling by hand.
    `env` is added to the environment, for the one caller that points git at an
    index of its own -- trees.snapshot -- for the same reason.
    """
    result = subprocess.run(
        ["git", "-C", repo, *arguments],
        input=input,
        capture_output=True,
        env=None if env is None else {**os.environ, **env},
    )
    return result.returncode, result.stdout, result.stderr.decode("utf-8", "replace").strip()


def git_text(repo: str, *arguments: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Git output as text, with undecodable bytes replaced rather than fatal.

    The replacement character is the honest answer: it says "this byte was not
    text" in the one place a reviewer can see it, and it costs one character
    instead of one review.
    """
    code, out, error = git(repo, *arguments, env=env)
    return code, out.decode("utf-8", "replace"), error


def toplevel(path: str) -> str | None:
    """The top of the checkout that `path` is in, or None where it is in none.

    What a run records as its repository, and release later runs git in, so
    scope.py's --repo and prompts.py's --checkout both resolve here: two
    spellings could record two repositories for one run.
    """
    code, out, _ = git_text(path, "rev-parse", "--show-toplevel")
    return out.strip() if code == 0 else None
