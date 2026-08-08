#!/usr/bin/env python3
"""Build the report page from a merged artifact.

Everything here knows what a finding is. Nothing here knows where the page ends
up, and nothing here parses markdown -- that is `markdown_subset`.
"""

import html

# Imported by render.py, which is the only file here run directly and the only
# one that puts this directory on sys.path. A library module that mutates global
# import state as a side effect changes how every later import in the process
# resolves, so it does not do that.
import validate
from markdown_subset import Markdown


DISPOSITION_ORDER = ("blocking", "follow-up", "note")
DISPOSITION_LABEL = {"blocking": "Blocking", "follow-up": "Follow-up", "note": "Notes"}
PRODUCER_LABEL = {"security": "Security & correctness", "quality": "Code quality"}
SCOPE_MODE_LABEL = {"revisions": "revision range", "local-patch": "local working patch"}

CATEGORY_LABEL = {
    "structural-regression": "Structural code-quality regressions",
    "simplification-missed": "Missed opportunities for dramatic simplification / code-judo restructuring",
    "branching-complexity": "Spaghetti / branching complexity increases",
    "boundary-contract": "Boundary / abstraction / type-contract problems that make the code harder to reason about",
    "modularity-decomposition": "Modularity, abstraction, and decomposition issues",
    "legibility": "Legibility and maintainability concerns",
}

# How loudly a finding shouts inside its disposition. Severity and category are
# not comparable to each other, but both were always answering this one
# question, so the renderer holds the table and the artifact stays unchanged.
SEVERITY_RANK = {"critical": 0, "high": 0, "medium": 1, "low": 2}
CATEGORY_RANK = {
    "structural-regression": 0,
    "simplification-missed": 1,
    "branching-complexity": 1,
    "boundary-contract": 2,
    "modularity-decomposition": 2,
    "legibility": 3,
}
UNRANKED = 3  # a security note carries no severity, so it has nothing to rank by


# --- ordering ----------------------------------------------------------------


def id_sort_key(finding_id):
    match = validate.ID_RE.match(finding_id or "")
    if not match:
        return ("", 0)
    return (match.group(1), int(match.group(2)))


def rank_of(finding):
    if finding.get("producer") == "security":
        return SEVERITY_RANK.get(finding.get("severity"), UNRANKED)
    return CATEGORY_RANK.get(finding.get("category"), UNRANKED)


def build_units(findings):
    """Group corroborating findings so partners always render adjacent.

    Union-find rather than pairing, so a future three-way link needs no change
    here. Two cards arguing one defect from two angles read as the page
    repeating itself unless they sit together.
    """
    parent = {f["id"]: f["id"] for f in findings}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for finding in findings:
        for partner in finding.get("corroborated_by") or []:
            if partner in parent:
                a, b = find(finding["id"]), find(partner)
                if a != b:
                    parent[a] = b

    grouped = {}
    for finding in findings:
        grouped.setdefault(find(finding["id"]), []).append(finding)
    return list(grouped.values())


def ordered_units(findings):
    units = build_units(findings)
    for unit in units:
        unit.sort(key=lambda f: (rank_of(f), id_sort_key(f["id"])))
    units.sort(
        key=lambda unit: (
            DISPOSITION_ORDER.index(unit[0]["disposition"]),
            min(rank_of(f) for f in unit),
            0 if len(unit) > 1 else 1,
            min(id_sort_key(f["id"]) for f in unit),
        )
    )
    return units


# --- page --------------------------------------------------------------------


def producer_label(producer):
    """Escaped at the point of use, like every other string on the page."""
    return esc(PRODUCER_LABEL.get(producer, producer))


def esc(value):
    return html.escape(str(value), quote=True)


def truncate(text, limit=58):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def location_chip(location, primary):
    path = esc(location["path"])
    start, end = location.get("start_line"), location.get("end_line")
    if start and end and end != start:
        label = "{}:{}-{}".format(path, start, end)
    elif start:
        label = "{}:{}".format(path, start)
    else:
        label = path
    # One element, no interior markup, no interior whitespace: the reader's
    # whole job here is to select it in one gesture.
    return '<span class="chip{}">{}</span>'.format(" chip-primary" if primary else "", label)


