"""Read the GREMLIN patch notes Word document and hand the site something to draw.

The release history is not kept in this repository. It is a single ``.docx`` on
the engineering share that a team member edits in Word, and GREMLIN reads it on
request so a published note needs no deploy, no database write and no second
copy that can disagree with the first. This module is the whole of that reading:
open the file, recover the paragraphs Word stored, and group them into releases.

The document's shape
--------------------
Each release is a version line, a date line, then one or more headed lists::

    GREMLIN 0.1.0
    8/18/2026
    What's New
      * ...
      * ...
    Bug Fixes:
      * ...

Only the version line is load-bearing -- it is what starts a release, so
everything after it belongs to that release until the next one. The rest is read
the way a person reads it: the line under the version is a date if it can be
read as one, any other unbulleted line is the heading of the list beneath it,
and a bulleted line is an item of the list it follows.

That looseness is deliberate. The document is edited by hand in Word by whoever
is cutting the release, so this reader is forgiving of everything that does not
change the meaning: "GREMLIN 1.2.0", "v1.2.0" and "1.2.0" all start a release,
"What's New" and "New Features" are the same kind of heading, a bullet typed as
"- text" counts as a bullet even though Word never made it a list, and a heading
may be followed by a plain paragraph instead of a list. What it will not do is
guess at a version: a document with no recognisable version line reports itself
as unreadable rather than rendering as an empty page, because a page that is
merely empty looks like a release history nobody has written yet.

Word, not python-docx
---------------------
A ``.docx`` is a zip of XML, and ``word/document.xml`` inside it holds the
paragraphs. GREMLIN already writes Weibull reports that way
(``services.life_data_service._write_docx``), so reading one with the same two
standard-library modules keeps the dependency list where it is.
"""

from __future__ import annotations

import os
import re
import threading
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

# The engineering share. This is the only copy of the release history; a team
# member updates it in Word and the change is live on the next page load.
# GREMLIN_PATCH_NOTES_PATH overrides it, which is what a deployment that maps the
# share to a different letter -- or a workstation testing against a local copy --
# should set.
DEFAULT_PATCH_NOTES_PATH = Path(
    r"Z:\FACIL\MAIN-ENG\901 Reliability Projects\901 Reliability Projects"
    r"\GREMLIN Program\GREMLIN Documentation\GREMLIN_patchnotes.docx"
)

_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{_WORD_NS}}}"

# Word's own list styles, for a paragraph that carries one but no numbering
# reference of its own. A list the author built with the toolbar button has
# w:numPr and never reaches this; a paragraph that was indented into a list and
# then had its bullet turned off has the style alone, and still reads as an item.
_LIST_STYLE_IDS = frozenset({"listparagraph", "listbullet", "listnumber"})

# Bullets somebody typed rather than let Word draw. The dash forms are the ones
# autocorrect produces from "- ", so they turn up in a document that looks
# bulleted in Word but stores no list at all.
_TYPED_BULLETS = "\u2022\u00b7\u25aa\u25e6\u2023\u2043-\u2013\u2014*"

# "GREMLIN 0.1.0", "GREMLIN v0.1.0", "Release 1.2.0", "0.1.0" -- one optional
# word in front, since the document names the product and a person may not.
# Two to four numbers, so a date typed as 8/18/2026 is not read as a version.
#
# Anything after the version needs a separator (a dash, a colon, or brackets) to
# count as part of the version line. That is what keeps a sentence beginning with
# a number -- "1.5 hours of downtime were recorded" -- from starting a release of
# its own and taking the rest of the document with it.
_VERSION_RE = re.compile(
    r"^(?:[A-Za-z]+\s+)?v?(\d+(?:\.\d+){1,3})\s*"
    r"(?:[\-\u2013\u2014:|]\s*(.+)|\((.+)\))?$",
    re.IGNORECASE,
)

# Dates in the order the plant writes them: the document's own 8/18/2026 first,
# then the spelled-out forms Word offers, then ISO for anyone who prefers it.
_DATE_FORMATS = (
    "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m.%d.%Y",
    "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
    "%d %B %Y", "%d %b %Y",
    "%Y-%m-%d", "%Y/%m/%d",
)

# What a heading means, tested in this order because "Fixes and improvements"
# is a fix list and "New" appears in both kinds of wording. Anything unmatched
# is a note: it still renders, just without claiming to be one or the other.
_FIX_WORDS = ("fix", "bug", "defect", "resolved", "patch", "hotfix", "correct")
_FEATURE_WORDS = (
    "new", "feature", "added", "add", "improve", "enhance", "change",
    "update", "release", "highlight",
)

