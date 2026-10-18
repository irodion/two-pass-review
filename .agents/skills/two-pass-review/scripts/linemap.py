#!/usr/bin/env python3
"""The line map: where the findings land. Stdlib only, 3.10 syntax.

Not a script -- page.py's second renderer, as markdown_subset is its first
helper. page.py builds the report and knows what each finding says; this draws
one picture of where they all fall, and is the only part of the page that does
geometry: rows, columns, track templates, station offsets and a churn scale.
It reads the merged artifact and the findings in page order, and returns markup
and the stylesheet that draws it, which page.py places.
"""

from typing import Any, NamedTuple

from escaping import esc
from validate import path_key

# The most lanes that keep the width an id reads across at: eight 40px lanes,
# beside a path of MAP_PATH_MAX characters and its churn, is what the report's
# main column holds. Past it the lanes narrow and their ids turn on their side,
# and past the column's width the map scrolls sideways rather than squeezing
# the file names out.
LANES_WIDE_MAX = 8
# What one line in the map's key stands for: one finding, so singular, where a
# heading counts a list. In page order, which is the order the key lists them.
LANE_LABEL = {"blocking": "Blocking", "follow-up": "Follow-up", "note": "Note"}
# A pass named in passing: over its side of the map, and on page.py's filter
# pills and contents line, which take it from here so the page has one short
# name for each pass. A pass's full name, which heads its own sections and
# labels its cards, is page.py's PRODUCER_LABEL. Here rather than there because
# page.py imports this module, and the map is the one that needs it to draw.
PASS_NAME = {"security": "Security", "quality": "Quality"}
# Longer paths lose leading directories, as git's own diffstat shortens them;
# the full path rides along in the row's title.
MAP_PATH_MAX = 46
# Up to this many changed files, every one is a row, cited or not, which is
# what shows a reader the files no finding touched. Past it those rows are
# mostly empty track, so only the cited files are rows and one row counts the
# rest.
MAP_ALL_FILES_MAX = 30
# The widest churn bar, in px; the others scale to it.
CHURN_BAR_MAX = 56


def cited_paths(finding: dict[str, Any]) -> list[str]:
    """The files a finding cites, each once, in the order it first names them.

    Each under validate.path_key, as every path the map matches is: compared as
    written, a cited `./README.md` was a row of its own, marked as outside the
    diff that changed README.md."""
    paths: list[str] = []
    for location in finding.get("locations") or []:
        path = path_key(location["path"])
        if path not in paths:
            paths.append(path)
    return paths


def map_path(path: str) -> tuple[str, str]:
    """(directories, file name) as a map row prints `path`.

    Whole when it fits. Otherwise the leading directories go and `.../` stands
    for them, as in `git diff --stat`: the file name and the directories
    nearest it are what tell two rows apart.
    """
    head, slash, name = path.rpartition("/")
    if len(path) <= MAP_PATH_MAX or not head:
        return head + slash, name
    kept: list[str] = []
    for part in reversed(head.split("/")):
        if len(".../") + len("".join(f"{p}/" for p in [part, *kept])) + len(name) > MAP_PATH_MAX:
            break
        kept.insert(0, part)
    return ".../" + "".join(f"{p}/" for p in kept), name


def lane_classes(finding: dict[str, Any]) -> str:
    """The classes every part of a finding's lane carries, for the filters to read.

    Classes rather than the data- attributes a card carries, on purpose: the
    filters hide whatever carries those, and a lane hidden out of the grid
    would take the columns beside it along. The map dims instead, which keeps
    every line where it was drawn.
    """
    classes = ["lm-" + finding["disposition"], "lm-" + finding["producer"]]
    if finding.get("severity"):
        classes += ["lm-rated", "lm-sev-" + finding["severity"]]
    return esc(" ".join(classes))


class Band(NamedTuple):
    """The falsification check's band, as facts: its caption, whether the check
    examined anything, and which findings it contests.

    `hatched` says it examined nothing -- skipped, or its reply lost -- and then
    no finding gets a mark; otherwise every finding gets one, a contest if its
    id is in `contested`. Drawing is band_mark's, here and in the legend alike.
    """

    caption: str
    hatched: bool
    contested: frozenset[str]


