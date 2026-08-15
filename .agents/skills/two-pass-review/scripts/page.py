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


# Appended after the payload by the second copy button. A suffix, not a
# template: the payload is a complete finding, so there is nothing to interpolate
# into and no placeholder to validate.
PROMPT_WRAPPER = (
    "verify the review comment, assess it against the real code and propose "
    "solutions (options), wait for my choice, always recommend one and explain why."
)


def copy_payload(finding, partners):
    """The markdown one copy button puts on the clipboard, for one finding.

    Composed from the finding dict rather than scraped from the rendered card:
    `body_md` is right here, and an agent reads the pass's own markdown better
    than it reads the HTML the pass's markdown turned into. This is the only
    place the page carries un-rendered source text, and that is deliberate.
    """
    lines = ["## {} — {}".format(finding["id"], Markdown.plain(finding["title"])), ""]

    axis = finding.get("severity") or CATEGORY_LABEL.get(finding.get("category"), finding.get("category"))
    meta = "{} pass · {}".format(finding["producer"], finding["disposition"])
    if axis:
        meta += " · {}".format(axis)
    lines.append(meta)

    # Stated only when it is not high, matching the card. The rationale names the
    # missing evidence, which is exactly what the agent is being asked to go and
    # check -- on the card it is a `title` attribute nobody hovers.
    if finding.get("confidence") in ("medium", "low"):
        rationale = finding.get("confidence_rationale") or ""
        lines.append("{} confidence{}".format(finding["confidence"], " — " + rationale if rationale else ""))

    # The dispute travels with the claim. Whoever pastes this payload is the
    # adjudicator the contest exists for -- an agent with repository access the
    # diff-starved check never had -- so the payload carries both sides and
    # says which is which, and pre-judges neither.
    if finding.get("contested_md"):
        lines.append("")
        lines.append("Contested by a diff-only falsification check — verify which side the code supports:")
        lines.append("> " + finding["contested_md"].replace("\n", "\n> "))

    for location in finding.get("locations") or []:
        start, end = location.get("start_line"), location.get("end_line")
        if start and end and end != start:
            lines.append("`{}:{}-{}`".format(location["path"], start, end))
        elif start:
            lines.append("`{}:{}`".format(location["path"], start))
        else:
            lines.append("`{}`".format(location["path"]))

    # Named, not pasted. The partner is a second finding with its own button, and
    # two bodies under one button leave the reader unsure what they just copied.
    for partner in partners:
        lines.append(
            'Corroborated by {} — "{}"'.format(partner["id"], truncate(Markdown.plain(partner["title"]), 96))
        )

    lines += ["", finding["body_md"], "", "— two-pass-review finding {}".format(finding["id"])]
    return "\n".join(lines)


# Drawn here rather than fetched or set in a font: an icon font is a sibling
# asset and this file has none, and the page is read over file:// and out of a
# mail client. `aria-hidden`, because every one of these sits beside the word it
# illustrates -- a screen reader that announces both hears the label twice.
def icon(paths, size=13, stroke="1.5"):
    return (
        '<svg class="icon" width="{s}" height="{s}" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="{w}" stroke-linecap="round" stroke-linejoin="round" '
        'aria-hidden="true" focusable="false">{p}</svg>'
    ).format(s=size, w=stroke, p=paths)


ICON_CLIPBOARD = icon('<rect x="9" y="9" width="13" height="13" rx="2"></rect>'
                      '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>')
ICON_TERMINAL = icon('<rect x="3" y="4" width="18" height="16" rx="2"></rect>'
                     '<path d="M8 10l2.5 2-2.5 2M13 14h3"></path>')
ICON_CHECK = icon('<polyline points="20 6 9 17 4 12"></polyline>', stroke="2")
ICON_NO_ENTRY = icon('<circle cx="12" cy="12" r="10"></circle>'
                     '<line x1="4.9" y1="4.9" x2="19.1" y2="19.1"></line>', size=15, stroke="2")


def copy_controls(payload):
    """Two buttons, one payload, carried in an attribute.

    `esc` gives `quote=True`, which is what makes the value safe in an attribute;
    newlines are then encoded so the opening tag stays on one line. Both are
    escaping, not structure, so escape-first is untouched -- and the handler only
    ever moves this string to the clipboard, never evaluates it.

    Both icons ship on every button and CSS shows one, the same bargain
    `dismiss_control` strikes with its two captions: the script says *which state*
    the button is in and never has to know what that state looks like.
    """
    def attr(text):
        return esc(text).replace("\n", "&#10;")

    def button(text, idle, payload_text):
        return (
            '<button type="button" class="copy-btn" data-copy="{copy}">'
            '<span class="icon-idle">{idle}</span><span class="icon-done">{done}</span>'
            '<span class="copy-label">{text}</span></button>'
        ).format(copy=attr(payload_text), idle=idle, done=ICON_CHECK, text=text)

    return '<div class="copy">{}{}</div>'.format(
        button("Copy", ICON_CLIPBOARD, payload),
        button("Copy for agent", ICON_TERMINAL, payload + "\n\n" + PROMPT_WRAPPER),
    )


def dismiss_control():
    """The reader's `I have dealt with this` mark.

    It sits in the card foot, not the header: you know a finding is dealt with once
    you have read to the end of it, and the header already carries an id, a title
    and up to three tags. Both captions ship and CSS shows one, so the control
    reads correctly in either state without the script rewriting text.

    Carries no id of its own -- the handler reads the card's, which is already
    there. `aria-pressed` because this is a toggle and says so.
    """
    return (
        '<button type="button" class="dismiss" aria-pressed="false">'
        '<span class="dm-open">&#10003; Mark dealt with</span>'
        '<span class="dm-done">&#10003; Dealt with &middot; undo</span>'
        "</button>"
    )


