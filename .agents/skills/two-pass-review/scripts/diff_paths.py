#!/usr/bin/env python3
"""Read a unified diff's file headers into the paths they name. Stdlib only, 3.10 syntax.

Not a script -- a library two of them share. `scope.py` needs the post-image of
every changed file, to write the line-count manifest; `collect_docs.py` needs
both sides, because a deleted file's directory can still hold a document the
deletion invalidates. Those are two questions, but the reading underneath them
is one job, and it used to be written twice: two quote decoders and two header
walks, a few files apart, under the same function name. A quoting bug fixed in
one did not reach the other.

Both callers pass lines rather than text, so `collect_docs.py` can stream a
diff it never holds whole while `scope.py` splits the patch it already has.
"""

from collections.abc import Iterable
from typing import NamedTuple

# The one-character escapes git writes inside a quoted path. Every other byte it
# escapes is octal, and every byte it does not escape stands for itself.
UNESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, '"': 34}


class Header(NamedTuple):
    """What one `diff --git` block named.

    `old` and `new` are repository-relative and prefix-stripped, and either is
    None where the block named no such side -- an addition has no `old`, a
    deletion no `new` -- or where the name would not decode.

    `deleted` separates the two reasons `new` can be None. A deletion names no
    post-image on purpose; a header this module could not read names none by
    failure, and a caller that wants to notice its own blind spots needs to tell
    those apart. Nothing here reports that: the module returns one record per
    block and lets each caller do its own arithmetic.
    """

    old: str | None
    new: str | None
    deleted: bool


def unquote_path(field: str) -> tuple[str, int] | None:
    """Decode a git-quoted path: (path, index just past its closing quote).

    None when `field` does not open with a quote, when the quoting is malformed,
    or when the bytes it spells are not UTF-8.

    Worth decoding rather than skipping because git quotes by default, so this
    is not an exotic case: `core.quotepath` is on unless someone turned it off,
    and it fires on every non-ASCII name. A repository whose files are named in
    Japanese or Greek got nothing back and no indication why.

    Decoding is bounded and total: an unknown escape, a truncated octal, an
    unterminated quote and a raw byte above ASCII (which git would have escaped)
    each return None rather than a guess. And the quoted form is the *safer* one
    to read, which is the part that looks backwards: it is pure ASCII, so it
    passes through a lossy patch decode untouched, while an unquoted non-UTF-8
    name would already hold a replacement character by the time it arrived here.
    """
    if not field.startswith('"'):
        return None
    out = bytearray()
    index = 1
    while index < len(field):
        char = field[index]
        if char == '"':
            try:
                return out.decode("utf-8"), index + 1
            except UnicodeDecodeError:
                return None
        if char == "\\":
            escape = field[index + 1 : index + 2]
            if escape in UNESCAPES:
                out.append(UNESCAPES[escape])
                index += 2
                continue
            digits = field[index + 1 : index + 4]
            if len(digits) < 3 or any(digit not in "01234567" for digit in digits):
                return None
            out.append(int(digits, 8))
            index += 4
            continue
        if ord(char) > 0x7F:
            return None
        out.append(ord(char))
        index += 1
    return None


def side(field: str, prefix: str) -> str | None:
    """The path a `--- ` or `+++ ` field names, decoded and unprefixed.

    None for `/dev/null`, which names no file, and None for a field this module
    cannot read -- the caller that cares which it was has the `deleted` flag.
    """
    if field.startswith('"'):
        quoted = unquote_path(field)
        name = quoted[0] if quoted is not None else None
    else:
        # git appends a tab to an unquoted header path holding a space, and the
        # tab is not part of the name. Stripping exactly one is safe: a name
        # genuinely ending in a tab is a control character, so git quotes it,
        # and a quoted field ends at its closing quote -- any tab after that is
        # already outside what unquote_path consumed.
        name = field.removesuffix("\t")
    if name is None or not name.startswith(prefix):
        return None
    return name[len(prefix) :]


