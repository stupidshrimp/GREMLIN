"""The patch notes page reads a Word document off the engineering share.

Nobody can commit a broken release note here -- the document is edited in Word by
whoever cuts the release -- so these tests are mostly about what the reader does
with a document that was written by hand: versions in either order, bullets Word
never made into a list, a heading followed by prose, and a share that is simply
not there.
"""

import importlib
import os
import re
import zipfile
from xml.sax.saxutils import escape

import pytest

from services.patch_notes_service import PatchNotesReader, PatchNotesError, parse_document

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.wordprocessingml.document.main+xml"/></Types>'
)
RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Target="word/document.xml" Type="http://schemas.openxmlformats.org'
    '/officeDocument/2006/relationships/officeDocument"/></Relationships>'
)


def _document_xml(paragraphs) -> str:
    """The body of a .docx, from ``"text"`` lines and ``("text", level)`` bullets."""

    parts = []
    for paragraph in paragraphs:
        if isinstance(paragraph, tuple):
            text, level = paragraph
            properties = (
                '<w:pPr><w:pStyle w:val="ListParagraph"/><w:numPr>'
                f'<w:ilvl w:val="{level}"/><w:numId w:val="2"/></w:numPr></w:pPr>'
            )
        else:
            text, properties = paragraph, ""
        parts.append(
            f"<w:p>{properties}<w:r>"
            f'<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body>{"".join(parts)}</w:body></w:document>'
    )


def _write_docx(path, paragraphs):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", RELS)
        archive.writestr("word/document.xml", _document_xml(paragraphs))
    return path


# The document as the sample on the share is written: version, date, a heading,
# its bullets, then the fix list.
SAMPLE = [
    "GREMLIN 0.1.0",
    "8/18/2026",
    "Initial release:",
    ("The GREMLIN site brings CMMS data preparation into one web workspace.", 0),
    "Bug Fixes:",
    ("Clear all filter selection for metrics page", 0),
    ("Show downtime hours for all analysis", 0),
]


def _read(tmp_path, paragraphs, name="GREMLIN_patchnotes.docx"):
    return PatchNotesReader(_write_docx(tmp_path / name, paragraphs)).read()


def test_reads_a_release_the_way_the_document_is_written(tmp_path):
    notes = _read(tmp_path, SAMPLE)

    assert notes.error is None
    release = notes.latest
    assert release.version == "0.1.0"
    assert release.date_display == "August 18, 2026"
    assert release.released_on.isoformat() == "2026-08-18"
    assert [(s.title, s.kind, s.item_count) for s in release.sections] == [
        ("Initial release", "features", 1),
        ("Bug Fixes", "fixes", 2),
    ]
    assert release.fix_count == 2
    assert release.anchor == "v0-1-0"


def test_a_version_split_across_runs_is_still_one_version():
    """Word stores an edited line as several runs -- the sample's own 0.1.0 is
    "GREMLIN 0." + "1" + ".0" -- and none of them is a version on its own."""

    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p>"
        "<w:r><w:t>GREMLIN 0.</w:t></w:r><w:r><w:t>1</w:t></w:r><w:r><w:t>.0</w:t></w:r>"
        "</w:p><w:body></w:body></w:body></w:document>"
    ).encode()

    assert parse_document(xml)[0].version == "0.1.0"


def test_releases_are_ordered_newest_first_however_the_document_grew(tmp_path):
    """The author may add a release at either end, and 0.10.0 is not 0.1.0."""

    notes = _read(
        tmp_path,
        [
            "GREMLIN 0.9.1", "1/5/2026", "Bug Fixes:", ("An older fix", 0),
            "GREMLIN 0.10.0", "3/2/2026", "What's New:", ("A newer feature", 0),
            "GREMLIN 0.2.0", "12/1/2025", "What's New:", ("The oldest feature", 0),
        ],
    )

    assert [release.version for release in notes.releases] == ["0.10.0", "0.9.1", "0.2.0"]
    assert notes.latest.version == "0.10.0"


def test_two_entries_for_one_version_get_anchors_of_their_own(tmp_path):
    """The anchor is a DOM id and the jump list's target. Repeat it and every
    link lands on the first entry, and two articles name one heading."""

    notes = _read(
        tmp_path,
        [
            "GREMLIN 1.2.0", "1/1/2026", "Bug Fixes:", ("The original fix", 0),
            "GREMLIN 1.2.0 - Hotfix", "2/1/2026", "Bug Fixes:", ("The hotfix", 0),
            "GREMLIN 1.2.0 - Hotfix", "3/1/2026", "Bug Fixes:", ("The second hotfix", 0),
        ],
    )

    anchors = [release.anchor for release in notes.releases]
    assert anchors == ["v1-2-0", "v1-2-0-hotfix", "v1-2-0-hotfix-2"]
    assert len(set(anchors)) == len(anchors)