def render_finding(finding, markdown, partners):
    finding_id = finding["id"]
    bits = ['<article class="finding" id="finding-{}" data-producer="{}" data-disposition="{}">'.format(
        esc(finding_id), esc(finding["producer"]), esc(finding["disposition"])
    )]

    axis = ""
    if finding.get("severity"):
        axis = '<span class="tag tag-sev tag-{0}">{0}</span>'.format(esc(finding["severity"]))
    elif finding.get("category"):
        axis = '<span class="tag tag-cat" title="{}">{}</span>'.format(
            esc(CATEGORY_LABEL.get(finding["category"], finding["category"])), esc(finding["category"])
        )

    confidence = ""
    if finding.get("confidence") in ("medium", "low"):
        confidence = '<span class="tag tag-conf" title="{}">{} confidence</span>'.format(
            esc(finding.get("confidence_rationale") or ""), esc(finding["confidence"])
        )

    bits.append(
        '<header class="finding-head">'
        '<span class="fid">{fid}</span>'
        '<h3>{title}</h3>'
        '<div class="tags"><span class="tag tag-prod">{prod}</span>{axis}{conf}</div>'
        "</header>".format(
            fid=esc(finding_id),
            title=markdown.inline(finding["title"], self_id=finding_id),
            prod=producer_label(finding["producer"]),
            axis=axis,
            conf=confidence,
        )
    )

    chips = "".join(
        location_chip(location, index == 0) for index, location in enumerate(finding.get("locations") or [])
    )
    bits.append('<div class="locations">{}</div>'.format(chips))

    if partners:
        links = ", ".join(
            '<a class="xref" href="#finding-{0}">{0}</a>'.format(esc(p["id"])) for p in partners
        )
        quoted = esc(truncate(Markdown.plain(partners[0]["title"]), 96))
        bits.append(
            '<p class="corroboration"><strong>Corroborated by {links}</strong> '
            "&mdash; “{quoted}”</p>".format(links=links, quoted=quoted)
        )

    bits.append('<div class="body">{}</div>'.format(markdown.render(finding["body_md"], self_id=finding_id)))
    bits.append("</article>")
    return "\n".join(bits)


def verdict_sentence(verdict, findings):
    blocking = [f for f in findings if f["disposition"] == "blocking"]
    total = len(findings)
    if verdict == "blocked":
        by_producer = {}
        for finding in blocking:
            by_producer[finding["producer"]] = by_producer.get(finding["producer"], 0) + 1
        parts = ["{} from the {} pass".format(count, name) for name, count in sorted(by_producer.items())]
        return "{} of {} findings block this change &mdash; {}.".format(
            len(blocking), total, " and ".join(parts)
        )
    if total:
        return "{} findings, none of which block this change.".format(total)
    return "Neither pass found anything to report."


def render_scope(run):
    scope = run["scope"]
    rows = [
        ("Repository", esc(scope["repo"])),
        ("Scope mode", esc(SCOPE_MODE_LABEL.get(scope["mode"], scope["mode"]))),
        ("Base", '<span class="chip">{}</span>'.format(esc(scope["base"]))),
    ]
    if scope.get("head"):
        rows.append(("Head", '<span class="chip">{}</span>'.format(esc(scope["head"]))))
    rows.append(("Files changed", esc(scope["files_changed"])))
    rows.append(("Diff size", "{:,} bytes".format(scope["diff_bytes"])))
    rows.append(
        ("Run", '{} passes, generated <span class="nowrap">{}</span>'.format(esc(run["mode"]), esc(run["generated_at"])))
    )
    body = "".join("<div class=\"kv\"><dt>{}</dt><dd>{}</dd></div>".format(k, v) for k, v in rows)
    note = (
        "The passes reviewed this diff and read the repository around it, so a finding may cite a file "
        "the diff never touched &mdash; or one a remedy proposes and nothing has written yet."
    )
    untracked = ""
    if scope.get("untracked"):
        untracked = (
            '<p class="warn">{} file(s) in this working tree are untracked and were '
            "not reviewed. Git can only diff what it has been told about.</p>".format(scope["untracked"])
        )
    sequential = ""
    if run["mode"] == "sequential":
        sequential = (
            '<p class="warn">Both rubrics ran in one context window rather than side by side. '
            "A sequential run is the weaker run.</p>"
        )
    return (
        '<section id="scope" class="scope"><h2>Scope</h2><dl class="kvs">{}</dl>'
        '<p class="muted">{}</p>{}{}</section>'.format(body, note, untracked, sequential)
    )


