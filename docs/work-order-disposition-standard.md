# Work-Order Disposition Standard — implementation notes

This is how GREMLIN implements the *Work-Order Disposition Standard* spec (owner:
Tim; status: approved direction, code lists pending team sign-off). It records
where each part lives and every place the spec left a choice open, so the team can
confirm or change those choices.

## Where things are

| Part | File |
|---|---|
| The rules: categories, seed code lists, field validation, derived fields, legacy migration map, the renewal-stream life engine, validity warnings, the population definition string | `services/disposition_standard.py` (no database; unit-tested in `tests/test_disposition_standard_engine.py`) |
| Tables and migration: `disposition_code`, `wo_disposition`, `population_weibull_analysis`, `population_weibull_adjustment`; seeding; carrying REL dispositions over | `services/standard_disposition_schema.py` (called from `LifeDataService.ensure_schema`) |
| Code lists, the Disposition page's rows and saves (with the record class), and its Excel workbook | `services/standard_disposition_service.py` (tested in `tests/test_standard_disposition_service.py`, including the spec §8 acceptance cases, and `tests/test_population_analysis_service.py`) |
| Perform an Analysis: the population, readiness, Pareto, matrix, trend, downtime drivers, PM effectiveness, repeat fixes, the renewal-stream Weibull, saved runs, adjustments, rankings, the numbered report | `services/population_analysis_service.py` (tested in `tests/test_population_analysis_service.py`) |
| API | `app.py`, `/life-data-analysis/api/...` (tested in `tests/test_standard_disposition_api.py`) |
| **Disposition** (editors) | `/life-data-analysis/disposition` — `templates/disposition.html`, `static/js/disposition.js` |
| **Perform an Analysis** (any account that may see Life Data Analysis) | `/life-data-analysis/perform-analysis` — `templates/perform_analysis.html`, `static/js/life_data_analysis.js` |
| The disposition form both pages use | `static/js/disposition_form.js` (the Disposition page's right-hand window, and the editor that opens over Perform an Analysis from any task ID in its tables) |

There is one page for each job. **Disposition** shows an asset's work orders on
the left and the one picked on the right, in the disposition form; Save & next
moves down the list. Every column of the list sorts and filters from the ▾ on its
header, server-side, so both reach every row rather than the page on screen. The
picked work order shows its facts in a labelled grid (asset; created, started and
completed on the plant's clock; downtime; record class; category), then its
request title and description, its failure narrative and its completion notes,
each under its own heading, with an empty part marked rather than left out. The
same records can be dispositioned offline in a workbook (Download Excel / Upload
Excel), which holds the rows the list shows, and the code lists are edited from
there.
**Perform an Analysis** analyses one population: an asset, narrowed by
sub-asset, component, mechanism, mode, cause and action (in the selector, or by
clicking a Pareto bar or a matrix cell, which drills down sub-asset → component
→ mechanism; the sub-asset list shows the asset, then each sub-asset under its
parent), by disposition category and by date. Every view on it — Pareto,
failure trend, downtime drivers, PM effectiveness, repeat fixes and the Weibull —
reads that same population.

Both pages follow Life Data Analysis's access rules: reads for anyone who may see
Life Data Analysis, every write (a disposition, a code, a saved Weibull run, an
adjustment, a numbered report) for editors, recorded against the account. A
Weibull fit can be previewed by any reader; saving it needs an editor.

### The analyses, on the population

* **Readiness** counts the work orders in reach: dispositioned, not reviewed,
  migrated and still to confirm, failures and PM resets in the population, and
  failures or PM resets with no component (which no life can use).
* **Pareto** — one bar per value of the dimension chosen (sub-asset by default,
  where the asset's own work orders are one bar; component, mechanism, mode,
  cause, action, category, detection method), ranked by count or downtime, each
  bar split by failure cause. Work orders not coded on that dimension make no bar
  (nor a value in the trend's summary cards); the chart says how many were left out.
* **Failure trend** — the population's work orders per month (continuous axis),
  with most frequent / highest downtime / fastest growing / most improved read
  for the Pareto's dimension (growth: last three months against the three
  before, once there are six).
* **Downtime drivers** — total, average, median and longest stop; downtime per
  month; the distribution of stop lengths (0–1, 1–4, 4–8, 8–24, 24+ h); downtime
  by sub-asset; the ten longest stops.
* **PM effectiveness** — each completed PM on the asset or a sub-asset (a
  PM_RESET that renewed one of its components, or PM-class work there that renewed
  nothing) paired with the next failure of the population found on that sub-asset.
  Ratings as before: Excellent ≥ 180 days, Good ≥ 90, Fair ≥ 30, else Poor.
* **Repeat fix rate** — item by item (sub-asset + component), a failure is a
  repeat when it is found within the window (24 scheduled hours by default) of the
  previous failure's repair being finished, on that sub-asset's Weibull schedule.
* **Weibull** — the renewal-stream fit below, shown with its population
  definition, validity warnings, the items pooled, the sensitivity fit, the
  charts, the interpretation summary, the lives and the event processing table.
  The latest saved fit of exactly the population on screen opens by itself;
  Perform Analysis (editors) fits and saves a new run, Preview the fit (viewers)
  fits without saving. Adjusted beta and eta are kept beside a saved run, with the
  reason. **Generate Weibull Report** writes the numbered Word report of a saved
  run with its population definition, items pooled and warnings.
* **Rankings** — the latest saved fit of each one-component population the
  asset reaches, with at least 5 failure lives: by beta, and by the chance that
  one of its items in service fails in the next N weeks
  (1 − Π R(tᵢ + Δᵢ) / R(tᵢ) over the items still in service at the run's cutoff).

## Data model

* **`disposition_code`** — the five code lists (component, failure mechanism,
  failure mode, failure cause, action) as configuration. Each list is seeded from
  spec §3 the first time it is empty and is the team's from then on: a code they
  retire stays retired, a code they delete is not put back. A **code** is the
  stable key dispositions and saved populations store, so it cannot be renamed
  once added; its name, ISO reference and the rest can. A code nothing uses can be
  deleted; one a current disposition, a mechanism's default cause or a saved
  Weibull run names is refused, saying where, and is retired instead
  (`is_active = 0`) so the records coded with it still read; a retired code is
  accepted on re-save only while the record keeps it. A change to a list reaches
  the work order open on the Disposition page at once, and a code added there is
  set on its field, ready to save.
  What a code means *to the engine* is an attribute, not its spelling, so the team
  can rename and extend lists freely:
  * `renews_item` on actions (seeded on REPLACE, REPAIR, CLEAN_SERVICE);
  * `default_cause_code` on mechanisms (spec §3.2's "default cause");
  * `semantic`: `UNKNOWN` (mechanism UNK, cause Unknown, component Unknown),
    `INSPECTION_FINDING` (mode DEG → detection method Inspection),
    `NO_FAULT_FOUND` (mode NFF), `INDUCED` (causes Installation and Maintenance
    error), `REVIEW` (component Other).
* **`wo_disposition`** — one row per decision, with history (`is_current`), like
  `event_disposition`. Fields are spec §2's; renewed items are stored as a JSON list
  of component codes; the linked WO as a `mapped_record_id` (entered as a task ID).
  `legacy_disposition_category`, `legacy_failure_mode` and
  `legacy_failure_mechanism` keep the REL values and travel with every later
  disposition of the record.
* **`population_weibull_analysis`** — every saved run with its population
  definition string (spec §5.4), the selection, window, method version and the full
  result as it was shown.
* **`population_weibull_adjustment`** — an editor's adjusted beta and eta for a
  saved run, with who and why, kept beside the MLE rather than over it.
* `wo_disposition.record_class` keeps the record class each disposition was
  made with, and `weibull_report_log.population_weibull_analysis_id` the run a
  numbered report was made of.

The categories are fixed in code rather than a table: the engine's rules are
written against them.

## Decisions the spec left open

1. **Which categories need the coded fields.** The spec says "all except
   DUPLICATE_MISFILED". GREMLIN requires component, mechanism, mode, action and
   evidence for FAILURE, PM_RESET, EXTERNAL and NON_FAILURE. NOT_REVIEWED (the
   default, i.e. nothing decided) and HELD (reviewed but undecidable, shown "as
   UNKNOWN") may carry partial codes or none. DUPLICATE_MISFILED carries only its
   link and note; anything else sent with it is dropped.
2. **Sub-asset.** Stored as the child's asset number; blank means the WO's own
   asset ("leave at parent"). The choices are the asset's children in the Limble
   hierarchy GREMLIN already stores on each work order (`immediate_parent_asset_id`),
   so a sub-asset appears once it has a work order of its own.
3. **Renewed items** are accepted only on FAILURE and PM_RESET, the categories
   that renew anything in the life engine. On a FAILURE they must not repeat the
   component (its own renewal follows the action). A PM_RESET must list at least
   one; its component is *not* implicitly renewed — the editor pre-fills the list
   with it.
4. **Mode NFF is refused on FAILURE** ("no fault found" is NON_FAILURE in §2.1).
5. **A cause override equal to the mechanism's default is not an override** and is
   stored blank, so it needs no note.
6. **Legacy PM categories not in §2.2:** PM_CONTEXT_ONLY and REJECTED_PM_RESET map
   to NON_FAILURE (planned work, no life event).
7. **Migration runs continuously, not once.** Every record with a REL disposition
   gets a migrated standard disposition (category mapped, legacy text kept, a review
   flag saying what to confirm; INCLUDED_FAILURE's flag says to check for NFF /
   external). While that migrated row is current, a later save on the old
   Disposition screen is carried over; once an engineer saves the record under the
   standard, the old screen no longer touches it. Migrated FAILURE / PM_RESET rows
   have no component, so they belong to no renewal stream and are left out of the
   Weibull, counted in an "uncoded records" warning, until re-dispositioned.
   The only automatic conversion is the unambiguous one the spec names: a legacy
   mechanism of the form "`<S##>` clogged/plugged/blocked/dirty (air) filter" gives
   sub-asset `<S##>` (when it resolves to exactly one child), component Filter and
   mechanism CONT.
8. **Dates in a life.** A life starts at a renewal's *completion* (or the
   completion of the last DUPLICATE_MISFILED repair WO linked to it, per §6 — an
   open linked repair means no renewal yet). A failure ends a life at its *event
   date* (created date: when it was found). A PM reset ends the running life at
   its completion, since the item ran until the work was done, and starts the next
   one there. Items renewed alongside a failure end at the failure's event date.
   A failure dated inside the previous repair (before it was finished) makes no
   life and is noted as a possible duplicate.
9. **A failure that did not renew the item** (RESET_ONLY, ADJUST, INSPECT_ONLY)
   and counts for the population ends the running life as a failure, and *no new
   life starts until the item is next renewed* — the item was not made as good as
   new, so later events until then are recurrent events, not fresh lives. The
   stream is flagged for Crow-AMSAA. A non-renewing failure of another kind has no
   effect.
10. **Unknown mechanism sensitivity.** When a mechanism is selected and a failure
    with mechanism UNK could be it, both fits are made and shown: unknowns as
    suspensions (listed first) and as failures. With no mechanism selected every
    mechanism counts, UNK included, and only the unknown share is warned of.
11. **Minimum failures.** The REL rule stays: no fit under 5 lives ending in a
    failure (the intervals are still listed). The "small n" warning fires under 6,
    so a five-failure fit is made and flagged.
12. **Induced failures** (cause semantic INDUCED) can be excluded with a checkbox;
    excluded, they end a life as a suspension (the item was still renewed), and the
    POP string records `induced=suspensions`.
13. **Mode-keyed populations** warn whenever a failure mode is selected, with or
    without a mechanism, since the mode filter alone can drop same-mechanism
    failures (acceptance case 168996).
14. **Analysis window.** As on Perform an Analysis: the To date is the cutoff (end
    of that plant day); without one, the last completed Limble import when it is no
    earlier than every event, otherwise now. Events before the From date are left
    out and the first renewal inside the window starts the clock (the existing
    REL-WBL convention). The POP string's window starts at the From date, or at the
    earliest event in reach when none is entered.
15. **Life hours** are schedule-adjusted on each sub-asset's Weibull schedule; a
    sub-asset with no register entry of its own takes its parent's, then the plant
    default. Each life also shows its calendar days.
16. **What an asset includes.** Selecting an asset reaches the work orders logged
    on it, those logged on its sub-assets, and those dispositioned to a
    sub-asset under it. An asset's sub-assets are picked the way the PM
    calendar picks them (`PmCalendarService._asset_hierarchy`, `_parent_links`,
    `_with_descendants`): from Limble's asset list, each asset's parent is its
    `parentAssetID` (never `parentID`, which Limble also sends on top-level
    assets) when that parent is on the list, and an asset's sub-assets are
    every asset under it, level by level, in name order. The list is the one
    each Limble sync now stores in GREMLIN.db (`limble_asset`, every asset),
    else the PM calendar's copy (only assets that lead to a PM).
    * The PM calendar files by Limble id; work orders here are filed under the
      plant's numbers ("4002", "4002-S09"), which a Limble asset's name starts
      with ("4002 Panel Finishing System"). So an Asset Number is the Limble
      asset its work orders carry as their own id, else the one whose name it
      starts, else the one its work orders' ids name. A work order filed under
      4002 can carry the name and id of the sub-asset it was on; that never
      makes the sub-asset 4002, nor renames 4002. A Limble asset no work order
      is filed under yet is offered by the number its name starts with.
    * An Asset Number Limble's list does not have follows the plant's
      numbering (4002-S09 under 4002; a range like 3101-3107 is not a parent
      and child), else the parent its work orders were imported with.
    * The dropdown says which of these it read.
17. **Pattern views** read the categories ticked on Perform an Analysis, which
    starts at FAILURE alone; a request that names none gets the categories §2.1
    marks "pattern views: yes" (FAILURE, PM_RESET, EXTERNAL, NON_FAILURE, HELD).
    They date a record by its event date in the plant time zone. A HELD record's
    coded fields read as "Not coded".
18. **Record class.** The disposition form carries the work order's record class
    (corrective, PM, inspection, …). A correction is kept on the work order itself
    (`mapped_cmms_record.record_class_final`, where every screen reads it) and on
    the disposition; it decides the detection method (periodic maintenance).
19. **Excel.** The workbook holds the work orders *Show* is set to, one row each,
    with the record columns read-only and the disposition columns (category,
    record class, sub-asset, the five codes, evidence, renewed items, qty, item
    detail, linked WO, note) as dropdowns where they come from a list. On upload,
    only rows whose disposition columns differ from what is saved are saved,
    through the same checks as the form; one refused row refuses the upload,
    named by sheet row and task.
20. **Rankings** take only one-component populations, so a beta is about one kind
    of item, and only the latest saved run of each population.

## The REL screens

The REL Disposition table and the failure-mode/mechanism Perform an Analysis
have been replaced by the two pages above, and the `LifeDataService` code behind
them (the REL disposition table, its workbook, the mechanism Pareto, trend,
downtime, PM and repeat-fix analyses, and the mechanism Weibull) is removed. Their
tables are kept: every REL disposition was carried over as a migrated standard
disposition (decision 7), flagged for confirmation, the migration still runs on
start so a REL disposition changed directly in the database is carried over too,
and their saved fits and report log stay in the database. Nothing in the
application writes them any more.

## Open questions (spec §9 and from the above)

* Final code strings for mechanisms and causes (the seeds use the team's FTS /
  CONT / MNT-INT where given; cause codes `DESIGN`, `INSTALL`, `OPERATE`, `MNT-ERR`,
  `MNT-INT`, `EXP-WEAR`, `MGMT`, `UNK`; LOOS's default cause is Maintenance error).
* Whether Limble capture (ACCA fields) will align with these dropdowns.
* Decisions 1, 3, 8, 9 and 10 above are the ones most worth the team's review.