# A sub-bullet is drawn one step in from its parent. Past a few levels the
# indent is all that is left of the structure, so the reader stops nesting
# rather than let a stray deep item march off the side of the card.
_MAX_LIST_DEPTH = 4


class PatchNotesError(Exception):
    """The document could not be read. The message is shown to the visitor."""


@dataclass
class _MutableItem:
    text: str
    children: list["_MutableItem"] = field(default_factory=list)


@dataclass(frozen=True)
class NoteItem:
    """One bullet, with whatever sub-bullets Word had indented under it."""

    text: str
    children: tuple["NoteItem", ...] = ()


@dataclass(frozen=True)
class NoteSection:
    """A heading inside a release ("What's New", "Bug Fixes") and its contents.

    ``paragraphs`` holds unbulleted prose written under the heading, which the
    sample document uses for the sentence describing the initial release. Both
    it and ``items`` can be empty: a heading with nothing under it is drawn as
    a heading, because that is what the document says.
    """

    title: str
    kind: str
    items: tuple[NoteItem, ...] = ()
    paragraphs: tuple[str, ...] = ()

    @property
    def item_count(self) -> int:
        """Bullets including sub-bullets -- what "6 fixes" on the page counts."""

        def count(items: tuple[NoteItem, ...]) -> int:
            return sum(1 + count(item.children) for item in items)

        return count(self.items)


@dataclass(frozen=True)
class Release:
    """One version's worth of the document."""

    version: str
    # ``v0-1-0``: the release's id in the page and the jump list's target. Stored
    # rather than derived, because uniqueness is decided across the whole
    # document -- see _with_unique_anchors.
    anchor: str
    sort_key: tuple[int, ...]
    label: str | None
    released_on: date | None
    date_text: str
    sections: tuple[NoteSection, ...]

    @property
    def date_display(self) -> str:
        """The date as "August 18, 2026", or exactly what the document said.

        ``%-d`` would drop the leading zero in one step and is not available on
        Windows, which is where GREMLIN runs.
        """

        if self.released_on is None:
            return self.date_text
        return f"{self.released_on:%B} {self.released_on.day}, {self.released_on.year}"

    @property
    def fix_count(self) -> int:
        return sum(s.item_count for s in self.sections if s.kind == "fixes")

    @property
    def feature_count(self) -> int:
        return sum(s.item_count for s in self.sections if s.kind == "features")


@dataclass(frozen=True)
class PatchNotes:
    """Everything the page needs, including the reason there is nothing to draw.

    A failure is data rather than an exception here: the visitor should see the
    page with an explanation of why the share could not be read, not a 500.
    """

    releases: tuple[Release, ...] = ()
    source_path: str = ""
    # When Word last saved the file, from the file itself rather than from
    # anything written inside it. Nothing draws it today; it is what the cache
    # below is keyed on, and it is kept here so a caller can say how fresh the
    # reading is without stat-ing the share again.
    updated_at: datetime | None = None
    error: str | None = None

    @property
    def is_available(self) -> bool:
        return bool(self.releases)

    @property
    def latest(self) -> Release | None:
        return self.releases[0] if self.releases else None


# ---------------------------------------------------------------------------
# Reading the .docx
# ---------------------------------------------------------------------------


def _document_xml(path: Path) -> bytes:
    """The one part of the archive that holds the body text."""

    try:
        with zipfile.ZipFile(path) as archive:
            return archive.read("word/document.xml")
    except FileNotFoundError:
        raise PatchNotesError(
            "The patch notes document was not found. Check that the shared drive "
            "is mapped and that the file has not been renamed or moved."
        ) from None
    except PermissionError:
        raise PatchNotesError(
            "GREMLIN is not allowed to read the patch notes document. Its account "
            "needs read access to the folder on the shared drive."
        ) from None
    except (KeyError, zipfile.BadZipFile):
        raise PatchNotesError(
            "The patch notes file is not a Word document GREMLIN can read. Word's "
            "older .doc format has to be saved as .docx."
        ) from None
    except OSError as exc:
        # Everything the network itself can do: the share dropped mid-read, the
        # drive is not mapped on this host, the file is locked by a tool that
        # takes an exclusive handle. Worth naming the reason -- it is usually
        # the whole diagnosis.
        raise PatchNotesError(
            f"The patch notes document could not be opened ({exc.strerror or exc})."
        ) from None