def render_pass_prose(passes, markdown):
    out = []
    for envelope in passes:
        producer = envelope["producer"]
        for key, heading in (("what_holds_up_md", "What holds up"), ("closing_md", "Closing notes")):
            if not envelope.get(key):
                continue
            anchor = "prose-{}-{}".format(producer, key.split("_")[0])
            out.append(
                '<section id="{anchor}" class="prose"><h2>{heading} &mdash; {label}</h2>'
                "<details open><summary>{label}</summary>{body}</details></section>".format(
                    anchor=anchor,
                    heading=esc(heading),
                    label=producer_label(producer),
                    body=markdown.render(envelope[key]),
                )
            )
        if envelope.get("empty_reason_md"):
            out.append(
                '<section id="prose-{p}-empty" class="prose"><h2>Nothing reported &mdash; {label}</h2>{body}</section>'.format(
                    p=esc(producer),
                    label=producer_label(producer),
                    body=markdown.render(envelope["empty_reason_md"]),
                )
            )
    return "\n".join(out)


def render_page(merged):
    findings = merged["findings"]
    markdown = Markdown({f["id"] for f in findings})
    by_id = {f["id"]: f for f in findings}
    units = ordered_units(findings)

    groups = {d: [] for d in DISPOSITION_ORDER}
    for unit in units:
        groups[unit[0]["disposition"]].append(unit)

    main = []
    nav = []
    for disposition in DISPOSITION_ORDER:
        unit_list = groups[disposition]
        if not unit_list:
            continue
        flat = [f for unit in unit_list for f in unit]
        present = {f["producer"] for f in flat}
        classes = "group " + " ".join("has-" + p for p in sorted(present))
        counts = "".join(
            '<span class="count count-{}">{}</span>'.format(
                key, sum(1 for f in flat if key == "all" or f["producer"] == key)
            )
            for key in ("all", "security", "quality")
        )
        heading = '{} <span class="counts">&middot; {}</span>'.format(DISPOSITION_LABEL[disposition], counts)

        main.append(
            '<section class="{cls}" data-disposition="{d}"><h2 id="group-{d}">{heading}</h2>{cards}</section>'.format(
                cls=classes,
                d=disposition,
                heading=heading,
                cards="\n".join(
                    render_finding(f, markdown, [by_id[p] for p in (f.get("corroborated_by") or []) if p in by_id])
                    for f in flat
                ),
            )
        )

        entries = "".join(
            '<li class="nav-finding" data-producer="{prod}"><a href="#finding-{fid}">'
            '<span class="nav-id">{fid}</span> {title}</a></li>'.format(
                prod=esc(f["producer"]), fid=esc(f["id"]), title=esc(truncate(Markdown.plain(f["title"])))
            )
            for f in flat
        )
        nav.append(
            '<div class="{cls}" data-disposition="{d}"><p class="nav-group"><a href="#group-{d}">{label}</a> '
            '<span class="counts">&middot; {counts}</span></p><ul>{entries}</ul></div>'.format(
                cls=classes, d=disposition, label=DISPOSITION_LABEL[disposition].upper(), counts=counts, entries=entries
            )
        )

    prose_links = []
    for envelope in merged["passes"]:
        producer = envelope["producer"]
        for key, heading in (("what_holds_up_md", "What holds up"), ("closing_md", "Closing notes")):
            if envelope.get(key):
                prose_links.append(
                    '<li><a href="#prose-{p}-{k}">{h} &mdash; {label}</a></li>'.format(
                        p=esc(producer), k=key.split("_")[0], h=esc(heading), label=producer_label(producer)
                    )
                )
        if envelope.get("empty_reason_md"):
            prose_links.append(
                '<li><a href="#prose-{p}-empty">Nothing reported &mdash; {label}</a></li>'.format(
                    p=esc(producer), label=producer_label(producer)
                )
            )

    verdict = merged["verdict"]
    scope = merged["run"]["scope"]
    scope_line = "{} &middot; {} &middot; {} files".format(
        esc(scope["repo"]), esc(SCOPE_MODE_LABEL.get(scope["mode"], scope["mode"])), scope["files_changed"]
    )

    return PAGE.format(
        title=esc("Two-Pass Review — {}".format(scope["repo"])),
        css=CSS,
        verdict=esc(verdict),
        verdict_label="Blocked" if verdict == "blocked" else "Clear",
        sentence=verdict_sentence(verdict, findings),
        scope_line=scope_line,
        nav="\n".join(nav),
        prose_links="".join(prose_links),
        scope_section=render_scope(merged["run"]),
        groups="\n".join(main),
        prose=render_pass_prose(merged["passes"], markdown),
    )


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<input type="radio" name="prod" id="f-prod-all" class="filter" checked>
<input type="radio" name="prod" id="f-prod-security" class="filter">
<input type="radio" name="prod" id="f-prod-quality" class="filter">
<input type="radio" name="block" id="f-block-all" class="filter" checked>
<input type="radio" name="block" id="f-block-only" class="filter">
<div class="layout">
  <aside class="sidebar">
    <div class="verdict verdict-{verdict}">
      <span class="verdict-label">{verdict_label}</span>
    </div>
    <div class="filters">
      <p class="filter-title">Pass</p>
      <label for="f-prod-all" class="pill pill-prod-all">Both</label>
      <label for="f-prod-security" class="pill pill-prod-security">Security</label>
      <label for="f-prod-quality" class="pill pill-prod-quality">Quality</label>
      <p class="filter-title">Show</p>
      <label for="f-block-all" class="pill pill-block-all">Everything</label>
      <label for="f-block-only" class="pill pill-block-only">Blocking only</label>
    </div>
    <nav>
      <p class="nav-group"><a href="#scope">Scope</a></p>
      {nav}
      <ul class="nav-prose">{prose_links}</ul>
    </nav>
  </aside>
  <main>
    <header class="masthead">
      <div class="verdict verdict-{verdict}"><span class="verdict-label">{verdict_label}</span></div>
      <p class="sentence">{sentence}</p>
      <p class="scope-line">{scope_line}</p>
    </header>
    {scope_section}
    {groups}
    {prose}
  </main>
