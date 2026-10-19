#!/usr/bin/env python3
"""The linters the hints step runs, and how each is asked and read. Stdlib only, 3.10 syntax.

Not a script -- hints.py imports LINTERS and the types they are written in. A
linter here is a rule selection, a runner that starts the tool on a directory of
copied files and reads its report into Diagnostics, and for Biome the binary
npm's launcher would start. What then becomes of the diagnostics -- which end of
the change each came from, which are new, what is handed on -- is hints.py's
and needs nothing from here but the table, so a tool's rules or its report
format change in this file alone.
"""

import json
import os
import platform
import sys
from collections.abc import Callable
from typing import NamedTuple

# Bare, for page.py's reason: the entrypoint that imported this put the
# directory on the path.
from tools import ruff_report, run

# Biome's rule groups, each with its recommended rules only. A whole group
# holds rules that mean something only beside the repository's own
# configuration, which is never read: noUndeclaredVariables named every Jest
# global in a test file, 18 of them, and a change that adds tests went over
# the cap with its one real defect beside them.
BIOME_GROUPS = ("correctness", "suspicious")

# Rules outside the recommended set that are about behaviour. useAwait was the
# one lead on the bench's dropped await.
BIOME_ADDED = ("suspicious/useAwait",)

# Typing rules in the recommended set. On @tanstack/query-core they made 315 of
# its 321 standing diagnostics, none of them a defect.
BIOME_SKIPPED = (
    "suspicious/noExplicitAny",
    "suspicious/noImplicitAnyLet",
    "suspicious/noConfusingVoidType",
    "suspicious/noEmptyInterface",
)

# Where npm puts the binary its Biome launcher starts, by platform: the name
# of the package that holds it, as the launcher spells it.
BIOME_PLATFORMS = {
    ("linux", "x86_64"): "linux-x64",
    ("linux", "aarch64"): "linux-arm64",
    ("darwin", "x86_64"): "darwin-x64",
    ("darwin", "arm64"): "darwin-arm64",
}

# ruff's rule families: bugbear, pylint's errors and warnings, pyflakes, and
# ruff's own, which RUFF_OWN narrows.
RUFF_FAMILIES = ("B", "PLE", "PLW", "F", "RUF")

# ruff's own rules that are about behaviour. The rest of RUF is style, idiom
# and suppression comments -- RUF100 calls any noqa comment for a rule this
# selection leaves out unused -- and on rich made 87 of its 167 standing
# diagnostics. Kept after the run rather than selected for it, as RUFF_SKIPPED
# is: a code an older ruff does not know makes it refuse the whole selection.
RUFF_OWN = (
    "RUF006",
    "RUF008",
    "RUF009",
    "RUF016",
    "RUF018",
    "RUF024",
    "RUF026",
    "RUF030",
    "RUF032",
    "RUF033",
    "RUF034",
    "RUF035",
    "RUF040",
    "RUF048",
    "RUF049",
    "RUF060",
    "RUF063",
    "RUF064",
    "RUF068",
    "RUF071",
)

# Style and convention rules in the other families: unused imports, zip
# without strict=, raise without from, a loop variable reused. On rich they
# made 57 of its 167 standing diagnostics.
RUFF_SKIPPED = (
    "B007",
    "B009",
    "B010",
    "B013",
    "B024",
    "B027",
    "B028",
    "B904",
    "B905",
    "B911",
    "B912",
    "F401",
    "F403",
    "F405",
    "PLW0108",
    "PLW0120",
    "PLW0602",
    "PLW0603",
    "PLW0604",
    "PLW1510",
    "PLW2901",
    "PLW3301",
)

# shellcheck's lowest severity reported: warnings and errors, no info or style.
SHELLCHECK_SEVERITY = "warning"


class Diagnostic(NamedTuple):
    path: str
    line: int
    code: str
    message: str


Lint = Callable[[str, str, list[str], dict[str, str], float], list[Diagnostic] | None]


class Linter(NamedTuple):
    name: str
    extensions: tuple[str, ...]
    rules: str
    lint: Lint
    # Given what PATH holds for the tool, the program it launches; see tools.find.
    launcher: Callable[[str], str | None] | None = None


def ruff(
    tool: str, directory: str, files: list[str], env: dict[str, str], timeout: float
) -> list[Diagnostic] | None:
    """One ruff run in `directory`, ignoring every configuration file, fixes off."""
    code, out, _ = run(
        [
            tool,
            "check",
            "--isolated",
            "--no-cache",
            "--no-fix",
            "--no-fix-only",
            "--exit-zero",
            "--output-format=json",
            f"--select={','.join(RUFF_FAMILIES)}",
            "--",
            *files,
        ],
        directory,
        env,
        timeout,
    )
    report = ruff_report(out, directory) if code == 0 else None
    if report is None:
        return None
    return [Diagnostic(*entry) for entry in report if ruff_kept(entry[2])]