def band_mark(contested: bool, finding_id: str | None = None) -> str:
    """One mark of the falsification band: a finding's, given its id, or with
    none, the legend's key for it.

    One function draws both because the key exists so a reader can match a mark
    to its meaning, and a key drawn apart from its mark is one change away from
    showing a glyph the band no longer uses. Only the finding's mark speaks: its
    title, and for a contest the link to the dispute and the label saying so.
    """
    if not contested:
        title = ' title="Read by the falsification check, not contested"' if finding_id else ""
        return f'<i class="lm-dot"{title}></i>'
    glyph = '<span aria-hidden="true">?</span>'
    if finding_id is None:
        return f'<span class="lm-contest">{glyph}</span>'
    fid = esc(finding_id)
    return (
        f'<a class="lm-contest" href="#contest-{fid}" '
        'title="Contested by the falsification check" '
        f'aria-label="{fid}: contested by the falsification check">{glyph}</a>'
    )


class MapRow(NamedTuple):
    """One file row, as everything that draws a row needs it.

    `churn` is (added, removed) for a file the diff touched, and None where
    there is none to show: a file outside the diff, which `outside` marks, or
    any file of a run that did not record churn. `notes` counts the docs
    check's notes on the file.
    """

    path: str
    churn: tuple[int, int] | None
    outside: bool
    notes: int


class RowPlan(NamedTuple):
    """Which rows the map draws, decided once.

    `totals` is the whole change's (added, removed), and None when the run
    recorded no churn, which leaves the churn column without a heading. `rest`
    is (files, added, removed) for the summary row past MAP_ALL_FILES_MAX, or
    None.
    """

    rows: list[MapRow]
    heading: str
    totals: tuple[int, int] | None
    rest: tuple[int, int, int] | None

    @property
    def shown(self) -> int:
        """The grid rows the files take: one each, and the summary row if any."""
        return len(self.rows) + (1 if self.rest else 0)


def map_rows(scope: dict[str, Any], named: set[str], notes: dict[str, int]) -> RowPlan:
    """The map's rows: the files `named` -- cited, or carrying a docs-check note --
    and, when the run recorded them and there are few enough, every file the
    change touched.

    An older artifact names no changed file, so its map shows only the named
    ones and says nothing about what the diff did not touch, because it cannot
    know. Past MAP_ALL_FILES_MAX changed files the rows no finding reaches are
    mostly empty track, so they become one counted row.
    """
    if "files" not in scope:
        rows = [MapRow(path, None, False, notes.get(path, 0)) for path in sorted(named)]
        # A docs note puts its document on the map whether a finding cites it
        # or not, and the heading says so rather than calling every row cited.
        heading = "Files cited, or noted by the docs check" if notes else "Files cited"
        return RowPlan(rows, heading, None, None)

    changed = {path_key(entry["path"]): entry for entry in scope["files"]}
    paths = sorted(named | set(changed)) if len(changed) <= MAP_ALL_FILES_MAX else sorted(named)
    rows = [
        MapRow(
            path,
            (changed[path]["added"], changed[path]["removed"]) if path in changed else None,
            path not in changed,
            notes.get(path, 0),
        )
        for path in paths
    ]
    # The files its rows stand for. files_changed counts files too, but also a
    # header scope.py could not read, which names no path and so has no row --
    # and an artifact from before scope.py counted files rather than blocks.
    count = len(changed)
    heading = "{} changed file{}".format(count, "" if count == 1 else "s")
    outside = sum(1 for row in rows if row.outside)
    if outside:
        heading += f", and {outside} outside the diff"
    listed = set(paths)
    left = [entry for path, entry in changed.items() if path not in listed]
    totals = (
        sum(e["added"] for e in changed.values()),
        sum(e["removed"] for e in changed.values()),
    )
    rest = (
        (len(left), sum(e["added"] for e in left), sum(e["removed"] for e in left))
        if left
        else None
    )
    return RowPlan(rows, heading, totals, rest)