</div>
</body>
</html>
"""

CSS = """
:root {
  --bg: #fbfaf8; --panel: #ffffff; --ink: #1c1a18; --muted: #6b6560; --line: #e2ddd6;
  --accent: #7a4b2a; --block: #a32a1e; --block-bg: #fbeae7; --follow: #7a5c12; --note: #5a6570;
  --code-bg: #f4f1ec; --chip-bg: #efeae2; --shadow: 0 1px 2px rgba(28,26,24,.06);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #17161a; --panel: #1e1d22; --ink: #e9e6e1; --muted: #a09a94; --line: #34323a;
    --accent: #d59a6c; --block: #f08b7e; --block-bg: #3a201d; --follow: #d6b45c; --note: #96a1ae;
    --code-bg: #26252b; --chip-bg: #2b2a31; --shadow: none;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
.filter { position: absolute; opacity: 0; pointer-events: none; }
.layout { display: grid; grid-template-columns: 300px minmax(0, 1fr); gap: 40px;
  max-width: 1240px; margin: 0 auto; padding: 32px 28px 96px; align-items: start; }
.sidebar { position: sticky; top: 24px; max-height: calc(100vh - 48px); overflow-y: auto; font-size: 14px; }
main { min-width: 0; }

.verdict { border-radius: 8px; padding: 10px 14px; margin-bottom: 18px; font-weight: 700;
  letter-spacing: .08em; text-transform: uppercase; font-size: 13px; }
.verdict-blocked { background: var(--block-bg); color: var(--block); border: 1px solid var(--block); }
.verdict-clear { background: var(--chip-bg); color: var(--ink); border: 1px solid var(--line); }
.masthead .verdict { display: inline-block; font-size: 15px; padding: 8px 18px; }
.masthead { margin-bottom: 34px; }
.sentence { font-size: 21px; line-height: 1.45; margin: 8px 0 6px; }
.scope-line { color: var(--muted); margin: 0; font-size: 14px; }

.filters { margin-bottom: 22px; }
.filter-title { margin: 12px 0 6px; font-size: 11px; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }
.pill { display: inline-block; padding: 3px 10px; margin: 0 4px 4px 0; border: 1px solid var(--line);
  border-radius: 999px; cursor: pointer; font-size: 13px; background: var(--panel); }
#f-prod-all:checked ~ .layout .pill-prod-all,
#f-prod-security:checked ~ .layout .pill-prod-security,
#f-prod-quality:checked ~ .layout .pill-prod-quality,
#f-block-all:checked ~ .layout .pill-block-all,
#f-block-only:checked ~ .layout .pill-block-only { background: var(--ink); color: var(--bg); border-color: var(--ink); }

nav .nav-group { margin: 16px 0 6px; font-size: 11px; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }
nav .nav-group a { color: inherit; text-decoration: none; }
nav ul { list-style: none; margin: 0; padding: 0; }
nav li { margin: 0 0 3px; line-height: 1.35; }
nav a { color: var(--ink); text-decoration: none; }
nav a:hover { text-decoration: underline; }
.nav-id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: var(--muted); }
[data-disposition="blocking"] .nav-id { color: var(--block); font-weight: 700; }
.nav-prose { margin-top: 18px; }
.nav-prose a { color: var(--muted); }

/* Counts stay honest under the producer filter: the renderer emits all three
   and CSS shows the one that matches. */
