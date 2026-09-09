"""The Excel help on the disposition page: button tooltips and the explainer.

"Download Excel" and "Disposition via Excel" name themselves without saying what
they do, so each carries a hover/focus tooltip, and the page carries a "How
dispositioning on Excel works" dialog that walks through the round trip.

The two halves live apart -- the buttons are built by the client script, the
explainer is markup in the template -- so these are mostly "do the two still
agree" tests. A renamed id leaves a button that opens nothing, which is exactly
the kind of break that looks fine in a diff. The template is read from disk
rather than requested, because the disposition page is editor-only and an
anonymous request renders the 403 page instead.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = (ROOT / "templates" / "disposition.html").read_text()
SCRIPT = (ROOT / "static" / "js" / "life_data_analysis.js").read_text()
STYLES = (ROOT / "static" / "css" / "life_data_analysis.css").read_text()


def test_the_page_carries_the_excel_explainer_dialog():
    assert 'id="lda-excel-help-dialog"' in TEMPLATE
    assert "How dispositioning on Excel works" in TEMPLATE
    # A native <dialog> is what gives the explainer its focus trap and its
    # Escape handling; a plain <div> would silently lose both.
    assert re.search(r"<dialog[^>]*id=\"lda-excel-help-dialog\"", TEMPLATE)


def test_the_script_opens_the_dialog_the_template_renders():
    for element_id in ("lda-excel-help-dialog", "lda-excel-help-close"):
        assert f'id="{element_id}"' in TEMPLATE, element_id
        assert f'"{element_id}"' in SCRIPT, element_id
    assert "dialog.showModal()" in SCRIPT
    assert "dialog.close()" in SCRIPT


def test_the_explainer_names_the_buttons_it_explains():
    """The steps walk through the two buttons by name, so a relabelled button
    would leave the explainer describing something the page no longer has."""
    for label in ("Download Excel", "Disposition via Excel"):
        assert f'text: "{label}"' in SCRIPT, label
        assert label in TEMPLATE, label


def test_the_explainer_says_the_weibull_inclusion_flag_has_to_be_set():
    """The one step the workbook makes easy to miss.

    An exported row always carries an explicit TRUE/FALSE in
    include_in_weibull_candidate, so the import never falls back to the default
    the category would otherwise imply (see _save_disposition_with_conn: the
    default only applies when the value arrives as None). A row dispositioned
    entirely in Excel and left at the FALSE it came with saves happily and is
    then excluded by every analysis query, so the explainer has to say to set it.
    """
    columns = re.search(r"<h3>Both record types</h3>(.*?)</ul>", TEMPLATE, re.S)
    assert columns, "the explainer no longer lists the shared disposition columns"
    flag = re.search(r"<li><code>include_in_weibull_candidate</code>.*?</li>", columns.group(1), re.S)
    assert flag, "the explainer no longer describes include_in_weibull_candidate"
    # Naming the two allowed values is not enough: the bullet has to tell the
    # reader to set it, which is the part a spreadsheet full of FALSE hides.
    assert "set" in flag.group(0).lower(), flag.group(0)
    assert "TRUE" in flag.group(0) and "FALSE" in flag.group(0)

    # And both "what makes a row usable" sentences count the flag among the
    # conditions, rather than stopping at the category.
    for condition in ("INCLUDED_FAILURE", "INCLUDED_PM_RESET_EVENT"):
        usable = re.search(
            r"[^<]*Weibull-usable only as <code>" + condition + r"</code>.*?</li>", TEMPLATE, re.S
        )
        assert usable, condition
        assert "include_in_weibull_candidate" in usable.group(0), condition


def test_the_explainer_says_to_clear_both_taxonomy_ids():
    """Clearing one id is not enough, and the wrong advice costs the whole file.

    import_disposition_excel prefers an id cell over the name beside it, so a row
    that keeps its old failure_mechanism_id lands that mechanism under the newly
    resolved mode -- which _save_disposition_with_conn rejects ("belongs to a
    different failure mode"), and the ValueError rolls back write_connection, so
    the entire workbook is refused rather than that one row. Reproduced against a
    temp database: clearing only failure_mode_id raises, and so does clearing both
    ids while leaving the old mode's mechanism name in place.
    """
    note = re.search(r"<strong>Changing a failure mode or mechanism\?</strong>.*?</p>", TEMPLATE, re.S)
    assert note, "the explainer no longer covers changing a mode or mechanism"
    for column in ("failure_mode_id", "failure_mechanism_id"):
        assert column in note.group(0), column
    assert "both" in note.group(0).lower(), note.group(0)


def test_the_explainer_warns_that_a_stale_workbook_overwrites():
    """An upload is last-write-wins, and neither the tooltip nor the steps may
    imply otherwise.

    import_disposition_excel compares each uploaded row against the database as
    it stands at upload time (_excel_disposition_matches_current reads
    disposition_rows there and then), not against the workbook as downloaded. A
    row the uploader never touched therefore counts as a difference once someone
    else has re-dispositioned that record, and writes the older values back over
    theirs -- reproduced with two services on one temp database.
    """
    note = re.search(r"<strong>A workbook is a snapshot[^<]*</strong>.*?</p>", TEMPLATE, re.S)
    assert note, "the explainer no longer warns that an old workbook overwrites newer edits"
    assert "not against the sheet as it was downloaded" in " ".join(note.group(0).split())

    # The explainer's step 5 used to promise only-what-you-changed.
    assert "rows you did not change are skipped" not in TEMPLATE

    # So did the upload tooltip, whose text is split across concatenated string
    # literals -- so match on the call, with the JS quoting collapsed out.
    call = re.search(r"withTooltip\(\n\s+upload,(.*?)\n\s+\),", SCRIPT, re.S)
    assert call, "the upload button no longer carries a tooltip"
    tooltip = re.sub(r'"\s*\+\s*"', "", call.group(1))
    assert "only the ones you changed are saved" not in tooltip
    assert "stale" in tooltip, tooltip


def test_both_excel_buttons_are_wrapped_in_a_tooltip():
    # The call sites, not the helper's own one-line declaration: each wraps its
    # button on the line after the opening bracket.
    tooltipped = re.findall(r"withTooltip\(\n\s+(\w+),", SCRIPT)
    assert tooltipped == ["download", "upload"]


def test_the_tooltip_opens_on_hover_and_on_keyboard_focus():
    """A bubble that only answers to a mouse keeps what the button does from
    anyone tabbing through the page."""
    assert ".lda-tip-wrap:hover .lda-tip" in STYLES
    assert ".lda-tip-wrap:has(:focus-visible) .lda-tip" in STYLES
    # Described, not labelled: the button's own text still has to be what a
    # screen reader announces first.
    assert 'control.setAttribute("aria-describedby", id)' in SCRIPT
    assert 'role: "tooltip"' in SCRIPT