def test_indented_bullets_are_nested_under_the_one_above(tmp_path):
    notes = _read(
        tmp_path,
        ["GREMLIN 1.0.0", "5/1/2026", "What's New", ("Parent", 0), ("Child", 1), ("Sibling", 0)],
    )

    items = notes.latest.sections[0].items
    assert [item.text for item in items] == ["Parent", "Sibling"]
    assert [child.text for child in items[0].children] == ["Child"]
    # Sub-bullets are part of what the release changed, so the chip counts them.
    assert notes.latest.feature_count == 3


def test_a_bullet_starting_deeper_than_the_list_does_is_kept(tmp_path):
    """Word can leave the first item of a list at level 1. It has nothing to nest
    under, and dropping it would lose a line of the release."""

    notes = _read(tmp_path, ["GREMLIN 1.0.0", "5/1/2026", "Notes", ("Orphan", 2)])

    assert [item.text for item in notes.latest.sections[0].items] == ["Orphan"]


def test_a_bullet_typed_as_a_dash_is_still_a_bullet(tmp_path):
    """Not everyone uses Word's list button; the page should look the same."""

    notes = _read(
        tmp_path,
        ["GREMLIN 1.1.0", "6/1/2026", "Bug Fixes:", "- Fixed the export", "• Fixed the filter"],
    )

    section = notes.latest.sections[0]
    assert [item.text for item in section.items] == ["Fixed the export", "Fixed the filter"]


def test_a_dash_glued_to_a_word_is_not_a_bullet(tmp_path):
    """The glyph only counts with a space after it. "-40% downtime" is a line of
    the document, and stripping its sign would change what the release claims."""

    notes = _read(
        tmp_path,
        ["GREMLIN 1.1.0", "6/1/2026", "Bug Fixes:", "-40% downtime after the PM change", ("Real", 0)],
    )

    written = [section.title for section in notes.latest.sections]
    written += [text for section in notes.latest.sections for text in section.paragraphs]
    written += [item.text for section in notes.latest.sections for item in section.items]
    assert "-40% downtime after the PM change" in written


def test_prose_under_a_heading_is_not_read_as_another_heading(tmp_path):
    """A sentence set in heading type, with the list hanging under it, is the
    ugliest way this can go wrong -- and the easiest to write by accident."""

    sentence = (
        "This release establishes the complete data path from Limble records to "
        "dispositioned events, reliability metrics and downloadable reports."
    )
    notes = _read(tmp_path, ["GREMLIN 1.2.0", "7/1/2026", "What's New", sentence, ("A feature", 0)])

    section = notes.latest.sections[0]
    assert section.title == "What's New"
    assert section.paragraphs == (sentence,)
    assert [item.text for item in section.items] == ["A feature"]


def test_a_paragraph_written_after_the_bullets_stays_after_them(tmp_path):
    notes = _read(
        tmp_path,
        ["GREMLIN 1.2.0", "7/1/2026", "What's New", ("A feature", 0), "Ask Reliability for access."],
    )

    sections = notes.latest.sections
    assert [(s.title, [i.text for i in s.items], s.paragraphs) for s in sections] == [
        ("What's New", ["A feature"], ()),
        ("", [], ("Ask Reliability for access.",)),
    ]


def test_the_date_may_sit_on_the_version_line(tmp_path):
    notes = _read(tmp_path, ["GREMLIN 1.3.0 - 9/9/2026", "What's New", ("A feature", 0)])

    assert notes.latest.date_display == "September 9, 2026"
    assert notes.latest.label is None
    assert notes.latest.sections[0].title == "What's New"


def test_a_release_with_no_date_keeps_its_first_heading(tmp_path):
    notes = _read(tmp_path, ["GREMLIN 1.4.0", "Bug Fixes:", ("A fix", 0)])

    assert notes.latest.date_display == ""
    assert notes.latest.released_on is None
    assert [section.title for section in notes.latest.sections] == ["Bug Fixes"]


@pytest.mark.parametrize(
    "heading, kind",
    [
        ("Bug Fixes:", "fixes"),
        ("Fixes and improvements", "fixes"),
        ("What's New", "features"),
        ("New Features", "features"),
        ("Initial release:", "features"),
        ("Known limitations", "notes"),
    ],
)
def test_headings_are_recognised_however_they_are_worded(tmp_path, heading, kind):
    notes = _read(tmp_path, ["GREMLIN 2.0.0", "1/1/2026", heading, ("An item", 0)])

    assert notes.latest.sections[0].kind == kind


@pytest.mark.parametrize(
    "line, version, label",
    [
        ("GREMLIN 0.1.0", "0.1.0", None),
        ("GREMLIN v1.2.0", "1.2.0", None),
        ("1.2.0", "1.2.0", None),
        ("Release 1.2.0", "1.2.0", None),
        ("GREMLIN 1.2.0 (Beta)", "1.2.0", "Beta"),
        ("GREMLIN 1.2.0 - Hotfix", "1.2.0", "Hotfix"),
    ],
)
def test_a_version_line_is_recognised_however_it_is_written(tmp_path, line, version, label):
    notes = _read(tmp_path, [line, "1/1/2026", "Bug Fixes:", ("A fix", 0)])

    assert notes.latest.version == version
    assert notes.latest.label == label


