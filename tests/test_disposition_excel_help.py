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
