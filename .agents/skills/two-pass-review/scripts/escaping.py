#!/usr/bin/env python3
"""The one escape the page's renderers share. Stdlib only, 3.10 syntax.

A module of its own because two renderers put text into HTML -- page.py, and
linemap.py beside it -- and the map cannot import page.py, which imports it.
Two spellings of this line would be two places for escaping to drift apart, and
escaping is what keeps a hostile repository's text from becoming markup.
"""

import html


def esc(value: object) -> str:
    """`value` as text, safe in element content and in a quoted attribute."""
    return html.escape(str(value), quote=True)
