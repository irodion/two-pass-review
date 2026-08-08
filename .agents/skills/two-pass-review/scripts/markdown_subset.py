#!/usr/bin/env python3
"""The permitted markdown subset, and only it.

Named `markdown_subset` rather than `markdown` on purpose: the scripts directory
goes on `sys.path`, and a module called `markdown` there would shadow the
well-known package of that name for anything else running in the process.
"""

import html
import re

# The page promises no JavaScript. An `href` is the one place a URL scheme can
# break that promise, so links carry an allowlist rather than a blocklist:
# `javascript:` is the obvious one, `data:text/html` and `vbscript:` are the
# ones a blocklist forgets. Anything else renders as the text the pass wrote.
#
# This is not hypothetical. A pass quotes the code under review, so a hostile
# repository can get a string of its choosing into `body_md`.
SAFE_URL_SCHEMES = ("http://", "https://", "mailto:")


def is_safe_url(url):
    return url.lower().startswith(SAFE_URL_SCHEMES)


# --- markdown subset ---------------------------------------------------------
#
# Escape first, then apply structure. Nothing downstream can emit an unescaped
# byte, which is what deletes the whole class of tag-termination bugs rather
# than mitigating it.

BULLET_RE = re.compile(r"^(\s*)[-*]\s+(.*)$")
NUMBER_RE = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
QUOTE = "&gt;"