def ruff_kept(code: str) -> bool:
    """Whether a ruff diagnostic is one this step reads: a bug rule's, or a syntax error, beside
    which ruff reports nothing else in the file."""
    if code.startswith("RUF"):
        return code in RUFF_OWN
    return code not in RUFF_SKIPPED


def biome_settings() -> dict[str, object]:
    """The whole configuration a Biome run is given: the linter's rules and nothing else."""
    rules: dict[str, dict[str, object]] = {group: {"recommended": True} for group in BIOME_GROUPS}
    for names, level in ((BIOME_ADDED, "error"), (BIOME_SKIPPED, "off")):
        for rule in names:
            group, name = rule.split("/")
            rules[group][name] = level
    return {
        "vcs": {"enabled": False},
        "files": {"ignoreUnknown": True},
        "formatter": {"enabled": False},
        "assist": {"enabled": False},
        "linter": {"enabled": True, "rules": {"recommended": False, **rules}},
    }


def biome(
    tool: str, directory: str, files: list[str], env: dict[str, str], timeout: float
) -> list[Diagnostic] | None:
    """One Biome run in `directory`, with the skill's configuration and nothing discovered.

    The configuration is written beside `directory`, never in it, and under a
    name Biome does not look for, so the only configuration it reads is the
    one named to it. A file that does not parse reports `parse` beside the
    rules it could still check, and that is kept too.
    """
    config = os.path.join(os.path.dirname(directory), "hint-rules.json")
    with open(config, "w", encoding="utf-8") as handle:
        json.dump(biome_settings(), handle)
    code, out, _ = run(
        [
            tool,
            "lint",
            f"--config-path={config}",
            "--vcs-enabled=false",
            "--reporter=json",
            "--max-diagnostics=none",
            "--no-errors-on-unmatched",
            "--",
            *files,
        ],
        directory,
        env,
        timeout,
    )
    # 1 is Biome saying it found something.
    if code not in (0, 1):
        return None
    try:
        return [
            Diagnostic(
                os.path.normpath(entry["location"]["path"]),
                int(entry["location"]["start"]["line"]),
                str(entry["category"]).removeprefix("lint/"),
                str(entry["message"]),
            )
            for entry in json.loads(out)["diagnostics"]
            if str(entry.get("category", "")).startswith("lint/")
            or entry.get("category") == "parse"
        ]
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def biome_binary(found: str) -> str | None:
    """The binary npm's Biome launcher at `found` would start, or None where `found` is no
    such launcher.

    npm installs Biome as a node script that starts a compiled binary from a
    package beside it, one per platform. A script that starts another program
    is what tools.find refuses, so the binary is found where the launcher
    looks for it -- inside its own package, or beside it once npm has hoisted
    it -- and started instead. musl's comes second, as npm installs both on
    some glibc systems.
    """
    script = os.path.realpath(found)
    if not script.endswith(os.path.join("@biomejs", "biome", "bin", "biome")):
        return None
    package = os.path.dirname(os.path.dirname(script))
    name = BIOME_PLATFORMS.get((sys.platform, platform.machine()))
    if name is None:
        return None
    for variant in (name, f"{name}-musl"):
        for scope in (os.path.join(package, "node_modules", "@biomejs"), os.path.dirname(package)):
            binary = os.path.join(scope, f"cli-{variant}", "biome")
            if os.path.isfile(binary):
                return binary
    return None


def shellcheck(
    tool: str, directory: str, files: list[str], env: dict[str, str], timeout: float
) -> list[Diagnostic] | None:
    """One shellcheck run in `directory`, ignoring every .shellcheckrc, warnings and errors only.
    A parse error is an error, so it is among them."""
    code, out, _ = run(
        [tool, "--norc", "--format=json1", f"--severity={SHELLCHECK_SEVERITY}", "--", *files],
        directory,
        env,
        timeout,
    )
    # 1 is shellcheck saying it found something.
    if code not in (0, 1):
        return None
    try:
        return [
            Diagnostic(
                os.path.normpath(entry["file"]),
                int(entry["line"]),
                f"SC{int(entry['code'])}",
                str(entry["message"]),
            )
            for entry in json.loads(out)["comments"]
        ]
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


LINTERS = (
    Linter(
        "ruff",
        (".py", ".pyi"),
        f"{', '.join(RUFF_FAMILIES[:-1])} less {len(RUFF_SKIPPED)} style rules, "
        f"and {len(RUFF_OWN)} RUF rules about behaviour",
        ruff,
    ),
    Linter(
        "biome",
        (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"),
        f"recommended {' and '.join(BIOME_GROUPS)} rules, "
        f"with {', '.join(rule.split('/')[1] for rule in BIOME_ADDED)}, "
        f"less {len(BIOME_SKIPPED)} typing rules",
        biome,
        biome_binary,
    ),
    Linter("shellcheck", (".sh", ".bash"), f"{SHELLCHECK_SEVERITY} and above", shellcheck),
)