def _paragraph_text(paragraph: ET.Element) -> str:
    """The visible text of one paragraph, as a reader would see it.

    Walking by hand rather than with ``itertext`` because three kinds of text in
    there are not part of the paragraph: a field's instruction code, the text of
    a tracked deletion (``w:delText``, simply never collected), and the body of a
    text box, which is a whole paragraph of its own nested inside this one and is
    read on its own pass.
    """

    parts: list[str] = []

    def walk(node: ET.Element) -> None:
        for child in node:
            tag = child.tag
            if tag == W + "p":
                continue
            if tag == W + "t":
                parts.append(child.text or "")
            elif tag in (W + "tab", W + "br", W + "cr"):
                parts.append(" ")
            elif tag == W + "instrText":
                continue
            else:
                walk(child)

    walk(paragraph)
    return " ".join("".join(parts).split())


def _list_level(paragraph: ET.Element) -> int | None:
    """How deeply this paragraph is bulleted, or ``None`` if it is not."""

    properties = paragraph.find(W + "pPr")
    if properties is None:
        return None

    numbering = properties.find(W + "numPr")
    if numbering is not None:
        # numId 0 is Word's way of saying this paragraph left the list it
        # inherited from its style. Reading it as an item would bullet a heading.
        num_id = numbering.find(W + "numId")
        if num_id is not None and (num_id.get(W + "val") or "").strip() == "0":
            return None
        level = numbering.find(W + "ilvl")
        return _clamp_level(level.get(W + "val") if level is not None else "0")

    style = properties.find(W + "pStyle")
    if style is not None and (style.get(W + "val") or "").lower() in _LIST_STYLE_IDS:
        return 0
    return None


def _clamp_level(raw: str | None) -> int:
    try:
        level = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return max(0, min(level, _MAX_LIST_DEPTH - 1))


def _split_typed_bullet(text: str) -> tuple[str, bool]:
    """Strip a bullet the author typed, and say whether there was one.

    "- Clear all filters" is an item in every way that matters to the reader,
    even though Word stored it as an ordinary paragraph beginning with a dash.
    The glyph has to be followed by a space, or "8/18/2026 - hotfix" and any
    hyphenated first word would be eaten.
    """

    if len(text) > 1 and text[0] in _TYPED_BULLETS and text[1].isspace():
        return text[1:].lstrip(), True
    return text, False


def _parse_date(text: str) -> date | None:
    candidate = text.strip().rstrip(".,;:").strip()
    if not candidate:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


# Where a heading stops looking like a heading. The document's own headings are
# two or three words; the sentence describing the initial release is 300
# characters. Anything in between is decided by the punctuation below.
_HEADING_MAX_CHARS = 72


def _looks_like_heading(text: str) -> bool:
    """Whether an unbulleted line introduces what follows or is prose itself.

    Both are legitimate: the author writes "Bug Fixes:" above a list, and may
    also write a sentence under a heading rather than bullet it. Reading a
    sentence as a heading is the damaging way to be wrong -- it sets a paragraph
    of prose in heading type and hangs the bullets that follow underneath it --
    so the test errs towards prose whenever the line carries a sentence's
    punctuation or a sentence's length.
    """

    stripped = text.strip()
    if stripped.endswith(":"):
        return True
    if len(stripped) > _HEADING_MAX_CHARS:
        return False
    return not stripped.endswith((".", "!", "?"))


def _section_kind(title: str) -> str:
    lowered = title.lower()
    if any(word in lowered for word in _FIX_WORDS):
        return "fixes"
    if any(word in lowered for word in _FEATURE_WORDS):
        return "features"
    return "notes"


# Long enough for a version and a few words of label, short enough that a
# release named after a whole sentence does not become a URL nobody can read.
_ANCHOR_MAX_CHARS = 60


def _anchor_slug(version: str, label: str | None) -> str:
    """``v1-2-0``, or ``v1-2-0-hotfix`` when the release carries a label.

    The label is in here because it is usually what tells two entries for the
    same version apart -- an original 1.2.0 and a "1.2.0 - Hotfix" written later.
    """

    text = f"{version} {label}" if label else version
    return ("v" + re.sub(r"[^0-9a-z]+", "-", text.lower()).strip("-"))[:_ANCHOR_MAX_CHARS]


def _with_unique_anchors(releases: list[Release]) -> tuple[Release, ...]:
    """Number any anchor a release has already taken.

    An anchor is a DOM id: two of them alike and every jump-list link lands on
    the first entry, the second cannot be linked at all, and the two articles
    name the same heading through aria-labelledby. The label usually separates
    them; a document that repeats a version *and* its label still cannot be
    allowed to repeat the id.
    """

    taken: set[str] = set()
    unique: list[Release] = []
    for release in releases:
        anchor = release.anchor
        suffix = 1
        while anchor in taken:
            suffix += 1
            anchor = f"{release.anchor}-{suffix}"
        taken.add(anchor)
        unique.append(release if anchor == release.anchor else replace(release, anchor=anchor))
    return tuple(unique)