class Markdown(object):
    """The permitted subset, and only it.

    Anything outside the subset is escaped and passed through as text. A
    hand-rolled subset that drops what it does not recognise turns its own gaps
    into silent rendering bugs; unreadable is recoverable, missing is not.
    """

    def __init__(self, known_ids):
        self.known_ids = known_ids
        self.id_re = None
        if known_ids:
            alternatives = "|".join(re.escape(i) for i in sorted(known_ids, key=len, reverse=True))
            self.id_re = re.compile(r"\b(" + alternatives + r")\b")

    def render(self, text, self_id=None):
        if not text:
            return ""
        escaped = html.escape(text, quote=True)
        return self._blocks(escaped.split("\n"), self_id)

    def inline(self, text, self_id=None):
        """One line, inline markers only -- for titles.

        Titles are not block markdown, but the passes reach for inline code in
        them constantly, and a title showing raw backticks beside a body that
        renders them reads as a broken page rather than a faithful one.
        """
        if not text:
            return ""
        return self._inline(html.escape(text, quote=True), self_id)

    @staticmethod
    def plain(text):
        """Strip inline markers for places that cannot carry markup."""
        text = re.sub(r"`([^`]+)`", r"\1", text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
        text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?!\w)", r"\1", text)
        return re.sub(r"\[([^\]\n]+)\]\([^)\s]+\)", r"\1", text)

    # -- blocks --

    def _blocks(self, lines, self_id):
        out = []
        index = 0
        while index < len(lines):
            line = lines[index]
            if not line.strip():
                index += 1
                continue
            if line.strip().startswith("```"):
                block, index = self._fence(lines, index)
                out.append(block)
            elif line.lstrip().startswith(QUOTE):
                block, index = self._quote(lines, index, self_id)
                out.append(block)
            elif BULLET_RE.match(line) or NUMBER_RE.match(line):
                block, index = self._list(lines, index, self_id)
                out.append(block)
            else:
                block, index = self._paragraph(lines, index, self_id)
                out.append(block)
        return "\n".join(out)

    def _fence(self, lines, index):
        language = lines[index].strip()[3:].strip()
        body = []
        index += 1
        while index < len(lines) and not lines[index].strip().startswith("```"):
            body.append(lines[index])
            index += 1
        attribute = ""
        if language:
            # The language is honoured, not parsed and discarded.
            attribute = ' class="language-{}"'.format(re.sub(r"[^A-Za-z0-9_+-]", "", language))
        return "<pre><code{}>{}</code></pre>".format(attribute, "\n".join(body)), index + 1

    def _quote(self, lines, index, self_id):
        body = []
        while index < len(lines) and lines[index].lstrip().startswith(QUOTE):
            stripped = lines[index].lstrip()[len(QUOTE) :]
            body.append(stripped[1:] if stripped.startswith(" ") else stripped)
            index += 1
        return "<blockquote>{}</blockquote>".format(self._blocks(body, self_id)), index

    def _paragraph(self, lines, index, self_id):
        """Consecutive non-blank lines are one paragraph.

        Bodies arrive hard-wrapped at ~150 words, so breaking per line would
        fragment nearly every finding on the page.
        """
        body = []
        while index < len(lines):
            line = lines[index]
            if not line.strip():
                break
            if line.strip().startswith("```") or line.lstrip().startswith(QUOTE):
                break
            if BULLET_RE.match(line) or NUMBER_RE.match(line):
                break
            body.append(line.strip())
            index += 1
        return "<p>{}</p>".format(self._inline("\n".join(body), self_id)), index

    def _list(self, lines, index, self_id):
        first = NUMBER_RE.match(lines[index])
        ordered = first is not None
        matcher = NUMBER_RE if ordered else BULLET_RE
        other = BULLET_RE if ordered else NUMBER_RE
        indent = len((first or BULLET_RE.match(lines[index])).group(1))

        items = []
        while index < len(lines):
            line = lines[index]
            match = matcher.match(line)
            if match and len(match.group(1)) == indent:
                items.append([match.group(2)])
                index += 1
                continue
            if not line.strip():
                following = index + 1
                while following < len(lines) and not lines[following].strip():
                    following += 1
                nested = following < len(lines) and (
                    BULLET_RE.match(lines[following]) or NUMBER_RE.match(lines[following])
                )
                if nested and len(nested.group(1)) > indent:
                    items[-1].append("")
                    index = following
                    continue
                break
            if other.match(line) and len(other.match(line).group(1)) == indent:
                break
            if not items:
                break
            depth = len(line) - len(line.lstrip())
            if depth > indent:
                items[-1].append(line[min(depth, indent + 2) :])
                index += 1
                continue
            items[-1].append(line.strip())  # lazy continuation of the current item
            index += 1

        rendered = []
        for item in items:
            structured = any(
                BULLET_RE.match(l) or NUMBER_RE.match(l) or l.strip().startswith("```") or l.lstrip().startswith(QUOTE)
                for l in item[1:]
            )
            if structured:
                inner = self._blocks(item, self_id)
                # A single leading paragraph inside a list item is noise.
                inner = re.sub(r"^<p>(.*?)</p>", r"\1", inner, count=1, flags=re.S)
                rendered.append("<li>{}</li>".format(inner))
            else:
                text = "\n".join(l for l in item if l.strip())
                rendered.append("<li>{}</li>".format(self._inline(text, self_id)))
        tag = "ol" if ordered else "ul"
        return "<{0}>{1}</{0}>".format(tag, "".join(rendered)), index

    # -- inline --

    def _inline(self, text, self_id):
        held = []

        def hold(markup):
            held.append(markup)
            return "\x00{}\x00".format(len(held) - 1)

        text = re.sub(r"`([^`]+)`", lambda m: hold("<code>{}</code>".format(self._link_ids(m.group(1), self_id))), text)
        def link(match):
            if not is_safe_url(match.group(2)):
                # Outside the subset is escaped and passed through, never dropped:
                # the reader still sees exactly what the pass wrote, inert.
                return match.group(0)
            return hold('<a href="{}" rel="noreferrer">{}</a>'.format(match.group(2), match.group(1)))

        text = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", link, text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?!\w)", r"<em>\1</em>", text)
        text = re.sub(r"(?<![\w_])_([^_\n]+)_(?!\w)", r"<em>\1</em>", text)
        text = self._link_ids(text, self_id)
        return re.sub(r"\x00(\d+)\x00", lambda m: held[int(m.group(1))], text)

    def _link_ids(self, text, self_id):
        """Turn in-prose ids into anchors, firing only on ids that exist.

        Both passes cross-reference their own findings while arguing, so this is
        the cheapest structural win the report has -- and a string that merely
        looks like an id is left exactly as written.
        """
        if self.id_re is None:
            return text

        def link(match):
            target = match.group(1)
            if target == self_id:
                return target
            return '<a class="xref" href="#finding-{0}">{0}</a>'.format(target)

        return self.id_re.sub(link, text)