def test_a_sentence_beginning_with_a_number_does_not_start_a_release(tmp_path):
    """A version line takes everything under it, so a false one would move the
    rest of the document into a release that does not exist."""

    notes = _read(
        tmp_path,
        [
            "GREMLIN 1.0.0",
            "1/1/2026",
            "Known limitations",
            "1.5 hours of downtime were recorded before the meter was replaced.",
            "Bug Fixes:",
            ("A fix", 0),
        ],
    )

    assert [release.version for release in notes.releases] == ["1.0.0"]
    assert [section.title for section in notes.latest.sections] == [
        "Known limitations",
        "Bug Fixes",
    ]


def test_a_document_with_no_version_line_says_so(tmp_path):
    """Rendering it as an empty page would look like a history nobody has written."""

    notes = _read(tmp_path, ["Patch notes", "Some notes about the notes."])

    assert not notes.is_available
    assert "version" in notes.error


def test_a_missing_document_is_reported_rather_than_raised(tmp_path):
    notes = PatchNotesReader(tmp_path / "not-mapped" / "GREMLIN_patchnotes.docx").read()

    assert not notes.is_available
    assert "not found" in notes.error
    assert "shared drive" in notes.error


def test_a_file_that_is_not_a_word_document_is_reported(tmp_path):
    path = tmp_path / "GREMLIN_patchnotes.docx"
    path.write_bytes(b"this is a .doc saved under the wrong extension")

    notes = PatchNotesReader(path).read()

    assert not notes.is_available
    assert ".docx" in notes.error


def test_a_damaged_document_is_reported(tmp_path):
    path = tmp_path / "GREMLIN_patchnotes.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", "<w:document><w:body>")

    notes = PatchNotesReader(path).read()

    assert not notes.is_available
    assert "damaged" in notes.error


def test_the_document_is_parsed_again_only_once_it_changes(tmp_path):
    """Every visit to the page asks for the notes, and the file is on a network
    share. Re-reading it per request is the cost this cache exists to avoid."""

    path = _write_docx(tmp_path / "GREMLIN_patchnotes.docx", SAMPLE)
    reader = PatchNotesReader(path)
    first = reader.read()

    assert reader.read() is first

    _write_docx(path, ["GREMLIN 0.2.0", "9/1/2026", "What's New", ("Something else", 0)])
    # The cache is keyed on the file's timestamp and size, and a test can rewrite
    # a file inside one filesystem tick.
    stamp = path.stat().st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(stamp, stamp))

    assert reader.read().latest.version == "0.2.0"


def test_parse_document_rejects_a_document_it_cannot_read():
    with pytest.raises(PatchNotesError):
        parse_document(b"not xml at all")


# ---------------------------------------------------------------------------
# The page itself
# ---------------------------------------------------------------------------


def _client(monkeypatch, tmp_path, notes_path):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_PATCH_NOTES_PATH", str(notes_path))
    import app

    return importlib.reload(app).app.test_client()


def test_the_page_gives_every_release_an_id_of_its_own(monkeypatch, tmp_path):
    """Two entries for one version is the case that used to collide: the ids are
    what the jump list points at and what aria-labelledby names."""

    document = _write_docx(
        tmp_path / "GREMLIN_patchnotes.docx",
        [
            "GREMLIN 2.0.0", "1/1/2026", "Bug Fixes:", ("A fix", 0),
            "GREMLIN 2.0.0", "2/1/2026", "Bug Fixes:", ("Another fix", 0),
        ],
    )
    body = _client(monkeypatch, tmp_path, document).get("/patch-notes").get_data(as_text=True)

    ids = re.findall(r'id="([^"]+)"', body)
    assert sorted(ids) == sorted(set(ids))
    assert 'href="#v2-0-0-2"' in body


def test_the_page_draws_what_the_document_says(monkeypatch, tmp_path):
    document = _write_docx(tmp_path / "GREMLIN_patchnotes.docx", SAMPLE)
    body = _client(monkeypatch, tmp_path, document).get("/patch-notes").get_data(as_text=True)

    assert "0.1.0" in body
    assert "August 18, 2026" in body
    assert "Clear all filter selection for metrics page" in body
    assert 'id="v0-1-0"' in body
    assert "Bug Fixes" in body


def test_an_unreachable_share_still_renders_the_page(monkeypatch, tmp_path):
    """A page that explains itself is worth more here than a 500: the visitor is
    told which document is missing, which is the thing that gets it fixed."""

    response = _client(monkeypatch, tmp_path, tmp_path / "Z" / "GREMLIN_patchnotes.docx").get(
        "/patch-notes"
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "not available right now" in body
    assert "shared drive" in body
