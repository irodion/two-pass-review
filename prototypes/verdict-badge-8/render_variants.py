#!/usr/bin/env python3
"""PROTOTYPE — throwaway. Renders the verdict-badge variants for issue #8.

Three variants of the report page's verdict badge, rendered from one real-shaped
artifact, plus the current both-badges page as a baseline. `page.py` is imported
and its PAGE/CSS constants are patched in memory — nothing in the shipping skill
is edited, so this stays runnable while #8 is open and deletable the moment it
closes.

There is no `?variant=` switcher and no floating bar, which is what the prototype
skill would normally ask for: the artifact under test is a JavaScript-free
`file://` page, and a switcher would have to be JavaScript. Variants are separate
files instead, listed by index.html, and the interesting comparison is made by
scrolling each one rather than by flipping between them.

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

SIDEBAR_BADGE = """    <div class="verdict verdict-{verdict}">
      <span class="verdict-label">{verdict_label}</span>
    </div>
"""
MASTHEAD_BADGE = (
    '      <div class="verdict verdict-{verdict}">'
    "<span class=\"verdict-label\">{verdict_label}</span></div>\n"
)

# The count is computed here, not passed as a format key, because adding a key
# would mean patching render_page as well. Digits only, so it cannot collide
# with str.format's braces.
COUNTED_SIDEBAR_BADGE = """    <div class="verdict verdict-{{verdict}}">
      <span class="verdict-label">{{verdict_label}}</span>
      <span class="verdict-count">{count} of {total}</span>
    </div>
"""

COUNT_CSS = """
.sidebar .verdict { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.verdict-count { font-weight: 400; font-size: 12px; opacity: .8; }
"""


def variants(count, total):
    """name -> (page template, css). Order is the order index.html lists them."""
    counted = BASE_PAGE.replace(SIDEBAR_BADGE, COUNTED_SIDEBAR_BADGE.format(count=count, total=total))
    counted = counted.replace(MASTHEAD_BADGE, "")
    return [
        ("current", "Both badges (today)", BASE_PAGE, BASE_CSS),
        ("a-sidebar", "A — sidebar only", BASE_PAGE.replace(MASTHEAD_BADGE, ""), BASE_CSS),
        ("b-masthead", "B — masthead only", BASE_PAGE.replace(SIDEBAR_BADGE, ""), BASE_CSS),
        ("c-sidebar-count", "C — sidebar only, badge carries the count", counted, BASE_CSS + COUNT_CSS),
    ]


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "out")
    os.makedirs(outdir, exist_ok=True)

    with open(os.path.join(HERE, "input", "findings.json")) as handle:
        merged = json.load(handle)
    findings = merged["findings"]
    blocking = sum(1 for f in findings if f["disposition"] == "blocking")

    links = []
    for slug, label, template, css in variants(blocking, len(findings)):
        page.PAGE, page.CSS = template, css
        html = page.render_page(merged)
        assert "<script" not in html.lower(), "prototype emitted a script tag"
        path = os.path.join(outdir, slug + ".html")
        with open(path, "w") as handle:
            handle.write(html)
        links.append('<li><a href="{}.html">{}</a></li>'.format(slug, label))
        print(path)

    index = os.path.join(outdir, "index.html")
    with open(index, "w") as handle:
        handle.write(
            "<!doctype html><meta charset=utf-8><title>PROTOTYPE — verdict badge (#8)</title>"
            "<style>body{font:16px/1.6 system-ui;margin:40px;max-width:40em}</style>"
            "<h1>PROTOTYPE — verdict badge (#8)</h1>"
            "<p>Scroll each one past the masthead before judging it.</p><ul>"
            + "".join(links)
            + "</ul>"
        )
    print(index)


if __name__ == "__main__":
    main()
