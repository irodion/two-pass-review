#!/usr/bin/env python3
"""PROTOTYPE — throwaway. Renders the sidebar-scrolling variants for issue #15.

The sidebar is `position: sticky` *and* its own scroll container, so the verdict
badge and the two filter rows — the first things in that box — are the first
things out of it once the nav overflows. Three candidate answers plus today's
page, rendered from the same 14-finding artifact the #8 prototype used, so the
two sets of screenshots are comparable.

`page.py` is imported and its PAGE/CSS constants patched in memory — nothing in
the shipping skill is edited, so this stays runnable while #15 is open.

Judge by scrolling *the sidebar*, not the main column. That is the whole ticket:
every variant looks identical until the nav is scrolled.

Usage:  /usr/bin/python3 render_variants.py [OUTDIR]
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "..", ".agents", "skills", "two-pass-review", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPTS))

import page  # noqa: E402

BASE_PAGE = page.PAGE
BASE_CSS = page.CSS

# The shipped rule, verbatim. Replaced rather than overridden so each variant's
# CSS reads as the rule it would actually ship, not as a pile of undo.
SIDEBAR_RULE = (
    ".sidebar { position: sticky; top: 24px; max-height: calc(100vh - 48px); "
    "overflow-y: auto; font-size: 14px; }"
)

# --- A: pin the head -------------------------------------------------------
# One wrapper around the two things that must not leave, and one sticky rule.
# No second scroll container and no flex: the sidebar keeps scrolling as one
# box, and the head simply refuses to leave the top of it.
HEAD_OPEN = '  <aside class="sidebar">\n    <div class="sidebar-head">\n    <div class="verdict'
HEAD_CLOSE = "    </div>\n    </div>\n    <nav>"

# The gap under the filters moves from `.filters`' margin to the head's padding.
# It has to: a margin would collapse out of the sticky box and leave a
# transparent strip for nav entries to slide through. 22px keeps the unscrolled
# page pixel-identical to today's.
HEAD_CSS = """
.sidebar-head { position: sticky; top: 0; z-index: 1; background: var(--bg); padding-bottom: 22px; }
.sidebar-head .filters { margin-bottom: 0; }
.sidebar nav > .nav-group:first-child { margin-top: 0; }
"""

# --- B: stop the sidebar scrolling ----------------------------------------
# Delete max-height/overflow-y. The nav reaches everything, but a sidebar
# taller than the viewport can no longer stick, so the badge and filters leave
# on the first scroll of the *main* column — the outcome #8 rejected.
NO_SCROLL_RULE = ".sidebar { position: sticky; top: 24px; font-size: 14px; }"

# --- C: nested flex, nav scrolls alone ------------------------------------
# The shape the ticket named. Same visible result as A, one more box and one
# more overflow boundary to get there.
# min-height: 0 is not optional — a flex item's default min-height is its
# content, so without it the nav refuses to shrink and overflows the sidebar
# instead of scrolling inside it. The :first-child rule restores the 22px gap
# that flex breaks: flex items' margins don't collapse, so the nav's own 16px
# top margin stops merging into the filters' 22px and starts adding to it.
FLEX_RULE = (
    ".sidebar { position: sticky; top: 24px; max-height: calc(100vh - 48px); "
    "display: flex; flex-direction: column; font-size: 14px; }\n"
    ".sidebar nav { overflow-y: auto; min-height: 0; }\n"
    ".sidebar nav > .nav-group:first-child { margin-top: 0; }"
)


def variants():
    """slug -> (label, page template, css). Order is the order index.html lists."""
    pinned = BASE_PAGE.replace('  <aside class="sidebar">\n    <div class="verdict', HEAD_OPEN)
    pinned = pinned.replace("    </div>\n    <nav>", HEAD_CLOSE)
    assert pinned != BASE_PAGE and "sidebar-head" in pinned

    return [
        ("current", "Today — sidebar scrolls as one box", BASE_PAGE, BASE_CSS),
        ("a-pinned-head", "A — badge + filters pinned to the top of the sidebar",
         pinned, BASE_CSS.replace(SIDEBAR_RULE, SIDEBAR_RULE + HEAD_CSS)),
        ("b-no-scroll", "B — sidebar stops scrolling on its own",
         BASE_PAGE, BASE_CSS.replace(SIDEBAR_RULE, NO_SCROLL_RULE)),
        ("c-flex-nav", "C — nested flex, only the nav scrolls",
         BASE_PAGE, BASE_CSS.replace(SIDEBAR_RULE, FLEX_RULE)),
    ]


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "out")
    os.makedirs(outdir, exist_ok=True)

    with open(os.path.join(HERE, "input", "findings.json")) as handle:
        merged = json.load(handle)

    assert SIDEBAR_RULE in BASE_CSS, "the shipped .sidebar rule moved; fix SIDEBAR_RULE"

    links = []
    for slug, label, template, css in variants():
        page.PAGE, page.CSS = template, css
        html = page.render_page(merged)
        path = os.path.join(outdir, slug + ".html")
        with open(path, "w") as handle:
            handle.write(html)
        links.append('<li><a href="{}.html">{}</a></li>'.format(slug, label))
        print(path)

    index = os.path.join(outdir, "index.html")
    with open(index, "w") as handle:
        handle.write(
            "<!doctype html><meta charset=utf-8><title>PROTOTYPE — sidebar scrolling (#15)</title>"
            "<style>body{font:16px/1.6 system-ui;margin:40px;max-width:40em}</style>"
            "<h1>PROTOTYPE — sidebar scrolling (#15)</h1>"
            "<p>Make the window short enough that the nav overflows, then scroll "
            "<em>inside the sidebar</em>. They are identical until you do.</p><ul>"
            + "".join(links)
            + "</ul>"
        )
    print(index)


if __name__ == "__main__":
    main()
