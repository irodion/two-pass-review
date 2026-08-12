# PROTOTYPE — sidebar scrolling (#15)

Throwaway. Not for `main`. This is the primary source behind the answer recorded on
[#15](https://github.com/irodion/two-pass-review/issues/15); `main` keeps the fix and none of this.

`.sidebar` is `position: sticky` **and** its own scroll container, so the verdict badge and the two
filter rows — the first things in that box — are the first things out of it once the nav overflows.
Four pages, rendered from the same 14-finding artifact the #8 prototype used so the screenshots are
comparable:

| file | what it is |
| --- | --- |
| `out/current.html` | today |
| `out/a-pinned-head.html` | **A** — badge + filters wrapped in `.sidebar-head`, sticky inside the existing scroll box |
| `out/b-no-scroll.html` | **B** — `max-height`/`overflow-y` deleted |
| `out/c-flex-nav.html` | **C** — sidebar becomes a column flex container, only the nav scrolls |

```sh
/usr/bin/python3 render_variants.py
```

`page.py` is imported and its `PAGE`/`CSS` constants patched in memory, so the shipping skill is
untouched and this stays runnable while #15 is open.

## How to judge it

Shrink the window until the nav overflows, then scroll **inside the sidebar**. Every variant is
identical until you do — that is the whole ticket, and it is why reading the CSS alone had already
fooled one ticket before this one.

The Chrome extension refuses `file://`, so the screenshots behind #15 were taken over
`python3 -m http.server`. Irrelevant to what is under test: nothing here depends on the protocol.

## What it measured

At 14 findings in a 903px viewport the sidebar overflows by 157px. Scrolled to the end of the nav:

| | badge visible | filters visible | last nav entry |
| --- | --- | --- | --- |
| today | 0 / 43px | 29 / 125px | reachable |
| A | 43 / 43px | 125 / 125px | reachable |
| B | — | — | **unreachable until the document's last scroll position** |
| C | 43 / 43px | 137 / 137px | reachable |

**B is disqualified on evidence.** The ticket assumed dropping the scroll box meant "the nav then
reaches everything". It does not: a sticky element taller than the viewport still pins its top edge at
`top: 24px`, so the sidebar's tail hangs permanently below the fold. Probed at scroll 0, 900 and 3000
the last nav entry is off screen every time, and it only appears at 6894 — the end of the document,
where sticky finally runs out of containing block. Today's page can at least scroll to it.

**A and C are visually identical scrolled.** They separate on the *unscrolled* page: A is pixel-identical
to today (first nav group at y=240, as now) once the gap under the filters moves from `.filters`'
margin to the head's padding — it has to move, because a margin collapses out of the sticky box and
leaves a transparent strip for nav entries to slide through. C lands 12px lower, because a flex item is
a block formatting context root and the pill margins that collapse out of `.filters` today stop
collapsing. C is fewer characters and needs no markup; it pays by putting flex's margin semantics under
a sidebar built on collapsing margins, with #9's dismissal filter still to be added to that same block.

A was chosen.