def render_finding(finding, markdown, partners):
    finding_id = finding["id"]
    # `data-severity` only where there is one. Its absence is what the severity
    # filter reads to leave a card alone: a quality finding is not rated on this
    # axis and a security note is not either, and both stay on screen whatever the
    # filter says. An empty attribute would be a rating of "".
    bits = ['<article class="finding" id="finding-{}" data-producer="{}" data-disposition="{}"{}>'.format(
        esc(finding_id), esc(finding["producer"]), esc(finding["disposition"]),
        ' data-severity="{}"'.format(esc(finding["severity"])) if finding.get("severity") else "",
    )]

    # One pill per card at most, and it is the severity: it is the only one of
    # these that says how loud the finding is. A category names a kind, a producer
    # names a pass and a confidence qualifies the claim -- none of them is
    # competing with the title, so none of them is drawn like something that is.
    axis = ""
    if finding.get("severity"):
        axis = '<span class="tag tag-sev tag-{0}">{0}</span>'.format(esc(finding["severity"]))
    elif finding.get("category"):
        axis = '<span class="meta-label" title="{}">{}</span>'.format(
            esc(CATEGORY_LABEL.get(finding["category"], finding["category"])), esc(finding["category"])
        )

    confidence = ""
    if finding.get("confidence") in ("medium", "low"):
        confidence = '<span class="meta-label meta-conf" title="{}">{} confidence</span>'.format(
            esc(finding.get("confidence_rationale") or ""), esc(finding["confidence"])
        )

    # A label, not a strike or a fold: the finding keeps its place, its
    # disposition and its force, and the label points at the dispute rendered
    # below the body. Naming the check rather than a judgment -- "contested",
    # never "suspicious" -- because the check is the wrong party often enough
    # that the badge must not say who loses.
    contested = ""
    if finding.get("contested_md"):
        contested = '<span class="meta-label meta-contested">contested</span>'

    # The metadata reads above the title rather than under it. Under it, the eye
    # left the title, crossed a row of tags and arrived at the claim -- so the two
    # sentences that carry the finding were separated by the labels that describe
    # it. Above it, the labels are what you skim past on the way in.
    bits.append(
        '<header class="finding-head">'
        '<div class="meta"><span class="fid">{fid}</span>'
        '<span class="meta-label">{prod}</span>{axis}{conf}{contested}</div>'
        "<h3>{title}</h3>"
        "</header>".format(
            fid=esc(finding_id),
            title=markdown.inline(finding["title"], self_id=finding_id),
            prod=producer_label(finding["producer"]),
            axis=axis,
            conf=confidence,
            contested=contested,
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
    # After the body, not before it: the card argues first and the dispute
    # reads as the reply it is. The finding's own force is untouched -- the
    # contest is the one voice on the card that is not the pass's, and the
    # reader adjudicates with both in view.
    if finding.get("contested_md"):
        bits.append(
            '<div class="contested"><p class="contested-label">Contested by the falsification check '
            "&mdash; the diff-only check disputes this claim:</p>{}</div>".format(
                markdown.render(finding["contested_md"], self_id=finding_id)
            )
        )
    # One strip, not two. The copy controls were already across the foot when
    # dismissal arrived, and a second full-width row would cost every card --
    # dismissed or not -- a second line of vertical space to say one word.
    bits.append(
        '<div class="card-foot">{copy}{dismiss}</div>'.format(
            copy=copy_controls(copy_payload(finding, partners)), dismiss=dismiss_control()
        )
    )
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


# What the run asked each pass to run on. Rendered as one labelled value per
# field and never fused into a phrase: "opus at max effort" is a model name a
# run can record with no effort beside it, and a page that renders that
# identically to a model "opus" at effort "max" has destroyed the difference
# between an effort nobody recorded and one that was. Absent from every
# artifact written before these fields existed, and from any run whose host
# does not let the orchestrator choose, so nothing here treats them as
# required.
PROVENANCE = (("requested_model", "Model", "model"), ("requested_effort", "Effort", "effort"))


def provenance_value(value):
    return esc(value) if value else '<span class="muted">not recorded</span>'


def provenance_state(passes):
    """What the passes recorded about model and effort, as three answers.

    Read by the sidebar, which lists the fields, and by the masthead, which warns
    about them. `differ` is not the negation of `agreed`, and it is asked one field
    at a time: a split is two passes naming *different* models, or different
    efforts. One pass recording an axis the other left blank is unequal evidence,
    not evidence of inequality, and the page cannot claim a split it did not
    observe.
    """
    tiers = [(e.get("requested_model"), e.get("requested_effort")) for e in passes]
    recorded = any(model or effort for model, effort in tiers)
    agreed = len(set(tiers)) == 1
    differ = any(len({e.get(field) for e in passes if e.get(field)}) > 1 for field, _, _ in PROVENANCE)
    return recorded, agreed, differ


def render_run_panel(run, passes):
    """The run's facts, in the sidebar rather than in the reading column.

    They are what the run was pointed at, not something a pass found, and between
    the verdict sentence and the first finding they were a page of reference
    material the reader had to scroll past to reach the report. Reference material
    belongs beside the report, where it stays available and stops interrupting.

    Two columns for the short fields, full width for the ones that must not wrap
    mid-value -- a repository path, a 40-character object id, a timestamp.
    """
    scope = run["scope"]
    # (label, value, full-width). Order is the reading order of the panel, and the
    # pairs fall on their own rows because each wide row closes the one above it.
    rows = [
        ("Repository", esc(scope["repo"]), True),
        ("Scope mode", esc(SCOPE_MODE_LABEL.get(scope["mode"], scope["mode"])), False),
        ("Files changed", esc(scope["files_changed"]), False),
        ("Base", '<span class="chip">{}</span>'.format(esc(scope["base"])), True),
    ]
    if scope.get("head"):
        rows.append(("Head", '<span class="chip">{}</span>'.format(esc(scope["head"])), True))
    rows.append(("Diff size", "{:,} bytes".format(scope["diff_bytes"]), False))
    rows.append(("Passes", esc(run["mode"]), False))
    # Only when the run recorded it: an artifact from before the check existed
    # has nothing to say here, and "not recorded" would read as an omission by
    # a run that never had the field to fill in.
    if run.get("falsification"):
        rows.append(("Falsification", esc(run["falsification"]), False))
    if run.get("docs_check"):
        rows.append(("Docs check", esc(run["docs_check"]), False))

    recorded, agreed, _ = provenance_state(passes)
    if recorded and agreed:
        for field, label, _ in PROVENANCE:
            rows.append((label, provenance_value(passes[0].get(field)), False))
    elif recorded:
        # Every pass gets its rows once any pass has them, including the passes
        # that recorded nothing. Listing only what is known would read as the
        # complete account of the run, and a reader comparing two passes has to
        # be able to see that the second one is missing rather than equal. Full
        # width, because the label is now a pass name and a field name.
        for envelope in passes:
            for field, _, name in PROVENANCE:
                rows.append(
                    (
                        "{} &middot; {}".format(producer_label(envelope["producer"]), name),
                        provenance_value(envelope.get(field)),
                        True,
                    )
                )
    rows.append(("Generated", '<span class="nowrap">{}</span>'.format(esc(run["generated_at"])), True))

    body = "".join(
        '<div class="kv{wide}"><dt>{k}</dt><dd>{v}</dd></div>'.format(
            wide=" kv-wide" if wide else "", k=label, v=value
        )
        for label, value, wide in rows
    )
    note = (
        "A finding may cite a file the diff never touched &mdash; or one a remedy proposes and nothing "
        "has written yet."
    )
    if recorded:
        note += " Model and effort are what this run asked for, not a measurement."
    return (
        '<section class="run"><p class="run-title">Run</p><dl class="run-facts">{}</dl>'
        '<p class="run-note">{}</p></section>'.format(body, note)
    )


def render_warnings(run, passes, findings):
    """What the reader has to know before believing the report, in the masthead.

    These went to the sidebar's neighbours in the old scope section, under the
    facts they qualify, which put the one thing on the page that reduces what the
    findings are worth below the fold. A warning is not reference material.
    """
    scope = run["scope"]
    warnings = []
    if scope.get("untracked"):
        warnings.append(
            "{} file(s) in this working tree are untracked and were not reviewed. Git can only "
            "diff what it has been told about.".format(scope["untracked"])
        )
    if run["mode"] == "sequential":
        warnings.append(
            "Both rubrics ran in one context window rather than side by side, and the second pass "
            "reviewed with whatever window the first left it. A sequential run is the weaker run."
        )
    # Both falsification warnings qualify findings the reader is about to
    # believe. With none on the page there is nothing a missed contradiction
    # could leave standing, and the callout would alarm over a check that had
    # no subject -- the run facts still record the state, neutrally.
    if findings and run.get("falsification") == "skipped":
        warnings.append(
            "The falsification check was skipped: nothing tried to disprove these findings against "
            "the diff, so a finding the diff contradicts would still be standing."
        )
    if findings and run.get("falsification") == "failed":
        warnings.append(
            "The falsification check ran but its reply could not be read, so every finding was kept "
            "unexamined &mdash; a finding the diff contradicts would still be standing."
        )
    if provenance_state(passes)[2]:
        warnings.append(
            "The passes were not asked for the same model and effort. Corroboration between them "
            "carries less than it appears to: two passes reaching one defect is evidence because "
            "they were peers."
        )
    return "".join(
        '<p class="callout"><span class="callout-mark" aria-hidden="true">!</span>'
        "<span>{}</span></p>".format(text)
        for text in warnings
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


def render_withdrawn(withdrawn, markdown):
    """The findings the falsification check withdrew, kept on the page.

    Marked, never deleted, is the artifact's rule, and this is the page keeping
    it: a report that silently lost a finding would be lying about what the
    passes wrote. The cards render without controls, filters or nav entries --
    a withdrawn finding is the record of a disagreement the diff settled, not
    something to act on -- but each keeps its anchor, because a standing body
    may still cite it and the link has to land somewhere.
    """
    if not withdrawn:
        return ""
    cards = []
    for finding in withdrawn:
        finding_id = finding["id"]
        chips = "".join(
            location_chip(location, index == 0) for index, location in enumerate(finding.get("locations") or [])
        )
        cards.append(
            '<article class="finding withdrawn" id="finding-{fid}">'
            '<header class="finding-head">'
            '<div class="meta"><span class="fid">{fid}</span>'
            '<span class="meta-label">{prod}</span>'
            '<span class="meta-label">withdrawn</span></div>'
            "<h3>{title}</h3>"
            "</header>"
            '<div class="locations">{chips}</div>'
            '<div class="body">{body}</div>'
            "</article>".format(
                fid=esc(finding_id),
                prod=producer_label(finding["producer"]),
                title=markdown.inline(finding["title"], self_id=finding_id),
                chips=chips,
                body=markdown.render(finding["body_md"], self_id=finding_id),
            )
        )
    note = (
        "Withdrawn at the merge: a falsification check that read only the diff found it directly "
        "contradicts each key claim below. Kept for the record &mdash; a withdrawn finding blocks "
        "nothing and corroborates nothing."
    )
    return (
        '<section class="withdrawn-group"><h2 id="group-withdrawn">Withdrawn at merge '
        '<span class="counts">&middot; {count}</span></h2>'
        '<p class="withdrawn-note">{note}</p>\n{cards}</section>'.format(
            count=len(withdrawn), note=note, cards="\n".join(cards)
        )
    )


DOC_NOTE_KIND_LABEL = {"stale": "stale claim", "missing": "missing coverage"}


def render_docs_check(docs_check, markdown):
    """The docs check's record: what was read, and any conflict it reported.

    Advisory, and drawn that way: a doc note is not a finding -- no id, no
    disposition, no controls, no filter reaches it -- so the cards are quiet
    and the section sits after the passes' prose. What the section must not be
    quiet about is coverage: it names exactly what was read and what the
    collector refused, because "nothing flagged" over an unstated set is a
    reassurance nobody can check -- and even over a stated set it only means
    no explicit contradiction, which the standing note says in so many words.
    """
    if docs_check is None:
        return ""
    examined = docs_check.get("examined") or []
    notes = docs_check.get("notes") or []
    skipped = docs_check.get("skipped") or []

    if not examined:
        # Two different empties. Candidates the collector refused are not
        # documents that do not exist, and claiming "nothing applies" above a
        # list of refusals would be the section contradicting itself.
        coverage = (
            "No agent-facing documents were read &mdash; every candidate was refused at collection."
            if skipped
            else "No agent-facing documents to check &mdash; nothing shaped like an AGENTS.md, CLAUDE.md or README applies to this diff."
        )
    else:
        chips = ", ".join('<span class="chip">{}</span>'.format(esc(p)) for p in examined)
        told = "The diff was read against {}.".format(chips)
        flagged = {note["path"] for note in notes}
        if flagged:
            told += " {} of these documents {} an explicit conflict.".format(
                len(flagged), "carries" if len(flagged) == 1 else "carry"
            )
        else:
            told += " None of them states anything the diff explicitly contradicts."
        coverage = told
    if skipped:
        coverage += " Not read: {}.".format(
            "; ".join("<span class=\"chip\">{}</span> ({})".format(esc(s["path"]), esc(s["reason"])) for s in skipped)
        )

    cards = []
    for note in notes:
        body = []
        if note.get("claim_md"):
            body.append('<blockquote class="doc-claim">{}</blockquote>'.format(markdown.render(note["claim_md"])))
        body.append(markdown.render(note["why_md"]))
        if note.get("owed_md"):
            body.append('<p class="doc-owed-label">Edit owed</p>{}'.format(markdown.render(note["owed_md"])))
        cards.append(
            '<article class="finding doc-note">'
            '<header class="finding-head">'
            '<div class="meta"><span class="chip chip-primary">{path}</span>'
            '<span class="meta-label">{kind}</span></div>'
            "</header>"
            '<div class="body">{body}</div>'
            "</article>".format(
                path=esc(note["path"]),
                kind=esc(DOC_NOTE_KIND_LABEL.get(note["kind"], note["kind"])),
                body="".join(body),
            )
        )

    disclaimer = (
        "An advisory check, outside the verdict: it reads the named documents against the diff for "
        "explicit contradiction only, so a clean result does not promise the documents are current "
        "&mdash; drift a change merely implies is beyond it."
    )
    return (
        '<section class="docscheck"><h2 id="docscheck">Documentation '
        '<span class="counts">&middot; {count}</span></h2>'
        '<p class="docs-note">{disclaimer}</p>'
        '<p class="docs-coverage">{coverage}</p>\n{cards}</section>'.format(
            count=len(notes), disclaimer=disclaimer, coverage=coverage, cards="\n".join(cards)
        )
    )


def render_self_check(self_check, markdown):
    """The reader's self-check, last on the page.

    Each answer sits collapsed under its question in a native <details>, so the
    reader meets the question before the answer -- collapsing is CSS-free and
    script-free, exactly the page's idiom. The anchors render as the same live
    cross-references a body uses, because the whole worth of an answer here is
    that the reader can open the finding it rests on -- and the validator
    guarantees each one stands, so every link lands on a card the reader is
    being asked to act on.

    Deliberately not a gate, and the note says so on the page: nothing is
    scored, recorded, or consulted by anything else here. It renders after the
    prose and outside the filters -- a question about the report is not a
    finding, and no data- attribute puts it in any filter's reach.
    """
    if not self_check:
        return ""
    items = []
    for entry in self_check:
        anchors = ", ".join(
            '<a class="xref" href="#finding-{0}">{0}</a>'.format(esc(anchor)) for anchor in entry["anchors"]
        )
        items.append(
            '<details class="sc-item"><summary>{question}</summary>'
            '<div class="sc-answer">{answer}</div>'
            '<p class="sc-anchors">Grounded in {anchors}</p></details>'.format(
                question=markdown.inline(entry["question"]),
                answer=markdown.render(entry["answer_md"]),
                anchors=anchors,
            )
        )
    note = (
        "A self-check, not a gate: nothing scores or records what you decide. Settle each answer in "
        "your head, then open it &mdash; every one can be checked against this page."
    )
    return (
        '<section class="selfcheck"><h2 id="selfcheck">Self-check '
        '<span class="counts">&middot; {count}</span></h2>'
        '<p class="sc-note">{note}</p>\n{items}</section>'.format(
            count=len(self_check), note=note, items="\n".join(items)
        )
    )


def render_page(merged):
    findings = merged["findings"]
    # Standing findings drive everything the reader acts on -- ordering, counts,
    # nav, the verdict sentence. Withdrawn ones render once, in their own
    # section, and appear nowhere else: a finding the diff contradicts is a
    # record, not a lead. The markdown cross-referencer still knows every id,
    # withdrawn included, because a standing body may cite a withdrawn finding
    # and the link must land on its card rather than dangle.
    live = [f for f in findings if f.get("falsified") is not True]
    withdrawn = [f for f in findings if f.get("falsified") is True]
    markdown = Markdown({f["id"] for f in findings})
    by_id = {f["id"]: f for f in live}
    units = ordered_units(live)

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
            '<li class="nav-finding" id="nav-{fid}" data-producer="{prod}"{sev}><a href="#finding-{fid}">'
            '<span class="nav-id">{fid}</span> {title}</a></li>'.format(
                prod=esc(f["producer"]),
                fid=esc(f["id"]),
                sev=' data-severity="{}"'.format(esc(f["severity"])) if f.get("severity") else "",
                title=esc(truncate(Markdown.plain(f["title"]))),
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
    if withdrawn:
        prose_links.append('<li><a href="#group-withdrawn">Withdrawn at merge &middot; {}</a></li>'.format(len(withdrawn)))
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

    docs_check = merged.get("docs_check")
    if docs_check is not None:
        prose_links.append(
            '<li><a href="#docscheck">Documentation &middot; {}</a></li>'.format(len(docs_check.get("notes") or []))
        )

    self_check = merged.get("self_check") or []
    if self_check:
        prose_links.append('<li><a href="#selfcheck">Self-check &middot; {}</a></li>'.format(len(self_check)))

    verdict = merged["verdict"]
    sentence = verdict_sentence(verdict, live)
    # The masthead owns the one-sentence account of the run, so the withdrawals
    # are said here and not only in their own section -- a reader who never
    # scrolls still learns the passes wrote more than the page is arguing.
    if withdrawn and live:
        sentence += " {} more {} withdrawn at the merge.".format(
            len(withdrawn), "was" if len(withdrawn) == 1 else "were"
        )
    elif withdrawn:
        sentence = (
            "The only finding was withdrawn at the merge; nothing stands."
            if len(withdrawn) == 1
            else "All {} findings were withdrawn at the merge; nothing stands.".format(len(withdrawn))
        )
    # Said in the masthead because it qualifies the sentence just made: some of
    # the findings counted there are disputed, and a reader who never scrolls
    # is owed that before the number settles in.
    contested_count = sum(1 for f in live if f.get("contested_md"))
    if contested_count:
        sentence += " The falsification check contests {} of {}.".format(
            contested_count, "them" if contested_count > 1 else "these"
        )
    # Advisory, so it joins the sentence only when it has something to say: a
    # clean docs check is coverage detail, and the section states it.
    doc_notes = (docs_check.get("notes") or []) if docs_check is not None else []
    flagged_docs = {note["path"] for note in doc_notes}
    if flagged_docs:
        sentence += " The docs check flagged {} document{}.".format(
            len(flagged_docs), "" if len(flagged_docs) == 1 else "s"
        )
    scope = merged["run"]["scope"]
    scope_line = "{} &middot; {} &middot; {} files".format(
        esc(scope["repo"]), esc(SCOPE_MODE_LABEL.get(scope["mode"], scope["mode"])), scope["files_changed"]
    )

    return PAGE.format(
        title=esc("Two-Pass Review — {}".format(scope["repo"])),
        css=CSS,
        verdict=esc(verdict),
        # Only the blocked badge gets one. `Clear` is the absence of a reason to
        # stop, and an icon on it would be a second thing claiming to say so.
        verdict_icon=ICON_NO_ENTRY if verdict == "blocked" else "",
        verdict_label="Blocked" if verdict == "blocked" else "Clear",
        sentence=sentence,
        scope_line=scope_line,
        nav="\n".join(nav),
        prose_links="".join(prose_links),
        warnings=render_warnings(merged["run"], merged["passes"], merged["findings"]),
        run_panel=render_run_panel(merged["run"], merged["passes"]),
        groups="\n".join(main),
        withdrawn=render_withdrawn(withdrawn, markdown),
        prose=render_pass_prose(merged["passes"], markdown),
        docscheck=render_docs_check(docs_check, markdown),
        selfcheck=render_self_check(self_check, markdown),
        script=SCRIPT,
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
<input type="radio" name="sev" id="f-sev-all" class="filter" checked>
<input type="radio" name="sev" id="f-sev-high" class="filter">
<input type="radio" name="sev" id="f-sev-medium" class="filter">
<input type="radio" name="sev" id="f-sev-low" class="filter">
<input type="radio" name="block" id="f-block-all" class="filter" checked>
<input type="radio" name="block" id="f-block-only" class="filter">
<input type="radio" name="dism" id="f-dism-keep" class="filter" checked>
<input type="radio" name="dism" id="f-dism-hide" class="filter">
<div class="layout">
  <aside class="sidebar">
    <div class="sidebar-head">
      <div class="verdict verdict-{verdict}">
        {verdict_icon}<span class="verdict-label">{verdict_label}</span>
      </div>
      <div class="filters">
        <p class="filter-title">Pass</p>
        <label for="f-prod-all" class="pill pill-prod-all">Both</label>
        <label for="f-prod-security" class="pill pill-prod-security">Security</label>
        <label for="f-prod-quality" class="pill pill-prod-quality">Quality</label>
        <p class="filter-title">Severity</p>
        <label for="f-sev-all" class="pill pill-sev-all">All</label>
        <label for="f-sev-high" class="pill pill-sev-high">High</label>
        <label for="f-sev-medium" class="pill pill-sev-medium">Medium</label>
        <label for="f-sev-low" class="pill pill-sev-low">Low</label>
        <p class="sev-note">Quality findings aren&rsquo;t rated by severity, so they stay visible.</p>
        <p class="filter-title">Show</p>
        <label for="f-block-all" class="pill pill-block-all">Everything</label>
        <label for="f-block-only" class="pill pill-block-only">Blocking only</label>
        <p class="dism-link" hidden>
          <label for="f-dism-hide" class="dism-hide">Hide <span class="dism-count">0</span> dismissed</label>
          <label for="f-dism-keep" class="dism-show">Show dismissed (<span class="dism-count">0</span>)</label>
        </p>
      </div>
    </div>
    <nav>
      {nav}
      <ul class="nav-prose">{prose_links}</ul>
    </nav>
    {run_panel}
  </aside>
  <main>
    <header class="masthead">
      <p class="sentence">{sentence}</p>
      <p class="scope-line">{scope_line}</p>
      <p class="disclaimer">This report is machine-written. A finding is a lead until a person has
      verified it against the repository, and a clear verdict means nothing was reported &mdash; never
      that nothing is there.</p>
      {warnings}
    </header>
    {groups}
    {withdrawn}
    {prose}
    {docscheck}
    {selfcheck}
  </main>
</div>
<script>{script}</script>
</body>
</html>
"""

# The page's whole script: the clipboard handler, and dismissal.
#
# Neither one parses, renders or evaluates anything a pass wrote. The copy handler
# moves a string from a data- attribute to the clipboard; dismissal only ever
# toggles a class and writes digits it counted itself. A hostile `body_md`
# reaching either is inert -- escaping still does the security work, exactly as it
# does everywhere else here.
#
# Delegated listeners rather than a handler per control: the page can carry a
# hundred findings, and three hundred listeners to do two things is silly.
#
# execCommand is a deprecated fallback on purpose. It is the one path confirmed by
# hand in Safari over file://, where the async clipboard write is the less certain
# of the two, and it costs eight lines to not be broken there.
SCRIPT = """
(function () {
  function fallback(text) {
    var area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    var copied = false;
    try { copied = document.execCommand('copy'); } catch (error) { copied = false; }
    document.body.removeChild(area);
    return copied;
  }

  // Writes the caption, not the button: the button also holds two icons, and
  // replacing its textContent would delete them. `data-busy` carries which of the
  // two outcomes happened rather than just `busy`, because CSS swaps in the
  // checkmark and a tick beside `Copy failed` would be a lie. Both values are
  // truthy, so the re-entry guard is unchanged.
  function flash(button, message, ok) {
    if (button.dataset.busy) { return; }
    button.dataset.busy = ok ? 'ok' : 'fail';
    var label = button.querySelector('.copy-label') || button;
    var original = label.textContent;
    label.textContent = message;
    setTimeout(function () {
      label.textContent = original;
      delete button.dataset.busy;
    }, 1200);
  }

  document.addEventListener('click', function (event) {
    var button = event.target.closest ? event.target.closest('.copy-btn') : null;
    if (!button) { return; }
    var text = button.getAttribute('data-copy');
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { flash(button, 'Copied', true); },
        function () {
          var copied = fallback(text);
          flash(button, copied ? 'Copied' : 'Copy failed', copied);
        }
      );
    } else {
      var copied = fallback(text);
      flash(button, copied ? 'Copied' : 'Copy failed', copied);
    }
  });

  // Dismissal: a reader's session-scoped mark, meaning `I have dealt with this`.
  // Nothing here is persisted, because a report is a snapshot of one diff and a
  // stale mark against a regenerated one would mislead.
  //
  // Recounted rather than decremented: the run's own number stays on the page
  // beside the reader's progress through it. A heading reads `Blocking · 7` until
  // something in it is dismissed and `Blocking · 3 of 7` after, so what the passes
  // found is never overwritten -- which was the decision when the counts were
  // frozen, and it survives the arrival of a mechanism that could decrement.
  // Which severity the pills are on, and whether a card survives it. `high` takes
  // `critical` with it, as the stylesheet does -- the two rules answer the same
  // question and disagreeing would put a card on screen that the count denies.
  // A card with no rating passes every setting: it was never rated on this axis.
  function activeSeverity() {
    var radio = document.querySelector('input[name="sev"]:checked');
    return radio ? radio.id.replace('f-sev-', '') : 'all';
  }

  function passesSeverity(card, severity) {
    if (severity === 'all') { return true; }
    var own = card.getAttribute('data-severity');
    if (!own) { return true; }
    return own === severity || (severity === 'high' && own === 'critical');
  }

  function retally(disposition) {
    var scope = '.group[data-disposition="' + disposition + '"]';
    var cards = document.querySelectorAll('section' + scope + ' .finding');
    var severity = activeSeverity();
    var tally = {all: [0, 0], security: [0, 0], quality: [0, 0]};
    for (var i = 0; i < cards.length; i++) {
      // A card the severity filter has taken off screen is neither live nor
      // total: under `High`, `3 of 7` would be counting four findings the reader
      // cannot see. The run's own number is what `of 7` protects, and here the
      // reader has narrowed what the run is being shown as.
      if (!passesSeverity(cards[i], severity)) { continue; }
      var live = !cards[i].classList.contains('dismissed');
      var keys = ['all', cards[i].getAttribute('data-producer')];
      for (var k = 0; k < keys.length; k++) {
        var slot = tally[keys[k]];
        if (!slot) { continue; }
        slot[1] += 1;
        if (live) { slot[0] += 1; }
      }
    }
    // Both copies of the count, and both halves of the group: `.group` and
    // data-disposition sit on the <section> and on the sidebar <div> alike.
    //
    // Emptiness is per producer, not just overall, because the producer filter
    // decides which cards are on screen: with `Security` selected, a disposition
    // whose security findings are all dismissed has nothing left to show even
    // though its quality findings are untouched. The script marks all three
    // cases and CSS picks the one the active filter asks for -- which keeps the
    // filter in the sibling selectors it already lives in.
    var boxes = document.querySelectorAll(scope);
    for (var b = 0; b < boxes.length; b++) {
      for (var key in tally) {
        boxes[b].classList.toggle(
          key === 'all' ? 'all-dismissed' : 'all-dismissed-' + key,
          tally[key][1] > 0 && tally[key][0] === 0
        );
        // Nothing rated this way at all, as against everything rated this way
        // dismissed. The two retire the same heading and the reader can undo only
        // one of them, so they are not the same class.
        boxes[b].classList.toggle(
          key === 'all' ? 'sev-empty' : 'sev-empty-' + key, tally[key][1] === 0
        );
        var span = boxes[b].querySelector('.count-' + key);
        if (!span) { continue; }
        span.textContent = tally[key][0] === tally[key][1]
          ? String(tally[key][1])
          : tally[key][0] + ' of ' + tally[key][1];
      }
    }
  }

  // The one control that cannot be a pill: until the reader dismisses something
  // there is nothing to hide, and a filter for an empty set is a lit control for a
  // state that does not exist. So the line is hidden until the first mark and
  // carries the count, which is the only thing here the script writes -- digits it
  // counted itself, into a span, never markup.
  function refreshDismissedLink() {
    var count = document.querySelectorAll('.finding.dismissed').length;
    var line = document.querySelector('.dism-link');
    if (!line) { return; }
    line.hidden = count === 0;
    var spans = line.querySelectorAll('.dism-count');
    for (var i = 0; i < spans.length; i++) { spans[i].textContent = String(count); }
  }

  document.addEventListener('click', function (event) {
    var button = event.target.closest ? event.target.closest('.dismiss') : null;
    if (!button) { return; }
    var card = button.closest('.finding');
    if (!card) { return; }
    var dismissed = card.classList.toggle('dismissed');
    button.setAttribute('aria-pressed', dismissed ? 'true' : 'false');
    // The nav entry carries the card's id under a different prefix, which is the
    // only thing the two subtrees need to know about each other.
    var entry = document.getElementById('nav-' + card.id.replace('finding-', ''));
    if (entry) { entry.classList.toggle('dismissed', dismissed); }
    retally(card.getAttribute('data-disposition'));
    refreshDismissedLink();
  });

  // The severity pills hide cards in CSS, like the two filters beside them, but a
  // heading counting cards the reader cannot see is a heading that is wrong. So
  // the one thing the script does here is count again: every disposition, because
  // the filter is global, and only on change, because nothing else moves it.
  var severityRadios = document.querySelectorAll('input[name="sev"]');
  for (var s = 0; s < severityRadios.length; s++) {
    severityRadios[s].addEventListener('change', function () {
      var sections = document.querySelectorAll('section.group[data-disposition]');
      for (var i = 0; i < sections.length; i++) {
        retally(sections[i].getAttribute('data-disposition'));
      }
    });
  }
})();
"""

CSS = """
:root {
  --bg: #fbfaf8; --panel: #ffffff; --ink: #1c1a18; --ink-2: #4d4843; --muted: #6b6560;
  --line: #e2ddd6;
  --accent: #7a4b2a; --block: #a32a1e; --block-bg: #fbeae7; --block-edge: #f0cdc6;
  --follow: #7a5c12; --note: #5a6570;
  --code-bg: #f4f1ec; --chip-bg: #efeae2; --shadow: 0 1px 2px rgba(28,26,24,.06);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #17161a; --panel: #1e1d22; --ink: #e9e6e1; --ink-2: #c2bcb5; --muted: #a09a94;
    --line: #34323a;
    --accent: #d59a6c; --block: #f08b7e; --block-bg: #3a201d; --block-edge: #5e332d;
    --follow: #d6b45c; --note: #96a1ae;
    --code-bg: #26252b; --chip-bg: #2b2a31; --shadow: none;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
/* `fixed`, not `absolute`. Clicking a <label> focuses the control it names, and
   the browser scrolls that control into view -- and an absolutely-positioned
   element with auto offsets sits at its static position, which is the top of
   <body>. A reader thirty cards down was thrown back to the masthead on every
   filter change. A fixed element is in the viewport by definition, so scrolling
   it into view is a no-op. It stays just as inert either way. */
.filter { position: fixed; top: 0; left: 0; width: 0; height: 0;
  opacity: 0; pointer-events: none; }
.layout { display: grid; grid-template-columns: 300px minmax(0, 1fr); gap: 40px;
  max-width: 1240px; margin: 0 auto; padding: 32px 28px 96px; align-items: start; }
.sidebar { position: sticky; top: 24px; max-height: calc(100vh - 48px); overflow-y: auto; font-size: 14px; }
/* The sidebar is sticky against the page *and* a scroll container of its own, so
   its first children -- the verdict badge and the filter pills -- are the first
   things out of it once the nav overflows. Sticky again one level in keeps them:
   the nav scrolls underneath a head that stays. The 22px gap is padding here
   rather than margin on `.filters` because a margin collapses out of the sticky
   box and leaves a transparent strip for nav entries to slide through. */
.sidebar-head { position: sticky; top: 0; z-index: 1; background: var(--bg); padding-bottom: 22px; }
main { min-width: 0; }

/* The badge renders once, in the sidebar, and there is no .masthead .verdict
   override because there is no masthead badge to resize. It used to render in
   both: the two sat about 200px apart on the same horizontal line and read as
   the same word printed twice, so the masthead's was deleted. This is the copy
   that stayed because it is the one that survives scrolling while `.sidebar` is
   sticky -- see `.sidebar-head` above -- so on the desktop layout the verdict is
   on every screen and not just the first. Below 900px it is not: the sidebar
   goes static and the badge scrolls away with it, because a single-column layout
   has no second column to persist anything in. */
.verdict { display: flex; align-items: center; gap: 7px; border-radius: 8px; padding: 10px 14px;
  margin-bottom: 18px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; font-size: 13px; }
.verdict .icon { flex-shrink: 0; }
.verdict-blocked { background: var(--block-bg); color: var(--block); border: 1px solid var(--block); }
.verdict-clear { background: var(--chip-bg); color: var(--ink); border: 1px solid var(--line); }
/* .sentence keeps its 8px top margin now that it leads the masthead. It is what
   lands the sentence level with the sidebar badge, so the two read as one unit
   and the masthead loses no signal. */
.masthead { margin-bottom: 34px; }
.sentence { font-size: 21px; line-height: 1.45; margin: 8px 0 6px; }
.scope-line { color: var(--muted); margin: 0; font-size: 14px; }

/* No margin: the gap under the filters is the head's padding now, see above. */
.filters { margin-bottom: 0; }
.filter-title { margin: 12px 0 6px; font-size: 11px; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }
.pill { display: inline-block; padding: 3px 10px; margin: 0 4px 4px 0; border: 1px solid var(--line);
  border-radius: 999px; cursor: pointer; font-size: 13px; background: var(--panel); }
#f-prod-all:checked ~ .layout .pill-prod-all,
#f-prod-security:checked ~ .layout .pill-prod-security,
#f-prod-quality:checked ~ .layout .pill-prod-quality,
#f-sev-all:checked ~ .layout .pill-sev-all,
#f-sev-high:checked ~ .layout .pill-sev-high,
#f-sev-medium:checked ~ .layout .pill-sev-medium,
#f-sev-low:checked ~ .layout .pill-sev-low,
#f-block-all:checked ~ .layout .pill-block-all,
#f-block-only:checked ~ .layout .pill-block-only { background: var(--ink); color: var(--bg); border-color: var(--ink); }

/* Said only while it is true, because the reader has to be able to trust that a
   filtered page is filtered: a quality finding on screen under `High` is not the
   filter leaking, it is a finding the rubric never rated. */
.sev-note { display: none; margin: 6px 0 0; font-size: 11.5px; line-height: 1.4; color: var(--muted); }
#f-sev-high:checked ~ .layout .sev-note,
#f-sev-medium:checked ~ .layout .sev-note,
#f-sev-low:checked ~ .layout .sev-note { display: block; }

/* Not a filter pair, because for most of a report's life there is nothing to
   filter: the reader has dismissed nothing, and two pills saying so were two
   permanently-lit controls for a state that does not exist yet. The line stays
   hidden until the first mark, and the script writes only the count -- both
   captions are labels for the two radios that were already here, so the state is
   still a radio and the swap is still CSS. */
.dism-link { margin: 12px 0 0; font-size: 12.5px; }
.dism-link label { color: var(--muted); cursor: pointer;
  text-decoration: underline; text-decoration-style: dotted; }
.dism-link label:hover { color: var(--ink); }
.dism-show { display: none; }
#f-dism-hide:checked ~ .layout .dism-hide { display: none; }
#f-dism-hide:checked ~ .layout .dism-show { display: inline; }

nav .nav-group { margin: 16px 0 6px; font-size: 11px; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }
/* The first group's top margin used to collapse into the filters' bottom margin
   and disappear. Padding does not collapse, so without this the gap would be
   22 + 16 and the unscrolled page would no longer match what it was. The rule
   reaches one level further in than it used to: the `Scope` link was the nav's
   own first child, and with it gone the first thing in there is a group block. */
nav > div:first-child .nav-group { margin-top: 0; }
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

/* Severity, on the bare attribute so a card and its nav entry obey one rule, and
   so anything without a rating is never matched: a quality finding has a category
   instead, and a security note has neither, and both belong on screen at every
   setting.
   `High` takes `critical` with it. The rubric can write either, the pills the
   design settled on are four, and the two already share a rank in SEVERITY_RANK
   -- so a critical finding hides from every pill but `All` unless High claims it,
   which is the one outcome a severity filter must never produce. */
#f-sev-high:checked ~ .layout [data-severity]:not([data-severity="high"]):not([data-severity="critical"]),
#f-sev-medium:checked ~ .layout [data-severity]:not([data-severity="medium"]),
#f-sev-low:checked ~ .layout [data-severity]:not([data-severity="low"]),
/* Set by the script, which is the only party that knows what the filter left:
   `sev-empty` means this disposition has nothing on screen at the current
   setting, so it retires its heading and its nav block like `all-dismissed`
   does. Not scoped to a radio -- it is recomputed every time the filter moves,
   so it is only ever set while it is true. */
.group.sev-empty,
#f-prod-security:checked ~ .layout .group.sev-empty-security,
#f-prod-quality:checked ~ .layout .group.sev-empty-quality { display: none; }

h2 { font-size: 13px; letter-spacing: .1em; text-transform: uppercase; color: var(--muted);
  border-bottom: 1px solid var(--line); padding-bottom: 8px; margin: 42px 0 18px; }
.counts { font-variant-numeric: tabular-nums; }

/* The run's facts, under the nav rather than above the findings. Two columns for
   the short ones; `.kv-wide` spans both for the values that must not be cut in
   half -- and a full 40-character SHA must not break mid-hash either, because
   half an object id on each of two lines is not a thing anyone can copy. */
.run { margin-top: 26px; padding-top: 16px; border-top: 1px solid var(--line); }
.run-title { margin: 0 0 10px; font-size: 11px; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }
.run-facts { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 14px; margin: 0; }
.kv-wide { grid-column: 1 / -1; }
.kv dt { font-size: 10.5px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin: 0; }
.kv dd { margin: 1px 0 0; font-size: 13px; overflow-wrap: anywhere; }
.run-facts .chip { font-size: 11px; line-height: 1.55; overflow-wrap: normal; word-break: keep-all; }
.run-note { margin: 12px 0 0; color: var(--muted); font-size: 12px; line-height: 1.5; }
.muted { color: var(--muted); font-size: 14px; }
.nowrap { white-space: nowrap; }
.finding-head h3 code { font-size: .92em; background: var(--code-bg); padding: 1px 5px; border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-weight: 600; }

/* The disclaimer is on every page and the warnings are not, so it stays quiet --
   muted like the scope line, never tinted. Tint it and the run-specific warnings
   below it stop standing out, which is the one job they have. */
.disclaimer { color: var(--muted); margin: 10px 0 0; font-size: 13px; line-height: 1.5; }

/* A warning is the one thing on the page that reduces what the findings are
   worth, so it sits in the masthead, tinted, above everything it qualifies. */
.callout { display: flex; gap: 9px; margin: 16px 0 0; padding: 9px 12px; background: var(--block-bg);
  border: 1px solid var(--block-edge); border-radius: 8px; color: var(--block);
  font-size: 13.5px; line-height: 1.5; }
.callout-mark { font-weight: 700; }

/* The left edge carries the disposition, so a card says which of the three lists
   it is in without the reader having to find the heading it is under -- which is
   what a deep-linked or filtered page costs otherwise. */
.finding { background: var(--panel); border: 1px solid var(--line); border-left: 3px solid var(--note);
  border-radius: 10px; padding: 18px 22px 15px; margin: 0 0 14px; box-shadow: var(--shadow); }
.finding[data-disposition="blocking"] { border-left-color: var(--block); }
.finding[data-disposition="follow-up"] { border-left-color: var(--follow); }
.meta { display: flex; align-items: baseline; flex-wrap: wrap; gap: 10px; margin-bottom: 7px; }
.fid { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
  letter-spacing: .04em; font-weight: 700; color: var(--note); }
[data-disposition="blocking"] .fid { color: var(--block); }
[data-disposition="follow-up"] .fid { color: var(--follow); }
/* `text-wrap: pretty` where it is supported, and nothing where it is not: a
   widowed last word is the failure it prevents, which is a wobble and not a bug. */
.finding-head h3 { margin: 0; font-size: 19px; line-height: 1.3; font-weight: 600;
  letter-spacing: -.005em; text-wrap: pretty; }
.meta-label { font-size: 11px; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }
/* Dashed underline rather than a dashed pill: the border said `there is more
   here` in the same shape the severity uses to say `this is how loud it is`. */
.meta-conf { border-bottom: 1px dashed var(--line); }
.tag { font-size: 11px; letter-spacing: .04em; text-transform: uppercase; padding: 2px 8px;
  border-radius: 999px; border: 1px solid var(--line); color: var(--muted); font-weight: 600; }
.tag-critical, .tag-high { color: var(--block); border-color: var(--block); }
.tag-medium { color: var(--follow); border-color: var(--follow); }

.locations { margin: 11px 0 0; display: flex; flex-wrap: wrap; gap: 6px; }
/* Inert on purpose: a chip that looks like a link and is not one is a lie. */
.chip { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px;
  background: var(--chip-bg); border: 1px solid var(--line); border-radius: 5px; padding: 2px 7px;
  user-select: all; -webkit-user-select: all; text-decoration: none; cursor: text; }
.chip-primary { border-color: var(--accent); color: var(--accent); }

/* Quiet until wanted: a report is for reading, and three controls per finding
   shouting for attention would compete with the finding itself. The foot keeps
   its rule and its spacing when the card folds -- the stub is a struck title over
   a line over its own undo, which is why the mark is reversible where it was
   made and not from a list somewhere else. */
.card-foot { display: flex; align-items: center; flex-wrap: wrap; gap: 8px;
  margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line); }
.copy { display: flex; gap: 8px; }
.copy-btn { display: flex; align-items: center; gap: 6px; font: inherit; font-size: 12.5px;
  color: var(--muted); background: var(--panel); border: 1px solid var(--line); border-radius: 5px;
  padding: 4px 10px; cursor: pointer; }
.copy-btn:hover { color: var(--ink); border-color: var(--accent); }
.copy-btn[data-busy] { color: var(--accent); border-color: var(--accent); }
/* Both icons ship; this picks. `ok` only -- the check is a claim that the text
   is on the clipboard, and on the failure path it is not. */
.copy-btn .icon-idle, .copy-btn .icon-done { display: flex; }
.copy-btn .icon-done, .copy-btn[data-busy="ok"] .icon-idle { display: none; }
.copy-btn[data-busy="ok"] .icon-done { display: flex; }

/* Always visible, never hover-revealed: this page is printed and forwarded by
   email, where there is no hover. Borderless until wanted, so the mark reads as
   quieter than the two buttons it shares the strip with. */
.dismiss { margin-left: auto; font: inherit; font-size: 12.5px; color: var(--muted);
  background: var(--panel); border: 1px solid transparent; border-radius: 5px;
  padding: 4px 10px; cursor: pointer; }
.dismiss:hover { color: var(--ink); border-color: var(--accent); }

/* Dismissal is a class, set by the script on the card and on its sidebar entry.
   The mark reaches the nav because a struck card under an unstruck nav entry
   leaves the sidebar advertising findings the reader has dealt with -- and
   reaching a second subtree is exactly what the CSS-only design had to buy with
   a checkbox per finding at the root of the document. Nothing here is generated
   per finding. */
/* Everything in the meta line except the id: a folded card is its id and its
   struck title, and the labels that qualified the finding have nothing left to
   qualify. */
.finding.dismissed .meta > :not(.fid),
.finding.dismissed .locations,
.finding.dismissed .corroboration,
.finding.dismissed .contested,
.finding.dismissed .body,
.finding.dismissed .copy,
.finding.dismissed .dm-open,
.dm-done { display: none; }
.finding.dismissed .dm-done { display: inline; }
/* The edge fades with the card. It says which list the finding is in, and a
   dismissed finding is not one the reader is still being pointed at. Later in
   the sheet than the three disposition colours, which is what makes it win. */
.finding.dismissed { border-left-color: var(--line); }
/* The nav entry is struck on the <li> rather than the <a>, so the more specific
   `nav a:hover` adds its underline instead of replacing the line through. */
.finding.dismissed .finding-head h3,
.nav-finding.dismissed { text-decoration: line-through; opacity: .45; }
/* `Hide dismissed` stays a radio and a sibling selector, like the two filters
   above it: the script owns the state, CSS filters on it. `.all-dismissed` is
   set on the <section> and on the nav <div> together, so a disposition nobody
   has anything left to read retires its heading and its sidebar block at once.
   The two producer variants are what makes that true under the `Pass` filter as
   well: with `Security` selected, a disposition whose security findings are all
   dismissed is empty on screen whatever its quality findings are doing. */
#f-dism-hide:checked ~ .layout .dismissed,
#f-dism-hide:checked ~ .layout .group.all-dismissed,
#f-prod-security:checked ~ #f-dism-hide:checked ~ .layout .group.all-dismissed-security,
#f-prod-quality:checked ~ #f-dism-hide:checked ~ .layout .group.all-dismissed-quality { display: none; }

/* Withdrawn cards are quieter than everything above them and immune to the
   filters -- no data- attributes, so no selector reaches them. Not struck
   through: the strike is the reader's dealt-with mark, and borrowing it would
   claim these were dealt with rather than disproved. */
.finding.withdrawn { opacity: .7; border-left-color: var(--line); }
.withdrawn-note { color: var(--muted); font-size: 13px; line-height: 1.5; margin: -8px 0 16px; }

/* A rule, not a filled block. The banner is one line of provenance about the
   finding above it, and a tinted panel gave it the weight of a second finding. */
.corroboration { border-left: 2px solid var(--accent); padding-left: 11px; margin: 13px 0 0;
  font-size: 13.5px; line-height: 1.5; color: var(--muted); }
.corroboration strong { color: var(--accent); font-weight: 600; }

/* The contest reads like the corroboration banner -- a voice about the
   finding, not a second finding -- in the follow-up amber, which is the
   page's colour for `attend to this, it does not block`. Never the blocking
   red: red would say the finding lost, and the check is wrong often enough
   that the card must not pre-judge the winner. */
.contested { border-left: 2px solid var(--follow); padding-left: 11px; margin: 13px 0 0;
  font-size: 13.5px; line-height: 1.5; color: var(--muted); }
.contested p { margin: 4px 0 0; }
.contested-label { color: var(--follow); font-weight: 600; margin: 0; }
.meta-contested { color: var(--follow); }
/* No margin of its own: the first block inside carries the gap, and which block
   that is depends on what the pass wrote. */
.body { margin-top: 0; }
/* The claim, then the argument. A finding's first paragraph is the thing being
   asserted and the rest is why -- they were rendered identically, so a reader
   skimming for what the finding says had to read the whole card to find out.
   `> p:first-child`, so a body that opens with a list is left alone. */
.body p { margin: 10px 0 0; font-size: 15px; line-height: 1.55; color: var(--ink-2); }
.body > p:first-child { margin-top: 13px; font-size: 16.5px; line-height: 1.5; color: var(--ink); }
.body ul, .body ol { margin: 12px 0; padding-left: 22px; }
.body li { margin: 4px 0; }
.body blockquote { margin: 12px 0; padding: 2px 14px; border-left: 3px solid var(--line); color: var(--muted); }
.body code, .chip { overflow-wrap: anywhere; }
.body code, .sc-answer code, .sc-item summary code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .89em;
  background: var(--code-bg); padding: 1px 5px; border-radius: 4px; }
.body pre, .sc-answer pre { background: var(--code-bg); border: 1px solid var(--line); border-radius: 8px;
  padding: 12px 14px; overflow-x: auto; }
.body pre code, .sc-answer pre code { background: none; padding: 0; font-size: 12.5px; line-height: 1.5; }
.xref { color: var(--accent); text-decoration: none; border-bottom: 1px dotted var(--accent); }
a { color: var(--accent); }

/* The questions read as sentences, not as the prose sections' uppercase labels:
   a summary here is the thing being asked, and small-caps would file it as
   furniture. The answer stays collapsed until the reader opens it, which is the
   native <details> behaviour and the whole interaction. */
.sc-note { color: var(--muted); font-size: 13px; line-height: 1.5; margin: -8px 0 16px; }
.sc-item { border: 1px solid var(--line); border-radius: 10px; background: var(--panel);
  padding: 0 18px; margin: 0 0 10px; }
.sc-item summary { cursor: pointer; padding: 12px 0; font-size: 15.5px; line-height: 1.45; }
.sc-item[open] summary { border-bottom: 1px solid var(--line); }
.sc-answer p { margin: 12px 0 0; font-size: 15px; line-height: 1.55; color: var(--ink-2); }
.sc-answer ul, .sc-answer ol { margin: 12px 0 0; padding-left: 22px; }
.sc-anchors { color: var(--muted); font-size: 13px; margin: 10px 0 14px; }

/* Doc notes borrow the finding card and stay off every filter's axis: no
   data- attributes, so no selector reaches them, like the withdrawn cards --
   but at full opacity, because these are live leads, just not findings. The
   neutral left edge is the default; a disposition colour would claim a
   disposition they do not have. */
.docs-note, .docs-coverage { color: var(--muted); font-size: 13px; line-height: 1.5; }
.docs-note { margin: -8px 0 12px; }
.docs-coverage { margin: 0 0 16px; }
.doc-owed-label { font-size: 11px; letter-spacing: .1em; text-transform: uppercase; color: var(--muted);
  margin: 12px 0 0; }

.prose details { border: 1px solid var(--line); border-radius: 10px; background: var(--panel); padding: 0 18px; }
.prose summary { cursor: pointer; padding: 12px 0; font-size: 13px; letter-spacing: .06em;
  text-transform: uppercase; color: var(--muted); }
.prose details[open] summary { border-bottom: 1px solid var(--line); margin-bottom: 8px; }
.prose details > *:last-child { margin-bottom: 16px; }

:target { scroll-margin-top: 24px; }
.finding:target { border-color: var(--accent); }

@media (max-width: 900px) {
  /* One column, and the report is not last in it. `display: contents` dissolves
     the sidebar's box so its three parts become items of the layout in their own
     right and can be ordered around `main`: the verdict and the filters stay
     above the report, and the nav and the run's facts -- a table of contents and
     a page of reference material, neither of which the reader came for -- move
     below it. Two columns are what made those readable beside the report at all,
     and there is no second column here to put them in.

     Ordering them needs no change to the markup, which is the point: the DOM
     order is the desktop order, where the sidebar is one sticky column and none
     of this applies. */
  .layout { display: flex; flex-direction: column; align-items: stretch;
    gap: 0; padding: 20px 16px 64px; }
  .sidebar { display: contents; }
  main { order: 1; }
  nav { order: 2; margin-top: 28px; }
  .run { order: 3; }
  /* Nothing scrolls out of a sidebar that no longer scrolls, and a head left
     sticky here would pin itself over the report instead. Its padding is the gap
     above the report now, which is what the sidebar's own margin used to be. */
  .sidebar-head { position: static; }
}
"""