def _version_sort_key(version: str) -> tuple[int, ...]:
    """A four-part key, so 0.2.0 sorts above 0.10 sorts above 0.9.1.

    Padding to four means 1.2 and 1.2.0.0 compare equal rather than the shorter
    one sorting first for having fewer parts.
    """

    parts = [int(part) for part in version.split(".")]
    return tuple((parts + [0, 0, 0, 0])[:4])


def parse_document(xml_bytes: bytes) -> tuple[Release, ...]:
    """Turn ``word/document.xml`` into releases, newest first."""

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        raise PatchNotesError(
            "The patch notes document is damaged and could not be read. Reopening "
            "it in Word and saving it again usually repairs the file."
        ) from None

    builder = _ReleaseBuilder()
    # iter() rather than a body-children walk so paragraphs inside a table are
    # read too: a release written as a two-column table is still a release.
    for paragraph in root.iter(W + "p"):
        text = _paragraph_text(paragraph)
        if not text:
            continue
        level = _list_level(paragraph)
        if level is None:
            text, typed_bullet = _split_typed_bullet(text)
            if typed_bullet:
                level = 0
        if level is None:
            builder.add_line(text)
        else:
            builder.add_item(text, level)

    releases = builder.finish()
    if not releases:
        raise PatchNotesError(
            "No releases were found in the patch notes document. Each release has "
            "to start on a line of its own that names the version, like "
            "\u201cGREMLIN 1.2.0\u201d."
        )
    # Newest first, whichever end of the document the author added it to.
    # Python's sort is stable, so two paragraphs claiming the same version keep
    # the order they were written in. Anchors are settled afterwards, so that
    # numbering follows the order the page draws rather than the file's.
    return _with_unique_anchors(
        sorted(releases, key=lambda release: release.sort_key, reverse=True)
    )


class _ReleaseBuilder:
    """Assembles paragraphs into releases as they arrive, in document order."""

    def __init__(self) -> None:
        self._releases: list[Release] = []
        self._version: str | None = None
        self._label: str | None = None
        self._released_on: date | None = None
        self._date_text = ""
        self._wants_date = False
        self._sections: list[dict] = []

    # -- input ------------------------------------------------------------
    def add_line(self, text: str) -> None:
        """An unbulleted paragraph: a version, a date, or a heading."""

        match = _VERSION_RE.match(text)
        if match:
            self._close_release()
            self._version = match.group(1)
            # Whichever of the two trailing forms matched -- "1.2.0 - hotfix" or
            # "1.2.0 (hotfix)". Only one group can be filled.
            trailer = (match.group(2) or match.group(3) or "").strip()
            # "GREMLIN 1.2.0 - 8/18/2026" puts the date on the version line.
            # Anything else there is a label the release chose to carry.
            trailing_date = _parse_date(trailer) if trailer else None
            if trailing_date is not None:
                self._released_on, self._date_text, self._label = trailing_date, trailer, None
            else:
                self._label = trailer or None
            self._wants_date = trailing_date is None
            return

        if self._version is None:
            # Anything before the first version line is the document's own
            # front matter -- a title, a note to whoever edits it. Not ours.
            return

        if self._wants_date:
            self._wants_date = False
            parsed = _parse_date(text)
            if parsed is not None:
                self._released_on, self._date_text = parsed, text
                return
            # Not a date, so the release simply has none and this line is the
            # first heading. Fall through rather than swallow it.

        if _looks_like_heading(text):
            self._open_section(text)
            return

        # Prose. It goes under the heading it follows -- unless that section has
        # already started listing, in which case the paragraph comes *after* the
        # bullets in the document and would be lifted above them by being filed
        # there. An untitled section of its own keeps the page in the order the
        # document was written.
        if not self._sections or self._sections[-1]["items"]:
            self._open_section("")
        self._sections[-1]["paragraphs"].append(text)

    def add_item(self, text: str, level: int) -> None:
        """A bulleted paragraph, nested under whatever it follows."""

        if self._version is None:
            return
        self._wants_date = False
        if not self._sections:
            # Bullets before any heading. The release wrote them directly under
            # the date, so they get an untitled section and are drawn as a plain
            # list rather than being dropped.
            self._open_section("")
        section = self._sections[-1]
        parent = self._parent_for(section["items"], level)
        parent.append(_MutableItem(text=text))

    # -- assembly ---------------------------------------------------------
    def _open_section(self, title: str) -> None:
        # "Bug Fixes:" is written with a colon because in Word it is a line of
        # body text introducing the list below it. Here it becomes a heading with
        # its own weight, size and icon, and the colon left dangling in front of
        # that list reads as a typo. Nothing else about the title is touched.
        self._sections.append(
            {"title": title.rstrip(" :\u2013\u2014-"), "items": [], "paragraphs": []}
        )

    def _parent_for(self, items: list[_MutableItem], level: int) -> list[_MutableItem]:
        """The list a bullet at ``level`` belongs to.

        Descends one level per step and stops early when the document skips a
        level or starts deep -- a first bullet written at level 2 has nothing to
        nest under, so it becomes a top-level item instead of vanishing.
        """

        target = items
        for _ in range(level):
            if not target:
                break
            target = target[-1].children
        return target

    def _close_release(self) -> None:
        if self._version is None:
            return
        sections = tuple(
            NoteSection(
                title=section["title"],
                kind=_section_kind(section["title"]),
                items=_freeze(section["items"]),
                paragraphs=tuple(section["paragraphs"]),
            )
            for section in self._sections
            if section["title"] or section["items"] or section["paragraphs"]
        )
        self._releases.append(
            Release(
                version=self._version,
                anchor=_anchor_slug(self._version, self._label),
                sort_key=_version_sort_key(self._version),
                label=self._label,
                released_on=self._released_on,
                date_text=self._date_text,
                sections=sections,
            )
        )
        self._version = None
        self._label = None
        self._released_on = None
        self._date_text = ""
        self._wants_date = False
        self._sections = []

    def finish(self) -> list[Release]:
        self._close_release()
        return self._releases