def falsifier_band(merged: dict[str, Any], ordered: list[dict[str, Any]]) -> Band | None:
    """The band above the rows, or None before the check existed.

    The check's result has no place on the map's rows: a contest disputes a
    finding's claim, never a file or a line. So it sits in a band above them,
    one mark per line. A grey dot is a finding the check read and did not
    contest -- deliberately not a tick, because the check reads the diff alone
    and its silence approves nothing. A contest is the amber diamond the card
    repeats. A check that was skipped or whose reply was lost marks nothing,
    and the band is hatched: a row of dots there would read as findings that
    were examined.

    Withdrawn findings, from version-2 and -3 artifacts, are not on the map, as
    they are in no count or list the page acts on; the caption still says how
    many there were.
    """
    state = merged["run"].get("falsification")
    if state is None:
        return None
    if state != "ran":
        caption = {
            "skipped": "Falsification check skipped: no finding was examined",
            "failed": "Falsification check failed: its reply could not be read",
        }.get(state, f"Falsification check {state}")
        return Band(caption, True, frozenset())

    total = len(merged["findings"])
    contested = [f for f in ordered if f.get("contested_md")]
    if merged["schema_version"] >= 4:
        caption = (
            f"Falsification check: contests {len(contested)} of {total}"
            if contested
            else f"Falsification check: read all {total}, contested none"
        )
    else:
        withdrawn = total - len(ordered)
        caption = f"Falsification check: read all {total}"
        if withdrawn:
            caption += f"; {withdrawn} withdrawn, not mapped"
    return Band(caption, False, frozenset(f["id"] for f in contested))


# A grid line, or a span between two. Integers only: every position the map
# writes is a number computed here, never text that came from an artifact.
Place = int | tuple[int, int]


def at(place: Place) -> str:
    return f"{place[0]} / {place[1]}" if isinstance(place, tuple) else str(place)


def cell(
    classes: str,
    column: Place | None,
    row: Place,
    inner: str = "",
    *,
    tag: str = "span",
    before: str = "",
    after: str = "",
) -> str:
    """One item of the map's grid, placed by `at`.

    Its place is the only style it carries, so the promise that every position
    is an integer is kept in one function rather than in each of the map's
    cells. `classes`, `inner` and the attributes either side of the style
    arrive escaped; None for `column` leaves it to the stylesheet, as a row
    spanning the whole grid does.
    """
    style = (
        f"grid-row:{at(row)}" if column is None else f"grid-column:{at(column)};grid-row:{at(row)}"
    )
    return f'<{tag} class="{classes}"{before} style="{style}"{after}>{inner}</{tag}>'


def lane_attr(finding: dict[str, Any]) -> str:
    """The data- attribute the dismissal script finds a finding's lane by: on its
    id, its line and its band mark alike."""
    return f' data-lane="{esc(finding["id"])}"'


HIDDEN = ' aria-hidden="true"'


def counts(added: int, removed: int) -> str:
    """`+12 &minus;3`, each side only when it is not zero, as git's summary line."""
    said = [text for text, n in ((f"+{added}", added), (f"&minus;{removed}", removed)) if n]
    return " ".join(said) or "0"


def churn(added: int, removed: int, widest: int) -> str:
    """A row's bar and counts. Added dark, removed light: red and green are the
    map's disposition colours, and a bar in either would read as one."""
    bar = "".join(
        f'<b class="{cls}" style="width:{max(1, round(n * CHURN_BAR_MAX / widest))}px"></b>'
        for cls, n in (("lm-add", added), ("lm-del", removed))
        if n
    )
    return f'<span class="lm-bar">{bar}</span>{counts(added, removed)}'


class Grid(NamedTuple):
    """Where the map's parts go: the files' column, the churn's, the first
    quality lane's, the first file row, and each finding's lane by id; how many
    lanes stand on each side; and the grid's own two templates."""

    path_col: int
    churn_col: int
    right_col: int
    first: int
    column: dict[str, int]
    left: int
    right: int
    columns: str
    rows: str


def layout(ordered: list[dict[str, Any]], plan: RowPlan, band: Band | None) -> Grid:
    """The grid's columns and rows, decided once for every builder below.

    The security pass's lanes stand left of the files and the quality pass's
    right, each side's first finding in page order nearest them.
    """
    left = [f for f in ordered if f["producer"] == "security"][::-1]
    right = [f for f in ordered if f["producer"] != "security"]
    path_col = len(left) + 2
    right_col = path_col + 3
    column = {f["id"]: index + 1 for index, f in enumerate(left)}
    column.update({f["id"]: right_col + index for index, f in enumerate(right)})
    # The files as wide as their names and no wider, so both sides' lines arrive
    # beside them; what width is left over goes to the last track, after the
    # quality pass's lines, where it separates nothing. The churn track is there
    # whether the run recorded churn or not: with nothing in it, a max-content
    # track is no width at all, so every run lays out with one grid.
    tracks = [
        *(["var(--lane)"] * len(left)),
        "14px",
        "minmax(12rem, max-content)",
        "max-content",
        "14px",
        *(["var(--lane)"] * len(right)),
        "1fr",
    ]
    rows = ["auto", "auto", *(["2.25rem"] if band else []), f"repeat({plan.shown}, var(--row))"]
    return Grid(
        path_col,
        path_col + 1,
        right_col,
        4 if band else 3,
        column,
        len(left),
        len(right),
        " ".join(tracks),
        " ".join(rows),
    )


