# PROTOTYPE — verdict badge (#8)

Throwaway. This branch is the primary source for the decision recorded on
[#8](https://github.com/irodion/two-pass-review/issues/8); `main` keeps only the decision.

**Question.** The verdict badge renders twice — sidebar and masthead — and the duplication is going
away. Which one stays?

**Answer: A, the sidebar badge.** The masthead one is deleted.

## Running it

```sh
/usr/bin/python3 render_variants.py            # writes out/
/usr/bin/python3 -m http.server 8731 --directory out
```

`page.py` is imported and its `PAGE`/`CSS` constants are patched in memory, so nothing in the
shipping skill is touched. The variants are separate files rather than one page with a `?variant=`
switcher, because the artifact under test carries no JavaScript and a switcher would have to.

`input/findings.json` is a hand-built 14-finding artifact — it validates, and it is larger than any
of the real runs on disk, which carry 0–2 findings each. #8 asked for a large artifact because the
trade only appears on a report long enough to scroll.

## What the variants showed

- **The sticky argument is weaker than #8 assumed.** `.sidebar` is `position: sticky` *and*
  `overflow-y: auto; max-height: calc(100vh - 48px)`. At 14 findings on a 737px viewport the nav
  overflows, so scrolling *inside* the sidebar to reach a nav entry scrolls the badge out of it. The
  badge survives main-column scrolling — the common case — but not nav use. Filed separately.
- **Dropping the sidebar badge buys ~54px the nav wants.** In B the nav fits down to `sec-6`; in
  the current page and in A it clips at `sec-5`.
- **Dropping the masthead badge costs almost nothing.** In A the sidebar badge and the sentence
  land on the same horizontal line and read as one unit, so the masthead loses no signal.
- **C is self-defeating at rest.** `BLOCKED 4 OF 14` sits level with a sentence that already says
  "4 of 14 findings block this change". It is the only variant that carries the count deep into the
  report, which was its whole reason for existing, and it pays for that twice over at the top.
