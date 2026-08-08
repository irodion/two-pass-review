#!/bin/sh
# Run the install commands the README actually prints, rather than a copy of them.
#
# The copy is the whole point. Both installs have been found broken -- three
# commands across two reviews, none of them in the code -- and a workflow that
# restated them would have passed every time, because it would have been testing
# itself. So the blocks are extracted from README.md and executed as written:
# edit the README badly and this fails.
#
# Placeholders are substituted, not rewritten: <your-repo> becomes a scratch
# project and /path/to/two-pass-review becomes this checkout. HOME is moved so
# the personal install writes into a bare directory instead of the runner's.
#
# The `git clone` block is deliberately NOT executed. It would fetch main, and
# on a pull request main is precisely the version whose README is not under
# test. The URL it contains is checked by hand.

set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

PROJECT="$WORK/project"
FAKE_HOME="$WORK/home"
mkdir -p "$PROJECT" "$FAKE_HOME"
(cd "$PROJECT" && git init -q)

# Every ```sh block in the README, in order, skipping the clone.
awk '/^```sh$/ {inblock=1; next} /^```$/ {inblock=0} inblock' "$ROOT/README.md" \
  | grep -v '^git clone ' > "$WORK/commands.sh"

if [ ! -s "$WORK/commands.sh" ]; then
    echo "no shell blocks found in README.md -- the extractor has drifted" >&2
    exit 1
fi

echo "--- commands taken from README.md ---"
cat "$WORK/commands.sh"
echo "--- replaying ---"

sed -e "s#<your-repo>#$PROJECT#g" \
    -e "s#/path/to/two-pass-review#$ROOT#g" \
    "$WORK/commands.sh" > "$WORK/resolved.sh"

HOME="$FAKE_HOME" sh -eux "$WORK/resolved.sh"

# The commands succeeding is not the same as the install working.
test -f "$PROJECT/.claude/skills/two-pass-review/SKILL.md" \
    || { echo "project install: skill does not resolve through .claude/skills" >&2; exit 1; }
test -f "$PROJECT/.agents/skills/two-pass-review/SKILL.md" \
    || { echo "project install: skill missing from .agents/skills" >&2; exit 1; }
case $(readlink "$PROJECT/.claude/skills/two-pass-review") in
    /*) echo "project install: symlink is absolute" >&2; exit 1 ;;
esac
test -f "$FAKE_HOME/.agents/skills/two-pass-review/SKILL.md" \
    || { echo "personal install: Cursor/Codex location does not resolve" >&2; exit 1; }
test -f "$FAKE_HOME/.claude/skills/two-pass-review/SKILL.md" \
    || { echo "personal install: Claude Code location does not resolve" >&2; exit 1; }

# And the copied skill has to load, not merely exist -- a cp that lost a sibling
# module resolves as a directory and fails at the first import.
python3 -c "import sys; sys.path.insert(0, '$PROJECT/.claude/skills/two-pass-review/scripts'); import render, validate, page, scope, markdown_subset"

echo "both installs resolve, and the copied scripts import."