def zebra_cells(grid: Grid, plan: RowPlan) -> list[str]:
    """Every other file row tinted, the summary row counted among them."""
    return [
        cell("lm-zebra", None, grid.first + index, after=HIDDEN)
        for index in range(1, plan.shown, 2)
    ]


def header_cells(grid: Grid, plan: RowPlan, ordered: list[dict[str, Any]]) -> list[str]:
    """Each pass's name over its lanes, the files' and the churn's headings, and
    each finding's id at the top of its line, linking to its card."""
    left, right = grid.left, grid.right
    cells: list[str] = []
    if left:
        cells.append(cell("lm-pass lm-pass-left", (1, grid.path_col), 1, PASS_NAME["security"]))
    if right:
        cells.append(
            cell(
                "lm-pass lm-pass-right",
                (grid.right_col, grid.right_col + right),
                1,
                PASS_NAME["quality"],
            )
        )
    if plan.totals is not None:
        cells.append(cell("lm-colhead", grid.churn_col, 2, counts(*plan.totals)))
    cells.append(cell("lm-colhead", grid.path_col, 2, plan.heading))
    for finding in ordered:
        finding_id = esc(finding["id"])
        cells.append(
            cell(
                f"lm-label {lane_classes(finding)}",
                grid.column[finding["id"]],
                2,
                finding_id,
                tag="a",
                before=f'{lane_attr(finding)} href="#finding-{finding_id}"',
            )
        )
    return cells


def band_cells(grid: Grid, band: Band | None, ordered: list[dict[str, Any]]) -> list[str]:
    """The falsification band: its ground, its caption, and a mark per line unless
    the check examined nothing."""
    if band is None:
        return []
    cells = [
        cell("lm-band" + (" lm-hatch" if band.hatched else ""), None, 3, after=HIDDEN),
        cell(
            "lm-band-caption" + (" lm-warn" if band.hatched else ""),
            grid.path_col,
            3,
            esc(band.caption),
        ),
    ]
    for finding in [] if band.hatched else ordered:
        cells.append(
            cell(
                f"lm-mark {lane_classes(finding)}",
                grid.column[finding["id"]],
                3,
                band_mark(finding["id"] in band.contested, finding["id"]),
                before=lane_attr(finding),
            )
        )
    return cells


def file_cells(grid: Grid, plan: RowPlan) -> list[str]:
    """A row per file -- its path, its docs-check notes, its churn -- and the
    summary row past MAP_ALL_FILES_MAX."""
    cells: list[str] = []
    widest = max([1, *(sum(row.churn) for row in plan.rows if row.churn is not None)])
    for index, row in enumerate(plan.rows):
        here = grid.first + index
        directory, name = map_path(row.path)
        tags = ""
        if row.notes:
            tags += '<a class="lm-tag" href="#docscheck">docs check: {} note{}</a>'.format(
                row.notes, "" if row.notes == 1 else "s"
            )
        cells.append(
            cell(
                "lm-file" + (" lm-outside" if row.outside else ""),
                grid.path_col,
                here,
                f'<span class="lm-dir">{esc(directory)}</span>{esc(name)}{tags}',
                after=f' title="{esc(row.path)}"',
            )
        )
        if row.churn is not None:
            cells.append(cell("lm-churn", grid.churn_col, here, churn(*row.churn, widest)))
        elif row.outside:
            # A finding may cite a file the diff never touched, and the docs check
            # reads documents beside the change. The run's facts say so in words;
            # the row says which, where its churn would be.
            cells.append(cell("lm-churn", grid.churn_col, here, "not in this diff"))
    if plan.rest:
        here = grid.first + len(plan.rows)
        files, added, removed = plan.rest
        cells.append(
            cell(
                "lm-file lm-outside",
                grid.path_col,
                here,
                "and {} more changed file{} no finding cites".format(
                    files, "" if files == 1 else "s"
                ),
            )
        )
        cells.append(cell("lm-churn", grid.churn_col, here, counts(added, removed)))
    return cells


