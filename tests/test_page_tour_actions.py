"""page_tour.js with steps that act: the tour picking what the page needs picked.

Perform an Analysis draws most of itself only once an asset is chosen, and its
Weibull results only once a mechanism is, so its tour does the choosing rather
than stop at whatever was on screen when it started. These run the real engine
under node against a stand-in for the few parts of the DOM it touches, and check
the promises the analysis tour rests on: the count on the first card is the
whole tour, Next runs the action and shows the same step again with what it
drew, an action is only offered once, and onEnd hears only the steps reached.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE_TOUR_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "page_tour.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the tour engine")

# Just enough of a document for page_tour.js: elements looked up by id, targets
# looked up by selector with a box that is either drawn or not, and a window with
# no sticky bars, no motion and no layout observer. Scenarios are async functions
# given the tour and the helpers; each returns what it saw.
_HARNESS = r"""
const fs = require("fs");

class FakeElement {
  constructor(id) {
    this.id = id;
    this.hidden = false;
    this.disabled = false;
    this.textContent = "";
    this.style = {};
    this.attributes = {};
    this.listeners = {};
    this.classes = new Set();
    this.classList = {
      toggle: (name, on) => { if (on) this.classes.add(name); else this.classes.delete(name); },
      contains: (name) => this.classes.has(name),
    };
    this.box = { top: 100, left: 100, width: 200, height: 50 };
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  click() { if (!this.disabled) (this.listeners.click || []).forEach((fn) => fn({})); }
  focus() { document.activeElement = this; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  contains(node) { return node === this; }
  querySelectorAll() { return []; }
  getBoundingClientRect() {
    const { top, left, width, height } = this.box;
    return { top, left, width, height, right: left + width, bottom: top + height };
  }
}

const byId = {};
[
  "page-tour", "page-tour-spotlight", "page-tour-card", "page-tour-step", "page-tour-title",
  "page-tour-body", "page-tour-back", "page-tour-next", "page-tour-skip",
].forEach((id) => { byId[id] = new FakeElement(id); });
const targets = {};

global.document = {
  readyState: "complete",
  activeElement: null,
  body: new FakeElement("body"),
  getElementById: (id) => byId[id] || null,
  querySelector: (selector) => targets[selector] || null,
  addEventListener() {},
  removeEventListener() {},
};
global.window = global;
global.addEventListener = () => {};
global.innerWidth = 1280;
global.innerHeight = 800;
global.pageYOffset = 0;
global.scrollTo = () => {};
global.getComputedStyle = () => ({ position: "static", top: "0" });
global.matchMedia = () => ({ matches: true });
global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
global.localStorage = { getItem: () => null, setItem() {} };

eval(fs.readFileSync(process.argv[2], "utf8"));
const tour = window.gremlinTour;

// A target on the page, drawn or not yet.
function target(selector, isDrawn) {
  const node = new FakeElement(selector);
  if (!isDrawn) node.box = { top: 0, left: 0, width: 0, height: 0 };
  targets[selector] = node;
  return node;
}
const draw = (node) => { node.box = { top: 300, left: 100, width: 400, height: 120 }; };
const card = () => ({
  step: byId["page-tour-step"].textContent,
  title: byId["page-tour-title"].textContent,
  body: byId["page-tour-body"].textContent,
  next: byId["page-tour-next"].textContent,
  nextDisabled: byId["page-tour-next"].disabled,
  backDisabled: byId["page-tour-back"].disabled,
  busy: byId["page-tour-card"].attributes["aria-busy"] === "true",
});
const next = () => byId["page-tour-next"].click();
const skip = () => byId["page-tour-skip"].click();
const settle = () => new Promise((resolve) => setTimeout(resolve, 5));

const scenarios = {
  __SCENARIOS__
};

scenarios[process.argv[3]]().then((seen) => console.log(JSON.stringify(seen)));
"""

_SCENARIOS = {
    # Pick an asset, which draws the results; one step that isn't for this page.
    "count_and_run": r"""
    async () => {
      const seen = {};
      const asset = target("#asset", true);
      const results = target("#results", false);
      target("#other-type", false);
      let picked = false;
      let finish;
      const steps = [
        {
          target: "#asset", title: "Pick", body: () => (picked ? "Picked the example." : "Nothing picked."),
          action: { label: "Pick an example ►", needed: () => !picked, run: () => new Promise((resolve) => { finish = resolve; }) },
        },
        { target: "#results", title: "Results", body: "The results." },
        { target: "#other-type", title: "Other", body: "Not this type.", when: () => false },
        { target: "#asset", title: "End", body: "Done." },
      ];
      seen.opened = tour.start(steps, {});
      seen.first = card();
      next();
      seen.running = card();
      picked = true;
      draw(results);
      finish();
      await settle();
      seen.afterRun = card();
      next();
      await settle();
      seen.results = card();
      seen.spotlightHidden = document.getElementById("page-tour-spotlight").hidden;
      seen.pageDimmed = document.getElementById("page-tour").classList.contains("is-whole-page");
      tour.end();
      return seen;
    }
    """,
    # An action that leaves its page still wanting it -- the example it looked for
    # wasn't there -- and a step behind it that nothing ever draws.
    "offered_once": r"""
    async () => {
      const seen = {};
      target("#asset", true);
      target("#results", false);
      let runs = 0;
      const steps = [
        { target: "#asset", title: "Pick", body: "Pick.", action: { label: "Pick an example ►", needed: () => true, run: async () => { runs += 1; } } },
        { target: "#results", title: "Results", body: "The results." },
      ];
      tour.start(steps, {});
      next();
      await settle();
      seen.afterRun = card();
      next();
      await settle();
      seen.results = card();
      seen.spotlightHidden = document.getElementById("page-tour-spotlight").hidden;
      seen.pageDimmed = document.getElementById("page-tour").classList.contains("is-whole-page");
      tour.end();
      seen.runs = runs;
      return seen;
    }
    """,
    # Skipped while the action is still loading: it lands on a closed tour.
    "skip_while_running": r"""
    async () => {
      const seen = {};
      target("#asset", true);
      let finish;
      const steps = [
        { target: "#asset", title: "Pick", body: "Pick.", action: { label: "Pick ►", needed: () => true, run: () => new Promise((resolve) => { finish = resolve; }) } },
        { target: "#asset", title: "End", body: "Done." },
      ];
      tour.start(steps, {});
      next();
      skip();
      seen.closedAfterSkip = !tour.isOpen();
      finish();
      await settle();
      seen.stillClosed = !tour.isOpen();
      seen.overlayHidden = document.getElementById("page-tour").hidden;
      seen.reopened = tour.start(steps, {});
      seen.reopenedCard = card();
      tour.end();
      return seen;
    }
    """,
    # Skipped on the second of three: onEnd hears the two it reached.
    "on_end_hears_what_was_reached": r"""
    async () => {
      target("#a", true);
      const steps = ["one", "two", "three"].map((title) => ({ target: "#a", title, body: title }));
      let reached = null;
      tour.start(steps, { onEnd: (steps) => { reached = steps.map((step) => step.title); } });
      next();
      await settle();
      skip();
      return { reached };
    }
    """,
    # No actions at all, as Home's tour: undrawn steps are still left out.
    "without_actions": r"""
    async () => {
      target("#drawn", true);
      target("#hidden", false);
      const steps = [
        { target: "#drawn", title: "One", body: "one" },
        { target: "#hidden", title: "Hidden", body: "hidden" },
        { target: "#drawn", title: "Two", body: "two" },
      ];
      tour.start(steps, {});
      const first = card();
      tour.end();
      return { first };
    }
    """,
}


def _run(tmp_path, scenario):
    harness = _HARNESS.replace(
        "__SCENARIOS__",
        ",\n".join(f"  {json.dumps(name)}: {body.strip()}" for name, body in _SCENARIOS.items()),
    )
    script = tmp_path / "tour_harness.js"
    script.write_text(harness)
    result = subprocess.run(
        ["node", str(script), str(PAGE_TOUR_JS), scenario],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(result.stdout)


def test_the_first_card_counts_the_whole_tour(tmp_path):
    seen = _run(tmp_path, "count_and_run")
    assert seen["opened"] is True
    # The results step isn't drawn yet but is counted; the step for another
    # analysis type is not.
    assert seen["first"]["step"] == "Step 1 of 3"
    assert seen["first"]["next"] == "Pick an example ►"


def test_next_runs_the_action_and_shows_the_same_step_again(tmp_path):
    seen = _run(tmp_path, "count_and_run")
    running = seen["running"]
    assert running["nextDisabled"] and running["backDisabled"] and running["busy"]
    assert running["next"] == "Loading…"

    after = seen["afterRun"]
    assert after["step"] == "Step 1 of 3"
    assert after["body"] == "Picked the example."
    assert after["next"] == "Next ►"
    assert not after["nextDisabled"] and not after["busy"]

    assert seen["results"]["step"] == "Step 2 of 3"
    assert seen["spotlightHidden"] is False
    # Lit by the spotlight, whose shadow does the dimming.
    assert seen["pageDimmed"] is False


def test_an_action_is_offered_once_a_tour(tmp_path):
    seen = _run(tmp_path, "offered_once")
    assert seen["runs"] == 1
    assert seen["afterRun"]["next"] == "Next ►"
    # Still undrawn when reached: shown as a card about the whole page, over a
    # page the overlay dims itself, having no spotlight to do it.
    assert seen["results"]["title"] == "Results"
    assert seen["spotlightHidden"] is True
    assert seen["pageDimmed"] is True


def test_an_action_landing_after_skip_leaves_the_tour_closed(tmp_path):
    seen = _run(tmp_path, "skip_while_running")
    assert seen["closedAfterSkip"] and seen["stillClosed"] and seen["overlayHidden"]
    assert seen["reopened"] is True
    # A fresh tour: the button isn't left disabled or busy from the one skipped.
    assert seen["reopenedCard"]["next"] == "Pick ►"
    assert not seen["reopenedCard"]["nextDisabled"]
    assert not seen["reopenedCard"]["busy"]


def test_on_end_hears_only_the_steps_reached(tmp_path):
    assert _run(tmp_path, "on_end_hears_what_was_reached")["reached"] == ["one", "two"]


def test_a_tour_without_actions_still_leaves_out_what_is_not_drawn(tmp_path):
    assert _run(tmp_path, "without_actions")["first"]["step"] == "Step 1 of 2"
