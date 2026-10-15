"""Draw the Weibull card's working figures at their opening states.

Six figures on the Documentation page's Weibull card (W4 to W9) can be worked:
sliders, buttons and a table of lives redraw them as they move. They are drawn
by ``static/js/weibull_tutorial.js``, and so that the card still reads, and
prints, in full without a script, the page carries each one already drawn at its
opening state. This writes that copy, ``templates/documentation_weibull_drawings.html``,
by running the script's own drawing functions, so the copy and the live figure
cannot differ.

Re-run it whenever the script's drawings, or the lives in the
``#weibull-figure-data`` block of ``templates/documentation_analysis.html``,
change. ``tests/test_standards_pages.py`` fails until you do, wherever Node is
installed.

Node runs the script and is not a GREMLIN runtime dependency: install it just
for this, as Pillow is for ``freeze_frame.py``.

Usage::

    python tools/prerender_weibull_figures.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "static" / "js" / "weibull_tutorial.js"
TEMPLATE = ROOT / "templates" / "documentation_analysis.html"
DRAWINGS = ROOT / "templates" / "documentation_weibull_drawings.html"

DATA_BLOCK = re.compile(r'<script type="application/json" id="weibull-figure-data">(.*?)</script>', re.S)
MACRO = re.compile(r"\{% macro ([a-z_]+)\(\) %\}\{% raw %\}(.*?)\{% endraw %\}\{% endmacro %\}", re.S)

HEADER = """{#- Written by tools/prerender_weibull_figures.py. Do not edit: run it again.

    The Weibull card's working figures (W4 to W9), each drawn at its opening
    state by static/js/weibull_tutorial.js's own drawing functions, one macro
    per piece. documentation_analysis.html places them inside the figures, so
    the card reads and prints in full without a script; with one, the script
    redraws each figure as its controls move. -#}
"""

# Reads the script's prerender() output for the figure data on stdin.
NODE_PROGRAM = (
    "const api = require(process.argv[1]); let input = '';"
    "process.stdin.setEncoding('utf8');"
    "process.stdin.on('data', (chunk) => { input += chunk; });"
    "process.stdin.on('end', () => process.stdout.write(JSON.stringify(api.prerender(JSON.parse(input)))));"
)


def figure_data(template_text: str) -> dict:
    """The lives the figures draw, from the template's #weibull-figure-data block."""

    match = DATA_BLOCK.search(template_text)
    if not match:
        raise SystemExit(f"No #weibull-figure-data block in {TEMPLATE}")
    return json.loads(match.group(1))


def node() -> str | None:
    return shutil.which("node")


def render(data: dict) -> dict[str, str]:
    """Every piece of every figure at its opening state, by name."""

    executable = node()
    if not executable:
        raise SystemExit("Node is needed to run static/js/weibull_tutorial.js; install it and try again.")
    completed = subprocess.run(
        [executable, "-e", NODE_PROGRAM, str(SCRIPT)],
        input=json.dumps(data),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(completed.stdout)


def drawings_text(pieces: dict[str, str]) -> str:
    macros = [
        f"{{% macro {name}() %}}{{% raw %}}{markup}{{% endraw %}}{{% endmacro %}}"
        for name, markup in pieces.items()
    ]
    return HEADER + "\n" + "\n\n".join(macros) + "\n"


def read_drawings(text: str) -> dict[str, str]:
    """The pieces a drawings file holds, by name: what drawings_text() wrote."""

    return dict(MACRO.findall(text))


def main() -> int:
    data = figure_data(TEMPLATE.read_text(encoding="utf-8"))
    pieces = render(data)
    DRAWINGS.write_text(drawings_text(pieces), encoding="utf-8")
    print(f"Wrote {len(pieces)} drawings to {DRAWINGS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
