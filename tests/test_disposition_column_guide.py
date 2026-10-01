"""The Disposition tour's column-by-column guide to filling in a row.

After the table step, the tour lights each column there is to fill in, one at a
time, and says what it means and what to put in it, following Reliability
Engineering's REL-WBL-DAT-002 Failure Definition and REL-WBL-PLN-003 Data
Requirements. The cards are written against the choices the server offers, so
these check the two stay in step: a card for every editable column of each
record type, and on the cards that list a dropdown's values, exactly the values
that dropdown has -- a category added on the server without a word on what it
means, or one dropped and still explained, fails here.
"""

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from services.life_data_service import PM_DISPOSITION_CATEGORIES, PM_RESET_DECISIONS, WO_DISPOSITION_CATEGORIES

ROOT = Path(__file__).resolve().parent.parent
ANALYSIS_JS = ROOT / "static" / "js" / "life_data_analysis.js"
APP_PY = ROOT / "app.py"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node to read the guide")


def _declaration(source, name, closing):
    found = re.search(rf"\n  const {name} = .*?{re.escape(closing)}", source, re.S)
    assert found, f"{name} was not found in {ANALYSIS_JS.name}"
    return found.group(0)


# The guide and the steps built from it, lifted out of the page script and run
# once per record type, as the tour would start with that Record Type chosen.
# Every field a card can have is evaluated, as the engine does when it shows it.
_SCRIPT = r"""
const state = { dispositionKind: "wo" };
__DECLARATIONS__
const value = (field) => (typeof field === "function" ? field() : field);
const tours = {};
for (const kind of ["wo", "pm"]) {
  state.dispositionKind = kind;
  tours[kind] = DISPOSITION_COLUMN_TOUR_STEPS.filter((step) => !step.when || step.when()).map((step) => ({
    target: step.target,
    title: step.title,
    label: value(step.label) || null,
    body: value(step.body),
    points: value(step.points) || [],
    cite: value(step.cite) || null,
  }));
}
console.log(JSON.stringify({ guide: DISPOSITION_COLUMN_GUIDE, columns: DISPOSITION_EDIT_COLUMNS, tours }));
"""