def _freeze(items: list[_MutableItem]) -> tuple[NoteItem, ...]:
    """Hand out an immutable tree: one parse is shared by every request."""

    return tuple(NoteItem(text=item.text, children=_freeze(item.children)) for item in items)


# ---------------------------------------------------------------------------
# Reading on request
# ---------------------------------------------------------------------------


class PatchNotesReader:
    """Reads the document, and re-reads it only once it has actually changed.

    Every visit to the page asks for the notes, and the file is on a network
    share, so the parse is cached against the file's own modification time and
    size. A ``stat`` per request is the cost of noticing an edit within seconds
    of Word writing it; unzipping and parsing per request is not.

    The lock is held across the read because GREMLIN serves requests on threads:
    without it, two visitors arriving together would both parse the file, and
    one could publish an older parse over the other's.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_PATCH_NOTES_PATH
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._cached: PatchNotes | None = None

    def read(self) -> PatchNotes:
        with self._lock:
            try:
                stat = self.path.stat()
            except OSError as exc:
                self._stamp = None
                self._cached = None
                return PatchNotes(
                    source_path=str(self.path),
                    error=self._stat_error(exc),
                )

            stamp = (stat.st_mtime_ns, stat.st_size)
            if self._cached is not None and stamp == self._stamp:
                return self._cached

            updated_at = datetime.fromtimestamp(stat.st_mtime)
            try:
                releases = parse_document(_document_xml(self.path))
            except PatchNotesError as exc:
                notes = PatchNotes(
                    source_path=str(self.path), updated_at=updated_at, error=str(exc)
                )
            else:
                notes = PatchNotes(
                    releases=releases, source_path=str(self.path), updated_at=updated_at
                )

            # Cached either way. A document that is there but unreadable does not
            # become readable by being unzipped again a second later, and the
            # stamp means the next save is still picked up immediately.
            self._stamp = stamp
            self._cached = notes
            return notes

    def _stat_error(self, exc: OSError) -> str:
        if isinstance(exc, FileNotFoundError):
            return (
                "The patch notes document was not found. Check that the shared "
                "drive is mapped on the machine running GREMLIN and that the file "
                "has not been renamed or moved."
            )
        if isinstance(exc, PermissionError):
            return (
                "GREMLIN is not allowed to read the patch notes document. Its "
                "account needs read access to the folder on the shared drive."
            )
        return f"The patch notes document could not be reached ({exc.strerror or exc})."


def build_reader() -> PatchNotesReader:
    """The reader the web app uses, pointed by GREMLIN_PATCH_NOTES_PATH."""

    return PatchNotesReader(os.environ.get("GREMLIN_PATCH_NOTES_PATH") or None)