.count { display: none; }
#f-prod-all:checked ~ .layout .count-all,
#f-prod-security:checked ~ .layout .count-security,
#f-prod-quality:checked ~ .layout .count-quality { display: inline; }

/* Filtering, entirely in CSS. */
#f-prod-security:checked ~ .layout [data-producer="quality"],
#f-prod-quality:checked ~ .layout [data-producer="security"],
#f-prod-security:checked ~ .layout .group:not(.has-security),
#f-prod-quality:checked ~ .layout .group:not(.has-quality),
#f-block-only:checked ~ .layout [data-disposition="follow-up"],
#f-block-only:checked ~ .layout [data-disposition="note"] { display: none; }

h2 { font-size: 13px; letter-spacing: .1em; text-transform: uppercase; color: var(--muted);
  border-bottom: 1px solid var(--line); padding-bottom: 8px; margin: 42px 0 18px; }
.counts { font-variant-numeric: tabular-nums; }

/* Wide enough for a full 40-character SHA, which must not break mid-hash --
   half an object id on each of two lines is not a thing anyone can copy. */
.scope .kvs { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 10px 24px; margin: 0 0 14px; }
.scope .chip { overflow-wrap: normal; word-break: keep-all; }
.kv dt { font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 0; }
.kv dd { margin: 2px 0 0; }
.muted { color: var(--muted); font-size: 14px; }
.nowrap { white-space: nowrap; }
.finding-head h3 code { font-size: .92em; background: var(--code-bg); padding: 1px 5px; border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-weight: 600; }
.warn { color: var(--block); font-size: 14px; }

.finding { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 20px 22px; margin: 0 0 16px; box-shadow: var(--shadow); }
.finding-head { display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; align-items: baseline; }
.fid { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; color: var(--muted); }
[data-disposition="blocking"] .fid { color: var(--block); font-weight: 700; }
.finding-head h3 { margin: 0; font-size: 18px; line-height: 1.35; }
.tags { grid-column: 2; display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
.tag { font-size: 11px; letter-spacing: .04em; text-transform: uppercase; padding: 2px 8px;
  border-radius: 999px; border: 1px solid var(--line); color: var(--muted); }
.tag-critical, .tag-high { color: var(--block); border-color: var(--block); }
.tag-medium { color: var(--follow); border-color: var(--follow); }
.tag-conf { border-style: dashed; }

.locations { margin: 14px 0 4px; display: flex; flex-wrap: wrap; gap: 6px; }
/* Inert on purpose: a chip that looks like a link and is not one is a lie. */
.chip { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px;
  background: var(--chip-bg); border: 1px solid var(--line); border-radius: 5px; padding: 2px 7px;
  user-select: all; -webkit-user-select: all; text-decoration: none; cursor: text; }
.chip-primary { border-color: var(--accent); color: var(--accent); }

.corroboration { background: var(--chip-bg); border-left: 3px solid var(--accent);
  padding: 8px 12px; margin: 14px 0 0; font-size: 14px; border-radius: 0 5px 5px 0; }
.body { margin-top: 6px; }
.body p { margin: 12px 0; }
.body ul, .body ol { margin: 12px 0; padding-left: 22px; }
.body li { margin: 4px 0; }
.body blockquote { margin: 12px 0; padding: 2px 14px; border-left: 3px solid var(--line); color: var(--muted); }
.body code, .chip { overflow-wrap: anywhere; }
.body code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .89em;
  background: var(--code-bg); padding: 1px 5px; border-radius: 4px; }
.body pre { background: var(--code-bg); border: 1px solid var(--line); border-radius: 8px;
  padding: 12px 14px; overflow-x: auto; }
.body pre code { background: none; padding: 0; font-size: 12.5px; line-height: 1.5; }
.xref { color: var(--accent); text-decoration: none; border-bottom: 1px dotted var(--accent); }
a { color: var(--accent); }

.prose details { border: 1px solid var(--line); border-radius: 10px; background: var(--panel); padding: 0 18px; }
.prose summary { cursor: pointer; padding: 12px 0; font-size: 13px; letter-spacing: .06em;
  text-transform: uppercase; color: var(--muted); }
.prose details[open] summary { border-bottom: 1px solid var(--line); margin-bottom: 8px; }
.prose details > *:last-child { margin-bottom: 16px; }

:target { scroll-margin-top: 24px; }
.finding:target { border-color: var(--accent); }

@media (max-width: 900px) {
  .layout { grid-template-columns: minmax(0, 1fr); gap: 0; padding: 20px 16px 64px; }
  .sidebar { position: static; max-height: none; margin-bottom: 24px; }
}
"""