@pytest.fixture(scope="module")
def lifted():
    source = ANALYSIS_JS.read_text(encoding="utf-8")
    declarations = "\n".join(
        [
            _declaration(source, "DISPOSITION_EDIT_COLUMNS", "\n  };"),
            _declaration(source, "FAILURE_DEFINITION", ";"),
            _declaration(source, "DATA_REQUIREMENTS", ";"),
            _declaration(source, "DISPOSITION_COLUMN_GUIDE", "\n  };"),
            _declaration(source, "DISPOSITION_COLUMN_TOUR_STEPS", "\n  );"),
        ]
    )
    result = subprocess.run(
        ["node", "-e", _SCRIPT.replace("__DECLARATIONS__", declarations)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def record_classes():
    """What the Record Class dropdown offers each record type: the two tuples
    app.py hands the editor, read from its source rather than by importing it,
    which would open its databases."""
    tree = ast.parse(APP_PY.read_text(encoding="utf-8"))
    offered = {
        node.targets[0].id: list(ast.literal_eval(node.value))
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"WO_RECORD_CLASS_OPTIONS", "PM_RECORD_CLASS_OPTIONS"}
    }
    assert len(offered) == 2, "app.py no longer names the record classes it offers; update this test"
    return {"wo": offered["WO_RECORD_CLASS_OPTIONS"], "pm": offered["PM_RECORD_CLASS_OPTIONS"]}


def _card(lifted, kind, key):
    [card] = [entry for entry in lifted["guide"][kind] if entry["key"] == key]
    return card


def _terms(card):
    return [point[0] for point in card.get("points", []) if isinstance(point, list)]


@pytest.mark.parametrize("kind", ["wo", "pm"])
def test_every_column_there_is_to_fill_in_has_one_card(lifted, kind):
    keys = [entry["key"] for entry in lifted["guide"][kind]]
    assert len(keys) == len(set(keys))
    assert sorted(keys) == sorted(column["key"] for column in lifted["columns"][kind])


@pytest.mark.parametrize(
    "kind, key, offered",
    [
        ("wo", "disposition_category", list(WO_DISPOSITION_CATEGORIES)),
        ("pm", "disposition_category", list(PM_DISPOSITION_CATEGORIES)),
        ("pm", "pm_reset_inclusion_decision", list(PM_RESET_DECISIONS)),
    ],
)
def test_a_dropdown_card_explains_exactly_the_values_it_offers(lifted, kind, key, offered):
    assert sorted(_terms(_card(lifted, kind, key))) == sorted(offered)


@pytest.mark.parametrize("kind", ["wo", "pm"])
def test_the_record_class_card_explains_exactly_the_classes_offered(lifted, record_classes, kind):
    assert sorted(_terms(_card(lifted, kind, "effective_record_class"))) == sorted(record_classes[kind])


@pytest.mark.parametrize("kind", ["wo", "pm"])
def test_filling_in_a_row_opens_on_how_to_decide_then_takes_each_column(lifted, kind):
    opening, *columns = lifted["tours"][kind]
    assert opening["title"] == "Filling in a row"
    assert opening["target"] == "#lda-disp-table th[data-disp-col]"
    assert opening["points"], "the opening card lists the order a record is decided in"

    labels = {column["key"]: column["label"] for column in lifted["columns"][kind]}
    guide = lifted["guide"][kind]
    assert [card["title"] for card in columns] == [labels[entry["key"]] for entry in guide]
    assert [card["target"] for card in columns] == [
        f'#lda-disp-table [data-disp-col="{entry["key"]}"]' for entry in guide
    ]
    assert [card["label"] for card in columns] == [f"Column {n} of {len(guide)}" for n in range(1, len(guide) + 1)]


@pytest.mark.parametrize("kind", ["wo", "pm"])
def test_every_card_names_the_standard_it_follows(lifted, kind):
    for card in lifted["tours"][kind]:
        assert card["body"], card["title"]
        assert card["cite"] and card["cite"].startswith("REL-WBL-"), card["title"]


def test_the_guide_follows_the_review_order_not_the_column_order(lifted):
    """The Failure Definition Document decides what a record was before where it
    goes: whether it is a failure at all, then the category, then the grouping."""
    wo = [entry["key"] for entry in lifted["guide"]["wo"]]
    assert wo.index("effective_record_class") < wo.index("disposition_category") < wo.index("failure_mode")
    assert wo.index("failure_mode") < wo.index("failure_mechanism")
    pm = [entry["key"] for entry in lifted["guide"]["pm"]]
    assert pm.index("pm_reset_inclusion_decision") < pm.index("disposition_category")
    assert pm.index("reset_target_failure_mode") < pm.index("pm_reset_renewal_rationale")


def _function_body(name):
    source = ANALYSIS_JS.read_text(encoding="utf-8")
    body = re.search(rf"\n  function {name}\([^)]*\) \{{\n(.*?)\n  \}}\n", source, re.S)
    assert body, f"{name} was not found in {ANALYSIS_JS.name}"
    return body.group(1)


def test_the_editor_marks_the_columns_the_cards_light():
    """The cards find their column by data-disp-col, on the header and on every
    cell; only the columns there are to fill in carry it."""
    source = _function_body("renderDispositionEditor")
    assert 'const editColumn = (column) => (extraColumns.includes(column) ? column.key : null);' in source
    assert '"data-disp-col": editColumn(column)' in source
    assert '"data-disp-col": column.key' in source
    assert 'id: "lda-disp-table"' in source


def test_the_column_cards_come_after_the_table_step():
    """Straight after the table as a whole: step 8 of the tour, when it starts
    from the top."""
    source = ANALYSIS_JS.read_text(encoding="utf-8")
    steps = re.search(r"const DISPOSITION_EDITOR_TOUR_STEPS = \[(.*?)\n  \];", source, re.S)
    assert steps
    editor = steps.group(1)
    assert editor.index('title: "One row per record"') < editor.index("...DISPOSITION_COLUMN_TOUR_STEPS,")
    assert editor.index("...DISPOSITION_COLUMN_TOUR_STEPS,") < editor.index('title: "Include a whole page"')