def bare(field: str) -> str | None:
    """The path a `rename to` / `rename from` / `copy to` field names.

    Unprefixed and untabbed by git, so there is nothing to strip -- only the
    quoting to undo.
    """
    if not field.startswith('"'):
        return field
    quoted = unquote_path(field)
    return quoted[0] if quoted is not None else None


def unrenamed_path(header: str) -> str | None:
    """`a/X b/X` -> X; None for a rename, a copy, or a path that will not decode.

    The `diff --git` line is the only path a file header carries when nothing
    else in the block does, and it is the awkward one to read: two paths on one
    line, space-separated, and a name may hold spaces.

    Unquoted, rather than guess where the split falls, this derives the one path
    length that could produce a line this long with the same name twice, then
    rebuilds the line and demands the bytes match. Quoted, each side ends at its
    own closing quote and no arithmetic is needed.

    Only the two matching cases are read. A line with one side quoted means the
    sides differ, which means a rename -- and a rename fails this either way,
    correctly, because `rename from` and `rename to` carry both sides plainly.
    """
    if header.startswith('"'):
        left = unquote_path(header)
        if left is None or header[left[1] : left[1] + 1] != " ":
            return None
        right = unquote_path(header[left[1] + 1 :])
        if right is None or left[1] + 1 + right[1] != len(header):
            return None
        if not right[0].startswith("b/"):
            return None
        path = right[0][len("b/") :]
        return path if left[0] == f"a/{path}" else None
    if len(header) < 5 or (len(header) - 5) % 2:
        return None
    width = (len(header) - 5) // 2
    path = header[2 : 2 + width]
    return path if header == f"a/{path} b/{path}" else None


def file_headers(lines: Iterable[str]) -> list[Header]:
    """One Header per `diff --git` block, in the order the diff names them.

    Walks the header/body state a unified diff already carries, because `+++ `
    and `--- ` at column zero are file headers only before the first hunk. A
    diff that itself modifies a patch file puts those same bytes in its body,
    and so does any file whose own lines start with `++` or `--`: prefixed with
    the `+` or `-` a hunk adds, they arrive here indistinguishable from headers
    unless the walk knows it has left the header.

    Four shapes reach the end of a block naming nothing in `---`/`+++`, and a
    reader of those two lines alone drops every one: a pure rename, which
    carries `rename to` and no content; a mode change, which carries only the
    two mode lines; and an empty file added or deleted, which has no content and
    so no sides at all. The `diff --git` line names the file in all of them, so
    it seeds both sides and the rest of the block overrides or cancels the seed.
    """
    headers: list[Header] = []
    in_header = False
    old: str | None = None
    new: str | None = None
    deleted = False
    started = False

    for line in lines:
        line = line.rstrip("\n")
        if line.startswith("diff --git "):
            if started:
                headers.append(Header(old, new, deleted))
            started = True
            in_header = True
            deleted = False
            # The same path on both sides until the block says otherwise, which
            # is the whole of what a mode change or an empty file ever says.
            old = new = unrenamed_path(line[len("diff --git ") :])
        elif not started or not in_header:
            continue
        elif line.startswith("@@"):
            in_header = False
        elif line.startswith("--- "):
            old = side(line[len("--- ") :], "a/")
        elif line.startswith("+++ "):
            # The last header line of a block that has content, so the walk can
            # leave the header here rather than waiting for the first hunk.
            in_header = False
            new = side(line[len("+++ ") :], "b/")
            if new is None and line == "+++ /dev/null":
                deleted = True
        elif line.startswith("rename from "):
            old = bare(line[len("rename from ") :])
        elif line.startswith("rename to ") or line.startswith("copy to "):
            new = bare(line.split(" to ", 1)[1])
        elif line.startswith("deleted file mode "):
            # An empty file's deletion has no `+++ /dev/null` to cancel the
            # seed, because it has no content and so no sides at all.
            new = None
            deleted = True
        elif line.startswith("new file mode "):
            # The mirror of the above, and the reason it matters is narrower:
            # only a caller reading the old side would otherwise be handed a
            # pre-image for a file that had none.
            old = None

    if started:
        headers.append(Header(old, new, deleted))
    return headers
