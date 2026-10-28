"""Print a self-contained HTML page to PDF with a headless browser.

The manager export is laid out once, in `templates/pm_export_report.html`,
and this prints that same page. The alternative -- drawing the PDF
separately with a reporting library -- means two layouts of the same report,
and two layouts drift: a column added to the HTML table is missing from the
PDF for months before anybody notices, and the two disagree about what a
figure means. Printing the page keeps them the same artefact by
construction.

Playwright drives Chromium, which already knows how to paginate a table,
repeat a `<thead>` across pages, honour `break-inside: avoid` and render the
inline SVG charts. None of that is work worth reimplementing.

Two things this module is careful about:

**It never fetches anything.** The page is handed to the browser as a string
through `set_content`, with no base URL, and the template inlines its own CSS
and SVG. A report that reached out to the network would be a report that
renders differently depending on where it is opened, and would hang when the
plant network says no.

**A missing browser is explained, not raised as a 500.** Playwright is a
Python package plus a browser binary that is downloaded separately, and
installing the first without the second is the normal way to get this wrong.
`PdfUnavailable` carries the command that fixes it so the page can show it.
"""

from __future__ import annotations

# Letter landscape, matching the @page rule in the report template. Passed
# as a format name rather than measurements so the two cannot drift.
_PAGE_FORMAT = "Letter"

# Chromium is started per export and thrown away. An export takes a second
# or two, runs when somebody presses a button, and a long-lived browser in a
# Flask worker is a process to supervise, a memory leak to watch and a thing
# to shut down cleanly on reload. Not worth it for this.
_LOAD_TIMEOUT_MS = 20_000

_INSTALL_HINT = "python -m playwright install chromium"


class PdfUnavailable(RuntimeError):
    """Raised when this machine cannot print a PDF, with how to fix it."""

    def __init__(self, message: str, *, hint: str = _INSTALL_HINT) -> None:
        super().__init__(message)
        self.hint = hint


def available() -> bool:
    """Whether the Playwright package is importable at all.

    Only the package: whether the browser binary is present is not knowable
    without starting it, so that failure surfaces from `render` instead.
    """

    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        return False
    return True


def render(html: str, *, landscape: bool = True) -> bytes:
    """Return `html` printed to PDF bytes.

    Backgrounds are printed, because the report's status colours are
    backgrounds and a report that only makes sense in colour on screen is a
    report that loses its meaning the moment somebody saves it. The page
    size comes from the template's own `@page` rule where it has one.
    """

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:  # pragma: no cover - import guard
        raise PdfUnavailable(
            "The PDF export needs the playwright package, which is not installed."
        ) from error

    try:
        with sync_playwright() as runner:
            browser = runner.chromium.launch(args=["--disable-dev-shm-usage"])
            try:
                page = browser.new_page()
                page.set_content(html, wait_until="load", timeout=_LOAD_TIMEOUT_MS)
                return page.pdf(
                    format=_PAGE_FORMAT,
                    landscape=landscape,
                    print_background=True,
                    prefer_css_page_size=True,
                )
            finally:
                browser.close()
    except PdfUnavailable:
        raise
    except Exception as error:
        message = str(error)
        if "Executable doesn't exist" in message or "playwright install" in message:
            raise PdfUnavailable(
                "The PDF export needs Chromium, which Playwright has not downloaded yet."
            ) from error
        raise PdfUnavailable(f"The PDF could not be produced: {message}") from error
