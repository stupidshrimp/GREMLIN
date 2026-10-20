# Work-Order Disposition Standard — implementation notes

This is how GREMLIN implements the *Work-Order Disposition Standard* spec (owner:
Tim; status: approved direction, code lists pending team sign-off). It records
where each part lives and every place the spec left a choice open, so the team can
confirm or change those choices.

## Where things are

| Part | File |
|---|---|
| The rules: categories, seed code lists, field validation, derived fields, legacy migration map, the renewal-stream life engine, validity warnings, the population definition string | `services/disposition_standard.py` (no database; unit-tested in `tests/test_disposition_standard_engine.py`) |
| Tables and migration: `disposition_code`, `wo_disposition`, `population_weibull_analysis`; seeding; carrying REL dispositions over | `services/standard_disposition_schema.py` (called from `LifeDataService.ensure_schema` and after every save on the old Disposition screen) |
| Reads and writes against GREMLIN.db: code-list editing, the editor's rows and saves, the analysis population, Pareto / trend / matrix, the Weibull run, saved runs | `services/standard_disposition_service.py` (tested in `tests/test_standard_disposition_service.py`, including the spec §8 acceptance cases) |
| API | `app.py`, `/life-data-analysis/api/standard/...` (tested in `tests/test_standard_disposition_api.py`) |
| Work-Order Disposition page (editors) | `/life-data-analysis/disposition/standard` — `templates/wo_disposition.html`, `static/js/wo_disposition.js` |
| Analysis Population page (any account) | `/life-data-analysis/population-analysis` — `templates/population_analysis.html`, `static/js/population_analysis.js` |

Both pages are linked from the existing Disposition and Perform an Analysis pages
and follow Life Data Analysis's access rules: reads for anyone who may see Life
Data Analysis, every write (a disposition, a code, a saved Weibull run) for
editors, recorded against the account. A Weibull fit can be previewed by any
reader; saving it needs an editor.

## Data model

* **`disposition_code`** — the five code lists (component, failure mechanism,
  failure mode, failure cause, action) as configuration. Each list is seeded from
  spec §3 the first time it is empty and is the team's from then on: a code they
  retire stays retired, a code they delete is not put back. A **code** is the
  stable key dispositions and saved populations store, so it cannot be renamed
  once added; its name, ISO reference and the rest can. Codes are retired
  (`is_active = 0`), not deleted, so records coded with them still read; a retired
  code is accepted on re-save only while the record keeps it.
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
15. **Life hours** are schedule-adjusted on each location's Weibull schedule; a
    sub-asset with no register entry of its own takes its parent's, then the plant
    default. Each life also shows its calendar days.
16. **What an asset includes.** Selecting an asset reaches the work orders logged
    on it, those logged on its sub-assets, and those dispositioned to a location
    under it.
17. **Pattern views** default to the categories §2.1 marks "pattern views: yes"
    (FAILURE, PM_RESET, EXTERNAL, NON_FAILURE, HELD) and can be narrowed. They date
    a record by its event date in the plant time zone. A HELD record's coded fields
    read as "Not coded".

## The old screens

The REL Disposition page and Perform an Analysis are unchanged and keep working,
including their saved fits and reports. Nothing reads the standard's tables but the
two new pages. When the code lists are signed off and the migrated records
confirmed, the old screens can be retired.

## Open questions (spec §9 and from the above)

* Final code strings for mechanisms and causes (the seeds use the team's FTS /
  CONT / MNT-INT where given; cause codes `DESIGN`, `INSTALL`, `OPERATE`, `MNT-ERR`,
  `MNT-INT`, `EXP-WEAR`, `MGMT`, `UNK`; LOOS's default cause is Maintenance error).
* Whether Limble capture (ACCA fields) will align with these dropdowns.
* Decisions 1, 3, 8, 9 and 10 above are the ones most worth the team's review.