def lane_cells(
    grid: Grid, plan: RowPlan, ordered: list[dict[str, Any]], cited: dict[str, list[str]]
) -> list[str]:
    """Each finding's line: one element from the first file it cites to the last,
    with a station on each. A station's place is rows from the line's top, which
    is why every row is the same height."""
    row_of = {row.path: index for index, row in enumerate(plan.rows)}
    cells: list[str] = []
    for finding in ordered:
        stops = sorted(row_of[path] for path in cited[finding["id"]])
        if not stops:
            continue
        top, bottom = stops[0], stops[-1]
        stations = "".join(
            '<i class="lm-st{}" style="--r:{}"></i>'.format(
                " lm-out" if plan.rows[stop].outside else "", stop - top
            )
            for stop in stops
        )
        cells.append(
            cell(
                f"lm-lane {lane_classes(finding)}",
                grid.column[finding["id"]],
                (grid.first + top, grid.first + bottom + 1),
                stations,
                before=lane_attr(finding),
                after=HIDDEN,
            )
        )
    return cells


def legend_items(
    plan: RowPlan, band: Band | None, ordered: list[dict[str, Any]], cited: dict[str, list[str]]
) -> list[str]:
    """The key, naming only the marks this map uses."""
    present = {f["disposition"] for f in ordered}
    items = [
        f'<li><span class="lm-key lm-lane lm-{d}" aria-hidden="true"><i class="lm-st"></i></span>'
        f"{label}</li>"
        for d, label in LANE_LABEL.items()
        if d in present
    ]
    outside = {row.path for row in plan.rows if row.outside}
    if any(path in outside for paths in cited.values() for path in paths):
        items.append(
            '<li><span class="lm-key lm-key-out lm-lane" aria-hidden="true"><i class="lm-st lm-out"></i></span>'
            "a file the diff did not touch</li>"
        )
    if band and not band.hatched:
        items.append(
            f'<li><span class="lm-key-mark" aria-hidden="true">{band_mark(False)}</span>'
            "read by the falsification check</li>"
        )
        if band.contested:
            items.append(
                f'<li><span class="lm-key-mark" aria-hidden="true">{band_mark(True)}</span>'
                "contested by it</li>"
            )
    return items


def render_line_map(merged: dict[str, Any], ordered: list[dict[str, Any]]) -> str:
    """Where the findings land: one line per finding, through the files it cites.

    Files are the rows, in path order; each finding is a column, and its line
    runs from the first file it cites to the last, with a station at each. The
    security pass's lines stand left of the files and the quality pass's right,
    so the two passes read as two readers of one change, and a file both cite
    is one row with lines arriving from both sides. A line wears its
    disposition as the card's edge does.

    One grid, placed by number. Each line is a single element spanning its rows,
    which is what lets a filter dim a finding and a dismissal fade it by
    touching one element per lane rather than a cell per file. The plan is made
    here -- rows, band, layout -- and each builder draws one part of it; they
    are called in document order, which is the order the cells paint in, since
    the cells share one stacking level.

    The key stands in a column beside the map rather than under it, where the
    eye reaches it from the lines it names without leaving them -- wherever the
    window has room for the page's side track. Narrower, it follows the map:
    see CSS_NARROW.
    """
    if not ordered:
        return ""
    notes: dict[str, int] = {}
    for note in (merged.get("docs_check") or {}).get("notes") or []:
        path = path_key(note["path"])
        notes[path] = notes.get(path, 0) + 1
    cited = {f["id"]: cited_paths(f) for f in ordered}
    plan = map_rows(
        merged["run"]["scope"],
        {path for paths in cited.values() for path in paths} | set(notes),
        notes,
    )
    band = falsifier_band(merged, ordered)
    grid = layout(ordered, plan, band)
    cells = [
        *zebra_cells(grid, plan),
        *header_cells(grid, plan, ordered),
        *band_cells(grid, band, ordered),
        *file_cells(grid, plan),
        *lane_cells(grid, plan, ordered, cited),
    ]
    return (
        '<section class="linemap tracks"><div class="lm-main"><h2 id="map">Where the findings land</h2>'
        '<p class="lm-intro">Each finding is a line through the files it cites: the security '
        "pass&rsquo;s left of them, the quality pass&rsquo;s right.</p>"
        '<div class="lm-scroll"><div class="lm{narrow}" style="grid-template-columns:{cols};'
        'grid-template-rows:{rows}">{cells}</div></div></div>'
        '<div class="lm-side"><ul class="lm-legend">{legend}</ul></div></section>'.format(
            narrow=" lm-narrow" if len(ordered) > LANES_WIDE_MAX else "",
            cols=grid.columns,
            rows=grid.rows,
            cells="".join(cells),
            legend="".join(legend_items(plan, band, ordered, cited)),
        )
    )


