#!/usr/bin/env python3
"""The line map: where the findings land. Stdlib only, 3.10 syntax.

Not a script -- page.py's second renderer, as markdown_subset is its first
helper. page.py builds the report and knows what each finding says; this draws
one picture of where they all fall, and is the only part of the page that does
geometry: rows, columns, track templates, station offsets and a churn scale.
It reads the merged artifact and the findings in page order, and returns markup
and the stylesheet that draws it, which page.py places.
"""

from typing import Any

from escaping import esc

# The most lanes that keep the width an id reads across at: eight 40px lanes,
# beside a path of MAP_PATH_MAX characters and its churn, is what the report's
# main column holds. Past it the lanes narrow and their ids turn on their side,
# and past the column's width the map scrolls sideways rather than squeezing
# the file names out.
LANES_WIDE_MAX = 8
# What one line in the map's key stands for: one finding, so singular, where a
# heading counts a list. In page order, which is the order the key lists them.
LANE_LABEL = {"blocking": "Blocking", "follow-up": "Follow-up", "note": "Note"}
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
    """The files a finding cites, each once, in the order it first names them."""
    paths: list[str] = []
    for location in finding.get("locations") or []:
        if location["path"] not in paths:
            paths.append(location["path"])
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


def falsifier_band(
    merged: dict[str, Any], ordered: list[dict[str, Any]]
) -> tuple[str, bool, dict[str, str]] | None:
    """(caption, hatched, mark per finding id), or None before the check existed.

    The check's result has no place on the map's rows: a contest disputes a
    finding's claim, never a file or a line. So it sits in a band above them,
    one mark per line. A grey dot is a finding the check read and did not
    contest -- deliberately not a tick, because the check reads the diff alone
    and its silence passes nothing. A contest is the amber diamond the card
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
        return caption, True, {}

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

    marks: dict[str, str] = {}
    for finding in ordered:
        finding_id = esc(finding["id"])
        if finding.get("contested_md"):
            marks[finding["id"]] = (
                f'<a class="lm-contest" href="#contest-{finding_id}" '
                'title="Contested by the falsification check" '
                f'aria-label="{finding_id}: contested by the falsification check">'
                '<span aria-hidden="true">?</span></a>'
            )
        else:
            marks[finding["id"]] = (
                '<i class="lm-dot" title="Read by the falsification check, not contested"></i>'
            )
    return caption, False, marks


def render_line_map(merged: dict[str, Any], ordered: list[dict[str, Any]]) -> str:
    """Where the findings land: one line per finding, through the files it cites.

    Files are the rows, in path order; each finding is a column, and its line
    runs from the first file it cites to the last, with a station at each. The
    security pass's lines stand left of the files and the quality pass's right,
    each side's first finding in page order nearest them -- so the two passes
    read as two readers of one change, and a file both cite is one row with
    lines arriving from both sides. A line wears its disposition as the card's
    edge does.

    One grid, placed by number. Each line is a single element spanning its rows,
    which is what lets a filter dim a finding and a dismissal fade it by
    touching one element per lane rather than a cell per file. Every position
    here is an integer computed below, and every word a pass or the repository
    wrote is escaped, like everything else on the page.
    """
    if not ordered:
        return ""
    docs_check = merged.get("docs_check")
    notes: dict[str, int] = {}
    for note in (docs_check or {}).get("notes") or []:
        notes[note["path"]] = notes.get(note["path"], 0) + 1

    scope = merged["run"]["scope"]
    # Every file the change touched, with its churn, when the run recorded them
    # -- scope.py has since this map existed. An older artifact maps only the
    # files something cites, and says nothing about what the diff did not touch,
    # because it cannot know.
    changed: dict[str, dict[str, Any]] | None = None
    if "files" in scope:
        changed = {entry["path"]: entry for entry in scope["files"]}

    cited = {f["id"]: cited_paths(f) for f in ordered}
    named = {path for paths in cited.values() for path in paths} | set(notes)
    rest: list[dict[str, Any]] = []
    if changed is not None and len(changed) <= MAP_ALL_FILES_MAX:
        rows = sorted(named | set(changed))
    else:
        rows = sorted(named)
        if changed is not None:
            rest = [entry for path, entry in changed.items() if path not in named]
    row_of = {path: index for index, path in enumerate(rows)}
    outside = {path for path in rows if changed is not None and path not in changed}

    left = [f for f in ordered if f["producer"] == "security"][::-1]
    right = [f for f in ordered if f["producer"] != "security"]
    path_col = len(left) + 2
    churn_col = path_col + 1 if changed is not None else None
    right_col = path_col + (3 if changed is not None else 2)
    column = {f["id"]: index + 1 for index, f in enumerate(left)}
    column.update({f["id"]: right_col + index for index, f in enumerate(right)})
    # The files as wide as their names and no wider, so both sides' lines arrive
    # beside them; what width is left over goes to the last track, after the
    # quality pass's lines, where it separates nothing.
    tracks = [
        *(["var(--lane)"] * len(left)),
        "14px",
        "minmax(12rem, max-content)",
        *(["max-content"] if changed is not None else []),
        "14px",
        *(["var(--lane)"] * len(right)),
        "1fr",
    ]

    band = falsifier_band(merged, ordered)
    first = 4 if band else 3
    shown = len(rows) + (1 if rest else 0)
    template_rows = [
        "auto",
        "auto",
        *(["2.25rem"] if band else []),
        f"repeat({shown}, var(--row))",
    ]

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

    bits: list[str] = []
    for index in range(1, shown, 2):
        bits.append(
            f'<span class="lm-zebra" style="grid-row:{first + index}" aria-hidden="true"></span>'
        )
    if left:
        bits.append(
            f'<span class="lm-pass lm-pass-left" style="grid-column:1 / {path_col};grid-row:1">'
            "Security</span>"
        )
    if right:
        bits.append(
            f'<span class="lm-pass lm-pass-right" style="grid-column:{right_col} / {right_col + len(right)};'
            'grid-row:1">Quality</span>'
        )
    if changed is None:
        heading = "Files cited"
    else:
        count = scope["files_changed"]
        heading = "{} changed file{}".format(count, "" if count == 1 else "s")
        if outside:
            heading += f", and {len(outside)} outside the diff"
        bits.append(
            '<span class="lm-colhead" style="grid-column:{};grid-row:2">{}</span>'.format(
                churn_col,
                counts(
                    sum(e["added"] for e in changed.values()),
                    sum(e["removed"] for e in changed.values()),
                ),
            )
        )
    bits.append(
        f'<span class="lm-colhead" style="grid-column:{path_col};grid-row:2">{heading}</span>'
    )
    for finding in ordered:
        finding_id = esc(finding["id"])
        bits.append(
            f'<a class="lm-label {lane_classes(finding)}" data-lane="{finding_id}" '
            f'href="#finding-{finding_id}" style="grid-column:{column[finding["id"]]};grid-row:2">'
            f"{finding_id}</a>"
        )

    if band:
        caption, hatched, marks = band
        bits.append(
            '<span class="lm-band{}" style="grid-row:3" aria-hidden="true"></span>'.format(
                " lm-hatch" if hatched else ""
            )
        )
        bits.append(
            '<span class="lm-band-caption{}" style="grid-column:{};grid-row:3">{}</span>'.format(
                " lm-warn" if hatched else "", path_col, esc(caption)
            )
        )
        for finding in ordered:
            if finding["id"] in marks:
                bits.append(
                    f'<span class="lm-mark {lane_classes(finding)}" data-lane="{esc(finding["id"])}" '
                    f'style="grid-column:{column[finding["id"]]};grid-row:3">{marks[finding["id"]]}</span>'
                )

    widest = max(
        [1, *(e["added"] + e["removed"] for p, e in (changed or {}).items() if p in row_of)]
    )
    for index, path in enumerate(rows):
        directory, name = map_path(path)
        tags = ""
        if path in notes:
            count = notes[path]
            tags += '<a class="lm-tag" href="#docscheck">docs check: {} note{}</a>'.format(
                count, "" if count == 1 else "s"
            )
        bits.append(
            '<span class="lm-file{}" style="grid-column:{};grid-row:{}" title="{}">'
            '<span class="lm-dir">{}</span>{}{}</span>'.format(
                " lm-outside" if path in outside else "",
                path_col,
                first + index,
                esc(path),
                esc(directory),
                esc(name),
                tags,
            )
        )
        if changed is not None and path in changed:
            entry = changed[path]
            bits.append(
                f'<span class="lm-churn" style="grid-column:{churn_col};grid-row:{first + index}">'
                f"{churn(entry['added'], entry['removed'], widest)}</span>"
            )
        elif path in outside:
            # A finding may cite a file the diff never touched, and the docs check
            # reads documents beside the change. The run panel says so in words;
            # the row says which, where its churn would be.
            bits.append(
                f'<span class="lm-churn" style="grid-column:{churn_col};grid-row:{first + index}">'
                "not in this diff</span>"
            )
    if rest:
        here = first + len(rows)
        bits.append(
            f'<span class="lm-file lm-outside" style="grid-column:{path_col};grid-row:{here}">'
            "and {} more changed file{} no finding cites</span>".format(
                len(rest), "" if len(rest) == 1 else "s"
            )
        )
        bits.append(
            f'<span class="lm-churn" style="grid-column:{churn_col};grid-row:{here}">'
            f"{counts(sum(e['added'] for e in rest), sum(e['removed'] for e in rest))}</span>"
        )

    for finding in ordered:
        stops = sorted(row_of[path] for path in cited[finding["id"]])
        if not stops:
            continue
        top, bottom = stops[0], stops[-1]
        stations = "".join(
            '<i class="lm-st{}" style="--r:{}"></i>'.format(
                " lm-out" if rows[stop] in outside else "", stop - top
            )
            for stop in stops
        )
        bits.append(
            f'<span class="lm-lane {lane_classes(finding)}" data-lane="{esc(finding["id"])}" '
            f'style="grid-column:{column[finding["id"]]};grid-row:{first + top} / {first + bottom + 1}" '
            f'aria-hidden="true">{stations}</span>'
        )

    present = {f["disposition"] for f in ordered}
    legend = [
        f'<li><span class="lm-key lm-lane lm-{d}" aria-hidden="true"><i class="lm-st"></i></span>'
        f"{label}</li>"
        for d, label in LANE_LABEL.items()
        if d in present
    ]
    if any(path in outside for paths in cited.values() for path in paths):
        legend.append(
            '<li><span class="lm-key lm-key-out lm-lane" aria-hidden="true"><i class="lm-st lm-out"></i></span>'
            "a file the diff did not touch</li>"
        )
    if band and not band[1]:
        legend.append(
            '<li><span class="lm-key-mark" aria-hidden="true"><i class="lm-dot"></i></span>'
            "read by the falsification check</li>"
        )
        if any(f.get("contested_md") for f in ordered):
            legend.append(
                '<li><span class="lm-key-mark" aria-hidden="true"><span class="lm-contest">'
                '<span aria-hidden="true">?</span></span></span>contested by it</li>'
            )

    return (
        '<section class="linemap"><h2 id="map">Where the findings land</h2>'
        '<p class="lm-intro">Each finding is a line through the files it cites: the security '
        "pass&rsquo;s left of them, the quality pass&rsquo;s right.</p>"
        '<div class="lm-scroll"><div class="lm{narrow}" style="grid-template-columns:{cols};'
        'grid-template-rows:{rows}">{bits}</div></div>'
        '<ul class="lm-legend">{legend}</ul></section>'.format(
            narrow=" lm-narrow" if len(ordered) > LANES_WIDE_MAX else "",
            cols=" ".join(tracks),
            rows=" ".join(template_rows),
            bits="".join(bits),
            legend="".join(legend),
        )
    )


CSS = """
/* The line map. Rows are --row tall and nothing else sets their height, because
   a station's place is computed as rows from the top of its line: a row that
   grew with its content would leave the stations below it off their files.
   Positions arrive as numbers in each element's style; everything else is here. */
.linemap h2 { margin-top: 30px; }
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
/* A line wears its disposition as a card's edge does. */
.lm-lane { position: relative; --c: var(--note); --w: 2px; --d: 14px; }
.lm-lane.lm-blocking { --c: var(--block); --w: 6px; --d: 17px; }
.lm-lane.lm-follow-up { --c: var(--follow-line); --w: 3px; }
.lm-lane::before { content: ""; position: absolute; left: 50%; top: calc(var(--row) / 2);
  bottom: calc(var(--row) / 2); transform: translateX(-50%); border-left: var(--w) solid var(--c); }
.lm-lane.lm-note::before { border-left-style: dashed; }
.lm-st { position: absolute; left: 50%; top: calc(var(--r, 0) * var(--row) + var(--row) / 2);
  width: var(--d); height: var(--d); transform: translate(-50%, -50%); border-radius: 50%;
  background: var(--panel); border: 3px solid var(--c); }
.lm-legend { display: flex; flex-wrap: wrap; gap: 6px 22px; list-style: none; margin: 12px 0 0; padding: 0;
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