CSS = """
/* The line map. Rows are --row tall and nothing else sets their height, because
   a station's place is computed as rows from the top of its line: a row that
   grew with its content would leave the stations below it off their files.
   Positions arrive as numbers in each element's style; everything else is here. */
/* The map in the page's reading track and its key in the side track -- the
   section takes the page's `.tracks` -- so the key stands under the copies in
   the masthead and the map's heading rule ends where every other one does. The
   side track starts at the heading rather than at the map's first row: the
   heading has no room to the right of it to lose. */
.linemap { align-items: start; margin-top: 30px; }
.linemap h2 { margin-top: 0; }
.lm-main { min-width: 0; }
.lm-side { padding-top: 4px; }
/* `lm-intro`, never `lm-note`: lanes carry their disposition as `lm-<name>`, and
   a note's lane would take this paragraph's margins with the name. */
.lm-intro { color: var(--muted); font-size: 13px; line-height: 1.5; margin: -8px 0 12px; }
.lm-scroll { overflow-x: auto; padding-bottom: 4px; }
/* As wide as its tracks want, and never narrower than the column. Held to the
   column's width instead, a long run's lanes alone overflow it, the grid has no
   width left to give the file names, and they are cut to their 12rem floor;
   sized to its content, the map scrolls sideways and every name stays whole. */
.lm { --row: 30px; --lane: 40px; display: grid; font-family: var(--disp); width: max-content; min-width: 100%; }
.lm.lm-narrow { --lane: 28px; }
.lm > * { min-width: 0; z-index: 1; }
.lm-zebra, .lm-band { grid-column: 1 / -1; z-index: 0; }
.lm-zebra { background: var(--zebra); }
.lm-band { border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.lm-band.lm-hatch { background: repeating-linear-gradient(135deg, var(--zebra) 0 6px, transparent 6px 12px); }
/* Each pass's name sits at the end of its rule nearest the files, so a run with
   twenty lanes on one side does not centre the name out of view. */
.lm-pass { align-self: end; margin: 0 4px 6px; padding-bottom: 3px; border-bottom: 1.5px solid var(--ink);
  font-size: 13px; font-weight: 600; color: var(--ink-2); white-space: nowrap; }
.lm-pass-left { text-align: right; }
.lm-pass-right { text-align: left; }
.lm-colhead { align-self: end; padding: 0 0 7px 10px; font-size: 12px; color: var(--muted); white-space: nowrap; }
.lm-label { align-self: end; justify-self: center; padding-bottom: 6px; font: 700 12px/1.2 var(--mono);
  color: var(--note); text-decoration: none; }
.lm-label.lm-blocking { color: var(--block); }
.lm-label.lm-follow-up { color: var(--follow); }
.lm-label:hover { text-decoration: underline; }
/* Set by the page's script on a link whose card or dispute is off the page: a
   dimmed line that still took a click would go nowhere. */
.lm .lm-inert { pointer-events: none; cursor: default; }
/* Turned to read bottom to top, so the narrow lanes of a long run keep whole ids. */
.lm-narrow .lm-label { writing-mode: vertical-rl; transform: rotate(180deg); padding: 6px 0 0; }
.lm-band-caption { align-self: center; padding-left: 10px; font-size: 12.5px; color: var(--ink-2);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.lm-band-caption.lm-warn { color: var(--block); }
.lm-mark { display: flex; align-items: center; justify-content: center; }
.lm-dot { display: block; width: 6px; height: 6px; border-radius: 50%; background: var(--muted); }
/* The contest's mark is a diamond because every ring on the map is a file; it is
   amber, the card's colour for a contest, because red would say who lost. */
.lm-contest { display: flex; align-items: center; justify-content: center; width: 17px; height: 17px;
  transform: rotate(45deg); border: 2px solid var(--follow-line); background: var(--panel); text-decoration: none; }
.lm-contest span { transform: rotate(-45deg); font: 700 11px/1 var(--disp); color: var(--follow); }
.lm-file { align-self: center; padding-left: 10px; font: 12.5px/1.2 var(--mono); white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
.lm-dir { color: var(--muted); }
.lm-tag { margin-left: 8px; padding: 1px 8px; border: 1px solid var(--line); border-radius: 999px;
  font: 11.5px/1.4 var(--disp); color: var(--accent); text-decoration: none; }
/* A line wears its disposition as a card's edge does, from the same tokens on
   the page's :root. The station size is the map's own, not a disposition's. */
.lm-lane { position: relative; --c: var(--note-c); --w: var(--note-w); --s: var(--note-s); --d: 14px; }
.lm-lane.lm-blocking { --c: var(--blocking-c); --w: var(--blocking-w); --s: var(--blocking-s); --d: 17px; }
.lm-lane.lm-follow-up { --c: var(--follow-up-c); --w: var(--follow-up-w); --s: var(--follow-up-s); }
.lm-lane::before { content: ""; position: absolute; left: 50%; top: calc(var(--row) / 2);
  bottom: calc(var(--row) / 2); transform: translateX(-50%); border-left: var(--w) var(--s) var(--c); }
.lm-st { position: absolute; left: 50%; top: calc(var(--r, 0) * var(--row) + var(--row) / 2);
  width: var(--d); height: var(--d); transform: translate(-50%, -50%); border-radius: 50%;
  background: var(--panel); border: 3px solid var(--c); }
.lm-legend { display: flex; flex-direction: column; gap: 6px; list-style: none; margin: 0; padding: 0;
  font: 13px/1.4 var(--disp); color: var(--ink-2); }
.lm-legend li { display: flex; align-items: center; gap: 8px; }
.lm-key { --row: 22px; width: 22px; height: 22px; }
.lm-key::before { top: 0; bottom: 0; }
.lm-key-mark { display: flex; align-items: center; justify-content: center; width: 22px; height: 22px; }
.lm-key-out::before { display: none; }
/* A station on a file the diff did not touch: the ring is drawn, not filled,
   as a cited file is real but the change is not in it. */
.lm-st.lm-out { border-style: dotted; background: transparent; }
.lm-file.lm-outside { color: var(--muted); }
.lm-churn { align-self: center; display: flex; align-items: center; gap: 8px; padding-left: 12px;
  font: 11.5px/1 var(--mono); color: var(--muted); white-space: nowrap; }
.lm-bar { display: flex; height: 8px; }
.lm-bar b { display: block; height: 8px; }
.lm-add { background: var(--ink-2); }
.lm-del { background: var(--muted); opacity: .4; }
/* The filters hide cards; on the map they dim, so the lines left lit stay where
   they were drawn and the dimmed ones still say what else is there. `High` takes
   `critical` with it, as it does for the cards. */
#f-prod-security:checked ~ .layout .lm .lm-quality,
#f-prod-quality:checked ~ .layout .lm .lm-security,
#f-block-only:checked ~ .layout .lm .lm-follow-up,
#f-block-only:checked ~ .layout .lm .lm-note,
#f-sev-high:checked ~ .layout .lm .lm-rated:not(.lm-sev-high):not(.lm-sev-critical),
#f-sev-medium:checked ~ .layout .lm .lm-rated:not(.lm-sev-medium),
#f-sev-low:checked ~ .layout .lm .lm-rated:not(.lm-sev-low) { opacity: .15; }
/* A dismissed finding fades, under `Hide dismissed` as without it. The page's
   `.dismissed` would be taken out of the flow by that control, and a map that
   lost lines would no longer say what the run found; so the script marks the
   map's parts `.lm-dismissed`, which nothing hides. */
.lm .lm-dismissed { opacity: .3; }
.lm-label.lm-dismissed { text-decoration: line-through; }
"""

# The map's rules for a window too narrow for the page's side track. page.py
# puts them inside its own query for that width, which is written only there.
CSS_NARROW = """
  /* The key follows the map, and lays out in a row again. */
  .lm-side { padding-top: 14px; }
  .lm-legend { flex-direction: row; flex-wrap: wrap; gap: 6px 22px; }
"""
